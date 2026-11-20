from datetime import date, timedelta

from app.config import Config
from app.db import get_db
from app.services.errors import ServiceError

# 超额落库选边：旁路凭证（账册原子改写剩余额度后再拒超额片，超额行不进表）
GATE_MODE_NOTE = "旁路配额账册原子改写剩余额度后再拒超额片"
EXCESS_RULE = "超额行不进表"
SAME_SOURCE_RULE = "额度与已灌量同账册同行，双账立刻拦"


def _ensure_ledger(conn, window_id: int):
    """每窗一册，缺则按默认额度立册；额度与已灌量同账册同行（同源）。"""
    conn.execute(
        "INSERT OR IGNORE INTO quota_ledger(window_id, quota) VALUES (?, ?)",
        (window_id, Config.DEFAULT_QUOTA),
    )
    return conn.execute(
        "SELECT * FROM quota_ledger WHERE window_id=?", (window_id,)
    ).fetchone()


def _ensure_all_ledgers(conn) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO quota_ledger(window_id, quota) "
        "SELECT id, ? FROM backfill_windows",
        (Config.DEFAULT_QUOTA,),
    )


def _event(conn, window_id: int, slice_id, kind: str, detail: str) -> None:
    """本地库留证：开闸 / 拒灌 / 双账拦截 / 额度调整。"""
    conn.execute(
        "INSERT INTO quota_events(window_id, slice_id, kind, detail) VALUES (?,?,?,?)",
        (window_id, slice_id, kind, detail),
    )


def list_windows(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT w.*, "
            "(SELECT COUNT(*) FROM backfill_slices s WHERE s.window_id=w.id) AS slice_count, "
            "(SELECT COUNT(*) FROM backfill_slices s WHERE s.window_id=w.id AND s.status='已落库') AS loaded_count "
            "FROM backfill_windows w ORDER BY w.id"
        ).fetchall()
        return [dict(r) for r in rows]


def list_quotas(db_path: str | None = None) -> list[dict]:
    """配额清单：额度/已灌/剩余/闸口/拒灌次数，一屏联查。"""
    with get_db(db_path) as conn:
        _ensure_all_ledgers(conn)
        rows = conn.execute(
            "SELECT w.id AS window_id, w.table_name, w.start_date, w.end_date, w.status, "
            "l.quota, l.poured, (l.quota - l.poured) AS remaining, "
            "l.gate_status, l.gate_no, l.gate_opened_by, l.gate_opened_at, "
            "(SELECT COUNT(*) FROM quota_events e WHERE e.window_id=w.id AND e.kind='拒灌') AS rejected, "
            "(SELECT MAX(e.created_at) FROM quota_events e WHERE e.window_id=w.id AND e.kind='拒灌') AS last_reject_at "
            "FROM backfill_windows w JOIN quota_ledger l ON l.window_id=w.id ORDER BY w.id"
        ).fetchall()
        return [dict(r) for r in rows]


def window_detail(window_id: int, db_path: str | None = None) -> dict:
    """窗界详情：窗界、账册、切片（含拒灌标记）、闸口留证。"""
    with get_db(db_path) as conn:
        win = conn.execute(
            "SELECT * FROM backfill_windows WHERE id=?", (window_id,)
        ).fetchone()
        if not win:
            raise ServiceError("回灌窗口不存在", 404)
        ledger = _ensure_ledger(conn, window_id)
        slices = conn.execute(
            "SELECT s.*, "
            "(SELECT COUNT(*) FROM quota_events e WHERE e.slice_id=s.id AND e.kind='拒灌') AS rejected "
            "FROM backfill_slices s WHERE s.window_id=? ORDER BY s.slice_date",
            (window_id,),
        ).fetchall()
        events = conn.execute(
            "SELECT * FROM quota_events WHERE window_id=? ORDER BY id DESC LIMIT 20",
            (window_id,),
        ).fetchall()
        return {
            "window": dict(win),
            "ledger": dict(ledger),
            "slices": [dict(s) for s in slices],
            "events": [dict(e) for e in events],
        }


def gate_policy(db_path: str | None = None) -> dict:
    """闸口策略：选边（旁路凭证）+ 各窗闸口号/状态。"""
    with get_db(db_path) as conn:
        _ensure_all_ledgers(conn)
        rows = conn.execute(
            "SELECT w.id AS window_id, w.table_name, l.gate_status, l.gate_no, "
            "l.quota, l.poured, (l.quota - l.poured) AS remaining, "
            "l.gate_opened_by, l.gate_opened_at "
            "FROM backfill_windows w JOIN quota_ledger l ON l.window_id=w.id ORDER BY w.id"
        ).fetchall()
        return {
            "mode": Config.GATE_MODE,
            "mode_note": GATE_MODE_NOTE,
            "excess_rule": EXCESS_RULE,
            "same_source": SAME_SOURCE_RULE,
            "gates": [dict(r) for r in rows],
        }


def set_quota(window_id: int, quota: int, db_path: str | None = None) -> dict:
    """调整日窗额度（可打到零）；不得低于已灌量。"""
    if quota < 0:
        raise ServiceError("额度不可为负")
    with get_db(db_path, immediate=True) as conn:
        win = conn.execute(
            "SELECT * FROM backfill_windows WHERE id=?", (window_id,)
        ).fetchone()
        if not win:
            raise ServiceError("回灌窗口不存在", 404)
        ledger = _ensure_ledger(conn, window_id)
        if quota < ledger["poured"]:
            raise ServiceError(f"额度低于已灌量{ledger['poured']}行", 409)
        conn.execute(
            "UPDATE quota_ledger SET quota=?, updated_at=datetime('now') WHERE window_id=?",
            (quota, window_id),
        )
        _event(conn, window_id, None, "额度调整", f"额度设为{quota}行")
        return dict(_ensure_ledger(conn, window_id))


def confirm_gate(window_id: int, operator: str = "调度", db_path: str | None = None) -> dict:
    """调度确认开闸：BEGIN IMMEDIATE 当场落成；重复确认回短句冲突。"""
    with get_db(db_path, immediate=True) as conn:
        win = conn.execute(
            "SELECT * FROM backfill_windows WHERE id=?", (window_id,)
        ).fetchone()
        if not win:
            raise ServiceError("回灌窗口不存在", 404)
        ledger = _ensure_ledger(conn, window_id)
        if ledger["gate_status"] == "开闸":
            raise ServiceError(f"闸口已开：{ledger['gate_no']}", 409)
        gate_no = f"GATE-{window_id:04d}"
        conn.execute(
            "UPDATE quota_ledger SET gate_status='开闸', gate_no=?, gate_opened_by=?, "
            "gate_opened_at=datetime('now'), updated_at=datetime('now') WHERE window_id=?",
            (gate_no, operator or "调度", window_id),
        )
        _event(conn, window_id, None, "开闸", f"闸口{gate_no}开闸")
        return dict(_ensure_ledger(conn, window_id))


def _daterange(start: str, end: str) -> list[str]:
    s = date.fromisoformat(start)
    e = date.fromisoformat(end)
    if e < s:
        raise ServiceError("结束日不能早于开始日")
    out = []
    cur = s
    while cur <= e:
        out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def plan_slices(window_id: int, db_path: str | None = None) -> dict:
    with get_db(db_path, immediate=True) as conn:
        win = conn.execute(
            "SELECT * FROM backfill_windows WHERE id=?", (window_id,)
        ).fetchone()
        if not win:
            raise ServiceError("回灌窗口不存在", 404)
        if win["status"] not in ("草稿", "已计划"):
            raise ServiceError("当前状态不可再生成切片", 409)
        ledger = _ensure_ledger(conn, window_id)
        if ledger["quota"] - ledger["poured"] <= 0:
            raise ServiceError("配额耗尽停止出片", 409)
        days = _daterange(win["start_date"], win["end_date"])
        conn.execute("DELETE FROM backfill_slices WHERE window_id=?", (window_id,))
        for d in days:
            conn.execute(
                "INSERT INTO backfill_slices(window_id, slice_date, status, row_count) "
                "VALUES (?,?, '待重放', 0)",
                (window_id, d),
            )
        conn.execute(
            "UPDATE backfill_windows SET status='已计划' WHERE id=?",
            (window_id,),
        )
        slices = conn.execute(
            "SELECT * FROM backfill_slices WHERE window_id=? ORDER BY slice_date",
            (window_id,),
        ).fetchall()
        return {"window_id": window_id, "slices": [dict(s) for s in slices]}


def list_slices(window_id: int, db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM backfill_slices WHERE window_id=? ORDER BY slice_date",
            (window_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def replay_slice(slice_id: int, db_path: str | None = None) -> dict:
    """重放落库：闸口未开拒灌；账册原子扣额度，超额片拒灌且行不进表。"""
    rejected = None
    result = None
    with get_db(db_path, immediate=True) as conn:
        sl = conn.execute(
            "SELECT * FROM backfill_slices WHERE id=?", (slice_id,)
        ).fetchone()
        if not sl:
            raise ServiceError("切片不存在", 404)
        if sl["status"] == "已落库":
            raise ServiceError("切片已落库", 409)
        if sl["status"] != "待重放":
            raise ServiceError("仅待重放切片可重放", 409)
        ledger = _ensure_ledger(conn, sl["window_id"])
        # deterministic fake row count from date digits
        rows = 100 + int(sl["slice_date"].replace("-", "")) % 97
        if ledger["gate_status"] != "开闸":
            _event(conn, sl["window_id"], slice_id, "拒灌", f"闸口未开拒灌{sl['slice_date']}")
            rejected = "闸口未开拒灌"
        else:
            # 旁路凭证硬闸：原子改写剩余额度，扣不动即超额片，整片拒灌
            cur = conn.execute(
                "UPDATE quota_ledger SET poured=poured+?, updated_at=datetime('now') "
                "WHERE window_id=? AND gate_status='开闸' AND quota-poured >= ?",
                (rows, sl["window_id"], rows),
            )
            if cur.rowcount == 1:
                conn.execute(
                    "INSERT INTO target_rows(window_id, slice_id, slice_date, row_count) "
                    "VALUES (?,?,?,?)",
                    (sl["window_id"], slice_id, sl["slice_date"], rows),
                )
                conn.execute(
                    "UPDATE backfill_slices SET status='已落库', row_count=? WHERE id=?",
                    (rows, slice_id),
                )
                conn.execute(
                    "UPDATE backfill_windows SET status='执行中' WHERE id=?",
                    (sl["window_id"],),
                )
                pending = conn.execute(
                    "SELECT COUNT(*) AS c FROM backfill_slices WHERE window_id=? AND status!='已落库'",
                    (sl["window_id"],),
                ).fetchone()["c"]
                if pending == 0:
                    conn.execute(
                        "UPDATE backfill_windows SET status='已完成' WHERE id=?",
                        (sl["window_id"],),
                    )
                result = dict(
                    conn.execute(
                        "SELECT * FROM backfill_slices WHERE id=?", (slice_id,)
                    ).fetchone()
                )
            else:
                remaining = ledger["quota"] - ledger["poured"]
                _event(
                    conn,
                    sl["window_id"],
                    slice_id,
                    "拒灌",
                    f"超配额拒灌{sl['slice_date']}：剩余{remaining}行需{rows}行",
                )
                rejected = f"超配额拒灌：剩余{remaining}行，需{rows}行"
    if rejected:
        raise ServiceError(rejected, 409)
    return result


def coverage_report(window_id: int, db_path: str | None = None) -> dict:
    """对账回放台复验：同源核查（双账立刻拦）+ 超额行数核算。"""
    dual = None
    report = None
    with get_db(db_path, immediate=True) as conn:
        win = conn.execute(
            "SELECT * FROM backfill_windows WHERE id=?", (window_id,)
        ).fetchone()
        if not win:
            raise ServiceError("回灌窗口不存在", 404)
        ledger = _ensure_ledger(conn, window_id)
        planned = conn.execute(
            "SELECT COUNT(*) AS c FROM backfill_slices WHERE window_id=?",
            (window_id,),
        ).fetchone()["c"]
        loaded = conn.execute(
            "SELECT COUNT(*) AS c FROM backfill_slices WHERE window_id=? AND status='已落库'",
            (window_id,),
        ).fetchone()["c"]
        missing = planned - loaded
        target_total = conn.execute(
            "SELECT COALESCE(SUM(row_count),0) AS t FROM target_rows WHERE window_id=?",
            (window_id,),
        ).fetchone()["t"]
        if target_total != ledger["poured"]:
            # 额度与已灌量须同源：账册与目标表对不上即双账，立刻拦
            _event(
                conn,
                window_id,
                None,
                "双账拦截",
                f"账册{ledger['poured']}行≠目标表{target_total}行",
            )
            dual = f"双账不一致：账册{ledger['poured']}行，目标表{target_total}行，已拦截"
        else:
            excess = max(0, target_total - ledger["quota"])
            remaining = ledger["quota"] - ledger["poured"]
            summary = (
                f"窗口#{window_id} {win['table_name']}：计划{planned}片，已落库{loaded}，缺口{missing}；"
                f"额度{ledger['quota']}，已灌{ledger['poured']}，剩余{remaining}，超额{excess}行"
            )
            cur = conn.execute(
                "INSERT INTO coverage_reports(window_id, planned, loaded, missing, quota, poured, excess, gate_no, summary) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    window_id,
                    planned,
                    loaded,
                    missing,
                    ledger["quota"],
                    ledger["poured"],
                    excess,
                    ledger["gate_no"],
                    summary,
                ),
            )
            report = dict(
                conn.execute(
                    "SELECT * FROM coverage_reports WHERE id=?", (cur.lastrowid,)
                ).fetchone()
            )
    if dual:
        raise ServiceError(dual, 409)
    return report


def list_coverage(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM coverage_reports ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]

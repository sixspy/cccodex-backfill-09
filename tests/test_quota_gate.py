"""backfill-09 日窗配额闸口验收：

把额度打到零再开闸 → 拒灌芯片须亮 → 目标表超额行数为零。
旁路凭证账册原子扣额度；双账立刻拦；并发确认不互相覆盖。
"""
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.db import get_db, init_db
from app.services.errors import ServiceError
from app.services.windows import (
    confirm_gate,
    coverage_report,
    list_slices,
    plan_slices,
    replay_slice,
    set_quota,
)


def _rows_for(date_str: str) -> int:
    return 100 + int(date_str.replace("-", "")) % 97


@pytest.mark.asyncio
async def test_zero_quota_gate_open_reject_chip_and_zero_excess(authed):
    """验收主链：额度打零 → 开闸 → 重放拒灌 → 芯片亮 → 超额行数为零。"""
    wid = (await authed.get("/api/windows")).json()["items"][0]["id"]
    planned = (await authed.post(f"/api/windows/{wid}/plan")).json()
    assert len(planned["slices"]) == 3

    # 把额度打到零
    r = await authed.post(f"/api/windows/{wid}/quota", json={"quota": 0})
    assert r.status_code == 200
    assert r.json()["ledger"]["quota"] == 0

    # 再开闸：后端当场落成，策略亮闸口号
    r = await authed.post(f"/api/windows/{wid}/gate/confirm", json={})
    assert r.status_code == 200
    assert r.json()["ledger"]["gate_no"] == f"GATE-{wid:04d}"
    policy = (await authed.get("/api/gate-policy")).json()
    assert policy["mode"] == "旁路凭证"
    gate = next(g for g in policy["gates"] if g["window_id"] == wid)
    assert gate["gate_no"] == f"GATE-{wid:04d}"
    assert gate["gate_status"] == "开闸"

    # 重放 → 超额拒灌（回短句）
    sid = planned["slices"][0]["id"]
    r = await authed.post(f"/api/slices/{sid}/replay")
    assert r.status_code == 409
    assert "拒灌" in r.json()["error"]

    # 清单亮超额拒灌（拒灌芯片数据）
    items = (await authed.get("/api/quotas")).json()["items"]
    q = next(i for i in items if i["window_id"] == wid)
    assert q["rejected"] >= 1
    assert q["poured"] == 0

    # 窗界详情留证（本地库）
    detail = (await authed.get(f"/api/windows/{wid}")).json()
    assert any(e["kind"] == "拒灌" for e in detail["events"])
    assert any(e["kind"] == "开闸" for e in detail["events"])

    # 回放台复验：目标表超额行数为零
    report = (await authed.post("/api/coverage-reports", json={"window_id": wid})).json()["report"]
    assert report["excess"] == 0
    assert report["poured"] == 0


def test_partial_quota_atomic_ledger_same_source(tmp_path):
    """旁路账册原子扣额度：够一片放一片，超额片拒灌且行不进表。"""
    db = str(tmp_path / "t.db")
    init_db(db)
    plan_slices(1, db)
    slices = list_slices(1, db)
    need = _rows_for(slices[0]["slice_date"])
    set_quota(1, need, db)  # 额度只够第一片
    confirm_gate(1, db_path=db)

    ok = replay_slice(slices[0]["id"], db)
    assert ok["status"] == "已落库"

    with pytest.raises(ServiceError) as ei:
        replay_slice(slices[1]["id"], db)
    assert ei.value.status_code == 409
    assert "超配额拒灌" in ei.value.message

    with get_db(db) as conn:
        led = conn.execute("SELECT * FROM quota_ledger WHERE window_id=1").fetchone()
        tgt = conn.execute(
            "SELECT COALESCE(SUM(row_count),0) AS t FROM target_rows WHERE window_id=1"
        ).fetchone()["t"]
        ev = conn.execute(
            "SELECT COUNT(*) AS c FROM quota_events WHERE window_id=1 AND kind='拒灌'"
        ).fetchone()["c"]
    # 额度与已灌量同源：账册 == 目标表；已灌不超额度；拒灌已留证
    assert led["poured"] == need == tgt
    assert led["poured"] <= led["quota"]
    assert ev == 1


def test_replay_requires_open_gate(tmp_path):
    db = str(tmp_path / "t.db")
    init_db(db)
    plan_slices(1, db)
    sid = list_slices(1, db)[0]["id"]
    with pytest.raises(ServiceError) as ei:
        replay_slice(sid, db)
    assert ei.value.status_code == 409
    assert "闸口未开" in ei.value.message
    with get_db(db) as conn:
        ev = conn.execute("SELECT COUNT(*) AS c FROM quota_events WHERE kind='拒灌'").fetchone()["c"]
    assert ev == 1  # 拒灌留证落库


def test_plan_blocked_when_quota_exhausted(tmp_path):
    db = str(tmp_path / "t.db")
    init_db(db)
    set_quota(1, 0, db)
    with pytest.raises(ServiceError) as ei:
        plan_slices(1, db)
    assert ei.value.status_code == 409
    assert "停止出片" in ei.value.message


def test_quota_below_poured_rejected(tmp_path):
    db = str(tmp_path / "t.db")
    init_db(db)
    plan_slices(1, db)
    confirm_gate(1, db_path=db)
    replay_slice(list_slices(1, db)[0]["id"], db)
    with pytest.raises(ServiceError) as ei:
        set_quota(1, 1, db)
    assert ei.value.status_code == 409
    assert "已灌量" in ei.value.message


def test_double_confirm_conflict(tmp_path):
    db = str(tmp_path / "t.db")
    init_db(db)
    confirm_gate(1, db_path=db)
    with pytest.raises(ServiceError) as ei:
        confirm_gate(1, db_path=db)
    assert ei.value.status_code == 409
    assert "闸口已开" in ei.value.message


def test_dual_books_blocked_immediately(tmp_path):
    """额度与已灌量须同源：绕过账册直写目标表 → 复验双账立刻拦。"""
    db = str(tmp_path / "t.db")
    init_db(db)
    plan_slices(1, db)
    confirm_gate(1, db_path=db)
    slices = list_slices(1, db)
    replay_slice(slices[0]["id"], db)
    # 绕过账册，直接往目标表灌一片（双账）
    with get_db(db) as conn:
        conn.execute(
            "INSERT INTO target_rows(window_id, slice_id, slice_date, row_count) "
            "VALUES (1, ?, ?, 50)",
            (slices[1]["id"], slices[1]["slice_date"]),
        )
    with pytest.raises(ServiceError) as ei:
        coverage_report(1, db)
    assert ei.value.status_code == 409
    assert "双账" in ei.value.message
    with get_db(db) as conn:
        ev = conn.execute(
            "SELECT COUNT(*) AS c FROM quota_events WHERE kind='双账拦截'"
        ).fetchone()["c"]
    assert ev == 1


def _try_confirm(db):
    try:
        confirm_gate(1, db_path=db)
        return "ok"
    except ServiceError as exc:
        assert exc.status_code == 409
        return "conflict"


def test_concurrent_gate_confirm_serialized(tmp_path):
    """两侧调度同时确认：有锁串行，一成一冲突，账册行不互相覆盖。"""
    db = str(tmp_path / "t.db")
    init_db(db)
    plan_slices(1, db)
    with ThreadPoolExecutor(max_workers=2) as ex:
        results = list(ex.map(lambda _: _try_confirm(db), range(2)))
    assert results.count("ok") == 1
    assert results.count("conflict") == 1
    with get_db(db) as conn:
        led = conn.execute("SELECT * FROM quota_ledger WHERE window_id=1").fetchone()
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM quota_events WHERE kind='开闸'"
        ).fetchone()["c"]
    assert led["gate_status"] == "开闸"
    assert led["gate_no"] == "GATE-0001"
    assert n == 1


def _try_replay(db, sid):
    try:
        replay_slice(sid, db)
        return "ok"
    except ServiceError as exc:
        assert exc.status_code == 409
        return "conflict"


def test_concurrent_replay_never_exceeds_quota(tmp_path):
    """并发重放抢同一额度：原子扣减，已灌量永不超配额。"""
    db = str(tmp_path / "t.db")
    init_db(db)
    plan_slices(1, db)
    slices = list_slices(1, db)
    need0 = _rows_for(slices[0]["slice_date"])
    need1 = _rows_for(slices[1]["slice_date"])
    # 每片单独都够，两片一起超额 → 只许成一片
    set_quota(1, max(need0, need1), db)
    confirm_gate(1, db_path=db)
    with ThreadPoolExecutor(max_workers=2) as ex:
        results = list(ex.map(lambda sid: _try_replay(db, sid), [slices[0]["id"], slices[1]["id"]]))
    assert results.count("ok") == 1
    assert results.count("conflict") == 1
    with get_db(db) as conn:
        led = conn.execute("SELECT * FROM quota_ledger WHERE window_id=1").fetchone()
        tgt = conn.execute(
            "SELECT COALESCE(SUM(row_count),0) AS t FROM target_rows WHERE window_id=1"
        ).fetchone()["t"]
    assert led["poured"] <= led["quota"]
    assert tgt == led["poured"]

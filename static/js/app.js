const msg = document.getElementById('msg');
const view = document.getElementById('view');
let quotas = [];

async function api(path, opts={}) {
  const r = await fetch(path, { headers:{'Content-Type':'application/json'}, ...opts });
  const j = await r.json().catch(()=>({}));
  if (!r.ok) throw new Error(j.error || ('HTTP '+r.status));
  return j;
}
function setMsg(t, err=false){ msg.textContent=t||''; msg.className = err?'err':'muted'; }
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function loadQuotas(){ quotas = (await api('/api/quotas')).items || []; }
const winOptions = () => quotas.map(q=>`<option value="${q.window_id}">#${q.window_id} ${esc(q.table_name)}</option>`).join('');
function gateTag(status, no){
  return status==='开闸'
    ? `<span class="tag ok">开闸 ${esc(no||'')}</span>`
    : `<span class="tag">闭闸</span>`;
}
const rejectTag = n => n>0 ? `<span class="tag danger">拒灌 ×${n}</span>` : '';

/* ---------- 配额清单 ---------- */
function renderQuotas(){
  view.innerHTML = `<div class="wall">${quotas.map(q=>`
    <article class="card">
      <h3>#${q.window_id} ${esc(q.table_name)}</h3>
      <p class="muted">${q.start_date} → ${q.end_date} · ${esc(q.status)}</p>
      <p>额度 ${q.quota} · 已灌 ${q.poured} · 剩余 ${q.remaining}</p>
      <p>${gateTag(q.gate_status, q.gate_no)} ${rejectTag(q.rejected)}</p>
      ${q.last_reject_at?`<p class="muted">最近拒灌 ${q.last_reject_at}</p>`:''}
      <div class="row">
        <button type="button" data-quota="${q.window_id}">设额度</button>
        <button type="button" class="primary" data-gate="${q.window_id}" ${q.gate_status==='开闸'?'disabled':''}>调度确认开闸</button>
      </div>
    </article>`).join('')}</div>`;
  view.querySelectorAll('button[data-quota]').forEach(b=>b.onclick=async()=>{
    const v = prompt('新额度（行，可打到 0）：','0');
    if (v===null) return;
    try{
      const j = await api(`/api/windows/${b.dataset.quota}/quota`, {method:'POST', body:JSON.stringify({quota:Number(v)})});
      setMsg(`窗口#${j.ledger.window_id} 额度已设为 ${j.ledger.quota} 行`);
    }catch(e){ setMsg(e.message,true); }
    await loadQuotas(); renderQuotas();
  });
  view.querySelectorAll('button[data-gate]').forEach(b=>b.onclick=async()=>{
    try{
      const j = await api(`/api/windows/${b.dataset.gate}/gate/confirm`, {method:'POST', body:'{}'});
      setMsg(`闸口已开：${j.ledger.gate_no}`);
    }catch(e){ setMsg(e.message,true); }
    await loadQuotas(); renderQuotas();
  });
}

/* ---------- 窗界详情 ---------- */
async function renderDetail(){
  await loadQuotas();
  view.innerHTML = `<div class="card">
    <label>窗口<select id="wid">${winOptions()}</select></label>
    <div id="detail"></div>
  </div>`;
  const draw = async ()=>{
    const id = document.getElementById('wid').value;
    if(!id){ document.getElementById('detail').innerHTML=''; return; }
    const d = await api(`/api/windows/${id}`);
    const w = d.window, l = d.ledger;
    document.getElementById('detail').innerHTML = `
      <h3>#${w.id} ${esc(w.table_name)}</h3>
      <p class="muted">窗界 ${w.start_date} → ${w.end_date} · ${esc(w.status)}</p>
      <p>额度 ${l.quota} · 已灌 ${l.poured} · 剩余 ${l.quota-l.poured} · ${gateTag(l.gate_status, l.gate_no)}</p>
      <p><button class="primary" id="plan" type="button">生成切片计划（出片）</button></p>
      <div class="wall">${d.slices.map(s=>`
        <article class="card">
          <h3>${s.slice_date}</h3>
          <p><span class="tag">${esc(s.status)}</span> ${rejectTag(s.rejected)}</p>
          <p class="muted">行数 ${s.row_count}</p>
          <button data-replay="${s.id}" class="primary" type="button" ${s.status!=='待重放'?'disabled':''}>重放</button>
        </article>`).join('') || '<p class="muted">尚未出片</p>'}</div>
      <h4>闸口留证</h4>
      <div>${d.events.map(e=>`<p class="muted">[${esc(e.kind)}] ${esc(e.detail)} · ${e.created_at}</p>`).join('') || '<p class="muted">暂无事件</p>'}</div>`;
    document.getElementById('plan').onclick = async ()=>{
      try{
        const j = await api(`/api/windows/${id}/plan`, {method:'POST', body:'{}'});
        setMsg(`已生成 ${j.slices.length} 片`);
      }catch(e){ setMsg(e.message,true); }
      await draw();
    };
    document.querySelectorAll('button[data-replay]').forEach(b=>b.onclick=async()=>{
      try{
        const j = await api(`/api/slices/${b.dataset.replay}/replay`, {method:'POST', body:'{}'});
        setMsg(`已落库 ${j.slice.slice_date} · ${j.slice.row_count} 行`);
      }catch(e){ setMsg(e.message,true); }
      await draw();
    });
  };
  document.getElementById('wid').onchange = draw;
  await draw();
}

/* ---------- 闸口策略 ---------- */
async function renderPolicy(){
  const p = await api('/api/gate-policy');
  view.innerHTML = `
    <div class="card">
      <h3>闸口策略 · ${esc(p.mode)}</h3>
      <p>${esc(p.mode_note)}</p>
      <p class="muted">${esc(p.excess_rule)} · ${esc(p.same_source)}</p>
    </div>
    <div class="wall" style="margin-top:0.8rem">${p.gates.map(g=>`
      <article class="card">
        <h3>#${g.window_id} ${esc(g.table_name)}</h3>
        <p>闸口号 <strong class="accent">${esc(g.gate_no||'—')}</strong></p>
        <p>${gateTag(g.gate_status, '')}</p>
        <p class="muted">额度 ${g.quota} · 已灌 ${g.poured} · 剩余 ${g.remaining}</p>
        <p class="muted">${g.gate_opened_by?`开闸人 ${esc(g.gate_opened_by)} · ${g.gate_opened_at}`:'未开闸'}</p>
      </article>`).join('')}</div>`;
}

/* ---------- 对账回放台 ---------- */
async function renderConsole(){
  await loadQuotas();
  const items = (await api('/api/coverage-reports')).items || [];
  view.innerHTML = `<div class="card">
    <label>窗口<select id="wid">${winOptions()}</select></label>
    <button class="primary" id="go" type="button">跑对账复验</button>
    <div class="wall" style="margin-top:0.8rem">${items.map(r=>`
      <article class="card">
        <h3>窗口 #${r.window_id}</h3>
        <p>${esc(r.summary)}</p>
        <p>${r.excess===0
          ? '<span class="tag ok">复验通过 · 超额0行</span>'
          : `<span class="tag danger">超额${r.excess}行</span>`}</p>
        <p class="muted">${r.gate_no?`闸口号 ${esc(r.gate_no)} · `:''}${r.created_at}</p>
      </article>`).join('') || '<p class="muted">暂无对账报告</p>'}</div>
  </div>`;
  document.getElementById('go').onclick = async ()=>{
    try{
      const j = await api('/api/coverage-reports', {method:'POST', body: JSON.stringify({window_id: Number(document.getElementById('wid').value)})});
      setMsg(j.report.summary);
      await renderConsole();
    }catch(e){ setMsg(e.message,true); }
  };
}

async function show(tab){
  setMsg('');
  try{
    if (tab==='quotas'){ await loadQuotas(); renderQuotas(); }
    if (tab==='detail') await renderDetail();
    if (tab==='policy') await renderPolicy();
    if (tab==='console') await renderConsole();
  }catch(e){ setMsg(e.message,true); }
}
document.querySelectorAll('.chips button').forEach(btn=>{
  btn.onclick=()=>{ document.querySelectorAll('.chips button').forEach(b=>b.classList.remove('on')); btn.classList.add('on'); show(btn.dataset.tab); };
});
document.getElementById('btn-logout').onclick = async ()=>{ await api('/api/logout',{method:'POST',body:'{}'}); location.href='/login'; };
show('quotas');

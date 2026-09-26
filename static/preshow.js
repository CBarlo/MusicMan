// Pre-show check screen, shared by Console and Admin.
// openPreshowCheck() builds its own overlay and reads /api/preshow/check.
(function () {
  const COLOR = { ok: '#3cb96a', warn: '#f5a623', fail: '#cc4444', info: '#8a8a8a' };
  const ICON  = { ok: '✓', warn: '⚠', fail: '✕', info: '•' };
  const esc = s => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  let overlay = null;

  function build() {
    overlay = document.createElement('div');
    overlay.id = 'preshow-overlay';
    overlay.style.cssText = 'position:fixed;inset:0;z-index:2000;display:none;flex-direction:column;background:rgba(0,0,0,.94);font-family:"Barlow Condensed",sans-serif;color:var(--text,#eee)';
    overlay.innerHTML = `
      <div style="display:flex;align-items:center;gap:12px;padding:14px 20px;background:var(--bg2,#141414);border-bottom:2px solid var(--amber-dim,#5a3d0a);flex-shrink:0">
        <div style="flex:1">
          <div style="font-size:16px;font-weight:800;letter-spacing:3px;color:var(--amber,#f5a623)">PRE-SHOW CHECK</div>
          <div id="preshow-summary" style="font-size:11px;letter-spacing:2px;margin-top:3px;color:var(--text-dim,#999)">CHECKING…</div>
        </div>
        <button id="preshow-rerun" style="padding:8px 14px;border-radius:6px;border:1px solid var(--border2,#444);background:var(--surface,#1c1c1c);color:var(--text,#eee);font:700 12px 'Barlow Condensed',sans-serif;letter-spacing:2px;cursor:pointer">↺ RE-RUN</button>
        <button id="preshow-close" style="width:40px;height:40px;border-radius:6px;border:1px solid var(--border2,#444);background:var(--surface,#1c1c1c);color:var(--text,#eee);font-size:20px;cursor:pointer">✕</button>
      </div>
      <div id="preshow-list" style="overflow-y:auto;flex:1;padding:14px 20px 30px;-webkit-overflow-scrolling:touch"></div>`;
    document.body.appendChild(overlay);
    overlay.querySelector('#preshow-close').onclick = () => { overlay.style.display = 'none'; };
    overlay.querySelector('#preshow-rerun').onclick = run;
  }

  async function run() {
    const list = overlay.querySelector('#preshow-list');
    const sum  = overlay.querySelector('#preshow-summary');
    sum.textContent = 'CHECKING…';
    sum.style.color = 'var(--text-dim,#999)';
    list.innerHTML = '';
    let d;
    try {
      d = await fetch('/api/preshow/check').then(r => r.json());
    } catch (e) {
      sum.textContent = 'COULD NOT REACH MUSICMAN';
      sum.style.color = COLOR.fail;
      return;
    }
    if (d.ok && !d.warns) { sum.textContent = 'ALL CLEAR — READY TO GO'; sum.style.color = COLOR.ok; }
    else if (d.ok)        { sum.textContent = d.warns + ' THING' + (d.warns > 1 ? 'S' : '') + ' TO LOOK AT — NOTHING BLOCKING'; sum.style.color = COLOR.warn; }
    else                  { sum.textContent = d.fails + ' PROBLEM' + (d.fails > 1 ? 'S' : '') + ' TO FIX BEFORE THE SHOW'; sum.style.color = COLOR.fail; }
    const rank = { fail: 0, warn: 1, info: 2, ok: 3 };
    const groups = {};
    d.items.forEach(i => (groups[i.group] = groups[i.group] || []).push(i));
    list.innerHTML = Object.keys(groups).map(g => {
      const rows = groups[g].sort((a, b) => rank[a.status] - rank[b.status]).map(i => `
        <div style="display:flex;gap:12px;align-items:flex-start;padding:9px 12px;margin-bottom:6px;border-radius:8px;background:var(--surface,#1c1c1c);border:1px solid ${i.status === 'ok' || i.status === 'info' ? 'var(--border,#333)' : COLOR[i.status] + '88'}">
          <div style="width:22px;height:22px;flex-shrink:0;border-radius:50%;background:${COLOR[i.status]}22;border:2px solid ${COLOR[i.status]};color:${COLOR[i.status]};display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:900">${ICON[i.status]}</div>
          <div style="flex:1;min-width:0">
            <div style="font-size:14px;font-weight:700;letter-spacing:.5px;line-height:1.3">${esc(i.label)}</div>
            ${i.fix ? `<div style="font-size:11px;color:var(--text-dim,#999);margin-top:3px;letter-spacing:.5px">${esc(i.fix)}</div>` : ''}
          </div>
        </div>`).join('');
      return `<div style="font-size:10px;letter-spacing:3px;font-weight:700;color:var(--text-dim,#999);margin:14px 0 8px">${esc(g.toUpperCase())}</div>${rows}`;
    }).join('');
  }

  window.openPreshowCheck = function () {
    if (!overlay) build();
    overlay.style.display = 'flex';
    run();
  };
})();

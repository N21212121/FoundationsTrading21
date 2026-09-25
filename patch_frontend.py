"""Inject the Journal and Performance tabs into static/index.html.

Idempotent: re-running detects the marker and refuses rather than duplicating.
"""
import sys

PATH = 'static/index.html'
MARKER = 'id="journalView"'

s = open(PATH, encoding='utf-8').read()
if MARKER in s:
    print('already patched; nothing to do')
    sys.exit(0)

# ── 1. TAB BUTTONS ────────────────────────────────────────────────────────────
old_tabs = '''  <div class="tab" title="next build pass">Performance</div>'''
new_tabs = '''  <div class="tab" id="tabJournal" onclick="showTab('journal')">Journal<span id="jrnBadge" class="badge" style="display:none">0</span></div>
  <div class="tab" id="tabPerf" onclick="showTab('perf')">Performance</div>'''
assert s.count(old_tabs) == 1, 'tab anchor not found'
s = s.replace(old_tabs, new_tabs)

# ── 2. CSS ────────────────────────────────────────────────────────────────────
old_css = '''  #dash {'''
new_css = '''  /* ── journal + heatmap ── */
  .badge {
    display:inline-block; margin-left:6px; padding:1px 6px; border-radius:9px;
    background: var(--scarlet); color: var(--navy); font-size:11px;
    font-weight:700; vertical-align:middle;
  }
  .gradeRow { display:flex; gap:6px; }
  .gradeBtn {
    flex:1; padding:10px 0; border:1px solid var(--border); border-radius:6px;
    background: var(--card-2); color: var(--text); cursor:pointer;
    font-size:15px; font-weight:600;
  }
  .gradeBtn:hover { background: var(--navy-3); color:#fff; border-color:var(--navy-3); }
  .gradeBtn.sel { background: var(--navy-3); color:#fff; border-color:var(--navy-3); }
  .chip {
    display:inline-block; padding:3px 9px; margin:2px 3px 2px 0;
    border:1px solid var(--border); border-radius:12px; cursor:pointer;
    font-size:12px; background:var(--card-2); user-select:none;
  }
  .chip.on  { background: var(--navy-3); color:#fff; border-color:var(--navy-3); }
  .chip.bad.on { background: var(--red); border-color: var(--red); color:#fff; }
  .hmTable { border-collapse: separate; border-spacing: 3px; }
  .hmTable th {
    font-size:11px; text-transform:uppercase; letter-spacing:1px;
    color: var(--text-dim); font-weight:600; padding:4px 6px;
  }
  .hmCell {
    width:92px; height:60px; border-radius:6px; text-align:center;
    border:1px solid var(--border); cursor:pointer; padding:4px 2px;
    transition: transform .07s;
  }
  .hmCell:hover { transform: scale(1.05); }
  .hmCell.empty { background: var(--card-2); opacity:.45; cursor:default; }
  .hmCell.sparse { border-style: dashed; opacity:.72; }
  .hmCell .n   { font-size:11px; color: rgba(0,0,0,.55); }
  .hmCell .val { font-size:15px; font-weight:700; }
  .hmCell.sel { outline:3px solid var(--navy); outline-offset:1px; }
  .layerBox {
    border:1px solid var(--border); border-radius:6px; padding:8px 10px;
    margin-bottom:8px; background: var(--card);
  }
  .layerBox h3 {
    font-size:11px; text-transform:uppercase; letter-spacing:1px;
    color:var(--text-dim); margin-bottom:6px; font-weight:600;
  }
  .kpi { display:flex; gap:18px; flex-wrap:wrap; margin-bottom:10px; }
  .kpi div { font-size:12.5px; color:var(--text-dim); }
  .kpi b { display:block; font-size:19px; color:var(--text); font-weight:700; }

  #dash {'''
assert s.count(old_css) == 1, 'css anchor not found'
s = s.replace(old_css, new_css, 1)

# ── 3. VIEWS ──────────────────────────────────────────────────────────────────
old_view_anchor = '''<div id="logsView" style="display:none; flex:1; padding:12px; min-height:0;
     flex-direction:column;">'''
new_views = '''<!-- ═══════════════ JOURNAL ═══════════════ -->
<div id="journalView" style="display:none; flex:1; padding:12px; min-height:0;
     gap:12px;">
  <div class="card" style="flex:0 0 380px; display:flex; flex-direction:column;">
    <h2>Ungraded Fills</h2>
    <div id="jrnQueueMeta" style="font-size:12.5px;color:var(--text-dim);margin-bottom:8px;"></div>
    <div id="jrnQueue" style="flex:1; overflow:auto; min-height:0;"></div>
    <div style="border-top:1px solid var(--border); padding-top:10px; margin-top:10px;">
      <h2 style="border:0;padding:0;margin-bottom:8px;">Import</h2>
      <input type="text" id="jrnImportPath" placeholder="path to exported CSV"
             style="width:100%; margin-bottom:6px;">
      <div style="display:flex; gap:6px;">
        <button onclick="jrnImport(true)"  style="flex:1;">Preview</button>
        <button onclick="jrnImport(false)" style="flex:1;">Import</button>
      </div>
      <div id="jrnImportOut" style="font-size:12px;color:var(--text-dim);margin-top:7px;
           max-height:150px;overflow:auto;"></div>
    </div>
  </div>

  <div class="card" style="flex:1; display:flex; flex-direction:column; min-height:0;">
    <h2>Grade This Fill</h2>
    <div id="jrnEditor" style="flex:1; overflow:auto; min-height:0;">
      <div style="color:var(--text-dim);padding:30px;text-align:center;">
        Nothing waiting. Every fill is graded.
      </div>
    </div>
  </div>
</div>

<!-- ═══════════════ PERFORMANCE ═══════════════ -->
<div id="perfView" style="display:none; flex:1; padding:12px; min-height:0; gap:12px;">
  <div class="card" style="flex:0 0 260px; overflow:auto;">
    <h2>Layers</h2>
    <div id="perfLayers" style="font-size:12.5px;"></div>
    <button onclick="perfClearLayers()" style="width:100%;margin-top:10px;">Clear all</button>
    <div id="perfRecon" style="font-size:11.5px;color:var(--text-dim);margin-top:12px;
         border-top:1px solid var(--border);padding-top:9px;"></div>
  </div>

  <div class="card" style="flex:1; display:flex; flex-direction:column; min-height:0;">
    <div style="display:flex;gap:10px;align-items:center;margin-bottom:10px;">
      <h2 style="margin:0;border:0;padding:0;">Entry &times; Exit Grade</h2>
      <select id="perfMetric" onchange="perfRender()">
        <option value="avg_pl">Avg P/L</option>
        <option value="total_pl">Total P/L</option>
        <option value="n">Trade count</option>
        <option value="win_rate">Win rate</option>
        <option value="median_hold_min">Median hold</option>
      </select>
      <button onclick="perfLoad()">Refresh</button>
      <span id="perfWarn" style="color:var(--red);font-size:12px;"></span>
    </div>
    <div class="kpi" id="perfKpi"></div>
    <div style="overflow:auto;"><div id="perfGrid"></div></div>
    <div style="flex:1;overflow:auto;min-height:0;margin-top:12px;
         border-top:1px solid var(--border);padding-top:10px;">
      <div id="perfDetailHead" style="font-size:12px;color:var(--text-dim);margin-bottom:6px;">
        Click a cell to list its trades.
      </div>
      <table id="perfDetail"><thead></thead><tbody></tbody></table>
    </div>
  </div>
</div>

''' + old_view_anchor
assert s.count(old_view_anchor) == 1, 'view anchor not found'
s = s.replace(old_view_anchor, new_views, 1)

# ── 4. showTab ────────────────────────────────────────────────────────────────
old_show = '''  if (which === 'logs') refreshLogs();'''
new_show = '''  $('journalView').style.display = which === 'journal' ? 'flex' : 'none';
  $('perfView').style.display    = which === 'perf'    ? 'flex' : 'none';
  $('tabJournal').className = 'tab' + (which === 'journal' ? ' active' : '');
  $('tabPerf').className    = 'tab' + (which === 'perf'    ? ' active' : '');
  if (which === 'logs') refreshLogs();
  if (which === 'journal') jrnLoad();
  if (which === 'perf') { perfLoadLayers(); perfLoad(); }'''
assert s.count(old_show) == 1, 'showTab anchor not found'
s = s.replace(old_show, new_show, 1)

# ── 5. JS ─────────────────────────────────────────────────────────────────────
old_js_anchor = '''/* ── logs ── */'''
new_js = r'''/* ══════════════ JOURNAL ══════════════ */

let JRN = { queue: [], cur: null, vocab: {tags:{good:[],bad:[]}, condition_keys:[], conditions:{}},
            g: null, tg: new Set(), tb: new Set() };

async function jrnLoad() {
  try {
    const [q, v] = await Promise.all([
      fetch('/api/journal/queue?limit=200').then(r => r.json()),
      fetch('/api/journal/vocab').then(r => r.json()),
    ]);
    JRN.queue = q.rows || [];
    JRN.vocab = v;
    jrnBadge(q.count || 0);
    $('jrnQueueMeta').textContent =
      q.count ? `${q.count} fill${q.count === 1 ? '' : 's'} awaiting a grade`
              : 'All caught up.';
    jrnRenderQueue();
    jrnSelect(JRN.queue.length ? JRN.queue[0].id : null);
  } catch (e) { $('jrnQueueMeta').textContent = 'Failed to load: ' + e; }
}

function jrnBadge(n) {
  const b = $('jrnBadge');
  if (!b) return;
  b.textContent = n;
  b.style.display = n > 0 ? 'inline-block' : 'none';
}

function jrnRenderQueue() {
  const box = $('jrnQueue');
  if (!JRN.queue.length) { box.innerHTML =
    '<div style="color:var(--text-dim);padding:14px;">Queue is empty.</div>'; return; }
  box.innerHTML = JRN.queue.map(r => {
    const sel = r.id === (JRN.cur && JRN.cur.id) ? 'background:var(--card-2);' : '';
    const sideCol = r.side === 'buy' ? 'var(--green)' : 'var(--red)';
    return `<div onclick="jrnSelect('${r.id}')" style="padding:7px 8px;cursor:pointer;
              border-bottom:1px solid var(--border);${sel}">
       <div><b>${r.symbol}</b>
         <span style="color:${sideCol};font-weight:600;">${r.side.toUpperCase()}</span>
         <span style="color:var(--text-dim);">${r.qty} @ ${r.price}</span></div>
       <div style="font-size:11.5px;color:var(--text-dim);">
         ${(r.filled_at || '').replace('T', ' ')} &middot; ${r.source}</div>
     </div>`;
  }).join('');
}

function jrnSelect(id) {
  JRN.cur = JRN.queue.find(r => r.id === id) || null;
  JRN.g = null; JRN.tg = new Set(); JRN.tb = new Set();
  jrnRenderQueue();
  jrnRenderEditor();
}

function jrnRenderEditor() {
  const box = $('jrnEditor');
  const r = JRN.cur;
  if (!r) { box.innerHTML =
    '<div style="color:var(--text-dim);padding:30px;text-align:center;">' +
    'Nothing waiting. Every fill is graded.</div>'; return; }

  const condKeys = JRN.vocab.condition_keys || [];
  const condVals = JRN.vocab.conditions || {};
  const cond = r.conditions || {};

  box.innerHTML = `
    <div style="margin-bottom:14px;">
      <div style="font-size:19px;font-weight:700;">${r.symbol}</div>
      <div style="color:var(--text-dim);font-size:12.5px;">
        ${r.side.toUpperCase()} ${r.qty} @ ${r.price}
        &middot; ${(r.filled_at || '').replace('T', ' ')}
        &middot; ${r.source} &middot; $${(r.cash || 0).toFixed(2)}</div>
    </div>

    <div class="layerBox">
      <h3>Grade &mdash; 0 worst, 4 best</h3>
      <div class="gradeRow">
        ${[0,1,2,3,4].map(g =>
          `<button class="gradeBtn" id="gb${g}" onclick="jrnSetGrade(${g})">${g}</button>`
        ).join('')}
      </div>
    </div>

    <div class="layerBox">
      <h3>Conditions at this fill</h3>
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:7px;">
        ${condKeys.map(k => {
          const opts = (condVals[k] || []);
          const cur = cond[k] || '';
          return `<label style="font-size:12px;color:var(--text-dim);">${k}
            <input list="dl_${k}" id="cond_${k}" value="${cur}"
                   placeholder="&mdash;" style="width:100%;margin-top:2px;">
            <datalist id="dl_${k}">${opts.map(o => `<option value="${o}">`).join('')}</datalist>
          </label>`;
        }).join('')}
      </div>
    </div>

    <div class="layerBox">
      <h3>What was good</h3>
      <div id="jrnTagsGood">${(JRN.vocab.tags.good || []).map(t =>
        `<span class="chip" onclick="jrnTag('g',this,'${esc(t)}')">${t}</span>`).join('')}</div>
      <input type="text" id="jrnNewGood" placeholder="new tag + Enter"
             onkeydown="jrnNewTag(event,'g')" style="margin-top:6px;width:100%;">
    </div>

    <div class="layerBox">
      <h3>What was bad</h3>
      <div id="jrnTagsBad">${(JRN.vocab.tags.bad || []).map(t =>
        `<span class="chip bad" onclick="jrnTag('b',this,'${esc(t)}')">${t}</span>`).join('')}</div>
      <input type="text" id="jrnNewBad" placeholder="new tag + Enter"
             onkeydown="jrnNewTag(event,'b')" style="margin-top:6px;width:100%;">
    </div>

    <div class="layerBox">
      <h3>Why</h3>
      <textarea id="jrnNote" rows="3" style="width:100%;"
        placeholder="What you saw, and why you took it.">${r.note || ''}</textarea>
    </div>

    <div style="display:flex;gap:8px;">
      <button onclick="jrnSave()" style="flex:1;padding:9px;">Save grade</button>
      <button onclick="jrnSkip()" style="flex:0 0 110px;">Skip</button>
    </div>
    <div id="jrnSaveMsg" style="margin-top:7px;font-size:12px;color:var(--text-dim);"></div>`;
}

function esc(t) { return String(t).replace(/'/g, "\\'"); }

function jrnSetGrade(g) {
  JRN.g = g;
  [0,1,2,3,4].forEach(i => {
    const el = $('gb' + i);
    if (el) el.className = 'gradeBtn' + (i === g ? ' sel' : '');
  });
}

function jrnTag(which, el, tag) {
  const set = which === 'g' ? JRN.tg : JRN.tb;
  if (set.has(tag)) { set.delete(tag); el.classList.remove('on'); }
  else { set.add(tag); el.classList.add('on'); }
}

function jrnNewTag(ev, which) {
  if (ev.key !== 'Enter') return;
  const val = ev.target.value.trim();
  if (!val) return;
  const box = $(which === 'g' ? 'jrnTagsGood' : 'jrnTagsBad');
  const span = document.createElement('span');
  span.className = 'chip' + (which === 'b' ? ' bad on' : ' on');
  span.textContent = val;
  span.onclick = function () { jrnTag(which, this, val); };
  box.appendChild(span);
  (which === 'g' ? JRN.tg : JRN.tb).add(val);
  ev.target.value = '';
}

async function jrnSave() {
  const r = JRN.cur;
  if (!r) return;
  if (JRN.g === null) { $('jrnSaveMsg').textContent = 'Pick a grade first.'; return; }

  const conds = {};
  (JRN.vocab.condition_keys || []).forEach(k => {
    const el = $('cond_' + k);
    if (el && el.value.trim()) conds[k] = el.value.trim();
  });

  try {
    if (Object.keys(conds).length) {
      await fetch('/api/journal/conditions', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({id: r.id, conditions: conds})});
    }
    const res = await fetch('/api/journal/grade', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id: r.id, grade: JRN.g,
        tags_good: [...JRN.tg], tags_bad: [...JRN.tb],
        note: $('jrnNote').value})}).then(x => x.json());
    if (!res.ok) { $('jrnSaveMsg').textContent = 'Error: ' + res.error; return; }
    await jrnLoad();
  } catch (e) { $('jrnSaveMsg').textContent = 'Failed: ' + e; }
}

function jrnSkip() {
  const i = JRN.queue.findIndex(r => r.id === (JRN.cur && JRN.cur.id));
  jrnSelect(JRN.queue[i + 1] ? JRN.queue[i + 1].id : null);
}

async function jrnImport(dry) {
  const path = $('jrnImportPath').value.trim();
  if (!path) return;
  $('jrnImportOut').textContent = dry ? 'Parsing…' : 'Importing…';
  try {
    const res = await fetch('/api/journal/import', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({path: path, dry_run: dry})}).then(r => r.json());
    if (!res.ok) { $('jrnImportOut').textContent = 'Error: ' + res.error; return; }
    const probs = (res.problems || []).slice(0, 25);
    $('jrnImportOut').innerHTML =
      `<b>${dry ? 'Preview' : 'Imported'}</b>: parsed ${res.parsed},
       ${dry ? 'would add' : 'added'} ${res.imported},
       skipped ${res.skipped_duplicates} duplicate(s).` +
      (probs.length ? `<div style="margin-top:6px;">${res.problems.length} row(s) needed
         inference:</div><ul style="margin-left:15px;">` +
         probs.map(p => `<li>row ${p.row} ${p.asset}: ${p.issue}</li>`).join('') + '</ul>'
       : '');
    if (!dry) jrnLoad();
  } catch (e) { $('jrnImportOut').textContent = 'Failed: ' + e; }
}

/* ══════════════ PERFORMANCE / HEATMAP ══════════════ */

let PERF = { layers: {}, sel: {}, grid: null, cell: null };

async function perfLoadLayers() {
  try {
    PERF.layers = await fetch('/api/heatmap/layers').then(r => r.json());
    const c = PERF.layers.conditions || {};
    const rows = Object.keys(c).map(k => `
      <div class="layerBox">
        <h3>${k.replace(/_/g, ' ')}</h3>
        ${c[k].map(v => `<span class="chip" id="ly_${cssId(k + '_' + v)}"
           onclick="perfToggle('${k}','${esc(v)}',this)">${v}</span>`).join('')}
      </div>`).join('');
    const tags = (PERF.layers.tags || []).map(t =>
      `<span class="chip bad" onclick="perfTagExclude('${esc(t)}',this)">${t}</span>`).join('');
    $('perfLayers').innerHTML = rows +
      (tags ? `<div class="layerBox"><h3>Exclude tag</h3>${tags}</div>` : '');
  } catch (e) { $('perfLayers').textContent = 'Failed: ' + e; }
}

function cssId(s) { return s.replace(/[^A-Za-z0-9_]/g, '_'); }

function perfToggle(key, val, el) {
  PERF.sel[key] = PERF.sel[key] || new Set();
  if (PERF.sel[key].has(val)) { PERF.sel[key].delete(val); el.classList.remove('on'); }
  else { PERF.sel[key].add(val); el.classList.add('on'); }
  perfLoad();
}

function perfTagExclude(tag, el) {
  PERF.sel._tag_none = PERF.sel._tag_none || new Set();
  if (PERF.sel._tag_none.has(tag)) { PERF.sel._tag_none.delete(tag); el.classList.remove('on'); }
  else { PERF.sel._tag_none.add(tag); el.classList.add('on'); }
  perfLoad();
}

function perfClearLayers() {
  PERF.sel = {};
  document.querySelectorAll('#perfLayers .chip').forEach(c => c.classList.remove('on'));
  perfLoad();
}

function perfQuery() {
  const p = [];
  Object.keys(PERF.sel).forEach(k => {
    if (k === '_tag_none') PERF.sel[k].forEach(v => p.push('tag_none=' + encodeURIComponent(v)));
    else PERF.sel[k].forEach(v => p.push('layer=' + encodeURIComponent(k + ':' + v)));
  });
  return p.join('&');
}

async function perfLoad() {
  try {
    const q = perfQuery();
    PERF.grid = await fetch('/api/heatmap' + (q ? '?' + q : '')).then(r => r.json());
    const rec = await fetch('/api/journal/reconcile').then(r => r.json());
    $('perfRecon').innerHTML = rec.ok
      ? `Pairing reconciles.<br>${rec.fills} fills &rarr; ${rec.legs} legs.`
      : `<span style="color:var(--red)">Pairing does NOT reconcile
         (off by $${rec.difference}). Treat the grid as unreliable.</span>`;
    perfRender();
  } catch (e) { $('perfGrid').textContent = 'Failed: ' + e; }
}

function perfRender() {
  const g = PERF.grid;
  if (!g) return;
  const metric = $('perfMetric').value;
  const s = g.summary.overall;

  $('perfKpi').innerHTML = `
    <div>Legs<b>${s.n}</b></div>
    <div>Total P/L<b>${money(s.total_pl)}</b></div>
    <div>Avg P/L<b>${money(s.avg_pl)}</b></div>
    <div>Win rate<b>${s.win_rate == null ? '&mdash;' : (s.win_rate * 100).toFixed(0) + '%'}</b></div>
    <div>Median hold<b>${s.median_hold_min == null ? '&mdash;' : s.median_hold_min + 'm'}</b></div>
    <div>Still open<b>${s.open_legs}</b></div>`;

  $('perfWarn').textContent = g.summary.sparse_cells
    ? `${g.summary.sparse_cells} cell(s) under ${g.summary.sparse_below} trades — dashed, read as anecdote`
    : '';

  const vals = [];
  g.exit_keys.forEach(x => g.grades.forEach(e => {
    const c = g.cells[e + '|' + x];
    if (c && c.n > 0 && c[metric] != null) vals.push(c[metric]);
  }));
  const lo = Math.min(...vals, 0), hi = Math.max(...vals, 0);

  let html = '<table class="hmTable"><thead><tr><th></th>' +
    g.exit_keys.map(x => `<th>${x === 'none' ? 'No exit' : 'Exit ' + x}</th>`).join('') +
    '<th style="border-left:2px solid var(--border)">All</th></tr></thead><tbody>';

  g.grades.forEach(e => {
    html += `<tr><th style="text-align:right">Entry ${e}</th>`;
    g.exit_keys.forEach(x => {
      const c = g.cells[e + '|' + x];
      html += perfCell(c, metric, lo, hi, e, x);
    });
    html += perfCell(g.row_margin[String(e)], metric, lo, hi, e, '*', true);
    html += '</tr>';
  });
  html += '<tr><th style="text-align:right;border-top:2px solid var(--border)">All</th>';
  g.exit_keys.forEach(x => {
    html += perfCell(g.col_margin[String(x)], metric, lo, hi, '*', x, true);
  });
  html += '<td></td></tr></tbody></table>';
  $('perfGrid').innerHTML = html;
}

function perfCell(c, metric, lo, hi, e, x, margin) {
  if (!c || !c.n) return '<td class="hmCell empty"></td>';
  const v = c[metric];
  const cls = 'hmCell' + (c.sparse ? ' sparse' : '') + (margin ? '' : '');
  const style = margin ? 'background:var(--card-2);' : `background:${heatColor(v, lo, hi, metric)};`;
  const click = margin ? '' : `onclick="perfCellClick('${e}','${x}',this)"`;
  return `<td class="${cls}" style="${style}" ${click}
            title="${c.n} trades, ${c.n_priced} priced">
      <div class="val">${fmtMetric(v, metric)}</div>
      <div class="n">${c.n} trade${c.n === 1 ? '' : 's'}</div></td>`;
}

function fmtMetric(v, m) {
  if (v == null) return '&mdash;';
  if (m === 'win_rate') return (v * 100).toFixed(0) + '%';
  if (m === 'n') return v;
  if (m === 'median_hold_min') return v < 90 ? v.toFixed(0) + 'm' : (v / 60).toFixed(1) + 'h';
  return money(v);
}

function money(v) {
  if (v == null) return '&mdash;';
  const n = Math.abs(v) >= 1000 ? (v / 1000).toFixed(1) + 'k' : Math.round(v);
  return (v < 0 ? '-$' : '$') + String(n).replace('-', '');
}

function heatColor(v, lo, hi, metric) {
  if (v == null) return 'var(--card-2)';
  // count and hold time are one-directional: pale -> blue.
  if (metric === 'n' || metric === 'median_hold_min') {
    const t = hi > 0 ? v / hi : 0;
    return `rgba(46,117,182,${0.10 + 0.75 * t})`;
  }
  const mid = metric === 'win_rate' ? 0.5 : 0;
  if (v >= mid) {
    const t = hi > mid ? (v - mid) / (hi - mid) : 0;
    return `rgba(30,126,77,${0.10 + 0.72 * t})`;
  }
  const t = lo < mid ? (mid - v) / (mid - lo) : 0;
  return `rgba(192,57,43,${0.10 + 0.72 * t})`;
}

async function perfCellClick(e, x, el) {
  document.querySelectorAll('.hmCell').forEach(c => c.classList.remove('sel'));
  el.classList.add('sel');
  const q = perfQuery();
  const legs = await fetch('/api/legs?limit=400' + (q ? '&' + q : '')).then(r => r.json());
  const rows = (legs.legs || []).filter(l =>
    String(l.entry_grade) === String(e) &&
    String(l.exit_grade == null ? 'none' : l.exit_grade) === String(x));
  $('perfDetailHead').innerHTML =
    `<b>Entry ${e} &rarr; ${x === 'none' ? 'no exit' : 'Exit ' + x}</b> &mdash; ${rows.length} trade(s)`;
  const cols = ['symbol','direction','outcome','qty','entry_at','exit_at',
                'hold_minutes','entry_cash','pl'];
  $('perfDetail').querySelector('thead').innerHTML =
    '<tr>' + cols.map(c => `<th>${c.replace(/_/g,' ')}</th>`).join('') + '<th>note</th></tr>';
  $('perfDetail').querySelector('tbody').innerHTML = rows.map(l =>
    '<tr>' + cols.map(c => {
      let v = l[c];
      if (c === 'pl') return `<td style="color:${v > 0 ? 'var(--green)' : 'var(--red)'}">${v == null ? '—' : money(v)}</td>`;
      if (c === 'entry_cash') return `<td>${money(v)}</td>`;
      if ((c === 'entry_at' || c === 'exit_at') && v) v = String(v).replace('T', ' ');
      return `<td>${v == null ? '—' : v}</td>`;
    }).join('') +
    `<td style="max-width:320px;font-size:11.5px;color:var(--text-dim);">${l.entry_note || ''}</td></tr>`
  ).join('');
}

/* keep the ungraded badge live */
setInterval(() => {
  fetch('/api/journal/stats').then(r => r.json())
    .then(s => jrnBadge(s.ungraded || 0)).catch(() => {});
}, 30000);

/* ── logs ── */'''
assert s.count(old_js_anchor) == 1, 'js anchor not found'
s = s.replace(old_js_anchor, new_js, 1)

open(PATH, 'w', encoding='utf-8').write(s)
print('frontend patched: 5 insertions')

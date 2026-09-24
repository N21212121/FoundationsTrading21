"""Performance tab: layers become a modal, grid gets the width, KPIs stack right.

Run after the earlier frontend patches. Idempotent.
"""
import re
import sys

PATH = 'static/index.html'
MARKER = 'id="perfLayerModal"'

s = open(PATH, encoding='utf-8').read()
if MARKER in s:
    print('already patched; nothing to do')
    sys.exit(0)
if 'id="perfView"' not in s:
    print('run the earlier patches first'); sys.exit(1)

# ── 1. replace the whole perfView block ──────────────────────────────────────
start = s.index('<!-- ═══════════════ PERFORMANCE ═══════════════ -->')
# The PERFORMANCE block is followed by logsView, NOT by the JOURNAL comment --
# JOURNAL sits earlier in the document. Slicing to an anchor that precedes
# `start` silently duplicates everything between them.
end = s.index('<div id="logsView"', start)
assert end > start, 'perfView end anchor precedes its start'

new_view = '''<!-- ═══════════════ PERFORMANCE ═══════════════ -->
<div id="perfView" style="display:none; flex:1; padding:12px; min-height:0;
     flex-direction:column; gap:10px;">

  <div class="card" style="flex:0 0 auto; display:flex; flex-direction:column;
       gap:9px; padding-bottom:11px;">
    <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;">
      <h2 style="margin:0;border:0;padding:0;">Entry &times; Exit Grade</h2>
      <select id="perfMetric" onchange="perfRender()">
        <option value="avg_pl">Avg P/L</option>
        <option value="total_pl">Total P/L</option>
        <option value="n">Trade count</option>
        <option value="win_rate">Win rate</option>
        <option value="median_hold_min">Median hold</option>
      </select>
      <button onclick="perfOpenLayers()">Layers<span id="perfLayerCount"
        class="badge" style="display:none">0</span></button>
      <button onclick="perfLoad()">Refresh</button>
      <span id="perfActive" style="font-size:12px;color:var(--text-dim);"></span>
      <span id="perfWarn" style="color:var(--red);font-size:12px;margin-left:auto;"></span>
    </div>

    <div style="display:flex; gap:14px; align-items:flex-start;">
      <div style="flex:1; overflow-x:auto;"><div id="perfGrid"></div></div>
      <div id="perfKpi" style="flex:0 0 132px;"></div>
    </div>
  </div>

  <div class="card" style="flex:1; display:flex; flex-direction:column; min-height:0;">
    <div id="perfDetailHead" style="font-size:12.5px;color:var(--text-dim);
         margin-bottom:7px;">Click a cell to list its trades.</div>
    <div style="flex:1; overflow:auto; min-height:0;">
      <table id="perfDetail"><thead></thead><tbody></tbody></table>
    </div>
  </div>
</div>

<!-- layers modal -->
<div id="perfLayerModal" style="display:none; position:fixed; inset:0;
     background:rgba(15,28,43,.45); z-index:900; align-items:center;
     justify-content:center;" onclick="perfLayerBackdrop(event)">
  <div style="background:var(--card); border:1px solid var(--border);
       border-radius:9px; width:min(940px,92vw); max-height:86vh;
       display:flex; flex-direction:column; box-shadow:0 12px 40px rgba(0,0,0,.28);">
    <div style="display:flex;align-items:center;gap:10px;padding:13px 16px;
         border-bottom:1px solid var(--border);">
      <h2 style="margin:0;border:0;padding:0;">Layers</h2>
      <span id="perfModalSummary" style="font-size:12px;color:var(--text-dim);"></span>
      <button onclick="perfClearLayers()" style="margin-left:auto;">Clear all</button>
      <button onclick="perfCloseLayers()">Done</button>
    </div>
    <div id="perfLayers" style="padding:13px 16px; overflow:auto; font-size:12.5px;"></div>
  </div>
</div>

'''
s = s[:start] + new_view + s[end:]

# ── 2. CSS ───────────────────────────────────────────────────────────────────
old_css = '''  .layerBox {'''
new_css = '''  .layerGrid {
    display:grid; grid-template-columns:repeat(auto-fill,minmax(270px,1fr));
    gap:9px;
  }
  .layerSideHead {
    grid-column:1/-1; font-size:11px; text-transform:uppercase;
    letter-spacing:1px; color:var(--text-dim); font-weight:700;
    border-bottom:1px solid var(--border); padding-bottom:3px; margin-top:4px;
  }
  .kpiStack div {
    border:1px solid var(--border); border-radius:6px; padding:6px 9px;
    margin-bottom:6px; font-size:11px; color:var(--text-dim);
    text-transform:uppercase; letter-spacing:.6px;
  }
  .kpiStack b {
    display:block; font-size:19px; color:var(--text); font-weight:700;
    letter-spacing:0; text-transform:none; margin-top:1px;
  }
  .layerBox {'''
assert s.count(old_css) == 1, 'css anchor missing'
s = s.replace(old_css, new_css, 1)

# ── 3. replace the JS block from perfLoadLayers through perfRender's KPI write
js_start = s.index("let PERF = { layers: {}, sel: {}, grid: null, cell: null };")
js_end = s.index("function perfCell(c, metric, lo, hi, e, x, margin) {")
new_js = r'''let PERF = { layers: {}, sel: {}, grid: null, cell: null };

async function perfLoadLayers() {
  try {
    PERF.layers = await fetch('/api/heatmap/layers').then(r => r.json());
    perfRenderLayers();
  } catch (e) { $('perfLayers').textContent = 'Failed: ' + e; }
}

function perfRenderLayers() {
  const groups = PERF.layers.groups || [];
  let html = '<div class="layerGrid">';
  let side = null;
  groups.forEach(g => {
    if (g.side !== side) {
      side = g.side;
      html += `<div class="layerSideHead">${side === 'entry'
        ? 'At entry' : 'At exit'}</div>`;
    }
    html += `<div class="layerBox"><h3>${g.label}</h3>` +
      g.values.map(v => {
        const on = PERF.sel[g.key] && PERF.sel[g.key].has(v) ? ' on' : '';
        return `<span class="chip${on}" id="ly_${cssId(g.key + '_' + v)}"
          onclick="perfToggle('${g.key}','${esc(v)}',this)">${v}</span>`;
      }).join('') + '</div>';
  });
  const tags = PERF.layers.tags || [];
  if (tags.length) {
    html += '<div class="layerSideHead">Exclude any trade carrying a tag</div>';
    html += '<div class="layerBox" style="grid-column:1/-1;">' +
      tags.map(t => {
        const on = PERF.sel._tag_none && PERF.sel._tag_none.has(t) ? ' on' : '';
        return `<span class="chip bad${on}"
          onclick="perfTagExclude('${esc(t)}',this)">${t}</span>`;
      }).join('') + '</div>';
  }
  $('perfLayers').innerHTML = html + '</div>';
  perfActiveSummary();
}

function perfOpenLayers() {
  if (!(PERF.layers.groups || []).length) perfLoadLayers();
  else perfRenderLayers();
  $('perfLayerModal').style.display = 'flex';
}

function perfCloseLayers() { $('perfLayerModal').style.display = 'none'; }

function perfLayerBackdrop(ev) {
  if (ev.target.id === 'perfLayerModal') perfCloseLayers();
}

function perfCountActive() {
  return Object.keys(PERF.sel)
    .reduce((n, k) => n + (PERF.sel[k] ? PERF.sel[k].size : 0), 0);
}

function perfActiveSummary() {
  const n = perfCountActive();
  const badge = $('perfLayerCount');
  badge.textContent = n;
  badge.style.display = n ? 'inline-block' : 'none';

  const bits = [];
  Object.keys(PERF.sel).forEach(k => {
    if (!PERF.sel[k] || !PERF.sel[k].size) return;
    const vals = [...PERF.sel[k]].join(', ');
    if (k === '_tag_none') bits.push(`excluding ${vals}`);
    else {
      const g = (PERF.layers.groups || []).find(x => x.key === k);
      const label = g ? `${g.side === 'entry' ? 'Entry' : 'Exit'} ${g.label}`
                      : k.replace(/_/g, ' ');
      bits.push(`${label}: ${vals}`);
    }
  });
  const txt = bits.length ? bits.join('  ·  ') : '';
  $('perfActive').textContent = txt.length > 110 ? txt.slice(0, 110) + '…' : txt;
  const ms = $('perfModalSummary');
  if (ms) ms.textContent = n ? `${n} filter${n === 1 ? '' : 's'} on` : 'no filters';
}

function cssId(s) { return s.replace(/[^A-Za-z0-9_]/g, '_'); }

function perfToggle(key, val, el) {
  PERF.sel[key] = PERF.sel[key] || new Set();
  if (PERF.sel[key].has(val)) { PERF.sel[key].delete(val); el.classList.remove('on'); }
  else { PERF.sel[key].add(val); el.classList.add('on'); }
  perfActiveSummary();
  perfLoad();
}

function perfTagExclude(tag, el) {
  PERF.sel._tag_none = PERF.sel._tag_none || new Set();
  if (PERF.sel._tag_none.has(tag)) { PERF.sel._tag_none.delete(tag); el.classList.remove('on'); }
  else { PERF.sel._tag_none.add(tag); el.classList.add('on'); }
  perfActiveSummary();
  perfLoad();
}

function perfClearLayers() {
  PERF.sel = {};
  document.querySelectorAll('#perfLayers .chip').forEach(c => c.classList.remove('on'));
  perfActiveSummary();
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
    perfRender();
  } catch (e) { $('perfGrid').textContent = 'Failed: ' + e; }
}

function perfRender() {
  const g = PERF.grid;
  if (!g) return;
  const metric = $('perfMetric').value;
  const s = g.summary.overall;

  $('perfKpi').className = 'kpiStack';
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

'''
s = s[:js_start] + new_js + s[js_end:]

# ── 4. drop the now-dead reconcile panel write (its element is gone) ─────────
s = s.replace('''    const rec = await fetch('/api/journal/reconcile').then(r => r.json());
    $('perfRecon').innerHTML = rec.ok
      ? `Pairing reconciles.<br>${rec.fills} fills &rarr; ${rec.legs} legs.`
      : `<span style="color:var(--red)">Pairing does NOT reconcile
         (off by $${rec.difference}). Treat the grid as unreliable.</span>`;
''', '')

# ── 5. Esc closes the modal ──────────────────────────────────────────────────
s = s.replace('''/* keep the ungraded badge live */''',
'''document.addEventListener('keydown', e => {
  if (e.key === 'Escape') perfCloseLayers();
});

/* keep the ungraded badge live */''', 1)

open(PATH, 'w', encoding='utf-8').write(s)
print('performance layout patched')

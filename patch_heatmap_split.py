"""Performance grid: segregate the no-exit column from the graded exits.

A no-exit leg is one pairing never saw a closing fill for -- still open, or
an option left to expire. It is not a sixth exit grade, and sitting it flush
against Exit 0-4 made it read as one: the worthless expiries took the red end
of the colour scale away from real graded exits, and the ALL margin counted
them as losses, which cost a good entry grade six points of win rate.

This patch:
  * walls the no-exit column off with a gutter, and again before ALL,
  * renders it neutral rather than on the graded colour scale,
  * points ALL at the graded-only margin the backend now ships,
  * adds one toggle that shows or hides the no-exit and ALL blocks together,
    remembered across reloads.

Run after patch_frontend.py and patch_perf_layout.py. Idempotent.
"""
import sys

PATH = 'static/index.html'
MARKER = 'function perfToggleMargins'

s = open(PATH, encoding='utf-8').read()
if MARKER in s:
    print('already patched; nothing to do')
    sys.exit(0)
if 'id="perfLayerModal"' not in s:
    print('run patch_perf_layout.py first'); sys.exit(1)

# ── 1. CSS ───────────────────────────────────────────────────────────────────
old_css = '''  .hmCell.sel { outline:3px solid var(--navy); outline-offset:1px; }'''
new_css = '''  .hmCell.sel { outline:3px solid var(--navy); outline-offset:1px; }
  /* the gutter that separates graded exits from no-exit, and no-exit from ALL */
  .hmTable .hmGap { width:13px; min-width:13px; padding:0; border:0;
    background:none; }
  .hmTable .hmGapRow td, .hmTable .hmGapRow th { height:9px; padding:0;
    border:0; background:none; }
  /* no-exit is a different population, so it does not get the graded ramp */
  .hmCell.noexit { background:var(--card-2); border-style:dotted;
    border-color:var(--text-dim); }
  .hmCell.noexit .n, .hmCell.margin .n { color:var(--text-dim); }
  .hmCell.noexit .val, .hmCell.margin .val { color:var(--text); }
  .hmCell.margin { background:var(--card-2); cursor:default; }
  .hmCell.margin:hover { transform:none; }
  .hmTable th.hmAside { color:var(--text); }
  .hmNote { font-size:11.5px; color:var(--text-dim); margin-top:8px;
    line-height:1.5; }
  #perfMarginBtn.on { background:var(--navy-3); border-color:var(--navy-3);
    color:#fff; }'''
assert s.count(old_css) == 1, 'hmCell.sel css anchor missing'
s = s.replace(old_css, new_css, 1)

# ── 2. the Margins toggle, next to Refresh ───────────────────────────────────
old_btn = '''      <button onclick="perfLoad()">Refresh</button>'''
new_btn = '''      <button onclick="perfLoad()">Refresh</button>
      <button id="perfMarginBtn" onclick="perfToggleMargins()"
        title="Show or hide the no-exit column and the ALL margins together"
        >Margins</button>'''
assert s.count(old_btn) == 1, 'Refresh button anchor missing'
s = s.replace(old_btn, new_btn, 1)

# ── 3. perfRender + perfCell ────────────────────────────────────────────────
assert s.count('function perfRender() {') == 1, 'perfRender anchor not unique'
assert s.count('function perfCell(') == 1, 'perfCell anchor not unique'
assert s.count('function fmtMetric(v, m) {') == 1, 'fmtMetric anchor not unique'
js_start = s.index('function perfRender() {')
js_end = s.index('function fmtMetric(v, m) {')
assert js_end > js_start, 'fmtMetric precedes perfRender'
new_js = r'''function perfMarginsOn() {
  try { return localStorage.getItem('perfMargins') !== 'off'; }
  catch (e) { return true; }
}

function perfToggleMargins() {
  try { localStorage.setItem('perfMargins', perfMarginsOn() ? 'off' : 'on'); }
  catch (e) {}
  perfRender();
}

function perfRender() {
  const g = PERF.grid;
  if (!g) return;
  const metric = $('perfMetric').value;
  const s = g.summary.overall;
  const gradedAll = (g.grand && g.grand.graded) || g.summary.overall_graded || s;
  const noneAll = (g.grand && g.grand.no_exit) || g.summary.overall_no_exit
                  || { n: 0 };
  const margins = perfMarginsOn();
  const btn = $('perfMarginBtn');
  if (btn) {
    btn.textContent = margins ? 'Margins on' : 'Margins off';
    btn.classList.toggle('on', margins);
  }

  // The KPI stack reports the graded population, because that is what the
  // grid's ALL column now reports. No-exit gets its own line so the legs
  // are never silently dropped from the count.
  $('perfKpi').className = 'kpiStack';
  $('perfKpi').innerHTML = `
    <div>Graded legs<b>${gradedAll.n}</b></div>
    <div>Total P/L<b>${money(gradedAll.total_pl)}</b></div>
    <div>Avg P/L<b>${money(gradedAll.avg_pl)}</b></div>
    <div>Win rate<b>${gradedAll.win_rate == null ? '&mdash;'
      : (gradedAll.win_rate * 100).toFixed(0) + '%'}</b></div>
    <div>Median hold<b>${gradedAll.median_hold_min == null ? '&mdash;'
      : gradedAll.median_hold_min + 'm'}</b></div>
    <div>No exit<b>${noneAll.n}</b></div>
    <div>Still open<b>${s.open_legs}</b></div>`;

  $('perfWarn').textContent = g.summary.sparse_cells
    ? `${g.summary.sparse_cells} cell(s) under ${g.summary.sparse_below} trades — dashed, read as anecdote`
    : '';

  // Scale the colour ramp over GRADED cells only. Including no-exit let a
  // batch of expired contracts at 0% own the red end and flatten everything
  // a real exit grade was trying to say.
  const exitGrades = g.exit_grades
    || g.exit_keys.filter(k => k !== 'none');
  const vals = [];
  exitGrades.forEach(x => g.grades.forEach(e => {
    const c = g.cells[e + '|' + x];
    if (c && c.n > 0 && c[metric] != null) vals.push(c[metric]);
  }));
  const lo = Math.min(...vals, 0), hi = Math.max(...vals, 0);

  const gapTh = '<th class="hmGap"></th>';
  let html = '<table class="hmTable"><thead><tr><th></th>' +
    exitGrades.map(x => `<th>Exit ${x}</th>`).join('');
  if (margins) {
    html += gapTh + '<th class="hmAside" title="Never closed: still open, or ' +
      'an option left to expire. Not an exit grade.">No exit</th>' +
      gapTh + '<th class="hmAside" title="Graded exits only. No-exit legs are ' +
      'excluded.">All exits</th>';
  }
  html += '</tr></thead><tbody>';

  g.grades.forEach(e => {
    html += `<tr><th style="text-align:right">Entry ${e}</th>`;
    exitGrades.forEach(x => {
      html += perfCell(g.cells[e + '|' + x], metric, lo, hi, e, x);
    });
    if (margins) {
      html += '<td class="hmGap"></td>' +
        perfCell(g.cells[e + '|none'], metric, lo, hi, e, 'none', 'noexit') +
        '<td class="hmGap"></td>' +
        perfCell(g.row_margin[String(e)], metric, lo, hi, e, '*', 'margin');
    }
    html += '</tr>';
  });

  if (margins) {
    const span = 1 + exitGrades.length + 4;
    html += `<tr class="hmGapRow"><td colspan="${span}"></td></tr>`;
    html += '<tr><th style="text-align:right">All entries</th>';
    exitGrades.forEach(x => {
      html += perfCell(g.col_margin[String(x)], metric, lo, hi, '*', x, 'margin');
    });
    html += '<td class="hmGap"></td>' +
      perfCell(g.col_margin['none'], metric, lo, hi, '*', 'none', 'margin') +
      '<td class="hmGap"></td>' +
      perfCell(gradedAll, metric, lo, hi, '*', '*', 'margin');
    html += '</tr>';
  }
  html += '</tbody></table>';

  if (margins) {
    html += `<div class="hmNote"><b>No exit</b> = ${noneAll.n} leg(s) that
      never got a closing fill — still open, or expired. They sit outside the
      graded scale and outside <b>All exits</b>, which covers
      ${gradedAll.n} graded leg(s).</div>`;
  }
  $('perfGrid').innerHTML = html;
}

function perfCell(c, metric, lo, hi, e, x, variant) {
  const margin = variant === 'margin';
  if (!c || !c.n) return '<td class="hmCell empty"></td>';
  const v = c[metric];
  const cls = 'hmCell' + (c.sparse ? ' sparse' : '') +
    (variant && variant !== true ? ' ' + variant : (margin ? ' margin' : ''));
  // margins and no-exit are read off the graded ramp on purpose: colouring
  // them would invite comparison between populations that do not compare.
  const style = (margin || variant === 'noexit')
    ? '' : `background:${heatColor(v, lo, hi, metric)};`;
  const click = margin ? '' : `onclick="perfCellClick('${e}','${x}',this)"`;
  return `<td class="${cls}" style="${style}" ${click}
            title="${c.n} trades, ${c.n_priced} priced">
      <div class="val">${fmtMetric(v, metric)}</div>
      <div class="n">${c.n} trade${c.n === 1 ? '' : 's'}</div></td>`;
}

'''
s = s[:js_start] + new_js + s[js_end:]

open(PATH, 'w', encoding='utf-8').write(s)
print('heatmap no-exit split patched')

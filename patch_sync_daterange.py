"""Replace the sync preset dropdown with a start/end date range.

Run after the earlier frontend patches. Idempotent.
"""
import sys

PATH = 'static/index.html'
MARKER = 'id="jrnSyncStart"'

s = open(PATH, encoding='utf-8').read()
if MARKER in s:
    print('already patched; nothing to do')
    sys.exit(0)
if 'id="jrnSyncDays"' not in s:
    print('run the earlier patches first'); sys.exit(1)

# ── 1. date inputs replace the preset select ─────────────────────────────────
old = '''        <select id="jrnSyncDays" style="width:100%;margin-bottom:6px;">
          <option value="7">Last 7 days</option>
          <option value="30" selected>Last 30 days</option>
          <option value="90">Last 90 days</option>
          <option value="365">Last year</option>
        </select>'''
new = '''        <div style="display:flex;gap:6px;margin-bottom:6px;">
          <label style="flex:1;font-size:11.5px;color:var(--text-dim);">From
            <input type="date" id="jrnSyncStart" style="width:100%;margin-top:2px;">
          </label>
          <label style="flex:1;font-size:11.5px;color:var(--text-dim);">To
            <input type="date" id="jrnSyncEnd" placeholder="today"
                   style="width:100%;margin-top:2px;">
          </label>
        </div>
        <div style="display:flex;gap:5px;margin-bottom:6px;">
          <button class="rangeBtn" onclick="jrnRange(0)">Today</button>
          <button class="rangeBtn" onclick="jrnRange(7)">7d</button>
          <button class="rangeBtn" onclick="jrnRange(30)">30d</button>
          <button class="rangeBtn" onclick="jrnRange('since')">Since last fill</button>
        </div>'''
assert s.count(old) == 1, 'preset select anchor missing'
s = s.replace(old, new)

# ── 2. CSS for the shortcut buttons ──────────────────────────────────────────
old_css = '''  .srcTab.on { background: var(--navy-3); color:#fff; border-color:var(--navy-3); }'''
new_css = '''  .srcTab.on { background: var(--navy-3); color:#fff; border-color:var(--navy-3); }
  .rangeBtn {
    flex:1; padding:3px 0; font-size:11px; border:1px solid var(--border);
    border-radius:4px; background:var(--card-2); color:var(--text-dim);
    cursor:pointer;
  }
  .rangeBtn:hover { background: var(--navy-3); color:#fff; border-color:var(--navy-3); }'''
assert s.count(old_css) == 1, 'css anchor missing'
s = s.replace(old_css, new_css, 1)

# ── 3. JS: send start/end, default start to today ────────────────────────────
old_js = '''async function jrnSync(dry) {
  jrnSrcOut(dry ? 'Fetching…' : 'Syncing…');
  try {
    const res = await fetch('/api/journal/sync', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({days: +$('jrnSyncDays').value, dry_run: dry})
    }).then(r => r.json());'''
new_js = r'''function todayISO() {
  // Local calendar day. toISOString() would hand back UTC, which after
  // 8pm Eastern is already tomorrow.
  const d = new Date();
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000)
    .toISOString().slice(0, 10);
}

function jrnRange(kind) {
  const end = todayISO();
  let start = end;
  if (kind === 'since') {
    start = JRN.lastFillDay || end;
  } else if (kind > 0) {
    const d = new Date();
    d.setDate(d.getDate() - kind);
    start = new Date(d.getTime() - d.getTimezoneOffset() * 60000)
      .toISOString().slice(0, 10);
  }
  $('jrnSyncStart').value = start;
  $('jrnSyncEnd').value = end;
}

async function jrnSync(dry) {
  const start = $('jrnSyncStart').value;
  const end = $('jrnSyncEnd').value;
  if (!start) { jrnSrcOut('Pick a start date.'); return; }
  if (end && end < start) { jrnSrcOut('The end date is before the start date.'); return; }
  jrnSrcOut(dry ? 'Fetching…' : 'Syncing…');
  try {
    const res = await fetch('/api/journal/sync', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({start: start, end: end || null, dry_run: dry})
    }).then(r => r.json());'''
assert s.count(old_js) == 1, 'jrnSync anchor missing'
s = s.replace(old_js, new_js, 1)

# ── 4. cutoff hint remembers the last fill day and seeds the inputs ──────────
old_cut = '''async function jrnCutoffHint() {
  try {
    const c = await fetch('/api/journal/cutoff').then(r => r.json());
    if (!c.latest) { $('jrnCutoff').textContent = ''; return; }
    $('jrnCutoff').innerHTML =
      `Newest fill on file: <b>${c.latest.replace('T', ' ')}</b>. ` +
      `A window reaching further back re-pulls trades you already have.`;
  } catch (e) { $('jrnCutoff').textContent = ''; }
}'''
new_cut = r'''async function jrnCutoffHint() {
  // Default the window to today, so a sync never reaches back into the
  // history that is already imported and graded.
  if (!$('jrnSyncStart').value) $('jrnSyncStart').value = todayISO();
  try {
    const c = await fetch('/api/journal/cutoff').then(r => r.json());
    if (!c.latest) { $('jrnCutoff').textContent = ''; return; }
    JRN.lastFillDay = c.latest.slice(0, 10);
    $('jrnCutoff').innerHTML =
      `Newest fill on file: <b>${c.latest.replace('T', ' ')}</b>. ` +
      `Anything earlier is already imported; re-pulling it is harmless but ` +
      `arrives ungraded.`;
  } catch (e) { $('jrnCutoff').textContent = ''; }
}'''
assert s.count(old_cut) == 1, 'cutoff anchor missing'
s = s.replace(old_cut, new_cut, 1)

open(PATH, 'w', encoding='utf-8').write(s)
print('date range patched: 4 insertions')

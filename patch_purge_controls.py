"""Add purge controls and a sync-cutoff hint to the Journal tab.

Run after patch_frontend.py and patch_setup_tab.py. Idempotent.
"""
import sys

PATH = 'static/index.html'
MARKER = 'id="jrnPurgeBox"'

s = open(PATH, encoding='utf-8').read()
if MARKER in s:
    print('already patched; nothing to do')
    sys.exit(0)
if 'id="srcBoxAlpaca"' not in s:
    print('run patch_frontend.py and patch_setup_tab.py first'); sys.exit(1)

# ── 1. cutoff hint + purge box inside the Alpaca source panel ─────────────────
old = '''        <div style="display:flex;gap:6px;">
          <button onclick="jrnSync(true)"  style="flex:1;">Preview</button>
          <button onclick="jrnSync(false)" style="flex:1;">Sync</button>
        </div>
      </div>'''
new = '''        <div id="jrnCutoff" style="font-size:11.5px;color:var(--text-dim);
             margin-bottom:6px;"></div>
        <div style="display:flex;gap:6px;">
          <button onclick="jrnSync(true)"  style="flex:1;">Preview</button>
          <button onclick="jrnSync(false)" style="flex:1;">Sync</button>
        </div>
      </div>

      <div id="jrnPurgeBox" style="margin-top:9px;border-top:1px solid var(--border);
           padding-top:8px;">
        <div style="font-size:11.5px;color:var(--text-dim);margin-bottom:5px;">
          Synced history you had already graded elsewhere? Remove the ungraded
          copies. Graded fills are never touched.</div>
        <div style="display:flex;gap:6px;">
          <button onclick="jrnPurge(true)"  style="flex:1;">Preview removal</button>
          <button onclick="jrnPurge(false)" style="flex:1;">Remove ungraded synced</button>
        </div>
      </div>'''
assert s.count(old) == 1, 'alpaca panel anchor missing'
s = s.replace(old, new)

# ── 2. JS ─────────────────────────────────────────────────────────────────────
old_js = '''async function jrnPasteGo(dry) {'''
new_js = r'''async function jrnCutoffHint() {
  try {
    const c = await fetch('/api/journal/cutoff').then(r => r.json());
    if (!c.latest) { $('jrnCutoff').textContent = ''; return; }
    $('jrnCutoff').innerHTML =
      `Newest fill on file: <b>${c.latest.replace('T', ' ')}</b>. ` +
      `A window reaching further back re-pulls trades you already have.`;
  } catch (e) { $('jrnCutoff').textContent = ''; }
}

async function jrnPurge(dry) {
  if (!dry && !confirm(
      'Remove every ungraded fill that came from Alpaca sync?\n\n' +
      'Graded fills are not touched. This cannot be undone.')) return;
  jrnSrcOut(dry ? 'Checking…' : 'Removing…');
  try {
    const res = await fetch('/api/journal/purge', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({source: 'engine', ungraded_only: true,
                            dry_run: dry})}).then(r => r.json());
    if (!res.ok) { jrnSrcOut('<span style="color:var(--red)">' + res.error + '</span>'); return; }
    let html = `<b>${dry ? 'Would remove' : 'Removed'}</b> ${res.removed} ungraded
                synced fill(s); ${res.remaining} record(s) remain.`;
    if ((res.sample || []).length) {
      html += '<ul style="margin-left:15px;margin-top:5px;">' +
        res.sample.map(r => `<li>${r.symbol} ${r.side} ${r.filled_at.replace('T',' ')}</li>`).join('') +
        (res.removed > res.sample.length ? `<li>…and ${res.removed - res.sample.length} more</li>` : '') +
        '</ul>';
    }
    jrnSrcOut(html);
    if (!dry) jrnLoad();
  } catch (e) { jrnSrcOut('Failed: ' + e); }
}

async function jrnPasteGo(dry) {'''
assert s.count(old_js) == 1, 'js anchor missing'
s = s.replace(old_js, new_js, 1)

# ── 3. refresh the cutoff hint whenever the journal loads ─────────────────────
old_load = '''    jrnRenderQueue();
    jrnSelect(JRN.queue.length ? JRN.queue[0].id : null);'''
new_load = '''    jrnRenderQueue();
    jrnSelect(JRN.queue.length ? JRN.queue[0].id : null);
    jrnCutoffHint();'''
assert s.count(old_load) == 1, 'jrnLoad anchor missing'
s = s.replace(old_load, new_load, 1)

open(PATH, 'w', encoding='utf-8').write(s)
print('purge controls patched: 3 insertions')

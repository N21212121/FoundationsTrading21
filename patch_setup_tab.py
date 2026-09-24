"""Add the Setup tab, plus Alpaca sync and paste ingestion in the Journal tab.

Run AFTER patch_frontend.py. Idempotent.
"""
import sys

PATH = 'static/index.html'
MARKER = 'id="setupView"'

s = open(PATH, encoding='utf-8').read()
if MARKER in s:
    print('already patched; nothing to do')
    sys.exit(0)
if 'id="journalView"' not in s:
    print('run patch_frontend.py first'); sys.exit(1)

# ── 1. TAB BUTTON ─────────────────────────────────────────────────────────────
old = '''  <div class="tab" title="next build pass">Setup</div>'''
new = '''  <div class="tab" id="tabSetup" onclick="showTab('setup')">Setup</div>'''
assert s.count(old) == 1, 'setup tab anchor missing'
s = s.replace(old, new)

# ── 2. SOURCE PANEL IN THE JOURNAL TAB ────────────────────────────────────────
old_import = '''    <div style="border-top:1px solid var(--border); padding-top:10px; margin-top:10px;">
      <h2 style="border:0;padding:0;margin-bottom:8px;">Import</h2>
      <input type="text" id="jrnImportPath" placeholder="path to exported CSV"
             style="width:100%; margin-bottom:6px;">
      <div style="display:flex; gap:6px;">
        <button onclick="jrnImport(true)"  style="flex:1;">Preview</button>
        <button onclick="jrnImport(false)" style="flex:1;">Import</button>
      </div>
      <div id="jrnImportOut" style="font-size:12px;color:var(--text-dim);margin-top:7px;
           max-height:150px;overflow:auto;"></div>
    </div>'''
new_import = '''    <div style="border-top:1px solid var(--border); padding-top:10px; margin-top:10px;">
      <div style="display:flex;gap:6px;margin-bottom:8px;">
        <button class="srcTab on" id="srcAlpaca" onclick="jrnSrc('alpaca')">Alpaca</button>
        <button class="srcTab" id="srcPaste"  onclick="jrnSrc('paste')">Paste</button>
        <button class="srcTab" id="srcFile"   onclick="jrnSrc('file')">File</button>
      </div>

      <div id="srcBoxAlpaca">
        <div style="font-size:12px;color:var(--text-dim);margin-bottom:5px;">
          Pulls fills off the connected account. Ungraded on arrival.</div>
        <select id="jrnSyncDays" style="width:100%;margin-bottom:6px;">
          <option value="7">Last 7 days</option>
          <option value="30" selected>Last 30 days</option>
          <option value="90">Last 90 days</option>
          <option value="365">Last year</option>
        </select>
        <div style="display:flex;gap:6px;">
          <button onclick="jrnSync(true)"  style="flex:1;">Preview</button>
          <button onclick="jrnSync(false)" style="flex:1;">Sync</button>
        </div>
      </div>

      <div id="srcBoxPaste" style="display:none;">
        <div style="font-size:12px;color:var(--text-dim);margin-bottom:5px;">
          Paste rows with a header line. Needs symbol, side, qty, price, time.</div>
        <textarea id="jrnPaste" rows="6" style="width:100%;margin-bottom:6px;
          font-family:monospace;font-size:11.5px;"
          placeholder="Symbol&#9;Side&#9;Qty&#9;Price&#9;Filled At"></textarea>
        <div style="display:flex;gap:6px;">
          <button onclick="jrnPasteGo(true)"  style="flex:1;">Preview</button>
          <button onclick="jrnPasteGo(false)" style="flex:1;">Add</button>
        </div>
      </div>

      <div id="srcBoxFile" style="display:none;">
        <div style="font-size:12px;color:var(--text-dim);margin-bottom:5px;">
          Path to an exported sheet on this machine.</div>
        <input type="text" id="jrnImportPath" placeholder="C:\\path\\to\\export.csv"
               style="width:100%; margin-bottom:6px;">
        <div style="display:flex; gap:6px;">
          <button onclick="jrnImport(true)"  style="flex:1;">Preview</button>
          <button onclick="jrnImport(false)" style="flex:1;">Import</button>
        </div>
      </div>

      <div id="jrnImportOut" style="font-size:12px;color:var(--text-dim);margin-top:7px;
           max-height:170px;overflow:auto;"></div>
    </div>'''
assert s.count(old_import) == 1, 'journal import panel anchor missing'
s = s.replace(old_import, new_import)

# ── 3. SETUP VIEW ─────────────────────────────────────────────────────────────
anchor = '<!-- ═══════════════ JOURNAL ═══════════════ -->'
setup_view = '''<!-- ═══════════════ SETUP ═══════════════ -->
<div id="setupView" style="display:none; flex:1; padding:12px; min-height:0;
     gap:12px; overflow:auto;">
  <div class="card" style="flex:0 0 480px;">
    <h2>API Credentials</h2>
    <div id="setupState" style="font-size:12.5px;color:var(--text-dim);margin-bottom:12px;"></div>

    <label style="font-size:12px;color:var(--text-dim);">API Key ID</label>
    <input type="text" id="cfgKey" placeholder="leave blank to keep current"
           style="width:100%;margin:3px 0 10px;">

    <label style="font-size:12px;color:var(--text-dim);">API Secret</label>
    <input type="password" id="cfgSecret" placeholder="leave blank to keep current"
           style="width:100%;margin:3px 0 14px;">

    <div class="layerBox" id="cfgAcctBox">
      <h3>Account type</h3>
      <div style="display:flex;gap:8px;margin-bottom:8px;">
        <button class="gradeBtn" id="acctPaper" onclick="cfgAcct(true)"
                style="font-size:13px;">Paper</button>
        <button class="gradeBtn" id="acctLive" onclick="cfgAcct(false)"
                style="font-size:13px;">Live</button>
      </div>
      <div id="cfgLiveWarn" style="display:none;">
        <div style="background:#fdecea;border:1px solid var(--red);border-radius:6px;
             padding:9px;font-size:12.5px;color:var(--red);margin-bottom:7px;">
          <b>Live account.</b> The bar loop can place orders on its own. Saving
          this disarms the engine and disconnects; you re-arm by hand afterwards.
          Type <b>LIVE</b> to confirm.
        </div>
        <input type="text" id="cfgConfirm" placeholder="type LIVE"
               style="width:100%;">
      </div>
    </div>

    <button onclick="cfgSave()" style="width:100%;padding:10px;margin-top:4px;">
      Save settings</button>
    <div id="cfgMsg" style="margin-top:9px;font-size:12.5px;"></div>

    <div style="border-top:1px solid var(--border);margin-top:14px;padding-top:11px;">
      <div style="display:flex;gap:6px;">
        <button onclick="cfgConnect()" style="flex:1;">Connect</button>
        <button onclick="cfgDisconnect()" style="flex:1;">Disconnect</button>
      </div>
      <div id="cfgConnMsg" style="margin-top:8px;font-size:12.5px;color:var(--text-dim);"></div>
    </div>
  </div>

  <div class="card" style="flex:1;">
    <h2>Journal Storage</h2>
    <div id="setupJournal" style="font-size:12.5px;"></div>
  </div>
</div>

''' + anchor
assert s.count(anchor) == 1, 'journal view anchor missing'
s = s.replace(anchor, setup_view, 1)

# ── 4. CSS ────────────────────────────────────────────────────────────────────
old_css = '''  .gradeRow { display:flex; gap:6px; }'''
new_css = '''  .srcTab {
    flex:1; padding:5px 0; font-size:12px; border:1px solid var(--border);
    border-radius:5px; background:var(--card-2); color:var(--text); cursor:pointer;
  }
  .srcTab.on { background: var(--navy-3); color:#fff; border-color:var(--navy-3); }
  .gradeRow { display:flex; gap:6px; }'''
assert s.count(old_css) == 1, 'css anchor missing'
s = s.replace(old_css, new_css, 1)

# ── 5. showTab ────────────────────────────────────────────────────────────────
old_show = '''  if (which === 'journal') jrnLoad();'''
new_show = '''  $('setupView').style.display = which === 'setup' ? 'flex' : 'none';
  $('tabSetup').className = 'tab' + (which === 'setup' ? ' active' : '');
  if (which === 'journal') jrnLoad();
  if (which === 'setup') cfgLoad();'''
assert s.count(old_show) == 1, 'showTab anchor missing'
s = s.replace(old_show, new_show, 1)

# ── 6. JS ─────────────────────────────────────────────────────────────────────
old_js = '''/* ══════════════ PERFORMANCE / HEATMAP ══════════════ */'''
new_js = r'''/* ══════════════ JOURNAL SOURCES ══════════════ */

function jrnSrc(which) {
  ['Alpaca','Paste','File'].forEach(n => {
    $('srcBox' + n).style.display = (n.toLowerCase() === which) ? 'block' : 'none';
    $('src' + n).className = 'srcTab' + (n.toLowerCase() === which ? ' on' : '');
  });
}

function jrnSrcOut(html) { $('jrnImportOut').innerHTML = html; }

function jrnProblems(res, dry, verb) {
  const probs = res.problems || [];
  let html = `<b>${dry ? 'Preview' : verb}</b>: ` +
    (res.fetched != null ? `fetched ${res.fetched}, ` : `parsed ${res.parsed}, `) +
    `${dry ? 'would add' : 'added'} ${res.imported}, ` +
    `skipped ${res.skipped_duplicates} duplicate(s).`;
  if (res.account) html += ` <span style="color:var(--text-dim)">(${res.account})</span>`;
  if (probs.length) {
    html += `<div style="margin-top:6px;">${probs.length} row(s) flagged:</div>
             <ul style="margin-left:15px;">` +
      probs.slice(0, 25).map(p =>
        `<li>${p.row != null ? 'row ' + p.row + ' ' : ''}${p.symbol || p.asset || ''}: ${p.issue}</li>`
      ).join('') + '</ul>';
  }
  return html;
}

async function jrnSync(dry) {
  jrnSrcOut(dry ? 'Fetching…' : 'Syncing…');
  try {
    const res = await fetch('/api/journal/sync', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({days: +$('jrnSyncDays').value, dry_run: dry})
    }).then(r => r.json());
    if (!res.ok) { jrnSrcOut('<span style="color:var(--red)">' + res.error + '</span>'); return; }
    jrnSrcOut(jrnProblems(res, dry, 'Synced'));
    if (!dry) jrnLoad();
  } catch (e) { jrnSrcOut('Failed: ' + e); }
}

async function jrnPasteGo(dry) {
  const text = $('jrnPaste').value;
  if (!text.trim()) { jrnSrcOut('Nothing pasted.'); return; }
  jrnSrcOut('Parsing…');
  try {
    const res = await fetch('/api/journal/paste', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({text: text, dry_run: dry})}).then(r => r.json());
    if (!res.ok) { jrnSrcOut('<span style="color:var(--red)">' + res.error + '</span>'); return; }
    jrnSrcOut(jrnProblems(res, dry, 'Added'));
    if (!dry) { $('jrnPaste').value = ''; jrnLoad(); }
  } catch (e) { jrnSrcOut('Failed: ' + e); }
}

/* ══════════════ SETUP ══════════════ */

let CFG = { paper: true, loaded: null };

async function cfgLoad() {
  try {
    const c = await fetch('/api/config').then(r => r.json());
    CFG.loaded = c;
    CFG.paper = c.paper_trading;
    $('setupState').innerHTML =
      `Key: ${c.has_key ? '&bull;&bull;&bull;&bull;' + c.key_tail : '<i>not set</i>'} &nbsp;
       Secret: ${c.has_secret ? 'set' : '<i>not set</i>'}<br>
       Saved account: <b>${c.paper_trading ? 'paper' : 'LIVE'}</b><br>
       Connection: ${c.connected
         ? `<b style="color:var(--green)">connected (${c.connected_as})</b>`
         : '<span style="color:var(--red)">not connected</span>'}`;
    cfgAcct(c.paper_trading, true);
    cfgJournalPanel();
  } catch (e) { $('setupState').textContent = 'Failed: ' + e; }
}

function cfgAcct(paper, quiet) {
  CFG.paper = paper;
  $('acctPaper').className = 'gradeBtn' + (paper ? ' sel' : '');
  $('acctLive').className  = 'gradeBtn' + (paper ? '' : ' sel');
  const changing = CFG.loaded && !paper && CFG.loaded.paper_trading;
  $('cfgLiveWarn').style.display = (!paper && changing) ? 'block' : 'none';
  if (!quiet) $('cfgMsg').textContent = '';
}

async function cfgSave() {
  const body = {paper_trading: CFG.paper};
  const k = $('cfgKey').value.trim(), sec = $('cfgSecret').value.trim();
  if (k) body.alpaca_key = k;
  if (sec) body.alpaca_secret = sec;
  if ($('cfgLiveWarn').style.display === 'block')
    body.confirm = $('cfgConfirm').value.trim();

  $('cfgMsg').textContent = 'Saving…';
  try {
    const res = await fetch('/api/config', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)}).then(r => r.json());
    if (!res.ok) {
      $('cfgMsg').innerHTML = '<span style="color:var(--red)">' + res.error + '</span>';
      return;
    }
    $('cfgKey').value = ''; $('cfgSecret').value = '';
    if ($('cfgConfirm')) $('cfgConfirm').value = '';
    $('cfgMsg').innerHTML = res.changed.length
      ? `<span style="color:var(--green)">Saved: ${res.changed.join(', ')}.</span>
         <div style="color:var(--text-dim);margin-top:3px;">${res.message || ''}</div>`
      : 'No changes.';
    if (res.engine_disarmed && typeof refreshEngineBtn === 'function') refreshEngineBtn();
    cfgLoad();
  } catch (e) { $('cfgMsg').textContent = 'Failed: ' + e; }
}

async function cfgConnect() {
  $('cfgConnMsg').textContent = 'Connecting…';
  try {
    const r = await fetch('/api/connect', {method: 'POST'}).then(x => x.json());
    $('cfgConnMsg').innerHTML = r.status === 'ok'
      ? `<span style="color:var(--green)">${r.message}</span>`
      : `<span style="color:var(--red)">${r.message}</span>`;
    cfgLoad();
  } catch (e) { $('cfgConnMsg').textContent = 'Failed: ' + e; }
}

async function cfgDisconnect() {
  await fetch('/api/disconnect', {method: 'POST'});
  $('cfgConnMsg').textContent = 'Disconnected.';
  cfgLoad();
}

async function cfgJournalPanel() {
  try {
    const [st, rec] = await Promise.all([
      fetch('/api/journal/stats').then(r => r.json()),
      fetch('/api/journal/reconcile').then(r => r.json()),
    ]);
    const by = st.by_source || {};
    $('setupJournal').innerHTML = `
      <div class="kpi">
        <div>Fills<b>${st.records}</b></div>
        <div>Graded<b>${st.graded}</b></div>
        <div>Ungraded<b>${st.ungraded}</b></div>
        <div>Legs<b>${rec.legs != null ? rec.legs : '—'}</b></div>
      </div>
      <div style="margin-bottom:10px;">
        From engine/sync: <b>${by.engine || 0}</b> &nbsp;
        pasted/manual: <b>${by.manual || 0}</b> &nbsp;
        imported: <b>${by.import || 0}</b>
      </div>
      <div style="margin-bottom:10px;">
        ${rec.ok
          ? '<span style="color:var(--green)">Pairing reconciles to the cent.</span>'
          : `<span style="color:var(--red)">Pairing does not reconcile
             (off by $${rec.difference}). Treat the heatmap as unreliable.</span>`}
      </div>
      <div style="font-size:11.5px;color:var(--text-dim);word-break:break-all;">
        Journal file: ${st.file}<br>Data dir: ${(CFG.loaded||{}).data_dir || ''}
      </div>`;
  } catch (e) { $('setupJournal').textContent = 'Failed: ' + e; }
}

/* ══════════════ PERFORMANCE / HEATMAP ══════════════ */'''
assert s.count(old_js) == 1, 'js anchor missing'
s = s.replace(old_js, new_js, 1)

open(PATH, 'w', encoding='utf-8').write(s)
print('setup tab patched: 6 insertions')

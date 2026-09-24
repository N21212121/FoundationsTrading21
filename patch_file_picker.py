"""Replace the typed-path import box with a real file picker.

Run after the earlier frontend patches. Idempotent.
"""
import sys

PATH = 'static/index.html'
MARKER = 'id="jrnFileInput"'

s = open(PATH, encoding='utf-8').read()
if MARKER in s:
    print('already patched; nothing to do')
    sys.exit(0)
if 'id="srcBoxFile"' not in s:
    print('run the earlier patches first'); sys.exit(1)

# ── 1. file picker replaces the path box (path kept as a fallback) ────────────
old = '''      <div id="srcBoxFile" style="display:none;">
        <div style="font-size:12px;color:var(--text-dim);margin-bottom:5px;">
          Path to an exported sheet on this machine.</div>
        <input type="text" id="jrnImportPath" placeholder="C:\\path\\to\\export.csv"
               style="width:100%; margin-bottom:6px;">
        <div style="display:flex; gap:6px;">
          <button onclick="jrnImport(true)"  style="flex:1;">Preview</button>
          <button onclick="jrnImport(false)" style="flex:1;">Import</button>
        </div>
      </div>'''
new = '''      <div id="srcBoxFile" style="display:none;">
        <div style="font-size:12px;color:var(--text-dim);margin-bottom:5px;">
          Your Trade Log sheet, exported as CSV. Grades and conditions come
          across with it.</div>
        <input type="file" id="jrnFileInput" accept=".csv,text/csv"
               onchange="jrnFilePicked()" style="width:100%;margin-bottom:6px;">
        <div id="jrnFileName" style="font-size:11.5px;color:var(--text-dim);
             margin-bottom:6px;"></div>
        <div style="display:flex; gap:6px;">
          <button onclick="jrnImport(true)"  style="flex:1;">Preview</button>
          <button onclick="jrnImport(false)" style="flex:1;">Import</button>
        </div>
        <details style="margin-top:7px;">
          <summary style="font-size:11.5px;color:var(--text-dim);cursor:pointer;">
            Type a path instead</summary>
          <input type="text" id="jrnImportPath" placeholder="C:\\path\\to\\export.csv"
                 style="width:100%;margin-top:5px;">
        </details>
      </div>'''
assert s.count(old) == 1, 'file panel anchor missing'
s = s.replace(old, new)

# ── 2. JS: read the picked file in the browser, post its text ────────────────
old_js = 'async function jrnImport(dry) {\n  const path = $(\'jrnImportPath\').value.trim();\n  if (!path) return;\n  $(\'jrnImportOut\').textContent = dry ? \'Parsing…\' : \'Importing…\';\n  try {\n    const res = await fetch(\'/api/journal/import\', {\n      method: \'POST\', headers: {\'Content-Type\': \'application/json\'},\n      body: JSON.stringify({path: path, dry_run: dry})}).then(r => r.json());\n    if (!res.ok) { $(\'jrnImportOut\').textContent = \'Error: \' + res.error; return; }\n    const probs = (res.problems || []).slice(0, 25);\n    $(\'jrnImportOut\').innerHTML =\n      `<b>${dry ? \'Preview\' : \'Imported\'}</b>: parsed ${res.parsed},\n       ${dry ? \'would add\' : \'added\'} ${res.imported},\n       skipped ${res.skipped_duplicates} duplicate(s).` +\n      (probs.length ? `<div style="margin-top:6px;">${res.problems.length} row(s) needed\n         inference:</div><ul style="margin-left:15px;">` +\n         probs.map(p => `<li>row ${p.row} ${p.asset}: ${p.issue}</li>`).join(\'\') + \'</ul>\'\n       : \'\');\n    if (!dry) jrnLoad();\n  } catch (e) { $(\'jrnImportOut\').textContent = \'Failed: \' + e; }\n}\n'
new_js = r'''let JRN_FILE_TEXT = null;

function jrnFilePicked() {
  const f = $('jrnFileInput').files[0];
  JRN_FILE_TEXT = null;
  if (!f) { $('jrnFileName').textContent = ''; return; }
  $('jrnFileName').textContent = `${f.name} (${Math.round(f.size / 1024)} KB) — reading…`;
  const reader = new FileReader();
  reader.onload = () => {
    JRN_FILE_TEXT = reader.result;
    $('jrnFileName').textContent = `${f.name} (${Math.round(f.size / 1024)} KB) — ready`;
  };
  reader.onerror = () => { $('jrnFileName').textContent = 'Could not read that file.'; };
  // The browser decodes it, so drive letters, quoting and code pages never
  // reach the server. The typed-path box remains for anything it can't see.
  reader.readAsText(f);
}

async function jrnImport(dry) {
  const pathEl = $('jrnImportPath');
  const path = pathEl ? pathEl.value.trim() : '';
  const body = {dry_run: dry};
  if (JRN_FILE_TEXT) body.text = JRN_FILE_TEXT;
  else if (path) body.path = path;
  else { jrnSrcOut('Choose a CSV first, or type a path.'); return; }

  jrnSrcOut(dry ? 'Parsing…' : 'Importing…');
  try {
    const res = await fetch('/api/journal/import', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)}).then(r => r.json());
    if (!res.ok) {
      jrnSrcOut('<span style="color:var(--red)">' + res.error + '</span>');
      return;
    }
    const probs = (res.problems || []).slice(0, 25);
    jrnSrcOut(
      `<b>${dry ? 'Preview' : 'Imported'}</b>: parsed ${res.parsed},
       ${dry ? 'would add' : 'added'} ${res.imported},
       skipped ${res.skipped_duplicates} duplicate(s).` +
      (probs.length ? `<div style="margin-top:6px;">${res.problems.length} note(s):</div>
         <ul style="margin-left:15px;">` +
         probs.map(p => `<li>${p.row ? 'row ' + p.row + ' ' : ''}${p.asset || ''}: ${p.issue}</li>`).join('') +
         '</ul>' : ''));
    if (!dry) jrnLoad();
  } catch (e) { jrnSrcOut('Failed: ' + e); }
}'''
assert s.count(old_js) == 1, 'jrnImport anchor missing'
s = s.replace(old_js, new_js, 1)

open(PATH, 'w', encoding='utf-8').write(s)
print('file picker patched: 2 insertions')

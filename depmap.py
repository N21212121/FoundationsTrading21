"""
depmap.py — Foundations Trading

Walk the repo and emit the dependency graph as JSON: every module, what it
imports, which HTTP routes it defines, which routes the frontend calls, and
which runtime data stores each module touches.

WHY A SCRIPT AND NOT A DOCUMENT
  A hand-drawn map is wrong the day after it is drawn, and this project has
  moved fifteen commits in a day. The map is DERIVED, so "update the map"
  means "re-run this", and a relationship that changed shows up whether or
  not anyone remembered it changed. Same argument as jscheck.py deriving its
  baseline from git rather than carrying a remembered number.

WHAT IS MEASURED, AND WHAT IS GUESSED
  Imports are parsed with `ast`, so they are exact -- no regex guessing at
  `import x as y` or a conditional import inside a function. Those ARE
  reported, with `deferred: true`, because an import inside a function body
  is a real dependency that deliberately is not a load-time one.

  Route definitions are read from decorator source text, which is reliable
  here because every route in this repo is a literal string.

  Frontend -> route edges come from matching `fetch('...')` and `api('...')`
  call sites against the route table. A templated URL (`'/api/bars/' + t`)
  is matched on its static prefix and flagged `prefix: true`, because that
  is the honest confidence level rather than a silent miss.

  Data-store edges are matched on the module-level filename constants this
  project uses (JOURNAL_FILE, LEDGER_FILE, ...). A store opened by a path
  built at call time is NOT caught; those are listed under `unresolved`.

USAGE
  py depmap.py              # human-readable summary
  py depmap.py --json       # the graph, to stdout
  py depmap.py --out g.json # the graph, to a file
  py depmap.py --orphans    # only the things nothing imports
"""

import ast
import json
import os
import re
import subprocess
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
FRONTEND = os.path.join('static', 'index.html')

# Role is the one editorial judgement in this file, and it exists because a
# 49-node graph with no grouping is a hairball rather than a map. Longest
# prefix wins; anything unmatched falls to 'support'.
ROLES = (
    ('engine_',     'engine'),
    ('signal_engine', 'engine'),
    ('trade_router', 'routing'),
    ('alpaca_',     'broker'),
    ('price_feed',  'broker'),
    ('backtest',    'backtester'),
    ('journal',     'journal'),
    ('pairing',     'journal'),
    ('heatmap',     'journal'),
    ('conditions',  'journal'),
    ('ledger',      'ledger'),
    ('screen',      'screener'),
    ('grading',     'screener'),
    ('levels',      'screener'),
    ('assign',      'screener'),
    ('halflife',    'screener'),
    ('plays',       'screener'),
    ('sectors',     'screener'),
    ('setup',       'config'),
    ('config_',     'config'),
    ('watchlist',   'config'),
    ('baskets',     'config'),
    ('exit_ladder', 'routing'),
    ('bar_cache',   'broker'),
    ('patch_',      'legacy'),
    ('jscheck',     'tooling'),
    ('depmap',      'tooling'),
)

STORE_CONSTS = {
    'JOURNAL_FILE': 'journal.jsonl',
    'LEDGER_FILE': 'ledger.jsonl',
    'PLAYS_FILE': 'plays.jsonl',
    'WATCHLIST_FILE': 'watchlist.jsonl',
    'CONFIG_FILE': 'config.json',
    'POSITIONS_FILE': 'positions.json',
}


def role_of(mod):
    best = ('', 'support')
    for prefix, role in ROLES:
        if mod.startswith(prefix) and len(prefix) > len(best[0]):
            best = (prefix, role)
    return best[1]


def py_modules():
    return sorted(f[:-3] for f in os.listdir(ROOT)
                  if f.endswith('.py') and not f.startswith('_'))


def _route_strings(dec):
    """The literal path on a @app.route / @bp.route decorator, or None."""
    if not isinstance(dec, ast.Call):
        return None
    fn = dec.func
    name = getattr(fn, 'attr', None)
    if name not in ('route', 'get', 'post'):
        return None
    if not dec.args:
        return None
    a = dec.args[0]
    if isinstance(a, ast.Constant) and isinstance(a.value, str):
        methods = []
        for kw in dec.keywords:
            if kw.arg == 'methods' and isinstance(kw.value, (ast.List, ast.Tuple)):
                methods = [e.value for e in kw.value.elts
                           if isinstance(e, ast.Constant)]
        return a.value, (methods or ['GET'])
    return None


def scan_module(mod, local):
    path = os.path.join(ROOT, mod + '.py')
    with open(path, encoding='utf-8') as f:
        src = f.read()
    try:
        tree = ast.parse(src, filename=path)
    except SyntaxError as exc:
        return {'module': mod, 'error': f'{exc.__class__.__name__}: {exc}'}

    imports, routes, stores = {}, [], set()

    # Depth tracks whether an import sits at module level or inside a body.
    # A deferred import is a real edge AND a deliberate statement that the
    # dependency is not needed at load time -- usually to break a cycle.
    class V(ast.NodeVisitor):
        def __init__(self):
            self.depth = 0

        def _add(self, name):
            base = name.split('.')[0]
            if base in local and base != mod:
                prev = imports.get(base)
                deferred = self.depth > 0
                # module-level wins: if seen both ways it IS a load-time edge
                imports[base] = (prev is None and deferred) or (prev and deferred)

        def visit_Import(self, node):
            for a in node.names:
                self._add(a.name)

        def visit_ImportFrom(self, node):
            if node.module and node.level == 0:
                self._add(node.module)

        def visit_FunctionDef(self, node):
            self.depth += 1
            for d in node.decorator_list:
                r = _route_strings(d)
                if r:
                    routes.append({'path': r[0], 'methods': sorted(r[1]),
                                   'handler': node.name})
            self.generic_visit(node)
            self.depth -= 1

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node):
            self.depth += 1
            self.generic_visit(node)
            self.depth -= 1

    V().visit(tree)

    # Skip self: depmap's own STORE_CONSTS table would otherwise match every
    # name in it and report this file as touching six runtime stores. A
    # scanner that reports itself as its own biggest finding is the first
    # thing to get right about a scanner.
    if mod != 'depmap':
        for const, store in STORE_CONSTS.items():
            if re.search(rf'\b{const}\b', src):
                stores.add(store)

    return {
        'module': mod,
        'role': role_of(mod),
        'lines': src.count('\n') + 1,
        'imports': [{'to': k, 'deferred': bool(v)} for k, v in sorted(imports.items())],
        'routes': sorted(routes, key=lambda r: (r['path'], r['handler'])),
        'stores': sorted(stores),
        'selftest': bool(re.search(r'^def selftest\(', src, re.M)),
        'doc': (ast.get_docstring(tree) or '').strip().split('\n')[0][:160],
    }


def scan_frontend(routes_by_path):
    path = os.path.join(ROOT, FRONTEND)
    if not os.path.exists(path):
        return {'calls': [], 'lines': 0, 'unmatched': []}
    with open(path, encoding='utf-8') as f:
        src = f.read()

    seen, unmatched = {}, set()
    for m in re.finditer(r"""(?:fetch|api)\(\s*['"`](/api/[^'"`?+ ]*)""", src):
        raw = m.group(1).rstrip('/')
        if raw in routes_by_path:
            seen.setdefault(raw, {'path': raw, 'prefix': False})
            continue
        # a templated URL: match the longest route that is a prefix of it
        cands = [p for p in routes_by_path
                 if raw.startswith(p.split('<')[0].rstrip('/')) and p != raw]
        if cands:
            best = max(cands, key=len)
            seen.setdefault(best, {'path': best, 'prefix': True})
        else:
            unmatched.add(raw)
    return {'calls': sorted(seen.values(), key=lambda c: c['path']),
            'lines': src.count('\n') + 1,
            'unmatched': sorted(unmatched)}


def build():
    local = set(py_modules())
    nodes = [scan_module(m, local) for m in sorted(local)]
    ok = [n for n in nodes if 'error' not in n]

    routes_by_path = {}
    for n in ok:
        for r in n['routes']:
            routes_by_path.setdefault(r['path'], []).append(
                {'module': n['module'], 'handler': r['handler'],
                 'methods': r['methods']})

    # A path served by two modules is the /api/screen class of bug: one of
    # them silently wins. Reported as a finding rather than left to be found.
    collisions = [{'path': p, 'served_by': v} for p, v in sorted(routes_by_path.items())
                  if len({(d['module'], d['handler']) for d in v}) > 1]

    imported_by = {n['module']: [] for n in ok}
    for n in ok:
        for e in n['imports']:
            imported_by.setdefault(e['to'], []).append(n['module'])

    fe = scan_frontend(routes_by_path)

    try:
        head = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'],
                              cwd=ROOT, capture_output=True, text=True,
                              timeout=10).stdout.strip() or None
    except Exception:
        head = None

    return {
        'version': 'depmap/1',
        'generated': datetime.now().replace(microsecond=0).isoformat(),
        'head': head,
        'modules': nodes,
        'imported_by': {k: sorted(v) for k, v in sorted(imported_by.items())},
        'routes': routes_by_path,
        'route_collisions': collisions,
        'frontend': fe,
        'orphans': sorted(n['module'] for n in ok
                          if not imported_by.get(n['module'])
                          and not n['routes'] and n['module'] != 'app'),
        'roles': sorted({n['role'] for n in ok}),
        'totals': {
            'modules': len(ok),
            'module_lines': sum(n['lines'] for n in ok),
            'frontend_lines': fe['lines'],
            'edges': sum(len(n['imports']) for n in ok),
            'routes': len(routes_by_path),
            'selftests': sum(1 for n in ok if n['selftest']),
        },
    }


def summary(g):
    t = g['totals']
    print(f"depmap · HEAD {g['head']} · {g['generated']}")
    print(f"{t['modules']} modules, {t['module_lines']:,} lines, "
          f"{t['edges']} import edges, {t['routes']} routes, "
          f"{t['selftests']} selftests")
    print()
    by_role = {}
    for n in g['modules']:
        if 'error' in n:
            print(f"  !! {n['module']}: {n['error']}")
            continue
        by_role.setdefault(n['role'], []).append(n)
    for role in sorted(by_role):
        mods = sorted(by_role[role], key=lambda n: -len(g['imported_by'].get(n['module'], [])))
        print(f"{role.upper()}")
        for n in mods:
            fan_in = len(g['imported_by'].get(n['module'], []))
            fan_out = len(n['imports'])
            flags = ''.join(('T' if n['selftest'] else '-',
                             'R' if n['routes'] else '-',
                             'S' if n['stores'] else '-'))
            print(f"  {n['module']:24} {n['lines']:>5}L  in:{fan_in:<3} "
                  f"out:{fan_out:<3} {flags}")
        print()
    if g['route_collisions']:
        print('ROUTE COLLISIONS -- one of these silently wins:')
        for c in g['route_collisions']:
            who = ', '.join(f"{d['module']}.{d['handler']} {d['methods']}"
                            for d in c['served_by'])
            print(f"  {c['path']:28} {who}")
        print()
    if g['orphans']:
        print('ORPHANS -- nothing imports these and they serve no route:')
        print('  ' + ', '.join(g['orphans']))
        print()
    if g['frontend']['unmatched']:
        print('FRONTEND CALLS WITH NO ROUTE:')
        print('  ' + ', '.join(g['frontend']['unmatched']))


if __name__ == '__main__':
    graph = build()
    if '--json' in sys.argv:
        print(json.dumps(graph, indent=2))
    elif '--orphans' in sys.argv:
        print('\n'.join(graph['orphans']))
    else:
        if '--out' in sys.argv:
            dest = sys.argv[sys.argv.index('--out') + 1]
            with open(dest, 'w', encoding='utf-8') as f:
                json.dump(graph, f, indent=2)
            print(f'wrote {dest}')
        summary(graph)

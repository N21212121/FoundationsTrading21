"""
depmap_page.py — Foundations Trading

Build the publishable wiring map: run depmap.build(), inject the graph into
depmap_template.html, write the result.

WHY A GENERATOR AND NOT A HAND-EDITED PAGE
  Nate asked for a map that "with each new update... would then be updated to
  show how relations between the different program files change." A map that
  has to be redrawn by hand is wrong by the next commit. So the page is
  derived: the template holds the drawing and the data block is replaced from
  a fresh scan, which makes "update the map" one command.

  The graph is INLINED rather than fetched. Two reasons: the page's first
  frame is then complete with no loading state (which is what a thumbnail and
  a cold open show), and there is one file to publish instead of two that can
  drift apart.

USAGE
  py depmap_page.py                     # -> depmap_built.html
  py depmap_page.py --out some/path.html
"""

import io
import json
import os
import sys

import depmap

ROOT = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(ROOT, 'depmap_template.html')
MARKER = '/*__GRAPH__*/'
DEFAULT_OUT = os.path.join(ROOT, 'depmap_built.html')


def build(out_path=DEFAULT_OUT):
    with io.open(TEMPLATE, encoding='utf-8') as f:
        tmpl = f.read()
    if tmpl.count(MARKER) != 1:
        raise SystemExit(f'template must contain {MARKER} exactly once')

    graph = depmap.build()
    # separators= keeps the payload tight; the page parses it, nobody reads it.
    blob = json.dumps(graph, separators=(',', ':'))
    # The blob sits inside <script type="application/json">, so the only thing
    # that can end the block early is a literal </script. Nothing in this data
    # should contain one -- a docstring could -- so it is escaped rather than
    # trusted.
    blob = blob.replace('</', '<\\/')

    page = tmpl.replace(MARKER, blob)
    with io.open(out_path, 'w', encoding='utf-8', newline='') as f:
        f.write(page)

    t = graph['totals']
    print(f"wrote {out_path}")
    print(f"  {len(page):,} bytes total, {len(blob):,} of it graph")
    print(f"  {t['modules']} modules · {t['edges']} imports · "
          f"{t['routes']} routes · {t['selftests']} selftests")
    if graph['route_collisions']:
        print(f"  {len(graph['route_collisions'])} route collision(s)")
    if graph['orphans']:
        print(f"  {len(graph['orphans'])} unimported module(s)")
    return out_path


if __name__ == '__main__':
    dest = DEFAULT_OUT
    if '--out' in sys.argv:
        dest = sys.argv[sys.argv.index('--out') + 1]
    build(dest)

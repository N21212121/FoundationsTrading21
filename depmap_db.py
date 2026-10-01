"""
depmap_db.py — Foundations Trading

Emit the wiring graph as the document the build board expects at
`meta/depgraph`, so the map on the board can be refreshed WITHOUT
republishing the page.

WHY THIS EXISTS SEPARATELY FROM depmap_page.py
  Two consumers, two shapes. The standalone map page inlines the graph at
  build time (one file, complete first frame). The board reads it from the
  artifact database and subscribes, so every open page redraws when the row
  changes. Nate asked for the map to "auto-update as changes are made", and
  the database is the half of that which needs no republish.

  Both read the same depmap.build(), so the two can never disagree about the
  repo — only about how fresh their copy is.

USAGE
  py depmap_db.py            # writes depgraph_doc.json next to the repo
  py depmap_db.py --out X    # somewhere else

  Then, from a session:
    ArtifactData set, collection "meta", doc_id "depgraph",
    file_path <the file this wrote>, url <the board>
  passing the `if_version` the last read reported.
"""

import io
import json
import os
import sys

import depmap

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.join(ROOT, 'depgraph_doc.json')


def build(out_path=DEFAULT_OUT):
    g = depmap.build()
    # The whole graph goes in one string field. A nested structure would be
    # prettier in the database and worse everywhere else: the page wants to
    # JSON.parse one value, and a string is the one shape no schema can
    # disagree about.
    blob = json.dumps(g, separators=(',', ':'))
    doc = {
        'json': blob,
        'generated': g['generated'],
        'head': g['head'],
        'modules': g['totals']['modules'],
        'edges': g['totals']['edges'],
        'routes': g['totals']['routes'],
        'bytes': len(blob),
        'rebuild': 'py depmap_db.py, then ArtifactData set meta/depgraph',
    }
    with io.open(out_path, 'w', encoding='utf-8') as f:
        json.dump(doc, f)
    print(f'wrote {out_path}')
    print(f"  {doc['modules']} modules · {doc['edges']} imports · "
          f"{doc['routes']} routes · {len(blob):,} bytes of graph")
    if g['route_collisions']:
        print(f"  {len(g['route_collisions'])} route collision(s)")
    if g['orphans']:
        print(f"  {len(g['orphans'])} unimported: {', '.join(g['orphans'])}")
    return out_path


if __name__ == '__main__':
    dest = DEFAULT_OUT
    if '--out' in sys.argv:
        dest = sys.argv[sys.argv.index('--out') + 1]
    build(dest)

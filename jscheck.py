"""Delimiter-balance check on static/index.html's <script> block.

    py -3 jscheck.py                     # checks static/index.html
    py -3 jscheck.py <path> [<baseline>]  # explicit paths

WHAT THIS IS, AND WHAT IT IS NOT. There is no Node, Deno or Bun on this
machine, so the frontend JavaScript cannot be executed or linted. This is the
substitute, and it is a crude one: it strips strings, template literals and
comments, then counts braces, parens and brackets. It catches one class of
error -- a scripted edit that left a block unclosed -- and nothing subtler.
A syntactically valid file that does the wrong thing passes here.

READ THE OUTPUT CORRECTLY. The checker cannot parse regex literals, so
static/index.html has a PRE-EXISTING imbalance: three braces and two parens.
"balanced" is therefore NOT the pass condition. The pass condition is:

    the open and close DELTAS against the baseline match exactly

which is what --against computes for you. A commit that adds 46 opening and
46 closing braces is internally balanced even though the absolute counts stay
wrong. That is the only honest reading of this tool.

See design/00-project-board.md section 1 for where this sits among the
verification that actually exists.
"""

import io
import re
import subprocess
import sys

BS = chr(92)          # backslash, spelled so no shell heredoc can eat it
QUOTES = '"' + chr(39) + '`'
NL = chr(10)
PAIRS = ('{}', '()', '[]')


def script_of(html):
    """The contents of the last <script> block, or None."""
    blocks = re.findall(r'<script>(.*?)</script>', html, re.S)
    return blocks[-1] if blocks else None


def strip(js):
    """Remove strings, template literals and comments. Not a parser."""
    out, i, n = [], 0, len(js)
    while i < n:
        c = js[i]
        if c in QUOTES:
            q = c
            i += 1
            while i < n and js[i] != q:
                if js[i] == BS:
                    i += 1
                i += 1
            i += 1
            out.append('S')
            continue
        if c == '/' and i + 1 < n and js[i + 1] == '/':
            while i < n and js[i] != NL:
                i += 1
            continue
        if c == '/' and i + 1 < n and js[i + 1] == '*':
            j = js.find('*/', i)
            i = (j + 2) if j >= 0 else n
            continue
        out.append(c)
        i += 1
    return ''.join(out)


def counts(js):
    code = strip(js)
    out = {}
    for pair in PAIRS:
        out[pair] = (code.count(pair[0]), code.count(pair[1]))
    depth, line, first_bad = 0, 1, None
    for ch in code:
        if ch == NL:
            line += 1
        elif ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth < 0 and first_bad is None:
                first_bad = line
    out['depth'] = depth
    out['first_bad'] = first_bad
    out['lines'] = js.count(NL) + 1
    out['chars'] = len(js)
    return out


def read(path):
    js = script_of(io.open(path, encoding='utf-8').read())
    if js is None:
        print('no <script> block in ' + path)
        sys.exit(2)
    return counts(js)


def baseline_from_git(path, rev):
    """The same counts for `path` at a git revision, or None if unavailable."""
    try:
        blob = subprocess.check_output(
            ['git', 'show', rev + ':' + path.replace('\\', '/')],
            stderr=subprocess.DEVNULL)
    except Exception:
        return None
    js = script_of(blob.decode('utf-8', 'replace'))
    return counts(js) if js is not None else None


def main(argv):
    path = argv[1] if len(argv) > 1 else 'static/index.html'
    rev = argv[2] if len(argv) > 2 else 'HEAD'

    cur = read(path)
    print('%s  %d chars, %d lines' % (path, cur['chars'], cur['lines']))
    for pair in PAIRS:
        a, b = cur[pair]
        print('  %s  %5d open / %5d close   %s'
              % (pair, a, b, 'balanced' if a == b else 'IMBALANCED by %+d' % (a - b)))
    print('  final brace depth: %d' % cur['depth'])
    if cur['first_bad']:
        print('  first unbalanced close near script line %d' % cur['first_bad'])

    base = baseline_from_git(path, rev)
    if base is None:
        print(NL + 'No baseline from git %s, so only absolute counts are shown.' % rev)
        print('Absolute balance is NOT the pass condition -- see this file' + "'" + 's docstring.')
        return 0

    print(NL + 'Against %s -- this is the reading that matters:' % rev)
    ok = True
    for pair in PAIRS:
        da = cur[pair][0] - base[pair][0]
        db = cur[pair][1] - base[pair][1]
        good = (da == db)
        ok = ok and good
        print('  %s  %+d open / %+d close   %s'
              % (pair, da, db, 'matched' if good else 'MISMATCH -- the edit is not internally balanced'))
    dd = cur['depth'] - base['depth']
    if dd != 0:
        ok = False
        print('  brace depth moved by %+d against the baseline' % dd)
    else:
        print('  brace depth unchanged against the baseline')

    print(NL + ('PASS -- deltas balance. This says nothing about behaviour.'
                if ok else 'FAIL -- the edit is not internally balanced.'))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))

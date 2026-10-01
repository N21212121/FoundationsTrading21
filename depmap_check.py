"""Delimiter + tag balance on the built map page, in the spirit of jscheck.py.

Not a parser, and it says nothing about behaviour. It catches the one class of
error that is easy to make by hand and fatal: an unclosed brace, paren,
bracket or element.

The first version of this script reported a mismatch that was its OWN fault:
it stripped strings but not REGEX LITERALS, so the `"` inside `/[&<>"']/g`
opened a phantom string and swallowed real structure. A checker that cries
wolf is worse than no checker, so regex literals are handled here, with the
standard heuristic -- a `/` in a position where a value is expected starts a
regex, a `/` after a value is division.

WHAT IT STILL CANNOT DO, found 2026-10-01 while adding the Trade Desk:
NESTED TEMPLATE LITERALS. A quote scanner that hits the outer backtick of

    `<a>${list.map(x => `<b>${x}</b>`).join('')}</a>`

runs forward to the NEXT backtick -- the inner one -- and everything after
it is misaligned, so code reads as string and string reads as code.
jscheck.py has the same flaw. Neither tool can validate a file that uses
them, and static/index.html now does, heavily.

The substitute for that case is a CONTEXT-FREE check on the DIFF rather
than on the file -- hunkbal.py: count braces and parens in each hunk's
removed and added lines and require the net change to be zero. That cannot
prove a file is balanced, but it does prove an edit closes everything it
opens, which is the error this whole family of tools exists to catch.
"""
import io
import re
import sys

path = sys.argv[1] if len(sys.argv) > 1 else 'depmap_built.html'
s = io.open(path, encoding='utf-8').read()

i = s.rindex('<script>')
j = s.rindex('</script>')
js = s[i + 8:j]

BS = chr(92)
PREFIX = set('(,=:[!&|?{};+-*%~^<>') | set(' \t\n\r')

out = []
k = 0
while k < len(js):
    c = js[k]
    if c in ('"', "'", '`'):
        q = c
        k += 1
        while k < len(js) and js[k] != q:
            k += 2 if js[k] == BS else 1
        k += 1
        continue
    if js.startswith('//', k):
        nl = js.find('\n', k)
        k = len(js) if nl < 0 else nl
        continue
    if js.startswith('/*', k):
        e = js.find('*/', k)
        k = len(js) if e < 0 else e + 2
        continue
    if c == '/':
        # regex literal only where a value could start
        prev = ''
        for p in range(len(out) - 1, -1, -1):
            if out[p] not in ' \t\n\r':
                prev = out[p]
                break
        if prev == '' or prev in PREFIX:
            k += 1
            in_class = False
            while k < len(js):
                ch = js[k]
                if ch == BS:
                    k += 2
                    continue
                if ch == '[':
                    in_class = True
                elif ch == ']':
                    in_class = False
                elif ch == '/' and not in_class:
                    break
                elif ch == '\n':
                    break          # not a regex after all; bail out safely
                k += 1
            k += 1
            while k < len(js) and js[k] in 'gimsuy':
                k += 1
            continue
    out.append(c)
    k += 1
clean = ''.join(out)

bad = 0
print(f'script block: {len(js):,} chars ({len(clean):,} after stripping '
      f'strings, comments and regex)')
for o, c, name in (('{', '}', 'braces'), ('(', ')', 'parens'),
                   ('[', ']', 'brackets')):
    a, b = clean.count(o), clean.count(c)
    ok = a == b
    bad += 0 if ok else 1
    print(f'  {name:9} {a:4} / {b:4}  ' + ('balanced' if ok else '*** MISMATCH ***'))

# Count tags OUTSIDE the json data block: a docstring in the scanned repo can
# legitimately contain the text "<script>" (jscheck.py's does), and that is
# data, not markup.
if 'id="graph">' in s:
    dstart = s.index('id="graph">')
    dend = s.index('</script>', dstart)
    markup = s[:dstart] + s[dend:]
else:
    markup = s          # no inlined data block (the board reads its graph
                        # from the artifact database instead)

VOID = ('input', 'img', 'br', 'link', 'meta')
print('elements (json data block excluded):')
for t in ('div', 'script', 'style', 'span', 'canvas', 'input', 'button',
          'h1', 'h2', 'h3', 'p', 'title'):
    o = len(re.findall(r'<' + t + r'[\s>]', markup))
    c = markup.count('</' + t + '>')
    if t in VOID:
        print(f'  <{t}> {o} (void)')
        continue
    ok = o == c
    bad += 0 if ok else 1
    print(f'  <{t}> {o} / </{t}> {c}  ' + ('ok' if ok else '*** MISMATCH ***'))

print()
print('FAIL' if bad else 'PASS -- structure balances. Says nothing about behaviour.')
sys.exit(1 if bad else 0)

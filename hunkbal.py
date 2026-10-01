"""Which hunk of a diff changes the brace/paren balance?

jscheck says the whole edit is off by one; this says WHERE, which is the
difference between a fix and a guess.
"""
import io
import sys

path = sys.argv[1]
cur = None
stats = {}
order = []
for ln in io.open(path, encoding='utf-8', errors='replace'):
    if ln.startswith('@@'):
        cur = ln.strip()
        stats[cur] = {'ro': 0, 'rc': 0, 'ao': 0, 'ac': 0,
                      'rp': 0, 'rq': 0, 'ap': 0, 'aq': 0}
        order.append(cur)
    elif cur and ln.startswith('-') and not ln.startswith('---'):
        stats[cur]['ro'] += ln.count('{'); stats[cur]['rc'] += ln.count('}')
        stats[cur]['rp'] += ln.count('('); stats[cur]['rq'] += ln.count(')')
    elif cur and ln.startswith('+') and not ln.startswith('+++'):
        stats[cur]['ao'] += ln.count('{'); stats[cur]['ac'] += ln.count('}')
        stats[cur]['ap'] += ln.count('('); stats[cur]['aq'] += ln.count(')')

bad = 0
for h in order:
    d = stats[h]
    db = (d['ao'] - d['ro']) - (d['ac'] - d['rc'])
    dp = (d['ap'] - d['rp']) - (d['aq'] - d['rq'])
    flag = '' if (db == 0 and dp == 0) else '   <== IMBALANCED'
    if flag:
        bad += 1
    print(f"{h[:60]:62} braces {db:+d}  parens {dp:+d}{flag}")
print()
print(f'{bad} imbalanced hunk(s) of {len(order)}')

import json, re, sys
from collections import Counter, defaultdict

src, out = sys.argv[1], sys.argv[2]
text = open(src).read()

ID = r'("[^"]+"|\w+)'


def unq(s):
    return s.strip('"') if s else s


tables = {}
for m in re.finditer(r'^CREATE TABLE (\w+)\.' + ID + r' \((.*?)^\);', text, re.M | re.S):
    schema, name, body = m.groups()
    name = unq(name)
    cols = []
    for line in body.strip().splitlines():
        line = line.strip().rstrip(',')
        if not line or line.startswith('CONSTRAINT'):
            continue
        cm = re.match(r'("[^"]+"|\S+)\s*(.*)', line)
        col, rest = unq(cm.group(1)), cm.group(2)
        typ = re.split(r' (NOT NULL|DEFAULT|GENERATED|COLLATE)', rest)[0].strip()
        cols.append({'n': col, 't': typ, 'nn': 'NOT NULL' in rest})
    tables[(schema, name)] = {'schema': schema, 'name': name, 'cols': cols, 'pk': [], 'fks': []}

alter = (r'^ALTER TABLE (?:ONLY )?(\w+)\.' + ID + r'\s+ADD CONSTRAINT \S+ (PRIMARY KEY|FOREIGN KEY) \(([^)]*)\)'
         r'(?: REFERENCES (\w+)\.' + ID + r'\(([^)]*)\))?')
for m in re.finditer(alter, text, re.M):
    s, t, kind, cols, rs, rt, rc = m.groups()
    tab = tables.get((s, unq(t)))
    if not tab:
        continue
    cols = [unq(c.strip()) for c in cols.split(',')]
    if kind == 'PRIMARY KEY':
        tab['pk'] = cols
    else:
        tab['fks'].append({'cols': cols, 'ref': f'{rs}.{unq(rt)}', 'rcols': [unq(c.strip()) for c in rc.split(',')]})

views = Counter(m.group(1) for m in re.finditer(r'^CREATE (?:MATERIALIZED )?VIEW (\w+)\.', text, re.M))
odmx_views = [unq(m.group(1)) for m in re.finditer(r'^CREATE (?:MATERIALIZED )?VIEW odmx\.' + ID, text, re.M)]
seqs = Counter(m.group(1) for m in re.finditer(r'^CREATE SEQUENCE (\w+)\.', text, re.M))


def sig(tab):
    return ' | '.join(f"{c['n']}:{c['t']}" for c in tab['cols'])


result = {'schemas': {}, 'detailed': [], 'groups': {}}
by_schema = defaultdict(list)
for tab in tables.values():
    by_schema[tab['schema']].append(tab)

for s in ['odmx', 'knowledge', 'datastreams', 'feeder', 'odmx_materialized_views']:
    tabs = by_schema.get(s, [])
    result['schemas'][s] = {'tables': len(tabs), 'views': views.get(s, 0), 'sequences': seqs.get(s, 0),
                            'fks': sum(len(t['fks']) for t in tabs)}

for s in ['odmx', 'knowledge']:
    for t in sorted(by_schema[s], key=lambda t: t['name']):
        result['detailed'].append(t)
result['odmx_views'] = odmx_views

for s in ['datastreams', 'feeder']:
    groups = defaultdict(list)
    for t in by_schema[s]:
        groups[sig(t)].append(t['name'])
    gl = []
    for k, names in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        ex = tables[(s, names[0])]
        gl.append({'count': len(names), 'cols': ex['cols'], 'pk': ex['pk'], 'examples': sorted(names)[:6],
                   'ncols': len(ex['cols'])})
    result['groups'][s] = gl
    if s == 'datastreams':
        result['ds_suffix'] = Counter(t['name'].rsplit('_', 1)[-1] for t in by_schema[s]).most_common(10)

json.dump(result, open(out, 'w'))
print(json.dumps(result['schemas'], indent=1))
for s in ['datastreams', 'feeder']:
    g = result['groups'][s]
    print(s, 'distinct layouts:', len(g), 'top counts:', [x['count'] for x in g[:8]], 'ncols:', [x['ncols'] for x in g[:8]])
    print('  top layout cols:', [c['n'] + ':' + c['t'] for c in g[0]['cols']])
print(result.get('ds_suffix'))
fk_targets = Counter(f['ref'] for t in result['detailed'] for f in t['fks'])
print('most referenced:', fk_targets.most_common(12))
print('odmx prefixes:', Counter(t['name'].split('_')[0] for t in result['detailed'] if t['schema'] == 'odmx').most_common(25))

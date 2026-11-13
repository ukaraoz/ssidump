#!/usr/bin/env python3
"""Write per-schema table inventories (TSV) from a schema-only SQL file.

    python3 schema_map/table_inventory.py schema_only.sql exports/full_2026-10-26/ table_inventory/

Needs no server. Uses:
  - schema_only.sql          tables, columns, keys, views and COMMENT ON descriptions
  - exports/full_<date>/     a table export made by ssiexportall.py:
        odmx/*.csv(.gz)      the tables that describe each datastream (site, variable,
                             units, depth, dates, count)
        <schema>/_tables.tsv row counts; tables that were not exported (e.g. Sentinel,
                             or a partial download) get an empty count

Writes, for each schema S in the dump:
  S_tables.tsv    one row per table (and view) with what it holds
  S_columns.tsv   one row per column (odmx and knowledge only; feeder and
                  datastreams columns are listed inline in S_tables.tsv)
"""
import ast
import csv
import gzip
import json
import os
import re
import sys
from collections import defaultdict

csv.field_size_limit(sys.maxsize)
here = os.path.dirname(os.path.abspath(__file__))
sql_path, export_dir, out_dir = sys.argv[1:4]
os.makedirs(out_dir, exist_ok=True)
text = open(sql_path).read()

ID = r'("[^"]+"|\w+)'


def unq(s):
    return s.strip('"') if s else s


def assigned_literal(path, name):
    """Read a literal module-level assignment (e.g. MODULES) without running the file."""
    for node in ast.parse(open(path).read()).body:
        if isinstance(node, ast.Assign) and any(getattr(t, 'id', None) == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise KeyError(name)


# ---- odmx themes (schema map rules) and datastream families (ssiexport rules)
MODULES = assigned_literal(os.path.join(here, 'build_data.py'), 'MODULES')
FAMILIES = [(f, re.compile(p, re.I))
            for f, p in assigned_literal(os.path.join(here, '..', 'ssiexport.py'), 'FAMILIES')]


def theme(name):
    for _, label, pats in MODULES:
        if any(re.fullmatch(p, name) for p in pats):
            return label
    return 'Other'


def family(name):
    for f, rx in FAMILIES:
        if rx.search(name):
            return f
    return 'other'


# ---- tables, columns, keys
tables = {}
for m in re.finditer(r'^CREATE TABLE (\w+)\.' + ID + r' \((.*?)^\);', text, re.M | re.S):
    schema, name, body = m.group(1), unq(m.group(2)), m.group(3)
    cols = []
    for line in body.strip().splitlines():
        line = line.strip().rstrip(',')
        if not line or line.startswith('CONSTRAINT'):
            continue
        cm = re.match(r'("[^"]+"|\S+)\s*(.*)', line)
        typ = re.split(r' (NOT NULL|DEFAULT|GENERATED|COLLATE)', cm.group(2))[0].strip()
        cols.append({'n': unq(cm.group(1)), 't': typ, 'nn': 'NOT NULL' in cm.group(2)})
    tables[(schema, name)] = {'kind': 'table', 'cols': cols, 'pk': [], 'fks': [], 'comment': ''}

for m in re.finditer(r'^CREATE (MATERIALIZED )?VIEW (\w+)\.' + ID, text, re.M):
    tables.setdefault((m.group(2), unq(m.group(3))),
                      {'kind': 'materialized view' if m.group(1) else 'view',
                       'cols': [], 'pk': [], 'fks': [], 'comment': ''})

alter = (r'^ALTER TABLE (?:ONLY )?(\w+)\.' + ID + r'\s+ADD CONSTRAINT \S+ (PRIMARY KEY|FOREIGN KEY) \(([^)]*)\)'
         r'(?: REFERENCES (\w+)\.' + ID + r'\(([^)]*)\))?')
for m in re.finditer(alter, text, re.M):
    s, t, kind, cols, rs, rt, _ = m.groups()
    tab = tables.get((s, unq(t)))
    if not tab:
        continue
    cols = [unq(c.strip()) for c in cols.split(',')]
    if kind == 'PRIMARY KEY':
        tab['pk'] = cols
    else:
        tab['fks'].append({'cols': cols, 'ref': f'{rs}.{unq(rt)}'})


def sql_string(s):
    return s.replace("''", "'")


col_comments = {}
for m in re.finditer(r"^COMMENT ON (TABLE|VIEW|COLUMN) (\w+)\." + ID + r"(?:\." + ID + r")? IS '((?:[^']|'')*)';",
                     text, re.M):
    what, s, t, c, body = m.groups()
    if what == 'COLUMN':
        col_comments[(s, unq(t), unq(c))] = sql_string(body)
    elif (s, unq(t)) in tables:
        tables[(s, unq(t))]['comment'] = sql_string(body)


def one_line(s):
    return re.sub(r'\s+', ' ', s or '').strip()


# ---- row counts and datastream descriptions from the table export (ssiexportall.py)
rowcounts = defaultdict(dict)           # schema -> table -> rows
for schema in ('odmx', 'knowledge', 'feeder', 'datastreams'):
    idx = os.path.join(export_dir, schema, '_tables.tsv')
    if os.path.exists(idx):
        with open(idx, newline='') as f:
            for r in csv.DictReader(f, delimiter='\t'):
                rowcounts[schema][r['table']] = int(r['rows'])


def open_table(schema, name):
    """An exported table as a text file object (.csv.gz preferred, plain .csv accepted)."""
    base = os.path.join(export_dir, schema, name)
    if os.path.exists(base + '.csv.gz'):
        return gzip.open(base + '.csv.gz', 'rt', encoding='utf-8', newline='')
    if os.path.exists(base + '.csv'):
        return open(base + '.csv', encoding='utf-8', newline='')
    sys.exit(f'table_inventory: {base}.csv.gz not found; export the odmx schema first (ssiexportall.py --schema odmx)')


def read_csv(name, key):
    with open_table('odmx', name) as f:
        return {r[key]: r for r in csv.DictReader(f)}


sites = read_csv('sampling_features', 'sampling_feature_id')
variables = read_csv('variables', 'variable_id')
units = read_csv('cv_units', 'units_id')
ds_meta = {}
with open_table('odmx', 'sampling_feature_timeseries_datastreams') as f:
    for r in csv.DictReader(f):
        if r['datastream_database'] == 'datastreams':
            ds_meta[r['datastream_tablename']] = r

# ---- write
by_schema = defaultdict(list)
for (s, n), t in tables.items():
    by_schema[s].append((n, t))


def col_list(t):
    return ', '.join(f"{c['n']} ({c['t']})" for c in t['cols'])


def write(path, header, rows):
    with open(path, 'w', newline='') as f:
        w = csv.writer(f, delimiter='\t', lineterminator='\n')
        w.writerow(header)
        w.writerows(rows)


summary = []
for schema in sorted(by_schema):
    items = sorted(by_schema[schema])
    ds_views = {n[:-5] for n, t in items if schema == 'datastreams' and t['kind'] == 'view' and n.endswith('_view')}
    if schema == 'datastreams':
        # each table has a matching <table>_view; list the tables and flag the view
        items = [(n, t) for n, t in items if not (t['kind'] == 'view' and n[:-5] in dict(items))]

    if schema in ('odmx', 'knowledge'):
        header = ['table', 'kind', 'theme', 'description', 'rows', 'n_columns',
                  'primary_key', 'references', 'columns']
        rows = []
        for n, t in items:
            refs = sorted({f['ref'] for f in t['fks']})
            rows.append([n, t['kind'], 'Knowledge store' if schema == 'knowledge' else theme(n),
                         one_line(t['comment']), rowcounts[schema].get(n, ''),
                         len(t['cols']) or '', ', '.join(t['pk']), ', '.join(refs), col_list(t)])
        crow = []
        for n, t in items:
            fk = {c: f['ref'] for f in t['fks'] for c in f['cols']}
            for c in t['cols']:
                crow.append([n, c['n'], c['t'], 'yes' if c['nn'] else '', 'yes' if c['n'] in t['pk'] else '',
                             fk.get(c['n'], ''), one_line(col_comments.get((schema, n, c['n']), ''))])
        write(os.path.join(out_dir, f'{schema}_columns.tsv'),
              ['table', 'column', 'type', 'not_null', 'primary_key', 'references', 'description'], crow)
    elif schema == 'datastreams':
        header = ['table', 'family', 'site_code', 'site_name', 'variable_term', 'variable_name', 'units',
                  'depth', 'depth_units', 'datastream_type', 'first_measurement', 'last_measurement',
                  'n_measurements', 'rows', 'has_view', 'columns']
        rows = []
        for n, t in items:
            d = ds_meta.get(n, {})
            site = sites.get(d.get('sampling_feature_id', ''), {})
            var = variables.get(d.get('variable_id', ''), {})
            u = units.get(d.get('units_id', ''), {})
            try:
                attr = json.loads(d.get('datastream_attribute') or '{}')
            except ValueError:
                attr = {}
            rows.append([n, family(n), site.get('sampling_feature_code', ''), site.get('sampling_feature_name', ''),
                         var.get('variable_term', ''), var.get('variable_name', ''),
                         u.get('units_abbreviation') or u.get('term', ''),
                         attr.get('depth', ''), attr.get('depth_units', ''), d.get('datastream_type', ''),
                         d.get('first_measurement_date', ''), d.get('last_measurement_date', ''),
                         d.get('total_measurement_numbers', ''), rowcounts[schema].get(n, ''),
                         'yes' if n in ds_views else '', col_list(t)])
    else:  # feeder and anything else
        header = ['table', 'kind', 'family', 'rows', 'n_columns', 'primary_key', 'columns']
        rows = [[n, t['kind'], family(n), rowcounts[schema].get(n, ''), len(t['cols']) or '', ', '.join(t['pk']),
                 col_list(t)] for n, t in items]

    write(os.path.join(out_dir, f'{schema}_tables.tsv'), header, rows)
    kinds = defaultdict(int)
    for _, t in items:
        kinds[t['kind']] += 1
    summary.append((schema, dict(kinds)))

for schema, kinds in summary:
    print(f'{schema:28} ' + ', '.join(f'{v:,} {k}s' for k, v in sorted(kinds.items())))

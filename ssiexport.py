#!/usr/bin/env python3
"""Summarize and export table families from the odmx_lbnlsfa database.

Needs the local server from ssidump.py to be running (`ssidump.py start`).
Python 3 standard library only; talks to the server through the bundled psql.

Three data sources:

  feeder       raw ingest tables in schema "feeder" (one wide table per logger/source)
  datastreams  per-variable time series in schema "datastreams", with site / variable /
               units / depth metadata from odmx.sampling_feature_timeseries_datastreams
               (geochem lives here as the "geochem" family; it has no feeder tables)
  samples      discrete specimen results in odmx (measurement_result_values -> results
               -> subspecimen -> specimen -> site), with standard deviation, censor
               (nonDetect) and quality codes. This is the original form of the geochem data.

Examples:

  ssiexport.py families                                   # table families in feeder + datastreams
  ssiexport.py summary feeder --family influxdb -o influxdb_summary.txt
  ssiexport.py summary datastreams --family geochem -o geochem_summary.txt
  ssiexport.py summary samples --geochem-only -o geochem_samples_summary.txt
  ssiexport.py export feeder --family influxdb --out influxdb_tables/          # one TSV per table
  ssiexport.py export feeder --family snotel --layout long --out snotel_long.tsv.gz
  ssiexport.py export datastreams --family geochem --out geochemdata_all.tsv
  ssiexport.py export samples --geochem-only --out geochem_samples.tsv
  ssiexport.py export datastreams --family geochem --variables Calcium Sodium --sites Evans

Table selection (feeder / datastreams): --family NAME (repeatable), --pattern REGEX
(Python regex, case-insensitive, matched against table names), --table NAME (repeatable).
Output files ending in .gz are gzip-compressed. Run `ssiexport.py <cmd> -h` for options.
"""
import argparse
import csv
import datetime
import gzip
import io
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(os.environ.get('SSIDUMP_ROOT', Path(__file__).resolve().parent))
PORT = int(os.environ.get('SSIDUMP_PORT', os.environ.get('PGPORT', 54329)))
USER = 'ssi'
DB = os.environ.get('SSIEXPORT_DB', 'odmx_lbnlsfa')
RUN = ROOT / 'pgrun'
NULL = 'NULL'          # how SQL NULL is shown in summaries (NaN is shown as NaN)
MARK = '#@@TABLE '     # separator between per-table outputs in batched psql scripts

# Families, first match wins. Datastream tables are named after their feeder table
# (e.g. influxdb_snod1_node012_temp01), so the same patterns work for both schemas.
FAMILIES = [
    ('geochem',       r'^geochemdata_'),
    ('sentinel',      r'^sentinel'),
    ('influxdb',      r'^influxdb_'),
    ('decagon',       r'^decagon_'),
    ('ess_dive',      r'^feeder_ess_dive_'),
    ('phenocam',      r'^feeder_phenocam_'),
    ('flux_stations', r'^(feeder_(er|sg|tr)_|metstation_)'),
    ('streamflow',    r'streamflow'),
    ('nwis',          r'^nwis_'),
    ('snotel',        r'^snotel_'),
    ('gradient_sm',   r'^gradient_sm_'),
    ('cr1000',        r'^((am|bb|jf|kp|sm|mc|cb)_|m[1-4]_)'),
]
FAMILY_RX = [(f, re.compile(p, re.I)) for f, p in FAMILIES]
FAMILY_NAMES = [f for f, _ in FAMILIES] + ['other']


# ---------------------------------------------------------------- helpers

def die(msg):
    sys.exit(f'ssiexport: {msg}')


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def psql_exe():
    exe = ROOT / 'pgenv' / 'bin' / 'psql'
    return str(exe) if exe.exists() else (shutil.which('psql') or die('psql not found'))


def psql_cmd(*extra):
    return [psql_exe(), '-X', '-q', '-v', 'ON_ERROR_STOP=1', '-h', str(RUN), '-p', str(PORT),
            '-U', USER, '-d', DB, *extra]


def ident(name):
    return '"' + name.replace('"', '""') + '"'


def lit(s):
    return "'" + str(s).replace("'", "''") + "'"


def q(query):
    """Run one SELECT, return a list of dicts (values are strings, NULL -> None)."""
    p = subprocess.run(psql_cmd('-c', f"COPY ({query}) TO STDOUT WITH (FORMAT csv, HEADER, NULL '{NULL}')"),
                       capture_output=True, text=True)
    if p.returncode:
        die(p.stderr.strip() or 'psql failed')
    return [{k: (None if v == NULL else v) for k, v in r.items()}
            for r in csv.DictReader(io.StringIO(p.stdout))]


def batch(statements):
    """Run many (label, SELECT) pairs in one psql session.
    Returns {label: (header, rows)} with rows as lists of strings (NULL -> None)."""
    lines = []
    for label, sel in statements:
        lines.append(f'\\echo {MARK}{label}')
        lines.append(f"COPY ({sel}) TO STDOUT WITH (FORMAT csv, HEADER, NULL '{NULL}');")
    p = subprocess.run(psql_cmd('-f', '-'), input='\n'.join(lines) + '\n', capture_output=True, text=True)
    if p.returncode:
        die(p.stderr.strip() or 'psql failed')
    out, label, buf = {}, None, []

    def flush():
        if label is not None:
            rows = list(csv.reader(io.StringIO(''.join(buf))))
            out[label] = (rows[0] if rows else [], [[None if v == NULL else v for v in r] for r in rows[1:]])
    for line in io.StringIO(p.stdout):
        if line.startswith(MARK):
            flush()
            label, buf = line[len(MARK):].rstrip('\n'), []
        else:
            buf.append(line)
    flush()
    return out


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def batch_all(statements, size=500):
    out = {}
    for part in chunks(statements, size):
        out.update(batch(part))
    return out


def stream_copy(sql_lines, out_path, header=None):
    """Run COPY ... TO STDOUT statements and stream their output into one file (.gz ok)."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    opener = gzip.open if out_path.suffix == '.gz' else open
    with opener(out_path, 'wt', encoding='utf-8', newline='') as fh:
        if header:
            fh.write('\t'.join(header) + '\n')
            fh.flush()
        # SQL goes in via a temp file and stderr to another, so only stdout is piped
        # (feeding a big script on stdin while psql fills stdout would deadlock).
        with tempfile.NamedTemporaryFile('w', suffix='.sql', delete=False) as sf, \
                tempfile.TemporaryFile('w+') as ef:
            sf.write('\n'.join(sql_lines) + '\n')
            sf.close()
            try:
                p = subprocess.Popen(psql_cmd('-f', sf.name), stdout=subprocess.PIPE, stderr=ef,
                                     text=True, encoding='utf-8')
                shutil.copyfileobj(p.stdout, fh, 1 << 20)
                rc = p.wait()
            finally:
                os.unlink(sf.name)
            if rc:
                ef.seek(0)
                die(ef.read().strip() or 'psql failed')


def count_lines(path):
    path = Path(path)
    opener = gzip.open if path.suffix == '.gz' else open
    with opener(path, 'rb') as fh:
        return sum(1 for _ in fh)


def human(nbytes):
    n = float(nbytes or 0)
    for unit in ('B', 'kB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024


def fmt_table(header, rows, maxw=60):
    """Plain-text aligned table."""
    def cell(v):
        s = NULL if v is None else str(v)
        s = s.replace('\n', ' ').replace('\t', ' ')
        return s if len(s) <= maxw else s[:maxw - 1] + '…'
    body = [[cell(v) for v in r] for r in rows]
    head = [cell(h) for h in header]
    w = [max([len(h)] + [len(r[i]) for r in body]) for i, h in enumerate(head)]
    line = lambda r: ' | '.join(v.ljust(w[i]) for i, v in enumerate(r)).rstrip()
    sep = '-+-'.join('-' * x for x in w)
    return '\n'.join([line(head), sep] + [line(r) for r in body] + [f'({len(body)} rows)'])


class Report:
    def __init__(self):
        self.parts = []

    def h1(self, title):
        self.parts += ['', title, '=' * len(title)]

    def h2(self, title):
        self.parts += ['', title, '-' * len(title)]

    def text(self, *lines):
        self.parts += list(lines)

    def table(self, header, rows, maxw=60):
        self.parts.append(fmt_table(header, rows, maxw))

    def write(self, path):
        s = '\n'.join(self.parts).lstrip('\n') + '\n'
        if path in (None, '-'):
            sys.stdout.write(s)
        else:
            Path(path).write_text(s, encoding='utf-8')
            log(f'wrote {path} ({len(s):,} bytes, {s.count(chr(10)):,} lines)')


def report_header(rep, title, selection):
    db_note = q(f"select coalesce(shobj_description(oid, 'pg_database'), '') as c from pg_database "
                f"where datname = {lit(DB)}")
    rep.h1(title)
    note = db_note[0]['c'] if db_note and db_note[0]['c'] else ''
    rep.text(f'Database : {DB}' + (f'  ({note})' if note else ''),
             f'Generated: {datetime.datetime.now().strftime("%Y-%m-%d %H:%M")} on {socket.gethostname()}'
             f' by {Path(sys.argv[0]).name} {" ".join(sys.argv[1:])}',
             f'Selection: {selection}')


def check_server():
    p = subprocess.run(psql_cmd('-Atc', 'select 1'), capture_output=True, text=True)
    if p.returncode:
        die(f'cannot connect to {DB} via {RUN} port {PORT}: {p.stderr.strip()}\n'
            f'  start the server first:  ssidump.py start')


# ---------------------------------------------------------------- table selection

def family_of(name):
    for fam, rx in FAMILY_RX:
        if rx.search(name):
            return fam
    return 'other'


def schema_tables(schema):
    return q(f"""select c.relname as name, pg_total_relation_size(c.oid) as bytes,
                        greatest(c.reltuples, 0)::bigint as est_rows
                 from pg_class c join pg_namespace n on n.oid = c.relnamespace
                 where n.nspname = {lit(schema)} and c.relkind in ('r', 'p') order by c.relname""")


def pick(names, args):
    fams = args.family or []
    rx = re.compile(args.pattern, re.I) if getattr(args, 'pattern', None) else None
    explicit = set(args.table or [])
    if not (fams or rx or explicit):
        return list(names)
    bad = [f for f in fams if f not in FAMILY_NAMES]
    if bad:
        die(f'unknown family {bad}; choose from {FAMILY_NAMES}')
    return [n for n in names if n in explicit or family_of(n) in fams or (rx and rx.search(n))]


def selection_text(args, extra=''):
    bits = []
    if args.family:
        bits.append('family=' + ','.join(args.family))
    if getattr(args, 'pattern', None):
        bits.append(f'pattern=/{args.pattern}/i')
    if args.table:
        bits.append('tables=' + ','.join(args.table))
    return ' '.join(bits + ([extra] if extra else [])) or 'all tables'


def columns_of(schema, names):
    """{table: [(column, type)]} in column order."""
    rows = q(f"""select c.relname as t, a.attname as col, format_type(a.atttypid, a.atttypmod) as typ
                 from pg_attribute a join pg_class c on c.oid = a.attrelid
                 join pg_namespace n on n.oid = c.relnamespace
                 where n.nspname = {lit(schema)} and c.relkind in ('r', 'p') and a.attnum > 0
                   and not a.attisdropped order by c.relname, a.attnum""")
    want, out = set(names), defaultdict(list)
    for r in rows:
        if r['t'] in want:
            out[r['t']].append((r['col'], r['typ']))
    return out


NUMERIC = ('double precision', 'real', 'numeric', 'integer', 'bigint', 'smallint')
TIME_COLS = ('timestamp', 'utc_time')
META_COLS = ('index',) + TIME_COLS + ('qa_flag',)


def time_info(cols):
    """(column, SQL expression giving a UTC/naive timestamp) or (None, None)."""
    names = [c for c, _ in cols]
    if 'timestamp' in names:
        return 'timestamp', ident('timestamp')
    if 'utc_time' in names:
        return 'utc_time', "(to_timestamp(utc_time) at time zone 'UTC')"
    return None, None


def order_key(cols):
    names = [c for c, _ in cols]
    for k in ('index', 'utc_time', 'timestamp'):
        if k in names:
            return k
    return None


def value_cols(cols):
    return [(c, t) for c, t in cols if c not in META_COLS]


def real_cond(cols, alias=''):
    """SQL: row has at least one value column that is non-NULL and not NaN."""
    a = alias + '.' if alias else ''
    parts = []
    for c, t in value_cols(cols):
        col = a + ident(c)
        if t in ('double precision', 'real', 'numeric'):
            parts.append(f"({col} is not null and {col} <> 'NaN')")
        else:
            parts.append(f'{col} is not null')
    return '(' + ' or '.join(parts) + ')' if parts else 'true'


def time_filter(cols, start, end):
    tcol, texpr = time_info(cols)
    conds = []
    if tcol == 'utc_time':
        if start:
            conds.append(f"utc_time >= extract(epoch from {lit(start)}::timestamp at time zone 'UTC')")
        if end:
            conds.append(f"utc_time < extract(epoch from {lit(end)}::timestamp at time zone 'UTC')")
    elif tcol:
        if start:
            conds.append(f'{texpr} >= {lit(start)}::timestamp')
        if end:
            conds.append(f'{texpr} < {lit(end)}::timestamp')
    return conds


# ---------------------------------------------------------------- families

def cmd_families(args):
    schemas = ['feeder', 'datastreams'] if args.schema == 'all' else [args.schema]
    rep = Report()
    report_header(rep, 'TABLE FAMILIES', ', '.join(schemas))
    for schema in schemas:
        tabs = schema_tables(schema)
        groups = defaultdict(list)
        for t in tabs:
            groups[family_of(t['name'])].append(t)
        rep.h2(f'schema {schema}: {len(tabs):,} tables, {human(sum(int(t["bytes"]) for t in tabs))}')
        rows = []
        for fam in FAMILY_NAMES:
            g = groups.get(fam)
            if g:
                rows.append([fam, len(g), human(sum(int(t['bytes']) for t in g)),
                             f'{sum(int(t["est_rows"]) for t in g):,}',
                             ', '.join(t['name'] for t in g[:3]) + (' …' if len(g) > 3 else '')])
        rep.table(['family', 'tables', 'size', 'est_rows', 'examples'], rows, maxw=110)
    rep.text('', 'Family patterns (Python regex, case-insensitive, first match wins):')
    rep.text(*[f'  {f:<14} {p}' for f, p in FAMILIES], '  other          (anything else)')
    rep.write(args.output)


# ---------------------------------------------------------------- feeder summary / export

def feeder_selection(args):
    tabs = {t['name']: t for t in schema_tables('feeder')}
    names = pick(sorted(tabs), args)
    if not names:
        die('no feeder tables match the selection (see `ssiexport.py families`)')
    return names, tabs


def spaced(names, k):
    """k evenly spaced items (all if k <= 0 or k >= len)."""
    if k <= 0 or k >= len(names):
        return list(names)
    return [names[round(i * (len(names) - 1) / (k - 1))] for i in range(k)] if k > 1 else names[:1]


def cmd_summary_feeder(args):
    names, tabs = feeder_selection(args)
    cols = columns_of('feeder', names)
    log(f'summarizing {len(names)} feeder tables ...')
    rep = Report()
    report_header(rep, 'FEEDER TABLE SUMMARY', selection_text(args))

    # per-table stats
    stmts = []
    for t in names:
        c = cols[t]
        _, texpr = time_info(c)
        stmts.append((t, f"""select count(*) as nrows, {f'min({texpr})' if texpr else 'null'} as first_ts,
                              {f'max({texpr})' if texpr else 'null'} as last_ts,
                              count(*) filter (where {real_cond(c)}) as real_rows
                              from feeder.{ident(t)}"""))
    stats = {t: dict(zip(h, r[0])) for t, (h, r) in batch_all(stmts).items()}
    tot_rows = sum(int(stats[t]['nrows']) for t in names)
    tot_real = sum(int(stats[t]['real_rows']) for t in names)
    tot_bytes = sum(int(tabs[t]['bytes']) for t in names)
    fams = Counter(family_of(t) for t in names)

    rep.h2('OVERVIEW')
    rep.text(f'Tables    : {len(names):,}   ({", ".join(f"{f}: {n}" for f, n in fams.most_common())})',
             f'Size      : {human(tot_bytes)} (tables + indexes)',
             f'Rows      : {tot_rows:,} total; {tot_real:,} with at least one non-NaN, non-NULL value',
             f'Time span : {min((stats[t]["first_ts"] for t in names if stats[t]["first_ts"]), default="-")}'
             f' .. {max((stats[t]["last_ts"] for t in names if stats[t]["last_ts"]), default="-")}',
             'Feeder tables are wide: index, "timestamp" (no time zone) + source-specific columns.',
             'Mixed-case / hyphenated names must be double-quoted in SQL, e.g. feeder."sentinel_ER-AMT1".',
             'In PostgreSQL NaN = NaN is true, so filter missing readings with  col <> \'NaN\'.')

    rep.h2('PER-TABLE SIZE, ROWS AND TIME RANGE')
    rows = []
    for t in names:
        s = stats[t]
        n, rr = int(s['nrows']), int(s['real_rows'])
        rows.append([t, family_of(t), human(tabs[t]['bytes']), len(cols[t]), f'{n:,}', f'{rr:,}',
                     f'{100 * rr / n:.0f}%' if n else '-', s['first_ts'], s['last_ts']])
    rep.table(['table', 'family', 'size', 'ncols', 'rows', 'real_rows', 'real%', 'first', 'last'], rows, maxw=80)

    # column sets
    rep.h2('COLUMN SETS (tables sharing identical columns)')
    sets = defaultdict(list)
    for t in names:
        sets[tuple(cols[t])].append(t)
    for i, (cs, ts) in enumerate(sorted(sets.items(), key=lambda kv: -len(kv[1])), 1):
        rep.text('', f'[{i}] {len(ts)} table(s): ' + ', '.join(ts[:8]) + (f' … (+{len(ts) - 8})' if len(ts) > 8 else ''))
        rep.text(*[f'      {c:<40} {typ}' for c, typ in cs])

    # column stats
    if not args.no_column_stats:
        log('column statistics ...')
        stmts = []
        for t in names:
            vc = value_cols(cols[t])
            if not vc:
                continue
            vals = []
            for c, typ in vc:
                col = 't.' + ident(c)
                num = f'{col}::float8' if typ in NUMERIC else 'null::float8'
                ok = (f"({col} is not null and {col} <> 'NaN')" if typ in ('double precision', 'real', 'numeric')
                      else f'{col} is not null')
                nan = f"({col} = 'NaN')" if typ in ('double precision', 'real', 'numeric') else 'false'
                vals.append(f'({lit(c)}, {num}, {ok}, {nan})')
            stmts.append((t, f"""select v.col, count(*) filter (where v.ok) as n_ok,
                                  count(*) filter (where v.nan) as n_nan,
                                  count(*) filter (where not v.ok and not v.nan) as n_null,
                                  min(v.x) filter (where v.ok) as min, max(v.x) filter (where v.ok) as max
                                  from feeder.{ident(t)} t cross join lateral (values {', '.join(vals)}) v(col, x, ok, nan)
                                  group by v.col"""))
        cstats = batch_all(stmts, size=200)
        rep.h2('COLUMN STATISTICS (per value column: readings, NaN, NULL, min, max)')
        for t in names:
            if t not in cstats:
                continue
            h, r = cstats[t]
            order = {c: i for i, (c, _) in enumerate(cols[t])}
            r.sort(key=lambda x: order.get(x[0], 0))
            n = int(stats[t]['nrows']) or 1
            rep.text('', f'### feeder.{ident(t)}  ({int(stats[t]["nrows"]):,} rows)')
            rep.table(['column', 'readings', 'read%', 'NaN', 'NULL', 'min', 'max'],
                      [[x[0], f'{int(x[1]):,}', f'{100 * int(x[1]) / n:.1f}%', f'{int(x[2]):,}', f'{int(x[3]):,}',
                        x[4], x[5]] for x in r])

    # samples
    if args.rows > 0:
        sample_tabs = spaced(names, args.sample_tables)
        note = f' ({len(sample_tabs)} of {len(names)} tables, evenly spaced by name)' if len(sample_tabs) < len(names) else ''
        for kind in (['mid'] + ([] if args.no_real_rows else ['real'])):
            stmts = []
            for t in sample_tabs:
                c = cols[t]
                k = order_key(c)
                ko = ident(k) if k else None
                src = f'feeder.{ident(t)}'
                if not ko:
                    sel = f'select * from {src} limit {args.rows}'
                elif kind == 'mid':
                    sel = (f'select * from {src} where {ko} >= (select min({ko}) + (max({ko}) - min({ko})) / 2 from {src}) '
                           f'order by {ko} limit {args.rows}')
                else:
                    sel = (f'select t.* from {src} t, (select min({ko}) + (max({ko}) - min({ko})) / 2 as mid from {src}) m '
                           f'where {real_cond(c, "t")} order by (t.{ko} < m.mid), t.{ko} limit {args.rows}')
                stmts.append((t, sel))
            res = batch_all(stmts, size=200)
            if kind == 'mid':
                rep.h1(f'SAMPLE ROWS: {args.rows} consecutive rows from the middle of each table{note}')
                rep.text('Middle = first row at or after the midpoint of the index range.')
            else:
                rep.h1(f'SAMPLE ROWS WITH READINGS: {args.rows} rows per table with >= 1 non-NaN, non-NULL value{note}')
                rep.text('Taken from the middle of the table onward, then from the start if needed.')
            for t in sample_tabs:
                h, r = res[t]
                extra = f'   (rows with readings: {int(stats[t]["real_rows"]):,} of {int(stats[t]["nrows"]):,})' if kind == 'real' else ''
                rep.text('', f'### feeder.{ident(t)}{extra}')
                rep.table(h, r)
    rep.write(args.output)


def cmd_export_feeder(args):
    names, tabs = feeder_selection(args)
    cols = columns_of('feeder', names)
    out = Path(args.out or (('_'.join(args.family) if args.family else 'feeder') +
                            ('_long.tsv' if args.layout == 'long' else '_tables')))
    if args.layout == 'tables':
        out.mkdir(parents=True, exist_ok=True)
        lines, files = [], {}
        for t in names:
            c = cols[t]
            conds = time_filter(c, args.start, args.end) + ([real_cond(c)] if args.drop_empty_rows else [])
            k = order_key(c)
            path = out / (re.sub(r'[^A-Za-z0-9._-]', '_', t) + '.tsv')
            files[t] = path
            sel = (f'select * from feeder.{ident(t)}' + (' where ' + ' and '.join(conds) if conds else '') +
                   (f' order by {ident(k)}' if k else ''))
            lines.append(f"\\copy ({sel}) to {lit(path)} with (format csv, delimiter E'\\t', header, null '')")
        log(f'exporting {len(names)} tables to {out}/ ...')
        for part in chunks(lines, 500):
            p = subprocess.run(psql_cmd('-f', '-'), input='\n'.join(part) + '\n', capture_output=True, text=True)
            if p.returncode:
                die(p.stderr.strip())
        man = []
        for t in names:
            man.append([t, family_of(t), files[t].name, count_lines(files[t]) - 1, len(cols[t]),
                        ','.join(c for c, _ in cols[t])])
        with open(out / '_manifest.tsv', 'w') as fh:
            fh.write('table\tfamily\tfile\trows\tncols\tcolumns\n')
            for m in man:
                fh.write('\t'.join(map(str, m)) + '\n')
        log(f'wrote {len(names)} files + _manifest.tsv to {out}/ ({sum(m[3] for m in man):,} rows)')
    else:
        lines = []
        for t in names:
            c = cols[t]
            vc = value_cols(c)
            if not vc:
                continue
            _, texpr = time_info(c)
            vals = ', '.join(f'({lit(cn)}, t.{ident(cn)}::text)' for cn, _ in vc)
            conds = [f"v.val is not null and v.val <> 'NaN'"] + [x.replace('utc_time', 't.utc_time') if 'utc_time' in x
                                                                  else x for x in time_filter(c, args.start, args.end)]
            k = order_key(c)
            sel = (f"select {lit(t)}, {('t.' + ident('index')) if k == 'index' else 'null'}, "
                   f"{('to_char(' + texpr.replace(ident('timestamp'), 't.' + ident('timestamp')) + ', ' + lit('YYYY-MM-DD HH24:MI:SS') + ')') if texpr else 'null'}, "
                   f"v.col, v.val from feeder.{ident(t)} t cross join lateral (values {vals}) v(col, val) "
                   f"where {' and '.join(conds)}" + (f' order by t.{ident(k)}' if k else ''))
            lines.append(f"COPY ({sel}) TO STDOUT WITH (FORMAT csv, DELIMITER E'\\t', NULL '');")
        log(f'exporting {len(lines)} tables (long format) to {out} ...')
        stream_copy(lines, out, header=['table', 'index', 'timestamp', 'column', 'value'])
        log(f'wrote {out} ({count_lines(out) - 1:,} rows)')


# ---------------------------------------------------------------- datastreams

def datastream_registry(args):
    existing = {t['name']: t for t in schema_tables('datastreams')}
    reg = q("""select d.datastream_id, d.datastream_tablename as tab, d.datastream_type as dtype,
                      sf.sampling_feature_code as site_code, sf.sampling_feature_name as site_name,
                      sf.sampling_feature_type_cv as site_type, sf.latitude, sf.longitude,
                      v.variable_term, v.variable_name, coalesce(u.units_abbreviation, u.term) as units,
                      d.datastream_attribute::jsonb ->> 'depth' as depth,
                      d.datastream_attribute::jsonb ->> 'depth_units' as depth_units,
                      d.equipment_id, d.total_measurement_numbers as reg_rows
               from odmx.sampling_feature_timeseries_datastreams d
               join odmx.sampling_features sf using (sampling_feature_id)
               left join odmx.variables v using (variable_id)
               left join odmx.cv_units u on u.units_id = d.units_id""")
    names = set(pick(sorted(existing), args))
    lower = lambda xs: {x.lower() for x in xs} if xs else None
    vs, ss, ts = lower(args.variables), lower(args.sites), lower(args.type)
    sel = [r for r in reg if r['tab'] in names
           and (vs is None or (r['variable_term'] or '').lower() in vs)
           and (ss is None or (r['site_code'] or '').lower() in ss)
           and (ts is None or (r['dtype'] or '').lower() in ts)]
    if not sel:
        die('no registered datastream tables match the selection')
    unreg = sorted(names - {r['tab'] for r in reg}) if not (vs or ss or ts) else []
    for r in sel:
        r['bytes'] = int(existing[r['tab']]['bytes'])
    sel.sort(key=lambda r: (r['site_code'] or '', r['variable_term'] or '', float(r['depth']) if r['depth'] else -1))
    return sel, unreg


def ds_selection_text(args):
    extra = []
    if args.variables:
        extra.append('variables=' + ','.join(args.variables))
    if args.sites:
        extra.append('sites=' + ','.join(args.sites))
    if args.type:
        extra.append('type=' + ','.join(args.type))
    return selection_text(args, ' '.join(extra))


def cmd_summary_datastreams(args):
    sel, unreg = datastream_registry(args)
    log(f'summarizing {len(sel)} datastream tables ...')
    stmts = [(r['tab'], f"""select count(*) as nrows,
                 count(*) filter (where data_value is null or data_value = 'NaN') as n_nan,
                 count(*) filter (where data_value = -9999) as n_fill,
                 to_char(to_timestamp(min(utc_time)) at time zone 'UTC', 'YYYY-MM-DD HH24:MI:SS') as first_ts,
                 to_char(to_timestamp(max(utc_time)) at time zone 'UTC', 'YYYY-MM-DD HH24:MI:SS') as last_ts,
                 min(data_value) filter (where data_value <> 'NaN') as vmin,
                 max(data_value) filter (where data_value <> 'NaN') as vmax,
                 (select string_agg(coalesce(f, 'NULL') || ':' || n, ' ' order by f)
                    from (select qa_flag f, count(*) n from datastreams.{ident(r['tab'])} group by 1) x) as qa
                 from datastreams.{ident(r['tab'])}""") for r in sel]
    st = {t: dict(zip(h, rr[0])) for t, (h, rr) in batch_all(stmts).items()}
    for r in sel:
        r.update(st[r['tab']])
        r['nrows'], r['n_nan'], r['n_fill'] = int(r['nrows']), int(r['n_nan']), int(r['n_fill'])

    rep = Report()
    report_header(rep, 'DATASTREAM SUMMARY', ds_selection_text(args))
    tot = sum(r['nrows'] for r in sel)
    qa = Counter()
    for r in sel:
        for kv in (r['qa'] or '').split():
            f, n = kv.rsplit(':', 1)
            qa[f] += int(n)
    rep.h2('OVERVIEW')
    rep.text(f'Tables     : {len(sel):,} in schema datastreams (registered in odmx.sampling_feature_timeseries_datastreams)',
             f'Types      : {dict(Counter(r["dtype"] for r in sel))}',
             f'Sites      : {len({r["site_code"] for r in sel}):,}    Variables: {len({r["variable_term"] for r in sel}):,}',
             f'Size       : {human(sum(r["bytes"] for r in sel))}',
             f'Rows       : {tot:,} ({sum(r["n_nan"] for r in sel):,} NaN/NULL values, '
             f'{sum(r["n_fill"] for r in sel):,} = -9999 fill code)',
             f'Time span  : {min((r["first_ts"] for r in sel if r["first_ts"]), default="-")} .. '
             f'{max((r["last_ts"] for r in sel if r["last_ts"]), default="-")} (UTC)',
             f'With depth : {sum(1 for r in sel if r["depth"]):,} tables',
             f'qa_flag    : {dict(qa)}',
             'Each table: utc_time bigint (Unix epoch, UTC), data_value double precision, qa_flag char(1).')
    if unreg:
        rep.text(f'Not in registry (no site/variable metadata): {len(unreg)} tables, e.g. {", ".join(unreg[:5])}')
    if any(r['dtype'] == 'chemicaldata' for r in sel):
        rep.text('', 'Geochem notes:',
                 '- Built from the odmx sample results (see `summary samples`): only instrumentReading values;',
                 '  standard deviations, censor (nonDetect) and quality codes are NOT carried over.',
                 '- Non-detects are stored as 0 in odmx and appear as 0 here, indistinguishable from real zeros.',
                 '- -9999 is a fill/missing code (see n_-9999 below); export with --fill blank|drop.',
                 '- Negative values are expected for deltaD / deltaO18 (isotope ratios).',
                 '- utc_time = local sample time + 7 h (odmx utc offset -7); a 12:00 sample shows as 19:00 UTC.',
                 '- Same analyte can have different units at different sites; check the units column.',
                 '- deltaD / deltaO18 are registered as "unitless" (presumably per mil vs VSMOW; not stated).')

    rep.h2('BY VARIABLE')
    byv = defaultdict(list)
    for r in sel:
        byv[r['variable_term']].append(r)
    rows = []
    for v in sorted(byv, key=lambda x: (x or '').lower()):
        g = byv[v]
        fl = lambda key, fn: fn((float(x[key]) for x in g if x[key] not in (None, '')), default=None)
        rows.append([v, ', '.join(sorted({x['units'] or '' for x in g})), len(g), len({x['site_code'] for x in g}),
                     f'{sum(x["nrows"] for x in g):,}', f'{sum(x["n_fill"] for x in g):,}',
                     min((x['first_ts'] for x in g if x['first_ts']), default=None),
                     max((x['last_ts'] for x in g if x['last_ts']), default=None),
                     f'{fl("vmin", min):g}' if fl('vmin', min) is not None else None,
                     f'{fl("vmax", max):g}' if fl('vmax', max) is not None else None])
    rep.table(['variable', 'units', 'tables', 'sites', 'rows', 'n_-9999', 'first', 'last', 'min', 'max'], rows)

    rep.h2('BY SITE')
    bys = defaultdict(list)
    for r in sel:
        bys[r['site_code']].append(r)
    rows = []
    for s in sorted(bys, key=lambda x: (x or '').lower()):
        g = bys[s]
        depths = sorted({float(x['depth']) for x in g if x['depth']})
        rows.append([s, g[0]['site_name'], g[0]['site_type'], g[0]['latitude'], g[0]['longitude'],
                     len({x['variable_term'] for x in g}), len(g), f'{sum(x["nrows"] for x in g):,}',
                     min((x['first_ts'] for x in g if x['first_ts']), default=None),
                     max((x['last_ts'] for x in g if x['last_ts']), default=None),
                     (', '.join(f'{d:g}' for d in depths) + ' m') if depths else ''])
    rep.table(['site_code', 'site_name', 'site_type', 'lat', 'lon', 'nvars', 'tables', 'rows', 'first', 'last', 'depths'],
              rows, maxw=70)

    if args.list_tables:
        rep.h2('PER-TABLE LIST')
        rep.table(['table', 'site', 'variable', 'units', 'depth', 'rows', 'NaN', 'n_-9999', 'first', 'last', 'min', 'max', 'qa'],
                  [[r['tab'], r['site_code'], r['variable_term'], r['units'], r['depth'], f'{r["nrows"]:,}', r['n_nan'], r['n_fill'],
                    r['first_ts'], r['last_ts'], r['vmin'], r['vmax'], r['qa']] for r in sel], maxw=80)

    if args.rows > 0 and args.sample_by != 'none':
        if args.sample_by == 'variable':
            picks = [max(byv[v], key=lambda x: x['nrows']) for v in sorted(byv, key=lambda x: (x or '').lower())]
            title = f'SAMPLE ROWS: {args.rows} rows per variable (from the middle of its largest table)'
        elif args.sample_by == 'site':
            picks = [max(bys[s], key=lambda x: x['nrows']) for s in sorted(bys, key=lambda x: (x or '').lower())]
            title = f'SAMPLE ROWS: {args.rows} rows per site (from the middle of its largest table)'
        else:
            picks = [r for r in sel if r['tab'] in set(spaced([x['tab'] for x in sel], args.sample_tables))]
            title = f'SAMPLE ROWS: {args.rows} rows from the middle of each table ({len(picks)} of {len(sel)})'
        stmts = []
        for r in picks:
            src = f'datastreams.{ident(r["tab"])}'
            stmts.append((r['tab'], f"""select utc_time, to_char(to_timestamp(utc_time) at time zone 'UTC', 'YYYY-MM-DD HH24:MI:SS') as datetime_utc,
                         data_value, qa_flag from {src}
                         where utc_time >= (select min(utc_time) + (max(utc_time) - min(utc_time)) / 2 from {src})
                         order by utc_time limit {args.rows}"""))
        res = batch_all(stmts, size=300)
        rep.h1(title)
        for r in picks:
            h, rr = res[r['tab']]
            depth = f', depth {r["depth"]} {r["depth_units"]}' if r['depth'] else ''
            rep.text('', f'### datastreams.{ident(r["tab"])}   site {r["site_code"]} ({r["site_type"]}), '
                         f'{r["variable_term"]} [{r["units"]}]{depth}, {r["nrows"]:,} rows')
            rep.table(h, rr)
    rep.write(args.output)


DS_HEADER = ['datastream_id', 'datastream_table', 'datastream_type', 'site_code', 'site_name', 'site_type',
             'latitude', 'longitude', 'variable_term', 'variable_name', 'units', 'depth', 'depth_units',
             'utc_time', 'datetime_utc', 'data_value', 'qa_flag']


def cmd_export_datastreams(args):
    sel, _ = datastream_registry(args)
    out = Path(args.out or (('_'.join(args.family) if args.family else 'datastreams') + '_datastreams.tsv'))
    tc = [('utc_time', 'bigint')]
    conds = time_filter(tc, args.start, args.end) + (["data_value is not null and data_value <> 'NaN'"] if args.drop_nan else [])
    if args.fill == 'drop':
        conds.append('data_value is distinct from -9999')
    where = (' where ' + ' and '.join(conds)) if conds else ''
    dv = 'nullif(data_value, -9999)' if args.fill == 'blank' else 'data_value'
    lines = []
    for r in sel:
        meta = [r['datastream_id'], r['tab'], r['dtype'], r['site_code'], r['site_name'], r['site_type'], r['latitude'],
                r['longitude'], r['variable_term'], r['variable_name'], r['units'], r['depth'], r['depth_units']]
        consts = ', '.join('null' if m is None else lit(m) for m in meta)
        lines.append(f"COPY (select {consts}, utc_time, to_char(to_timestamp(utc_time) at time zone 'UTC', "
                     f"'YYYY-MM-DD HH24:MI:SS'), {dv}, qa_flag from datastreams.{ident(r['tab'])}{where} "
                     f"order by utc_time) TO STDOUT WITH (FORMAT csv, DELIMITER E'\\t', NULL '');")
    log(f'exporting {len(sel)} datastream tables to {out} ...')
    stream_copy(lines, out, header=DS_HEADER)
    log(f'wrote {out} ({count_lines(out) - 1:,} rows)')


# ---------------------------------------------------------------- odmx samples

def samples_sql(args):
    """One row per result: reading + standard deviation, with specimen and site."""
    conds = []
    if args.geochem_only:
        conds.append("r.variable_id in (select variable_id from odmx.sampling_feature_timeseries_datastreams "
                     "where datastream_type = 'chemicaldata')")
    if args.variables:
        conds.append('lower(v.variable_term) in (' + ', '.join(lit(x.lower()) for x in args.variables) + ')')
    if args.sites:
        conds.append('lower(site.sampling_feature_code) in (' + ', '.join(lit(x.lower()) for x in args.sites) + ')')
    if args.start:
        conds.append(f'vals.value_date_time - make_interval(hours => vals.utc_offset) >= {lit(args.start)}::timestamp')
    if args.end:
        conds.append(f'vals.value_date_time - make_interval(hours => vals.utc_offset) < {lit(args.end)}::timestamp')
    value, sd = 'vals.value', 'vals.sd'
    if getattr(args, 'fill', 'keep') == 'blank':
        value, sd = 'nullif(vals.value, -9999)', 'nullif(vals.sd, -9999)'
    if getattr(args, 'nondetect', 'keep') == 'blank':
        value = f"case when vals.censor_code = 'nonDetect' then null else {value} end"
    return f"""
    with vals as (
      select result_id,
        max(data_value) filter (where aggregation_statistic_cv = 'instrumentReading') as value,
        max(data_value) filter (where aggregation_statistic_cv = 'standardDeviation') as sd,
        max(censor_code_cv) filter (where aggregation_statistic_cv = 'instrumentReading') as censor_code,
        max(quality_code_cv) filter (where aggregation_statistic_cv = 'instrumentReading') as quality_code,
        max(value_date_time) filter (where aggregation_statistic_cv = 'instrumentReading') as value_date_time,
        max(value_date_time_utc_offset) filter (where aggregation_statistic_cv = 'instrumentReading') as utc_offset
      from odmx.measurement_result_values group by result_id)
    select site.sampling_feature_code as site_code, site.sampling_feature_name as site_name,
           site.sampling_feature_type_cv as site_type, site.latitude, site.longitude,
           spec.sampling_feature_code as specimen_code, sub.sampling_feature_code as subspecimen_code,
           sp.specimen_medium_cv as medium,
           v.variable_term, v.variable_name, coalesce(u.units_abbreviation, u.term) as units,
           {value} as value, {sd} as sd, vals.censor_code, vals.quality_code,
           to_char(vals.value_date_time, 'YYYY-MM-DD HH24:MI:SS') as datetime_local, vals.utc_offset,
           to_char(vals.value_date_time - make_interval(hours => vals.utc_offset), 'YYYY-MM-DD HH24:MI:SS') as datetime_utc,
           mr.z_location as depth, zu.units_abbreviation as depth_units,
           m.method_name, pl.processing_level_code as processing_level, r.result_id,
           exists (select 1 from odmx.sampling_feature_timeseries_datastreams d where d.datastream_type = 'chemicaldata'
                   and d.variable_id = r.variable_id and d.sampling_feature_id = site.sampling_feature_id) as in_datastreams
    from vals
    join odmx.results r using (result_id)
    join odmx.measurement_results mr using (result_id)
    join odmx.variables v on v.variable_id = r.variable_id
    left join odmx.cv_units u on u.units_id = r.units_id
    left join odmx.cv_units zu on zu.units_id = mr.z_location_units_id
    left join odmx.processing_levels pl on pl.processing_level_id = r.processing_level_id
    join odmx.feature_actions fa on fa.feature_action_id = r.feature_action_id
    join odmx.actions a on a.action_id = fa.action_id
    left join odmx.methods m on m.method_id = a.method_id
    join odmx.sampling_features sub on sub.sampling_feature_id = fa.sampling_feature_id
    left join odmx.specimens sp on sp.sampling_feature_id = sub.sampling_feature_id
    left join odmx.related_features r1 on r1.sampling_feature_id = sub.sampling_feature_id
         and r1.relationship_type_cv = 'isSubSpecimenOf'
    join odmx.sampling_features spec on spec.sampling_feature_id = coalesce(r1.related_feature_id, sub.sampling_feature_id)
    left join odmx.related_features r2 on r2.sampling_feature_id = spec.sampling_feature_id
         and r2.relationship_type_cv = 'wasCollectedAt'
    left join odmx.sampling_features site on site.sampling_feature_id = r2.related_feature_id
    {('where ' + ' and '.join(conds)) if conds else ''}"""


def samples_selection_text(args):
    bits = ['geochem-only (variables with geochemdata datastreams)' if args.geochem_only else 'all specimen results']
    if args.variables:
        bits.append('variables=' + ','.join(args.variables))
    if args.sites:
        bits.append('sites=' + ','.join(args.sites))
    return ' '.join(bits)


def cmd_summary_samples(args):
    base = samples_sql(args)
    log('summarizing odmx sample results ...')
    res = batch([
        ('overview', f"""select count(*) n, count(sd) n_sd, count(*) filter (where censor_code = 'nonDetect') n_nd,
                         count(*) filter (where value = -9999) n_fill, count(*) filter (where sd = -9999) n_fill_sd,
                         count(*) filter (where value > 1e9) n_huge,
                         count(*) filter (where site_code is null) n_nosite,
                         count(*) filter (where subspecimen_code = specimen_code) n_noparent,
                         count(distinct site_code) n_sites, count(distinct variable_term) n_vars,
                         count(distinct specimen_code) n_specimens, count(depth) n_depth,
                         count(*) filter (where in_datastreams) n_in_ds,
                         min(datetime_utc) first_utc, max(datetime_utc) last_utc,
                         count(distinct result_id) n_results from ({base}) s"""),
        ('codes', f"""select 'censor_code' k, coalesce(censor_code, 'NULL') v, count(*) n from ({base}) s group by 1, 2
                      union all select 'quality_code', coalesce(quality_code, 'NULL'), count(*) from ({base}) s group by 1, 2
                      union all select 'utc_offset', coalesce(utc_offset::text, 'NULL'), count(*) from ({base}) s group by 1, 2
                      union all select 'medium', coalesce(medium, 'NULL'), count(*) from ({base}) s group by 1, 2
                      union all select 'method', coalesce(method_name, 'NULL'), count(*) from ({base}) s group by 1, 2
                      union all select 'processing_level', coalesce(processing_level, 'NULL'), count(*) from ({base}) s group by 1, 2
                      union all select 'site_type', coalesce(site_type, 'NULL'), count(*) from ({base}) s group by 1, 2
                      order by 1, 3 desc"""),
        ('byvar', f"""select variable_term, string_agg(distinct units, ', ') units, count(*) n, count(sd) n_sd,
                      count(*) filter (where censor_code = 'nonDetect') n_nondetect,
                      count(*) filter (where value = -9999) "n_-9999",
                      count(*) filter (where value < 0 and value <> -9999) n_negative,
                      count(*) filter (where value > 1e9) "n_>1e9",
                      count(*) filter (where in_datastreams) n_in_datastreams,
                      count(distinct site_code) sites, min(datetime_utc) first_utc, max(datetime_utc) last_utc,
                      min(value) filter (where censor_code <> 'nonDetect' and value <> -9999) min_detected,
                      max(value) max_value
                      from ({base}) s group by 1 order by lower(variable_term)"""),
        ('bysite', f"""select site_code, max(site_name) site_name, max(site_type) site_type, max(latitude) lat, max(longitude) lon,
                       count(distinct variable_term) nvars, count(distinct specimen_code) specimens, count(*) n,
                       count(*) filter (where censor_code = 'nonDetect') n_nondetect,
                       min(datetime_utc) first_utc, max(datetime_utc) last_utc,
                       string_agg(distinct depth::text, ', ') depths_m
                       from ({base}) s group by 1 order by lower(site_code)"""),
        ('samples', f"""select * from (select s.*, row_number() over w rn, count(*) over (partition by variable_term) cnt
                        from ({base}) s window w as (partition by variable_term order by datetime_utc, site_code, result_id)) x
                        where rn > cnt / 2 and rn <= cnt / 2 + {max(args.rows, 0)}
                        order by lower(variable_term), rn"""),
    ])
    rep = Report()
    report_header(rep, 'ODMX SAMPLE RESULTS SUMMARY', samples_selection_text(args))
    o = dict(zip(*[res['overview'][0], res['overview'][1][0]]))
    rep.h2('OVERVIEW')
    rep.text(f'Results (1 per measured value): {int(o["n"]):,}  (distinct result_id {int(o["n_results"]):,})',
             f'  with standard deviation      : {int(o["n_sd"]):,}',
             f'  censor_code = nonDetect      : {int(o["n_nd"]):,}  (value stored as 0)',
             f'  value = -9999 (fill/missing) : {int(o["n_fill"]):,}  (sd = -9999: {int(o["n_fill_sd"]):,})',
             f'  value > 1e9 (suspect)        : {int(o["n_huge"]):,}',
             f'  with depth (z_location)      : {int(o["n_depth"]):,}',
             f'  site has matching datastream : {int(o["n_in_ds"]):,}',
             f'  without a site               : {int(o["n_nosite"]):,}',
             f'  specimen without subspecimen : {int(o["n_noparent"]):,}',
             f'Sites: {o["n_sites"]}   Variables: {o["n_vars"]}   Specimens: {int(o["n_specimens"]):,}',
             f'Time span (UTC): {o["first_utc"]} .. {o["last_utc"]}')
    rep.text('', 'Join path:',
             '  measurement_result_values (instrumentReading + standardDeviation rows, paired per result_id)',
             '    -> results -> measurement_results (z_location = depth) / variables / cv_units / processing_levels',
             '    -> feature_actions -> actions -> methods',
             '    -> sampling_features (subspecimen) -[isSubSpecimenOf]-> specimen -[wasCollectedAt]-> site',
             '', 'Notes:',
             '- nonDetect values are stored as 0; use censor_code to tell them from real zeros.',
             '- -9999 is a fill/missing code (censor_code notCensored!) in value and sd; export with --fill blank.',
             '- Negative values are expected for deltaD / deltaO18 / deltaH / deltaS (isotope ratios).',
             '- datetime_local is the stored value_date_time; datetime_utc = local - utc_offset hours.',
             '- method_name / processing_level are the same placeholder on every row in this dump.',
             '- The geochemdata datastreams hold only the instrumentReading values of the 48 geochem variables.')
    rep.h2('CODE DISTRIBUTIONS')
    rep.table(['field', 'value', 'n'], res['codes'][1], maxw=80)
    rep.h2('BY VARIABLE')
    rep.table(res['byvar'][0], res['byvar'][1])
    rep.h2('BY SITE')
    rep.table(res['bysite'][0], res['bysite'][1], maxw=70)
    if args.rows > 0:
        h, rows = res['samples']
        keep = [i for i, c in enumerate(h) if c not in ('rn', 'cnt')]
        rep.h1(f'SAMPLE ROWS: {args.rows} rows per variable (consecutive in time, from the middle of its records)')
        vi = h.index('variable_term')
        groups = defaultdict(list)
        for r in rows:
            groups[r[vi]].append([r[i] for i in keep])
        for v in sorted(groups, key=lambda x: (x or '').lower()):
            rep.text('', f'### {v}')
            rep.table([h[i] for i in keep], groups[v], maxw=50)
    rep.write(args.output)


SAMPLES_HEADER = ['site_code', 'site_name', 'site_type', 'latitude', 'longitude', 'specimen_code', 'subspecimen_code',
                  'medium', 'variable_term', 'variable_name', 'units', 'value', 'sd', 'censor_code', 'quality_code',
                  'datetime_local', 'utc_offset', 'datetime_utc', 'depth', 'depth_units', 'method_name',
                  'processing_level', 'result_id', 'in_datastreams']


def cmd_export_samples(args):
    out = Path(args.out or ('geochem_samples.tsv' if args.geochem_only else 'odmx_samples.tsv'))
    sel = f'select * from ({samples_sql(args)}) s order by site_code, variable_term, depth nulls first, datetime_utc, result_id'
    log(f'exporting odmx sample results to {out} ...')
    stream_copy([f"COPY ({sel}) TO STDOUT WITH (FORMAT csv, DELIMITER E'\\t', NULL '');"], out, header=SAMPLES_HEADER)
    log(f'wrote {out} ({count_lines(out) - 1:,} rows)')


# ---------------------------------------------------------------- CLI

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('families', help='list table families and their sizes')
    p.add_argument('--schema', choices=['feeder', 'datastreams', 'all'], default='all')
    p.add_argument('-o', '--output', help='write to file instead of stdout')
    p.set_defaults(fn=cmd_families)

    def selection(p):
        g = p.add_argument_group('table selection')
        g.add_argument('--family', action='append', metavar='NAME', help=f'one of {FAMILY_NAMES} (repeatable)')
        g.add_argument('--pattern', metavar='REGEX', help='Python regex on table names (case-insensitive)')
        g.add_argument('--table', action='append', metavar='NAME', help='exact table name (repeatable)')

    def ds_filters(p):
        g = p.add_argument_group('datastream filters')
        g.add_argument('--variables', nargs='+', metavar='TERM', help='variable_term values, e.g. Calcium Sodium')
        g.add_argument('--sites', nargs='+', metavar='CODE', help='sampling_feature_code values, e.g. Evans CC-BOG0')
        g.add_argument('--type', nargs='+', metavar='TYPE', help='datastream_type: physicalsensor, chemicaldata')

    def sample_filters(p):
        g = p.add_argument_group('sample filters')
        g.add_argument('--geochem-only', action='store_true',
                       help='only the variables that have geochemdata datastreams (48 analytes)')
        g.add_argument('--variables', nargs='+', metavar='TERM')
        g.add_argument('--sites', nargs='+', metavar='CODE')

    def time_args(p):
        p.add_argument('--start', metavar='YYYY-MM-DD[ HH:MM]', help='keep rows at/after this time (UTC for datastreams/samples)')
        p.add_argument('--end', metavar='YYYY-MM-DD[ HH:MM]', help='keep rows before this time')

    s = sub.add_parser('summary', help='write a text summary').add_subparsers(dest='source', required=True)
    p = s.add_parser('feeder', help='feeder tables: sizes, time ranges, columns, column stats, sample rows')
    selection(p)
    p.add_argument('--rows', type=int, default=15, help='sample rows per table (0 = none; default 15)')
    p.add_argument('--sample-tables', type=int, default=0, metavar='K', help='sample only K tables, evenly spaced (default all)')
    p.add_argument('--no-real-rows', action='store_true', help='skip the "rows with readings" samples')
    p.add_argument('--no-column-stats', action='store_true', help='skip per-column NaN/min/max statistics')
    p.add_argument('-o', '--output', help='output text file (default stdout)')
    p.set_defaults(fn=cmd_summary_feeder)

    p = s.add_parser('datastreams', help='datastream tables with registry metadata (e.g. --family geochem)')
    selection(p)
    ds_filters(p)
    p.add_argument('--rows', type=int, default=15, help='sample rows (default 15)')
    p.add_argument('--sample-by', choices=['variable', 'site', 'table', 'none'], default='variable')
    p.add_argument('--sample-tables', type=int, default=0, metavar='K', help='with --sample-by table: only K tables')
    p.add_argument('--list-tables', action='store_true', help='include a per-table listing')
    p.add_argument('-o', '--output')
    p.set_defaults(fn=cmd_summary_datastreams)

    p = s.add_parser('samples', help='odmx specimen results (original geochem, with SD and censor codes)')
    sample_filters(p)
    p.add_argument('--rows', type=int, default=15, help='sample rows per variable (default 15)')
    p.add_argument('-o', '--output')
    p.set_defaults(fn=cmd_summary_samples, start=None, end=None)

    e = sub.add_parser('export', help='export data to TSV').add_subparsers(dest='source', required=True)
    p = e.add_parser('feeder', help='feeder tables')
    selection(p)
    time_args(p)
    p.add_argument('--layout', choices=['tables', 'long'], default='tables',
                   help='tables: one TSV per table in --out DIR (+ _manifest.tsv); '
                        'long: one TSV of table, index, timestamp, column, value (NaN/NULL dropped)')
    p.add_argument('--drop-empty-rows', action='store_true', help='tables layout: skip rows with no reading')
    p.add_argument('--out', help='directory (tables) or file (long; .gz ok)')
    p.set_defaults(fn=cmd_export_feeder)

    p = e.add_parser('datastreams', help='datastream tables as one long TSV with site/variable/units/depth')
    selection(p)
    ds_filters(p)
    time_args(p)
    p.add_argument('--drop-nan', action='store_true', help='skip NaN/NULL values')
    p.add_argument('--fill', choices=['keep', 'blank', 'drop'], default='keep',
                   help='-9999 fill codes: keep as stored, blank (empty value) or drop the row')
    p.add_argument('--out', help='output file (.gz ok)')
    p.set_defaults(fn=cmd_export_datastreams)

    p = e.add_parser('samples', help='odmx specimen results, one row per value with SD and censor codes')
    sample_filters(p)
    time_args(p)
    p.add_argument('--nondetect', choices=['keep', 'blank'], default='keep',
                   help='keep: value as stored (0); blank: empty value when censor_code = nonDetect')
    p.add_argument('--fill', choices=['keep', 'blank'], default='keep',
                   help='keep: -9999 fill codes as stored; blank: write -9999 value/sd as empty')
    p.add_argument('--out', help='output file (.gz ok)')
    p.set_defaults(fn=cmd_export_samples)

    args = ap.parse_args()
    for a in ('family', 'table', 'pattern', 'variables', 'sites', 'type', 'geochem_only'):
        if not hasattr(args, a):
            setattr(args, a, None)
    check_server()
    args.fn(args)


if __name__ == '__main__':
    main()

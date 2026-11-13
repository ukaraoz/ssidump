#!/usr/bin/env python3
"""Export every table of chosen schemas to gzipped CSV, one file per table, with a manifest.

Needs the server running (`ssidump.py start`). Python 3 standard library only; uses psql.

    ssiexportall.py --schema odmx --schema knowledge --schema feeder --schema datastreams \\
                    --exclude 'datastreams:^sentinel_' --out exports/full_2026-10-26 -j 6

Writes  OUT/<schema>/<table>.csv.gz   (header row; NULL = empty field, empty string = "",
                                       NaN = NaN, geometry = hex WKB)
        OUT/<schema>/_tables.tsv       table, rows, bytes, sha256
        OUT/manifest.json              command, database, dump, per-schema totals, script sha256

Only tables are exported (views are derived from them). --exclude SCHEMA:REGEX (repeatable)
skips tables of that schema whose name matches the Python regex; skipped tables are listed
in the manifest. --first SCHEMA:REGEX (repeatable) exports matching tables of that schema
before the others. Schemas are exported in the order given. Rerunning skips tables whose
file already exists, so an interrupted run can be resumed; --overwrite redoes them.
"""
import argparse
import concurrent.futures as cf
import datetime
import hashlib
import json
import os
import re
import shlex
import socket
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ssiexport as sx  # noqa: E402  (psql_cmd, q, check_server)


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for b in iter(lambda: fh.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


class Stuck(RuntimeError):
    pass


def export_one(schema, table, out_dir, tmp_dir, idx, level, timeout):
    """COPY one table through gzip into a temp file, then move it into place. Returns rows."""
    final = out_dir / f'{table}.csv.gz'
    tmp = tmp_dir / f'{idx}.csv.gz'
    meta = f'\\copy {sx.ident(schema)}.{sx.ident(table)} to program \'gzip -{level} > {tmp}\' with (format csv, header)'
    # without -q psql prints the "COPY <rows>" status line, which gives the row count
    try:
        p = subprocess.run([a for a in sx.psql_cmd('-c', meta) if a != '-q'], capture_output=True, text=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        tmp.unlink(missing_ok=True)
        raise Stuck(f'{schema}.{table}: no result after {timeout} s (storage hang?)')
    m = re.search(r'COPY (\d+)', p.stdout)
    if p.returncode or not m:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f'{schema}.{table}: {p.stderr.strip() or p.stdout.strip()}')
    os.replace(tmp, final)
    return int(m.group(1))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0], epilog=__doc__.split('\n', 2)[2],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--schema', action='append', required=True, help='schema to export (repeatable)')
    ap.add_argument('--out', required=True, help='output directory')
    ap.add_argument('-j', '--jobs', type=int, default=4, help='tables exported in parallel (default 4)')
    ap.add_argument('--level', type=int, default=6, help='gzip level 1-9 (default 6)')
    ap.add_argument('--overwrite', action='store_true', help='redo tables whose file already exists')
    ap.add_argument('--table-timeout', type=int, default=1800, help='give up on a table after this many seconds (default 1800)')
    ap.add_argument('--max-stuck', type=int, default=8,
                    help='stop the run after this many tables time out (default 8); rerun later to resume')
    ap.add_argument('--first', action='append', default=[], metavar='SCHEMA:REGEX',
                    help="export tables matching REGEX in SCHEMA before the rest, e.g. 'datastreams:^influxdb_'")
    ap.add_argument('--exclude', action='append', default=[], metavar='SCHEMA:REGEX',
                    help="skip tables matching REGEX in SCHEMA, e.g. 'datastreams:^sentinel_'")
    args = ap.parse_args()

    sx.check_server()
    out = Path(args.out).resolve()
    tmp_dir = out / '_tmp'
    tmp_dir.mkdir(parents=True, exist_ok=True)
    started = datetime.datetime.now(datetime.timezone.utc)
    totals, stuck_total, aborted = {}, [0], [False]
    for schema in args.schema:
        if aborted[0]:
            break
        tables = [r['relname'] for r in sx.q(
            f"select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace "
            f"where n.nspname = {sx.lit(schema)} and c.relkind in ('r', 'p') order by c.relname")]
        rx = [re.compile(e.split(':', 1)[1]) for e in args.exclude if e.split(':', 1)[0] == schema]
        skipped = [t for t in tables if any(r.search(t) for r in rx)]
        tables = [t for t in tables if t not in set(skipped)]
        if skipped:
            log(f'{schema}: skipping {len(skipped):,} tables matching ' + ', '.join(r.pattern for r in rx))
        if not tables:
            log(f'{schema}: no tables'); continue
        sdir = out / schema
        sdir.mkdir(parents=True, exist_ok=True)
        index_path = sdir / '_tables.tsv'
        done = {}
        if index_path.exists() and not args.overwrite:
            for line in open(index_path):
                t, n, b, h = line.rstrip('\n').split('\t')
                if t != 'table' and (sdir / f'{t}.csv.gz').exists():
                    done[t] = (int(n), int(b), h)
        todo = [t for t in tables if t not in done]
        frx = [re.compile(e.split(':', 1)[1]) for e in args.first if e.split(':', 1)[0] == schema]
        if frx:   # stable: matching tables first, each group still in name order
            todo.sort(key=lambda x: not any(r.search(x) for r in frx))
            log(f'{schema}: {sum(1 for x in todo if any(r.search(x) for r in frx)):,} tables matching '
                + ', '.join(r.pattern for r in frx) + ' go first')
        log(f'{schema}: {len(tables):,} tables, {len(done):,} already done, exporting {len(todo):,} with {args.jobs} jobs')
        t0, failed = datetime.datetime.now(), []
        with cf.ThreadPoolExecutor(args.jobs) as pool:
            futs = {pool.submit(export_one, schema, t, sdir, tmp_dir, f'{schema}_{i}', args.level, args.table_timeout): t
                    for i, t in enumerate(todo)}
            for k, f in enumerate(cf.as_completed(futs), 1):
                t = futs[f]
                try:
                    n = f.result()
                    path = sdir / f'{t}.csv.gz'
                    done[t] = (n, path.stat().st_size, sha256(path))
                except cf.CancelledError:
                    continue
                except Stuck as e:
                    failed.append(str(e)); stuck_total[0] += 1
                    log(f'  STUCK {e}')
                    if stuck_total[0] >= args.max_stuck and not aborted[0]:
                        aborted[0] = True
                        log(f'  {stuck_total[0]} tables timed out; stopping. Rerun the same command later to resume.')
                        for other in futs:
                            other.cancel()
                except Exception as e:  # keep going; report at the end
                    failed.append(str(e))
                if k % 500 == 0 or k == len(todo):
                    el = (datetime.datetime.now() - t0).total_seconds()
                    log(f'  {schema}: {k:,}/{len(todo):,} tables in {el / 60:.1f} min')
                    with open(index_path, 'w') as fh:   # checkpoint so a rerun can resume
                        fh.write('table\trows\tbytes\tsha256\n')
                        fh.writelines(f'{x}\t{n}\t{b}\t{h}\n' for x, (n, b, h) in sorted(done.items()))
        with open(index_path, 'w') as fh:
            fh.write('table\trows\tbytes\tsha256\n')
            fh.writelines(f'{x}\t{n}\t{b}\t{h}\n' for x, (n, b, h) in sorted(done.items()))
        totals[schema] = {'tables': len(tables), 'exported': len(done), 'failed': failed,
                          'excluded_patterns': [r.pattern for r in rx], 'excluded_tables': len(skipped),
                          'rows': sum(v[0] for v in done.values()), 'bytes': sum(v[1] for v in done.values())}
        log(f'{schema}: {totals[schema]["rows"]:,} rows, {totals[schema]["bytes"] / 1e9:.2f} GB, {len(failed)} failed')
        for e in failed:
            log(f'  FAILED {e}')
    try:
        tmp_dir.rmdir()
    except OSError:
        pass
    dumps = sorted(Path(sx.ROOT).glob('ssi_*.dump'))
    manifest_path = out / 'manifest.json'
    old = json.load(open(manifest_path)) if manifest_path.exists() else {}
    old_schemas = old.get('schemas', {})
    old_schemas.update(totals)
    json.dump({
        'tool': 'ssiexportall.py', 'tool_sha256': sha256(Path(__file__).resolve()),
        'command': shlex.join([Path(sys.argv[0]).name] + sys.argv[1:]),
        'started_utc': started.strftime('%Y-%m-%d %H:%M:%S'),
        'finished_utc': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
        'host': socket.gethostname(), 'database': sx.DB,
        'dump': dumps[-1].name if dumps else None,
        'format': 'one gzip CSV per table with header; NULL = empty field, empty string = "", NaN = NaN, '
                  'geometry = hex WKB; per-schema _tables.tsv has rows, bytes and sha256 per file',
        'schemas': old_schemas,
    }, open(manifest_path, 'w'), indent=2)
    log(f'manifest: {manifest_path}')
    if aborted[0] or any(v['failed'] for v in totals.values()):
        sys.exit(1)


if __name__ == '__main__':
    main()

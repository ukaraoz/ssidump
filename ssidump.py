#!/usr/bin/env python3
"""Restore and explore SSI / ODMX PostgreSQL dumps (odmx_lbnlsfa) on NERSC.

Everything lives under ROOT (default: the directory holding this script):

    ROOT/pgenv/     conda env with PostgreSQL 17 + PostGIS
    ROOT/pgdata/    database cluster
    ROOT/pgrun/     Unix socket directory (no TCP listener)
    ROOT/logs/      restore logs

Typical use:

    ssidump.py setup                      # once: conda env + cluster
    ssidump.py restore --replace          # restore the newest ssi_*.dump in ROOT
    ssidump.py export-csv --schema odmx   # dump tables straight to CSV, no server needed
    ssidump.py start | stop | status
    ssidump.py psql                       # interactive shell on the database

The server should run on one node at a time; `start` refuses to run if the
cluster looks like it is already running on another host (override with --force).
"""
import argparse
import csv
import datetime
import os
import re
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ.get('SSIDUMP_ROOT', Path(__file__).resolve().parent))
PORT = int(os.environ.get('SSIDUMP_PORT', 54329))
USER = 'ssi'
DB = 'odmx_lbnlsfa'
CONDA = os.environ.get('SSIDUMP_CONDA', '/global/homes/u/ukaraoz/bin/miniconda3/bin/conda')
PG_SPEC = 'postgresql=17'

ENV = ROOT / 'pgenv'
DATA = ROOT / 'pgdata'
RUN = ROOT / 'pgrun'
LOGS = ROOT / 'logs'
HOST_FILE = DATA / 'ssidump_host'

SERVER_CONF = f"""
# --- added by ssidump.py ---
listen_addresses = ''
port = {PORT}
unix_socket_directories = '{RUN}'
shared_buffers = 4GB
maintenance_work_mem = 2GB
max_wal_size = 16GB
max_locks_per_transaction = 1024
"""

# Durability settings switched off only for the duration of a restore.
FAST_LOAD = {'fsync': 'off', 'full_page_writes': 'off', 'synchronous_commit': 'off'}


def die(msg):
    sys.exit(f'ssidump: {msg}')


def pgbin(name):
    exe = ENV / 'bin' / name
    if not exe.exists():
        die(f'{exe} not found; run `ssidump.py setup` first')
    return str(exe)


def run(cmd, **kw):
    print('+', ' '.join(str(c) for c in cmd), flush=True)
    return subprocess.run([str(c) for c in cmd], check=True, **kw)


def conn_args(db=DB):
    return ['-h', RUN, '-p', PORT, '-U', USER, '-d', db]


def sql(query, db='postgres'):
    """Run one statement and return stripped stdout."""
    out = subprocess.run([pgbin('psql'), *map(str, conn_args(db)), '-v', 'ON_ERROR_STOP=1',
                          '-Atqc', query], check=True, capture_output=True, text=True)
    return out.stdout.strip()


def db_exists(name):
    return sql(f"select 1 from pg_database where datname = '{name}'") == '1'


def newest_dump():
    dumps = sorted(ROOT.glob('ssi_*.dump'))
    if not dumps:
        die(f'no ssi_*.dump files in {ROOT}; pass the dump path explicitly')
    return dumps[-1]


# ---------------------------------------------------------------- server

def is_running():
    return subprocess.run([pgbin('pg_ctl'), '-D', DATA, 'status'],
                          capture_output=True).returncode == 0


def cmd_setup(args):
    if not ENV.exists():
        run([CONDA, 'create', '-y', '-q', '-p', ENV, '-c', 'conda-forge',
             '--override-channels', PG_SPEC, 'postgis'])
    else:
        print(f'{ENV} exists, skipping conda env')
    if not (DATA / 'PG_VERSION').exists():
        run([pgbin('initdb'), '-D', DATA, '-U', USER, '--auth=trust', '-E', 'UTF8'],
            stdout=subprocess.DEVNULL)
        with open(DATA / 'postgresql.conf', 'a') as f:
            f.write(SERVER_CONF)
    else:
        print(f'{DATA} exists, skipping initdb')
    RUN.mkdir(exist_ok=True)
    LOGS.mkdir(exist_ok=True)


def cmd_start(args=None):
    host = socket.gethostname()
    if is_running():
        print('server already running')
        return
    if (DATA / 'postmaster.pid').exists() and HOST_FILE.exists():
        owner = HOST_FILE.read_text().strip()
        if owner != host and not getattr(args, 'force', False):
            die(f'postmaster.pid exists and the server was last started on {owner}. '
                f'Stop it there first, or use --force if that node is gone.')
    RUN.mkdir(exist_ok=True)
    run([pgbin('pg_ctl'), '-D', DATA, '-l', DATA / 'server.log', '-w', 'start'])
    HOST_FILE.write_text(host + '\n')


def cmd_stop(args=None):
    if not is_running():
        print('server not running')
        return
    run([pgbin('pg_ctl'), '-D', DATA, '-m', 'fast', '-w', 'stop'])
    HOST_FILE.unlink(missing_ok=True)


def cmd_status(args):
    host = HOST_FILE.read_text().strip() if HOST_FILE.exists() else '?'
    if not is_running():
        print(f'server not running on {socket.gethostname()} (last host: {host})')
        return
    print(f'server running on {host}, socket {RUN}, port {PORT}')
    print(sql("select datname, pg_size_pretty(pg_database_size(datname)), "
              "coalesce(shobj_description(oid, 'pg_database'), '') "
              "from pg_database where not datistemplate and datname <> 'postgres'"))


def cmd_psql(args):
    os.execv(pgbin('psql'), ['psql', *map(str, conn_args(args.db)), *args.psql_args])


# ---------------------------------------------------------------- restore

def set_fast_load(on):
    for key, val in FAST_LOAD.items():
        sql(f'alter system set {key} = {val}' if on else f'alter system reset {key}')
    sql('select pg_reload_conf()')
    if not on:
        sql('checkpoint')


def only_cross_schema_fk_errors(log):
    """True if every pg_restore error is a foreign key into a schema left out of the restore."""
    blocks = re.split(r'(?m)^(?=pg_restore: (?:error|warning):)', log.read_text())
    errors = [b for b in blocks if b.startswith('pg_restore: error:')]
    return bool(errors) and all('does not exist' in b and 'FOREIGN KEY' in b for b in errors)


def cmd_restore(args):
    dump = Path(args.dump) if args.dump else newest_dump()
    if not dump.exists():
        die(f'{dump} not found')
    cmd_start(args)
    target, tmp = args.db, f'{args.db}__restoring'
    if db_exists(target) and not args.replace:
        die(f'database {target} already exists; pass --replace to swap it out '
            f'after the new restore succeeds, or --db to restore under another name')

    LOGS.mkdir(exist_ok=True)
    log = LOGS / f'restore_{dump.stem}_{datetime.datetime.now():%Y%m%dT%H%M%S}.log'
    print(f'restoring {dump} -> {target} (log: {log})')

    if db_exists(tmp):  # leftover from an interrupted run
        sql(f'drop database "{tmp}" with (force)')
    sql(f'create database "{tmp}"')

    cmd = [pgbin('pg_restore'), *conn_args(tmp), '--no-owner', '--no-acl', '-j', args.jobs]
    if args.schema:
        # pg_restore -n restores objects in a schema but not the schema itself
        # (or the extensions its tables use), so create those first.
        sql('create extension if not exists postgis', db=tmp)
        for s in args.schema:
            sql(f'create schema "{s}"', db=tmp)
            cmd += ['-n', s]
    cmd.append(dump)

    set_fast_load(True)
    try:
        with open(log, 'w') as f:
            rc = subprocess.run([str(c) for c in cmd], stdout=f, stderr=subprocess.STDOUT).returncode
    finally:
        set_fast_load(False)
    if rc != 0 and not (args.schema and only_cross_schema_fk_errors(log)):
        die(f'pg_restore exited {rc}; see {log}. Partial restore left in database {tmp}; '
            f'{target} was not touched.')
    if rc != 0:
        print(f'warning: skipped foreign keys into schemas that were not restored; see {log}')

    run([pgbin('vacuumdb'), *conn_args(tmp), '--analyze-only', '-j', args.jobs, '-q'])
    if db_exists(target):
        sql(f'drop database "{target}" with (force)')
    sql(f'alter database "{tmp}" rename to "{target}"')
    sql(f"comment on database \"{target}\" is 'restored from {dump.name} "
        f"on {datetime.datetime.now():%Y-%m-%d %H:%M}'")
    print(sql("select table_schema, table_type, count(*) from information_schema.tables "
              "where table_schema not in ('pg_catalog', 'information_schema') "
              "group by 1, 2 order by 1, 2", db=target))
    print(f'done: {target} is {sql(f"select pg_size_pretty(pg_database_size(current_database()))", db=target)}')


# ---------------------------------------------------------------- csv export

COPY_HEADER = re.compile(r'^COPY \S+ \((.*)\) FROM stdin;$')
COPY_ESCAPE = re.compile(r'\\(x[0-9a-fA-F]{1,2}|[0-7]{1,3}|.)')
COPY_CHARS = {'b': '\b', 'f': '\f', 'n': '\n', 'r': '\r', 't': '\t', 'v': '\v', '\\': '\\'}


def copy_unescape(s):
    def rep(m):
        g = m.group(1)
        if g[0] == 'x' and len(g) > 1:
            return chr(int(g[1:], 16))
        if g[0] in '01234567':
            return chr(int(g, 8))
        return COPY_CHARS.get(g, g)
    return COPY_ESCAPE.sub(rep, s)


def copy_to_csv(lines, out_path):
    """Write the COPY block from pg_restore text output to CSV; return the row count.

    NULL becomes an empty field, so NULL and '' are indistinguishable in the CSV.
    """
    rows = 0
    with open(out_path, 'w', newline='') as f:
        w = csv.writer(f)
        in_copy = False
        for line in lines:
            line = line.rstrip('\n')
            if not in_copy:
                m = COPY_HEADER.match(line)
                if m:
                    w.writerow([c.strip().strip('"') for c in m.group(1).split(',')])
                    in_copy = True
                continue
            if line == '\\.':
                break
            w.writerow(['' if v == '\\N' else copy_unescape(v) for v in line.split('\t')])
            rows += 1
    return rows


def dump_tables(dump, schema):
    toc = subprocess.run([pgbin('pg_restore'), '-l', str(dump)],
                         check=True, capture_output=True, text=True).stdout
    # e.g. "144801; 0 506123 TABLE DATA odmx results ssi"
    return [p[6] for p in (l.split() for l in toc.splitlines())
            if len(p) >= 7 and p[3:6] == ['TABLE', 'DATA', schema]]


def cmd_export_csv(args):
    dump = Path(args.dump) if args.dump else newest_dump()
    out = Path(args.out) if args.out else ROOT / f'{args.schema}_csv'
    out.mkdir(parents=True, exist_ok=True)
    tables = dump_tables(dump, args.schema)
    if args.table:
        missing = set(args.table) - set(tables)
        if missing:
            die(f'not in schema {args.schema}: {", ".join(sorted(missing))}')
        tables = args.table
    if not tables:
        die(f'no tables with data in schema {args.schema}')
    print(f'exporting {len(tables)} tables from {dump.name} to {out}')
    counts = []
    for t in tables:
        p = subprocess.Popen([pgbin('pg_restore'), '-a', '-n', args.schema, '-t', t, '-f', '-',
                              str(dump)], stdout=subprocess.PIPE, text=True)
        n = copy_to_csv(p.stdout, out / f'{t}.csv')
        p.stdout.close()
        if p.wait() != 0:
            die(f'pg_restore failed on {args.schema}.{t}')
        counts.append((t, n))
    with open(out / '_rowcounts.tsv', 'a' if args.table else 'w') as f:
        f.writelines(f'{t}\t{n}\n' for t, n in counts)
    print(f'done: {sum(n for _, n in counts)} rows; counts in {out / "_rowcounts.tsv"}')


# ---------------------------------------------------------------- cli

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)

    sub.add_parser('setup', help='create the conda env and database cluster').set_defaults(fn=cmd_setup)
    p = sub.add_parser('start', help='start the server on this node')
    p.add_argument('--force', action='store_true', help='start even if it was last run on another host')
    p.set_defaults(fn=cmd_start)
    sub.add_parser('stop', help='stop the server').set_defaults(fn=cmd_stop)
    sub.add_parser('status', help='show server and database status').set_defaults(fn=cmd_status)

    p = sub.add_parser('psql', help='open psql (extra args are passed through)')
    p.add_argument('--db', default=DB)
    p.add_argument('psql_args', nargs=argparse.REMAINDER)
    p.set_defaults(fn=cmd_psql)

    p = sub.add_parser('restore', help='restore a dump into the local server')
    p.add_argument('dump', nargs='?', help='dump file (default: newest ssi_*.dump in ROOT)')
    p.add_argument('--db', default=DB, help=f'target database (default {DB})')
    p.add_argument('--replace', action='store_true', help='replace an existing target database')
    p.add_argument('--schema', action='append', help='restore only this schema (repeatable)')
    p.add_argument('-j', '--jobs', default='4', help='parallel restore jobs (default 4)')
    p.add_argument('--force', action='store_true', help=argparse.SUPPRESS)
    p.set_defaults(fn=cmd_restore)

    p = sub.add_parser('export-csv', help='export tables from a dump to CSV without a server')
    p.add_argument('dump', nargs='?', help='dump file (default: newest ssi_*.dump in ROOT)')
    p.add_argument('--schema', default='odmx', help='schema to export (default odmx)')
    p.add_argument('--table', action='append', help='export only this table (repeatable)')
    p.add_argument('--out', help='output directory (default ROOT/<schema>_csv)')
    p.set_defaults(fn=cmd_export_csv)

    args, extra = ap.parse_known_args()
    if extra and args.cmd != 'psql':
        ap.error(f'unrecognized arguments: {" ".join(extra)}')
    if args.cmd == 'psql':
        args.psql_args = extra + args.psql_args
    args.fn(args)


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Convert SSI datastreams to an ESS-DIVE Sensor Time Series - Full package.

Reads either a table export written by ssiexportall (--export exports/tables_<date>; no server
needed) or the live database (server running: `ssidump start`). Python 3 standard library only.

    ssi2essdive sensor-full --export exports/tables_2026-10-26 \\
        --owner Wielandt --data-type soilTemperature --sensor-type "TMP 117" \\
        --prefix snod1_soil_temperature --title "SNOD1 soil temperature" \\
        --config configs/snod1_soil_temperature.json -o exports/essdive/stijn_wielandt/snod1_soil_temperature

    ssi2essdive validate exports/essdive/stijn_wielandt/influxdb_SNOD1_soil_nodes

Selection options are those of ssiselect (--owner, --data-type, --sensor-type, --site, --role).
--start/--end (UTC) limit the time window. --config is an optional JSON file:

    {"methods": {"<method_id>": {"description": "...", "lower_bound": -40, "upper_bound": 60,
                                 "instrument_precision": "0.1"}}}

The package directory gets one data file and data dictionary per logger, a methods and attributes
file, a locations file and a file-level metadata (FLMD) file, then is validated. A
<prefix>_manifest.json beside the package records the command, source, selection, checksums and
the problems found by essdive.checks (it is not part of the ESS-DIVE package).
"""
import argparse
import datetime
import hashlib
import json
import re
import shlex
import socket
import sys
from pathlib import Path

from . import export as sx
from . import selection as sel
from .essdive import checks, metadata, ssi_source, validate, writers, __version__


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


def cmd_sensor_full(args):
    selection = {k: getattr(args, k) for k in ('owner', 'data_type', 'sensor_type', 'site', 'role') if getattr(args, k)}
    config = json.load(open(args.config)) if args.config else {}
    if args.export:
        exp = Path(args.export)
        if not (exp / 'odmx' / '_tables.tsv').exists():
            sys.exit(f'ssi2essdive: {exp} is not an ssiexportall table export (no odmx/_tables.tsv)')
        print(f'reading datastream catalog from {exp} ...', file=sys.stderr)
        rows = sel.select(sel.load_catalog(export=exp), selection)
        source = {'export': str(exp)}
        for f in ('manifest.json', 'DOWNLOAD.json'):
            if (exp / f).exists():
                source[f] = {'sha256': sha256(exp / f)}
                if f == 'manifest.json':
                    m = json.load(open(exp / f))
                    source['dump'] = m.get('dump')
                    source['database'] = m.get('database')
    else:
        sx.check_server()
        print('reading datastream catalog from the database ...', file=sys.stderr)
        rows = sel.select(sel.load_catalog(), selection)
        source = {'database': sx.DB}
    if not rows:
        sys.exit('ssi2essdive: no datastreams match the selection')
    tables = [r['datastream_table'] for r in rows]
    codes = sorted({r['site_code'] for r in rows})
    print(f'{len(rows)} datastreams; building package ...', file=sys.stderr)
    if args.export:
        fetch, qcodes, sites = (ssi_source.export_fetch(exp), ssi_source.export_quality_codes(exp, tables),
                                ssi_source.export_sites(exp, codes))
    else:
        fetch, qcodes, sites = ssi_source.db_fetch(sx), ssi_source.db_quality_codes(sx, tables), ssi_source.db_sites(sx, codes)
    pkg = ssi_source.build_package(
        rows, prefix=args.prefix, title=args.title or args.prefix.replace('_', ' '),
        fetch=fetch, quality_codes=qcodes, sites=sites, start=args.start, end=args.end, config=config,
        column_naming=args.column_names, file_naming=args.file_names, split_variables=args.split_variables)
    out = Path(args.out)
    if out.exists() and any(out.iterdir()) and not args.overwrite:
        sys.exit(f'ssi2essdive: {out} is not empty (use --overwrite)')
    res = writers.write_package(pkg, out, log=lambda m: print(m, file=sys.stderr))
    rep = validate.validate_package(out)
    print(rep.summary(), file=sys.stderr)
    print('checking the package for problems ...', file=sys.stderr)
    cells = {} if args.problem_flags else None
    problems = checks.run(pkg, out, rows, sites=sites, validation=rep, config=config, cells=cells)
    problem_flags = None
    if args.problem_flags:
        problem_flags = checks.apply_problem_flags(pkg, out, cells)
        print('problem flags added: ' + (', '.join(f'{k} {v:,}' for k, v in problem_flags.items()) or 'none'),
              file=sys.stderr)
        rep = validate.validate_package(out)
        print('after adding problem flags: ' + rep.summary().splitlines()[0], file=sys.stderr)
        res['flags_used'] = sorted(set(res['flags_used']) | set(problem_flags))
    sev = {k: sum(p['severity'] == k for p in problems) for k in ('error', 'warning', 'info')}
    print(f"problems: {sev['error']} error, {sev['warning']} warning, {sev['info']} info", file=sys.stderr)
    record = {
        'tool': f'ssi2essdive (essdive {__version__})', 'tool_sha256': sha256(Path(__file__).resolve()),
        'command': shlex.join([Path(sys.argv[0]).name] + sys.argv[1:]),
        'created_utc': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
        'host': socket.gethostname(), 'source': source, 'selection': selection,
        'start': args.start, 'end': args.end, 'config': config, 'column_names': args.column_names, 'file_names': args.file_names, 'split_variables': args.split_variables,
        'reporting_format': 'ESS-DIVE Sensor Time Series - Full v1.0.0 + Location Metadata v2.0.0',
        'datastreams': len(rows), 'rows': res['rows'], 'flags_used': res['flags_used'],
        'validation': {'errors': rep.errors, 'warnings': rep.warnings},
        'files': {f: {'bytes': (out / f).stat().st_size, 'sha256': sha256(out / f)} for f in res['files']},
        'source_tables': sorted(tables),
        'problem_flags': problem_flags,
        'problems_summary': sev,
        'problems': problems,
    }
    rec_path = out.parent / f'{args.prefix}_manifest.json'
    json.dump(record, open(rec_path, 'w'), indent=2)
    print(f'wrote {len(res["files"])} files to {out}\nmanifest (with problems): {rec_path}', file=sys.stderr)
    sys.exit(0 if rep.ok else 1)


def cmd_metadata(args):
    """One metadata row per selected datastream table (see essdive/metadata.py), from a table export."""
    selection = {k: getattr(args, k) for k in ('owner', 'data_type', 'sensor_type', 'site', 'role') if getattr(args, k)}
    exp = Path(args.export)
    rows = sel.select(sel.load_catalog(export=exp), selection)
    if not rows:
        sys.exit('ssi2essdive: no datastreams match the selection')
    key = lambda r: [int(x) if x.isdigit() else x.lower() for x in re.split(r'(\d+)', r['datastream_table'])]  # noqa: E731
    rows = sorted(rows, key=key)
    db = metadata.Odmx(exp)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    header, all_rows = None, []
    for r in rows:
        h, v = metadata.table_metadata(db, r['datastream_table'], r['feeder_column'])
        if header is None:
            header = h
        elif h != header:
            sys.exit(f'ssi2essdive: metadata columns differ for {r["datastream_table"]}')
        all_rows.append(v)
        if not args.combined_only:
            metadata.write_tsv(out / f'{r["datastream_table"]}_metadata.tsv', h, [v])
    combined = out / f'{args.name}.tsv'
    metadata.write_tsv(combined, header, all_rows)
    print(f'{len(all_rows)} tables, {len(header)} columns: {combined}'
          + ('' if args.combined_only else f' and one <table>_metadata.tsv per table in {out}'), file=sys.stderr)


def cmd_validate(args):
    rep = validate.validate_package(args.dir)
    print(rep.summary(limit=args.limit))
    sys.exit(0 if rep.ok else 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0],
                                 epilog=__doc__.split('\n', 2)[2], formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('sensor-full', help='write a Sensor Time Series - Full package')
    p.add_argument('--export', help='read this ssiexportall table export (exports/tables_<date>) instead of the database')
    p.add_argument('--owner', action='append')
    p.add_argument('--data-type', dest='data_type', action='append')
    p.add_argument('--sensor-type', dest='sensor_type', action='append')
    p.add_argument('--site', action='append')
    p.add_argument('--role')
    p.add_argument('--start', help='first UTC time to include, e.g. 2023-01-01')
    p.add_argument('--end', help='UTC time to stop before')
    p.add_argument('--prefix', required=True, help='file name stem, e.g. snod1_soil_temperature')
    p.add_argument('--title', help='short human-readable name used in file descriptions')
    p.add_argument('--config', help='JSON with method descriptions and bounds')
    p.add_argument('--problem-flags', dest='problem_flags', action='store_true',
                   help='also mark cells the checks find suspect with extra flags in the _flag column '
                        '(e.g. "qa_z; suspect_shifted_column"); values are never changed')
    p.add_argument('--file-names', dest='file_names', choices=ssi_source.FILE_NAMING, default='prefix-site',
                   help='data file names: <prefix>_<site code> (default) or <feeder table>_<site code>')
    p.add_argument('--split-variables', dest='split_variables', action='store_true',
                   help='one data file per logger and variable (variable added to the file name)')
    p.add_argument('--column-names', dest='column_names', choices=ssi_source.COLUMN_NAMING, default='variable-sensor',
                   help='measurement column names: <variable>_<sensor> (default) or <datastream table>_data_value')
    p.add_argument('-o', '--out', required=True, help='package directory')
    p.add_argument('--overwrite', action='store_true')
    p.set_defaults(fn=cmd_sensor_full)
    p = sub.add_parser('metadata', help='write the database metadata of the selected datastream tables as TSV')
    p.add_argument('--export', required=True, help='ssiexportall table export (exports/tables_<date>)')
    p.add_argument('--owner', action='append')
    p.add_argument('--data-type', dest='data_type', action='append')
    p.add_argument('--sensor-type', dest='sensor_type', action='append')
    p.add_argument('--site', action='append')
    p.add_argument('--role')
    p.add_argument('--name', default='source_tables_metadata',
                   help='file name of the combined TSV (default source_tables_metadata)')
    p.add_argument('--combined-only', dest='combined_only', action='store_true', help='skip the per-table files')
    p.add_argument('-o', '--out', required=True, help='output directory, e.g. <package>/source_tables_metadata')
    p.set_defaults(fn=cmd_metadata)
    p = sub.add_parser('validate', help='check a package directory')
    p.add_argument('dir')
    p.add_argument('--limit', type=int, default=50)
    p.set_defaults(fn=cmd_validate)
    args = ap.parse_args()
    args.fn(args)


if __name__ == '__main__':
    main()

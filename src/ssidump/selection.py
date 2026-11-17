#!/usr/bin/env python3
"""Select datastreams by equipment owner, data type and sensor type, and export them.

Queries the odmx_lbnlsfa database directly (start the server first: `ssidump start`).
Python 3 standard library only; uses ssiexport's psql helpers.

Every datastream is resolved to the equipment that made it:

  datastream ── equipment_id ──► logger   (the device the datastream is registered to,
                                            e.g. a SNOD1 node or a PhenoCam)
  logger ◄── isAttachedTo ── sensor        (a device attached to the logger whose
                                            equipment_name is the datastream's feeder
                                            column, e.g. probe "Temp05")

If no attached device matches, the logger itself is the sensor. Owners come from
odmx.equipment_persons_bridge, for the sensor and for the logger.

Selection (each option repeatable; OR within an option, AND between options):

  --owner       person_id, or text matched against "first last" (case-insensitive substring)
  --data-type   variable term or name, exact (case-insensitive) or glob,
                e.g. soilTemperature, "Soil Temperature", 'soil*'
  --sensor-type sensor model name / part number / equipment code / equipment type,
                substring or glob, e.g. "TMP 117", 'LBNL*'
  --site        site code or name (substring or glob), e.g. SG-DTP26
  --role        owner role to match (default: owner; "any" for all roles)

Commands:

  ssiselect options  [selection]           what owners, data types, sensor types and sites
                                              the current selection still contains
  ssiselect catalog  [selection] -o cat.tsv one row per matching datastream with all metadata
  ssiselect hierarchy [selection] -o h.json site -> device -> attached sensors -> datastreams,
                                              with a data summary per site
  ssiselect feeder   [selection] -o raw.tsv.gz the raw feeder rows behind the selection: one row per
                                              feeder row, one column per selected feeder column,
                                              plus device and site metadata
  ssiselect export   [selection] -o data.tsv.gz
                                              one row per measurement, joined to the catalog;
                                              also writes data.catalog.tsv and data.manifest.json

  ssiselect export --config data.manifest.json -o again.tsv.gz    # rerun a saved selection

Examples:

  ssiselect options --owner Wielandt
  ssiselect catalog --owner Wielandt --data-type soilTemperature -o soil_catalog.tsv
  ssiselect export  --owner Wielandt --data-type soilTemperature --sensor-type "TMP 117" \\
                       --start 2023-01-01 --end 2024-01-01 -o soil_2023.tsv.gz
"""
import argparse
import csv
import datetime
import fnmatch
import gzip
import hashlib
import json
import re
import shlex
import socket
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

from . import export as sx  # psql helpers: q, lit, ident, stream_copy, check_server

VERSION = '1.0'
SEL_KEYS = ('owner', 'data_type', 'sensor_type', 'site', 'role')

CATALOG_COLS = [
    'datastream_id', 'datastream_table', 'datastream_type',
    'variable', 'variable_name', 'units',
    'site_code', 'site_name', 'site_type', 'latitude', 'longitude', 'site_elevation_m',
    'logger_equipment_id', 'logger_name', 'logger_model',
    'sensor_equipment_id', 'sensor_name', 'sensor_model', 'sensor_part_number', 'sensor_type',
    'sensor_manufacturer', 'sensor_z_offset_m', 'sensor_installed_utc', 'sensor_removed_utc',
    'sensor_owner', 'logger_owner',
    'feeder_table', 'feeder_column',
    'first_measurement', 'last_measurement', 'n_measurements', 'datastream_attribute',
]
DATA_COLS = ['datetime_utc', 'utc_time', 'value', 'qa_flag']


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def die(msg):
    sys.exit(f'ssiselect: {msg}')


def utc(epoch):
    if epoch in (None, ''):
        return None
    return datetime.datetime.fromtimestamp(int(float(epoch)), datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


# ---------------------------------------------------------------- catalog from the database

def load_catalog(export=None):
    """Every datastream in the datastreams schema, resolved to logger, sensor, owners and feeder column.

    Reads the database, or with export=DIR an ssiexportall table export (exports/tables_<date>/);
    from an export, only datastream tables present in DIR/datastreams are listed."""
    return _build_catalog(**(_export_tables(export) if export else _db_tables()))


def _db_tables():
    ds = sx.q("""
        select d.datastream_id, d.datastream_tablename, d.datastream_type, d.equipment_id,
               d.first_measurement_date, d.last_measurement_date, d.total_measurement_numbers,
               d.datastream_attribute::text as datastream_attribute,
               v.variable_term, v.variable_name, coalesce(u.units_abbreviation, u.term) as units,
               sf.sampling_feature_code, sf.sampling_feature_name, sf.sampling_feature_type_cv,
               sf.latitude, sf.longitude, sf.elevation_m
        from odmx.sampling_feature_timeseries_datastreams d
        left join odmx.variables v on v.variable_id = d.variable_id
        left join odmx.cv_units u on u.units_id = d.units_id
        left join odmx.sampling_features sf on sf.sampling_feature_id = d.sampling_feature_id
        where d.datastream_database = 'datastreams'""")
    eq = {r['equipment_id']: r for r in sx.q("""
        select e.equipment_id, e.equipment_code, e.equipment_name, e.equipment_description, e.equipment_serial_number,
               e.equipment_documentation_link, m.equipment_model_name,
               m.equipment_model_part_number, m.equipment_type_cv, o.organization_name as manufacturer
        from odmx.equipment e
        left join odmx.equipment_models m on m.equipment_model_id = e.equipment_model_id
        left join odmx.organizations o on o.organization_id = m.equipment_model_manufacturer_id""")}
    children = defaultdict(list)
    for r in sx.q("""select equipment_id, related_equipment_id from odmx.related_equipment
                     where relationship_type_cv = 'isAttachedTo'"""):
        children[r['related_equipment_id']].append(r['equipment_id'])
    owners = defaultdict(list)
    for r in sx.q("""select b.equipment_id, b.person_role_cv, p.person_id,
                            trim(concat_ws(' ', p.person_first_name, p.person_middle_name, p.person_last_name)) as name
                     from odmx.equipment_persons_bridge b join odmx.persons p on p.person_id = b.person_id"""):
        owners[r['equipment_id']].append(r)
    pos = {}
    for r in sx.q("""select equipment_id, position_start_date_utc, position_end_date_utc, equipment_z_offset_m
                     from odmx.equipment_position order by equipment_id, position_start_date_utc"""):
        pos[r['equipment_id']] = r          # latest position wins
    fcols = defaultdict(dict)               # feeder table -> {lower column name: column name}
    for r in sx.q("""select table_name, column_name from information_schema.columns
                     where table_schema = 'feeder'"""):
        fcols[r['table_name']][r['column_name'].lower()] = r['column_name']
    existing = {r['table_name'] for r in sx.q(
        "select table_name from information_schema.tables where table_schema = 'datastreams' and table_type = 'BASE TABLE'")}
    return dict(ds=ds, eq=eq, children=children, owners=owners, pos=pos, fcols=fcols, existing=existing)


def read_export_table(export, schema, table):
    """Rows of one exported table as dicts; NULL and empty string both become None."""
    base = Path(export) / schema / table
    path = base.with_name(table + '.csv')
    if not path.exists():
        path = base.with_name(table + '.csv.gz')
    opener = gzip.open if path.suffix == '.gz' else open
    with opener(path, 'rt', encoding='utf-8', newline='') as fh:
        for r in csv.DictReader(fh):
            yield {k: (v if v != '' else None) for k, v in r.items()}


def export_table_names(export, schema):
    """Tables listed in <schema>/_tables.tsv (written by ssiexportall) whose file is present."""
    d = Path(export) / schema
    with open(d / '_tables.tsv', encoding='utf-8') as fh:
        names = [line.split('\t', 1)[0] for line in fh.read().splitlines()[1:] if line]
    return [t for t in names if (d / f'{t}.csv').exists() or (d / f'{t}.csv.gz').exists()]


def _export_header(export, schema, table):
    base = Path(export) / schema
    path = base / f'{table}.csv'
    if path.exists():
        with open(path, encoding='utf-8', newline='') as fh:
            return next(csv.reader(fh))
    with gzip.open(base / f'{table}.csv.gz', 'rt', encoding='utf-8', newline='') as fh:
        return next(csv.reader(fh))


def _export_tables(export):
    """The same inputs as _db_tables(), joined in Python from the exported odmx and feeder CSVs."""
    t = lambda name: list(read_export_table(export, 'odmx', name))   # noqa: E731
    by = lambda rows, key: {r[key]: r for r in rows}                  # noqa: E731
    variables, units = by(t('variables'), 'variable_id'), by(t('cv_units'), 'units_id')
    sf = by(t('sampling_features'), 'sampling_feature_id')
    ds = []
    for d in t('sampling_feature_timeseries_datastreams'):
        if d['datastream_database'] != 'datastreams':
            continue
        v, u, s = variables.get(d['variable_id'], {}), units.get(d['units_id'], {}), sf.get(d['sampling_feature_id'], {})
        ds.append({**{k: d[k] for k in ('datastream_id', 'datastream_tablename', 'datastream_type', 'equipment_id',
                                        'first_measurement_date', 'last_measurement_date', 'total_measurement_numbers',
                                        'datastream_attribute')},
                   'variable_term': v.get('variable_term'), 'variable_name': v.get('variable_name'),
                   'units': u.get('units_abbreviation') or u.get('term'),
                   **{k: s.get(k) for k in ('sampling_feature_code', 'sampling_feature_name', 'sampling_feature_type_cv',
                                            'latitude', 'longitude', 'elevation_m')}})
    models, orgs = by(t('equipment_models'), 'equipment_model_id'), by(t('organizations'), 'organization_id')
    eq = {}
    for e in t('equipment'):
        m = models.get(e['equipment_model_id'], {})
        eq[e['equipment_id']] = {
            **{k: e[k] for k in ('equipment_id', 'equipment_code', 'equipment_name', 'equipment_description',
                                 'equipment_serial_number', 'equipment_documentation_link')},
            **{k: m.get(k) for k in ('equipment_model_name', 'equipment_model_part_number', 'equipment_type_cv')},
            'manufacturer': orgs.get(m.get('equipment_model_manufacturer_id'), {}).get('organization_name')}
    children = defaultdict(list)
    for r in t('related_equipment'):
        if r['relationship_type_cv'] == 'isAttachedTo':
            children[r['related_equipment_id']].append(r['equipment_id'])
    persons = by(t('persons'), 'person_id')
    owners = defaultdict(list)
    for b in t('equipment_persons_bridge'):
        p = persons.get(b['person_id'])
        if p:
            name = ' '.join(x for x in (p['person_first_name'], p['person_middle_name'], p['person_last_name']) if x)
            owners[b['equipment_id']].append({'equipment_id': b['equipment_id'], 'person_role_cv': b['person_role_cv'],
                                              'person_id': p['person_id'], 'name': name.strip()})
    pos = {}
    for r in sorted(t('equipment_position'), key=lambda r: (int(r['equipment_id']), r['position_start_date_utc'] is None,
                                                            float(r['position_start_date_utc'] or 0))):
        pos[r['equipment_id']] = r          # latest position wins
    fcols = defaultdict(dict)
    for ft in export_table_names(export, 'feeder'):
        for c in _export_header(export, 'feeder', ft):
            fcols[ft][c.lower()] = c
    existing = set(export_table_names(export, 'datastreams'))
    return dict(ds=ds, eq=eq, children=children, owners=owners, pos=pos, fcols=fcols, existing=existing)


def _build_catalog(ds, eq, children, owners, pos, fcols, existing):
    ftables = sorted(fcols, key=len, reverse=True)
    rows = []
    for d in ds:
        t = d['datastream_tablename']
        if t not in existing:
            continue
        logger = eq.get(d['equipment_id'] or '', {})
        # feeder table = longest feeder table name that prefixes the datastream table name
        ft = next((f for f in ftables if t.lower().startswith(f.lower() + '_')), None)
        fc = None
        if ft:
            rest = t[len(ft) + 1:]
            stem = rest.rsplit('_', 1)[0] if '_' in rest else rest   # drop _meas / _calc
            fc = fcols[ft].get(stem.lower()) or fcols[ft].get(rest.lower())
        sensor = logger
        if fc and d['equipment_id']:
            hits = [eq[c] for c in children.get(d['equipment_id'], [])
                    if c in eq and (eq[c]['equipment_name'] or '').lower() == fc.lower()]
            if len(hits) == 1:
                sensor = hits[0]
        sid = sensor.get('equipment_id')
        p = pos.get(sid, {})

        def who(eid):
            return '; '.join(sorted({f"{o['name']} ({o['person_role_cv']}, person_id {o['person_id']})" for o in owners.get(eid, [])}))
        rows.append({
            'datastream_id': d['datastream_id'], 'datastream_table': t, 'datastream_type': d['datastream_type'],
            'variable': d['variable_term'], 'variable_name': d['variable_name'], 'units': d['units'],
            'site_code': d['sampling_feature_code'], 'site_name': d['sampling_feature_name'],
            'site_type': d['sampling_feature_type_cv'], 'latitude': d['latitude'], 'longitude': d['longitude'],
            'site_elevation_m': d['elevation_m'],
            'logger_equipment_id': logger.get('equipment_id'), 'logger_name': logger.get('equipment_name'),
            'logger_model': logger.get('equipment_model_name'),
            'sensor_equipment_id': sid, 'sensor_name': sensor.get('equipment_name'),
            'sensor_model': sensor.get('equipment_model_name'), 'sensor_part_number': sensor.get('equipment_model_part_number'),
            'sensor_type': sensor.get('equipment_type_cv'), 'sensor_manufacturer': sensor.get('manufacturer'),
            'sensor_z_offset_m': p.get('equipment_z_offset_m'),
            'sensor_installed_utc': utc(p.get('position_start_date_utc')), 'sensor_removed_utc': utc(p.get('position_end_date_utc')),
            'sensor_owner': who(sid), 'logger_owner': who(logger.get('equipment_id')),
            'feeder_table': f'feeder.{ft}' if ft else None, 'feeder_column': fc,
            'first_measurement': d['first_measurement_date'], 'last_measurement': d['last_measurement_date'],
            'n_measurements': d['total_measurement_numbers'], 'datastream_attribute': d['datastream_attribute'],
            # for selection only
            '_owners': owners.get(sid, []) + owners.get(logger.get('equipment_id'), []),
            '_sensor_keys': [sensor.get(k) for k in ('equipment_model_name', 'equipment_model_part_number', 'equipment_code', 'equipment_type_cv')],
            # for the hierarchy only
            '_logger': {**logger, 'position': pos.get(logger.get('equipment_id'), {}),
                        'feeder_n_columns': len(fcols[ft]) if ft else None},
            '_sensor': {**sensor, 'position': p},
        })
    return rows


# ---------------------------------------------------------------- selection

def match_text(pattern, values):
    """Case-insensitive: '=X' matches X exactly; glob if the pattern has * ? [; else substring."""
    p = pattern.lower()
    vals = [str(v).lower() for v in values if v not in (None, '')]
    if p.startswith('='):
        return p[1:] in vals
    if any(c in p for c in '*?['):
        return any(fnmatch.fnmatchcase(v, p) for v in vals)
    return any(p in v for v in vals)


def match_variable(pattern, r):
    """Data types match exactly (case-insensitive) on term or name, or by glob: 'soil*'."""
    names = [(r['variable'] or '').lower(), (r['variable_name'] or '').lower()]
    p = pattern.lower()
    if any(c in p for c in '*?['):
        return any(fnmatch.fnmatchcase(n, p) for n in names)
    return p in names


def select(rows, sel):
    role = (sel.get('role') or 'owner').lower()
    out = []
    for r in rows:
        if sel.get('owner'):
            os_ = [o for o in r['_owners'] if role == 'any' or (o['person_role_cv'] or '').lower() == role]
            if not any(any(str(o['person_id']) == p or match_text(p, [o['name']]) for o in os_) for p in sel['owner']):
                continue
        if sel.get('data_type') and not any(match_variable(p, r) for p in sel['data_type']):
            continue
        if sel.get('sensor_type') and not any(match_text(p, r['_sensor_keys']) for p in sel['sensor_type']):
            continue
        if sel.get('site') and not any(match_text(p, [r['site_code'], r['site_name']]) for p in sel['site']):
            continue
        out.append(r)
    return out


def selection_from(args):
    sel = {}
    if args.config:
        cfg = json.load(open(args.config))
        sel.update(cfg.get('selection', cfg))
    for k in SEL_KEYS:
        v = getattr(args, k, None)
        if v:
            sel[k] = v
    for k in ('start', 'end', 'qa', 'drop_missing', 'valid_min', 'valid_max', 'slim', 'all_columns'):
        v = getattr(args, k, None)
        if v not in (None, False, []):
            sel[k] = v
    return sel


# ---------------------------------------------------------------- output

def write_tsv(path, header, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    opener = gzip.open if path.suffix == '.gz' else open
    with opener(path, 'wt', encoding='utf-8', newline='') as fh:
        fh.write('\t'.join(header) + '\n')
        for r in rows:
            fh.write('\t'.join('' if r.get(c) is None else str(r[c]).replace('\t', ' ').replace('\n', ' ') for c in header) + '\n')


def summarize(rows):
    def top(key, n=40):
        c = Counter(r[key] or '(none)' for r in rows)
        return c.most_common(n)
    owners = Counter()
    for r in rows:
        for o in {(o['name'], o['person_id']) for o in r['_owners']}:
            owners[o] += 1
    return {
        'datastreams': len(rows),
        'measurements (metadata count)': sum(int(r['n_measurements'] or 0) for r in rows),
        'owners': [(f'{n} (person_id {i})', c) for (n, i), c in owners.most_common()],
        'data_types': top('variable'), 'sensor_types': top('sensor_model'), 'sites': top('site_code', 60),
    }


def print_summary(sel, rows):
    s = summarize(rows)
    log(f"selection: {json.dumps({k: v for k, v in sel.items()})}")
    log(f"{s['datastreams']:,} datastreams, {s['measurements (metadata count)']:,} measurements (per datastream metadata)")
    for k in ('owners', 'data_types', 'sensor_types', 'sites'):
        log(f'\n{k}:')
        for name, n in s[k]:
            log(f'  {n:>7,}  {name}')


def git_commit():
    try:
        return subprocess.run(['git', '-C', str(Path(__file__).resolve().parent), 'rev-parse', '--short', 'HEAD'],
                              capture_output=True, text=True).stdout.strip() or None
    except OSError:
        return None


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for b in iter(lambda: fh.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


def num(x):
    """psql text -> int/float (None stays None)."""
    if x in (None, ''):
        return None
    try:
        f = float(x)
    except ValueError:
        return x
    return int(f) if f.is_integer() and '.' not in str(x) and 'e' not in str(x).lower() else f


def natural(s):
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r'(\d+)', s or '')]


def hierarchy(rows, sel):
    """Site -> device (the logger a datastream is registered to) -> sensors attached to it.

    Datastreams made by the device itself (battery, camera channels) sit on the device;
    datastreams made by an attached sensor (a probe) sit on that sensor."""
    def ds_obj(r):
        return {'datastream_id': num(r['datastream_id']), 'table': f"datastreams.{r['datastream_table']}",
                'variable': r['variable'], 'variable_name': r['variable_name'], 'units': r['units'],
                'feeder_column': r['feeder_column'], 'first_measurement': r['first_measurement'],
                'last_measurement': r['last_measurement'], 'n_measurements': num(r['n_measurements'])}

    def equip(e, extra=None):
        p = e.get('position') or {}
        d = {'equipment_id': num(e.get('equipment_id')), 'name': e.get('equipment_name'),
             'code': e.get('equipment_code'), 'model': e.get('equipment_model_name'),
             'part_number': e.get('equipment_model_part_number'), 'type': e.get('equipment_type_cv'),
             'manufacturer': e.get('manufacturer'),
             'z_offset_m': num(p.get('equipment_z_offset_m')),
             'installed_utc': utc(p.get('position_start_date_utc')), 'removed_utc': utc(p.get('position_end_date_utc'))}
        for k in ('equipment_description', 'equipment_documentation_link'):
            if e.get(k):
                d[k.replace('equipment_', '')] = e[k]
        d.update(extra or {})
        return d

    sites = {}
    for r in rows:
        s = sites.setdefault(r['site_code'], {
            'site_code': r['site_code'], 'site_name': r['site_name'], 'site_type': r['site_type'],
            'latitude': num(r['latitude']), 'longitude': num(r['longitude']), 'elevation_m': num(r['site_elevation_m']),
            '_devices': {}})
        lg = r['_logger']
        dev = s['_devices'].get(r['logger_equipment_id'])
        if dev is None:
            ft = r['feeder_table']
            dev = s['_devices'][r['logger_equipment_id']] = equip(lg, {
                'label': ft.split('.', 1)[1] if ft else lg.get('equipment_name'),
                'owner': r['logger_owner'],
                'feeder_table': {'table': ft, 'n_columns': lg.get('feeder_n_columns')} if ft else None,
                'datastreams': [], '_sensors': {}})
        if r['sensor_equipment_id'] == r['logger_equipment_id']:
            dev['datastreams'].append(ds_obj(r))
        else:
            sen = dev['_sensors'].get(r['sensor_equipment_id'])
            if sen is None:
                sen = dev['_sensors'][r['sensor_equipment_id']] = equip(r['_sensor'], {'owner': r['sensor_owner'], 'datastreams': []})
            sen['datastreams'].append(ds_obj(r))

    out_sites = []
    for s in sorted(sites.values(), key=lambda s: natural(s['site_code'])):
        devices = []
        for dev in sorted(s.pop('_devices').values(), key=lambda d: natural(d['label'])):
            sens = sorted(dev.pop('_sensors').values(), key=lambda x: (-(x['z_offset_m'] or 0), natural(x['name'])))
            dev['datastreams'].sort(key=lambda x: natural(x['table']))
            dev['sensor_count'] = len(sens)
            dev['sensors'] = sens
            devices.append(dev)
        s['devices'] = devices
        streams = [x for d in devices for x in d['datastreams'] + [y for sn in d['sensors'] for y in sn['datastreams']]]
        by_var = {}
        for x in streams:
            v = by_var.setdefault(x['variable'], {'variable': x['variable'], 'units': x['units'], 'datastreams': 0,
                                                  'n_measurements': 0, 'first_measurement': None, 'last_measurement': None})
            v['datastreams'] += 1
            v['n_measurements'] += x['n_measurements'] or 0
            if x['first_measurement'] and (not v['first_measurement'] or x['first_measurement'] < v['first_measurement']):
                v['first_measurement'] = x['first_measurement']
            if x['last_measurement'] and (not v['last_measurement'] or x['last_measurement'] > v['last_measurement']):
                v['last_measurement'] = x['last_measurement']
        s['data_summary'] = {'datastreams': len(streams), 'n_measurements': sum(v['n_measurements'] for v in by_var.values()),
                             'data_types': sorted(by_var.values(), key=lambda v: -v['n_measurements'])}
        out_sites.append(s)

    owners = {}
    for r in rows:
        for o in r['_owners']:
            owners.setdefault(o['person_id'], {'person_id': num(o['person_id']), 'name': o['name'], 'roles': set()})['roles'].add(o['person_role_cv'])
    loggers = {d['equipment_id'] for s in out_sites for d in s['devices']}
    sensors = {sn['equipment_id'] for s in out_sites for d in s['devices'] for sn in d['sensors']}
    return {
        'tool': f'ssiselect {VERSION}', 'tool_sha256': sha256(Path(__file__).resolve()),
        'command': shlex.join([Path(sys.argv[0]).name] + sys.argv[1:]),
        'created_utc': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
        'host': socket.gethostname(), 'database': sx.DB,
        'selection': {k: v for k, v in sel.items() if k in SEL_KEYS},
        'owners': [{**o, 'roles': sorted(o['roles'])} for o in owners.values()],
        'counts': {'sites': len(out_sites), 'devices': len(loggers), 'sensors': len(sensors),
                   'equipment': len(loggers | sensors), 'datastreams': len(rows),
                   'n_measurements': sum(s['data_summary']['n_measurements'] for s in out_sites)},
        'sites': out_sites,
    }


def export(rows, sel, out):
    out = Path(out)
    stem = out.name.split('.')[0]
    cat_path, man_path = out.with_name(stem + '.catalog.tsv'), out.with_name(stem + '.manifest.json')
    meta_cols = ['datastream_id'] if sel.get('slim') else CATALOG_COLS
    conds = []
    if sel.get('start'):
        conds.append(f"utc_time >= extract(epoch from timestamptz {sx.lit(sel['start'] + ' UTC')})")
    if sel.get('end'):
        conds.append(f"utc_time < extract(epoch from timestamptz {sx.lit(sel['end'] + ' UTC')})")
    # missing values (NULL / NaN, qa_flag c) are kept unless drop_missing is set
    drop = bool(sel.get('drop_missing'))
    if drop:
        conds.append("data_value is not null and data_value <> 'NaN'")
    if sel.get('qa'):
        conds.append('qa_flag in (' + ', '.join(sx.lit(x) for x in sel['qa']) + ')')
    # range limits apply to real values only; missing rows pass through unless dropped
    keep_null = '' if drop else "data_value is null or data_value = 'NaN' or "
    if sel.get('valid_min') is not None:
        conds.append(f"({keep_null}data_value >= {float(sel['valid_min'])})")
    if sel.get('valid_max') is not None:
        conds.append(f"({keep_null}data_value <= {float(sel['valid_max'])})")
    where = (' where ' + ' and '.join(conds)) if conds else ''
    lines = []
    for r in rows:
        lits = ', '.join('null' if r[c] is None else sx.lit(r[c]) for c in meta_cols)
        lines.append(
            f"COPY (select to_char(to_timestamp(utc_time) at time zone 'UTC', 'YYYY-MM-DD HH24:MI:SS'), utc_time, "
            f"data_value, qa_flag, {lits} from datastreams.{sx.ident(r['datastream_table'])}{where} order by utc_time) "
            f"TO STDOUT WITH (FORMAT csv, DELIMITER E'\\t', NULL '');")
    log(f'exporting {len(rows):,} datastreams to {out} ...')
    t0 = datetime.datetime.now()
    sx.stream_copy(lines, out, header=DATA_COLS + meta_cols)
    write_tsv(cat_path, CATALOG_COLS, rows)
    n, by_flag = 0, Counter()
    with (gzip.open(out, 'rt', encoding='utf-8') if out.suffix == '.gz' else open(out, encoding='utf-8')) as fh:
        next(fh, None)
        for line in fh:
            n += 1
            by_flag[line.split('\t', 4)[3]] += 1
    manifest = {
        'tool': f'ssiselect {VERSION}', 'git_commit': git_commit(),
        'command': shlex.join([Path(sys.argv[0]).name] + sys.argv[1:]),
        'tool_sha256': sha256(Path(__file__).resolve()),
        'created_utc': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
        'host': socket.gethostname(), 'database': sx.DB,
        'selection': sel,
        'filters_sql': conds,
        'rows': n, 'rows_by_qa_flag': dict(sorted(by_flag.items())), 'datastreams': len(rows),
        'files': {out.name: {'bytes': out.stat().st_size, 'sha256': sha256(out)},
                  cat_path.name: {'bytes': cat_path.stat().st_size, 'sha256': sha256(cat_path)}},
        'columns': DATA_COLS + meta_cols,
        'notes': ['Rows are grouped by datastream, then ordered by time.',
                  'value is data_value from the datastream table. Each datastream table has one row per '
                  'feeder timestamp; where the feeder cell was NaN the value is empty and qa_flag is c. '
                  'These rows are kept unless drop_missing is set.',
                  'qa_flag codes are defined in odmx.cv_quality_code (z = good, c = NaN).'],
    }
    json.dump(manifest, open(man_path, 'w'), indent=2)
    log(f'{n:,} rows in {(datetime.datetime.now() - t0).seconds} s\n  {out}\n  {cat_path}\n  {man_path}')


# ---------------------------------------------------------------- CLI

FEEDER_META = ['feeder_table', 'logger_equipment_id', 'logger_name', 'logger_model', 'site_code', 'site_name',
               'site_type', 'latitude', 'longitude', 'site_elevation_m', 'logger_owner']


def export_feeder(rows, sel, out):
    """Raw feeder rows for the selected datastreams: one row per feeder row, one column per
    selected feeder column (union across tables, empty where a table lacks it), plus the
    logger/site metadata of the table. Feeder timestamps are UTC."""
    out = Path(out)
    stem = out.name.split('.')[0]
    cat_path, man_path = out.with_name(stem + '.catalog.tsv'), out.with_name(stem + '.manifest.json')
    by_table = {}
    for r in rows:
        if r['feeder_table'] and r['feeder_column']:
            by_table.setdefault(r['feeder_table'].split('.', 1)[1], []).append(r)
    if not by_table:
        die('none of the selected datastreams has a feeder table')
    names = sorted(by_table)
    order = {(t['table_name'], t['column_name']): int(t['ordinal_position']) for t in sx.q(
        "select table_name, column_name, ordinal_position from information_schema.columns "
        "where table_schema = 'feeder' and table_name in (" + ', '.join(sx.lit(n) for n in names) + ")")}
    value_cols = []                                   # union, in feeder column order of first appearance
    for n in names:
        for c in sorted({r['feeder_column'] for r in by_table[n]}, key=lambda c: order.get((n, c), 0)):
            if c not in value_cols:
                value_cols.append(c)
    extra = []
    if sel.get('all_columns'):                        # the rest of each table's columns, e.g. index, image_url
        for n in names:
            for (tn, c), _ in sorted(order.items(), key=lambda kv: kv[1]):
                if tn == n and c != 'timestamp' and c not in value_cols and c not in extra:
                    extra.append(c)
    meta_cols = ['feeder_table'] if sel.get('slim') else FEEDER_META
    conds = []
    if sel.get('start'):
        conds.append(f'"timestamp" >= {sx.lit(sel["start"])}::timestamp')
    if sel.get('end'):
        conds.append(f'"timestamp" < {sx.lit(sel["end"])}::timestamp')
    where = (' where ' + ' and '.join(conds)) if conds else ''
    lines = []
    for n in names:
        r0 = by_table[n][0]
        have = {c for (tn, c) in order if tn == n}
        cols = ', '.join(sx.ident(c) if c in have else 'null' for c in value_cols + extra)
        meta = {**r0, 'feeder_table': f'feeder.{n}'}
        lits = ', '.join('null' if meta.get(c) is None else sx.lit(meta[c]) for c in meta_cols)
        lines.append(
            f"COPY (select to_char(\"timestamp\", 'YYYY-MM-DD HH24:MI:SS'), "
            f"extract(epoch from \"timestamp\" at time zone 'UTC')::bigint, {cols}, {lits} "
            f"from feeder.{sx.ident(n)}{where} order by \"timestamp\") "
            f"TO STDOUT WITH (FORMAT csv, DELIMITER E'\\t', NULL '');")
    header = ['datetime_utc', 'utc_time'] + value_cols + extra + meta_cols
    log(f'exporting {len(names):,} feeder tables ({len(value_cols)} value columns) to {out} ...')
    t0 = datetime.datetime.now()
    sx.stream_copy(lines, out, header=header)
    write_tsv(cat_path, CATALOG_COLS, rows)
    fi = header.index('feeder_table')
    per_table = Counter()
    with (gzip.open(out, 'rt', encoding='utf-8') if out.suffix == '.gz' else open(out, encoding='utf-8')) as fh:
        next(fh, None)
        for line in fh:
            per_table[line.rstrip('\n').split('\t')[fi]] += 1
    n = sum(per_table.values())
    json.dump({
        'tool': f'ssiselect {VERSION}', 'tool_sha256': sha256(Path(__file__).resolve()),
        'command': shlex.join([Path(sys.argv[0]).name] + sys.argv[1:]),
        'created_utc': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
        'host': socket.gethostname(), 'database': sx.DB, 'source': 'feeder tables',
        'selection': sel, 'filters_sql': conds,
        'rows': n, 'rows_by_feeder_table': dict(sorted(per_table.items())),
        'feeder_tables': [f'feeder.{x}' for x in names], 'value_columns': value_cols, 'extra_columns': extra,
        'datastreams': len(rows),
        'files': {out.name: {'bytes': out.stat().st_size, 'sha256': sha256(out)},
                  cat_path.name: {'bytes': cat_path.stat().st_size, 'sha256': sha256(cat_path)}},
        'columns': header,
        'notes': ['One row per feeder row, ordered by feeder table, then time. Feeder timestamps are UTC.',
                  'Values are the raw feeder columns (no unit conversion, no qa_flag); NaN is written as NaN.',
                  'The catalog maps each feeder column (feeder_column) to its datastream, variable and units.'],
    }, open(man_path, 'w'), indent=2)
    log(f'{n:,} rows in {(datetime.datetime.now() - t0).seconds} s\n  {out}\n  {cat_path}\n  {man_path}')


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0], formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__.split('\n', 2)[2])
    sub = ap.add_subparsers(dest='cmd', required=True)

    def common(p):
        p.add_argument('--owner', action='append', metavar='NAME|ID')
        p.add_argument('--data-type', dest='data_type', action='append', metavar='VARIABLE')
        p.add_argument('--sensor-type', dest='sensor_type', action='append', metavar='MODEL')
        p.add_argument('--site', action='append', metavar='SITE')
        p.add_argument('--role', help='equipment role to match for --owner (default owner; "any" = all roles)')
        p.add_argument('--config', help='JSON file with a "selection" object (e.g. a previous manifest)')
    common(sub.add_parser('options', help='list what the selection contains'))
    p = sub.add_parser('catalog', help='write one row per matching datastream'); common(p)
    p.add_argument('-o', '--out', required=True)
    p = sub.add_parser('hierarchy', help='write site -> device -> sensor JSON'); common(p)
    p.add_argument('-o', '--out', required=True, help='output .json')
    p = sub.add_parser('feeder', help='write the raw feeder rows behind the selected datastreams'); common(p)
    p.add_argument('-o', '--out', required=True, help='output file (.tsv or .tsv.gz)')
    p.add_argument('--start', help='first UTC time to include')
    p.add_argument('--end', help='UTC time to stop before')
    p.add_argument('--all-columns', dest='all_columns', action='store_true',
                   help='also write the feeder columns that are not selected datastreams (e.g. index, image_url)')
    p.add_argument('--slim', action='store_true', help='only feeder_table beside the values (join the catalog yourself)')
    p = sub.add_parser('export', help='write one row per measurement'); common(p)
    p.add_argument('-o', '--out', required=True, help='output file (.tsv or .tsv.gz)')
    p.add_argument('--start', help='first UTC time to include, e.g. 2023-01-01 or "2023-01-01 06:00"')
    p.add_argument('--end', help='UTC time to stop before')
    p.add_argument('--qa', action='append', metavar='FLAG', help='keep only these qa_flag codes (e.g. z)')
    p.add_argument('--drop-missing', dest='drop_missing', action='store_true',
                   help='drop rows with no value (NULL/NaN, qa_flag c); kept by default')
    p.add_argument('--valid-min', dest='valid_min', type=float, help='drop values below this')
    p.add_argument('--valid-max', dest='valid_max', type=float, help='drop values above this')
    p.add_argument('--slim', action='store_true', help='only datastream_id beside each value (join the catalog yourself)')
    p.add_argument('--yes', action='store_true', help='export even if the selection is large')
    args = ap.parse_args()

    sel = selection_from(args)
    sx.check_server()
    log('reading datastream catalog ...')
    rows = select(load_catalog(), sel)
    if args.cmd == 'options':
        print_summary(sel, rows)
        return
    if not rows:
        die('no datastreams match the selection; run `ssiselect options` with fewer filters')
    if args.cmd == 'catalog':
        write_tsv(args.out, CATALOG_COLS, rows)
        print_summary(sel, rows)
        log(f'\nwrote {len(rows):,} datastreams to {args.out}')
        return
    if args.cmd == 'hierarchy':
        doc = hierarchy(rows, sel)
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        json.dump(doc, open(args.out, 'w'), indent=2)
        log(f"wrote {args.out}: {json.dumps(doc['counts'])}")
        return
    if args.cmd == 'feeder':
        export_feeder(rows, sel, args.out)
        return
    n = sum(int(r['n_measurements'] or 0) for r in rows)
    if n > 50_000_000 and not args.yes:
        die(f'selection holds about {n:,} measurements; narrow it or pass --yes')
    export(rows, sel, args.out)


if __name__ == '__main__':
    main()

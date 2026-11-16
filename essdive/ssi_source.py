"""Build a Sensor Time Series - Full package from SSI / ODMX datastreams.

Input is a list of catalog rows from ssiselect.load_catalog() + select() (one per datastream,
already resolved to logger, sensor, site, variable and units). The package has:

  - one data file per logger (e.g. one per SNOD1 node), one column per datastream (= per sensor),
    sorted top to bottom by sensor height, each with a _flag column from qa_flag;
  - one location per site (point) and one child location per sensor position (vertical_position
    relative to the ground surface, parent_location_id = site);
  - attributes: one method_id per (variable, sensor model), one sensor_id per sensor, and one
    flag_id per qa_flag code (definitions from odmx.cv_quality_code).

Values come from fetch(table, start, end) -> iterable of (utc_time, value or None, qa_flag):
db_fetch reads the datastreams schema through psql, export_fetch reads an ssiexportall table
export (exports/tables_<date>/datastreams/*.csv[.gz]). Tests pass a fake fetch.
"""
import csv
import datetime
import gzip
import io
import math
import re
import subprocess
from collections import OrderedDict, defaultdict
from pathlib import Path

from . import model, vocab

NO_RECORD = 'no_record'


def iso_utc(epoch):
    return datetime.datetime.fromtimestamp(int(epoch), datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _raw(x):
    """Keep a value's text exactly as stored (e.g. '1' stays '1', not '1.0'); None if missing or not a number."""
    return x if isinstance(x, str) and _num(x) is not None else None


def _num(x):
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


# ---------------------------------------------------------------- database access (via ssiexport helpers)

def db_fetch(sx):
    """Return a fetch(table, start, end) that streams a datastream table from the database."""
    def fetch(table, start=None, end=None):
        conds = []
        if start:
            conds.append(f"utc_time >= extract(epoch from timestamptz {sx.lit(start + ' UTC')})")
        if end:
            conds.append(f"utc_time < extract(epoch from timestamptz {sx.lit(end + ' UTC')})")
        where = (' where ' + ' and '.join(conds)) if conds else ''
        sql = (f"COPY (select utc_time, data_value, qa_flag from datastreams.{sx.ident(table)}{where} "
               f"order by utc_time) TO STDOUT WITH (FORMAT csv)")
        p = subprocess.run(sx.psql_cmd('-c', sql), capture_output=True, text=True)
        if p.returncode:
            raise RuntimeError(p.stderr.strip())
        for t, v, q in csv.reader(io.StringIO(p.stdout)):
            yield int(t), _raw(v), q
    return fetch


def db_quality_codes(sx, tables=None):
    """Quality codes from odmx.cv_quality_code; with tables, only the codes those tables use."""
    codes = {r['qa_flag']: r for r in sx.q('select qa_flag, term, name, definition from odmx.cv_quality_code')}
    if tables:
        used = set()
        for t in tables:
            used |= {r['qa_flag'] for r in sx.q(f'select distinct qa_flag from datastreams.{sx.ident(t)}')}
        codes = {k: v for k, v in codes.items() if k in used}
    return codes


def db_sites(sx, codes):
    if not codes:
        return {}
    rows = sx.q('select sampling_feature_code, sampling_feature_name, sampling_feature_description, '
                'latitude, longitude, elevation_m, elevation_datum_cv, feature_geometry_wkt from odmx.sampling_features '
                'where sampling_feature_code in (' + ', '.join(sx.lit(c) for c in codes) + ')')
    return {r['sampling_feature_code']: r for r in rows}


# ---------------------------------------------------------------- table export access (ssiexportall output)

def _export_path(export, schema, table):
    base = Path(export) / schema
    p = base / f'{table}.csv'
    return p if p.exists() else base / f'{table}.csv.gz'


def _export_rows(export, schema, table):
    p = _export_path(export, schema, table)
    opener = gzip.open if p.suffix == '.gz' else open
    with opener(p, 'rt', encoding='utf-8', newline='') as fh:
        yield from csv.DictReader(fh)


def _epoch(utc_text):
    t = datetime.datetime.fromisoformat(utc_text.replace('T', ' ').rstrip('Z'))
    return int(t.replace(tzinfo=datetime.timezone.utc).timestamp())


def export_fetch(export):
    """Return a fetch(table, start, end) that reads <export>/datastreams/<table>.csv[.gz]."""
    def fetch(table, start=None, end=None):
        lo = _epoch(start) if start else None
        hi = _epoch(end) if end else None
        out = []
        for r in _export_rows(export, 'datastreams', table):
            t = int(r['utc_time'])
            if (lo is None or t >= lo) and (hi is None or t < hi):
                out.append((t, _raw(r['data_value']), r['qa_flag']))
        out.sort(key=lambda x: x[0])
        return iter(out)
    return fetch


def export_quality_codes(export, tables=None):
    """Quality codes from odmx/cv_quality_code; with tables, only the codes those tables use."""
    codes = {r['qa_flag']: r for r in _export_rows(export, 'odmx', 'cv_quality_code')}
    if tables:
        used = set()
        for t in tables:
            used |= {r['qa_flag'] for r in _export_rows(export, 'datastreams', t)}
        codes = {k: v for k, v in codes.items() if k in used}
    return codes


def export_sites(export, codes):
    codes = set(codes)
    return {r['sampling_feature_code']: {k: (v if v != '' else None) for k, v in r.items()}
            for r in _export_rows(export, 'odmx', 'sampling_features') if r['sampling_feature_code'] in codes}


# ---------------------------------------------------------------- package construction

def _site_location(code, site, row):
    s = site or {}
    desc = s.get('sampling_feature_description') or ''
    name = s.get('sampling_feature_name') or row.get('site_name') or ''
    datum = s.get('elevation_datum_cv') or ''
    elev = _num(s.get('elevation_m') or row.get('site_elevation_m'))
    return model.Location(
        location_id=code, location_name=name,
        location_description=(desc or f'{name} ({code}), SSI / ODMX sampling feature.'),
        location_shape='point', location_confidence='unknown',
        latitude=_num(s.get('latitude') or row.get('latitude')),
        longitude=_num(s.get('longitude') or row.get('longitude')),
        elevation=elev, elevation_datum=(datum if datum else ('unknown' if elev is not None else '')),
        location_information_source='SSI ODMX database (odmx.sampling_features)')


def position_text(z):
    """0.05 -> '0.05 m above the ground surface'; -0.25 -> '0.25 m below ...'; 0 -> 'at ...'."""
    if z is None:
        return 'at an unknown height relative to the ground surface'
    if z == 0:
        return 'at the ground surface'
    return f'{abs(z):g} m {"above" if z > 0 else "below"} the ground surface'


def _sensor_location(code, r):
    z = _num(r['sensor_z_offset_m'])
    return model.Location(
        location_id=f'{code}_{vocab.slug(r["sensor_name"])}',
        location_name=f'{code} {r["sensor_name"]}',
        location_description=(f'Position of sensor {r["sensor_name"]} ({r["sensor_model"]}) at site {code}, '
                              f'{position_text(z)}.'),
        location_shape='point', location_confidence='unknown',
        latitude=_num(r['latitude']), longitude=_num(r['longitude']),
        vertical_position=z, vertical_position_reference='ground_surface' if z is not None else '',
        parent_location_id=code,
        location_information_source='SSI ODMX database (odmx.equipment_position)')


def _method_id(r):
    return f'method_{vocab.snake(r["variable"])}_{vocab.slug(r["sensor_model"] or "unknown").lower()}'


def _default_method_description(r, n_sensors, loggers):
    return (f'{r["variable_name"]} measured by {r["sensor_model"]} sensors ({n_sensors} in this package) '
            f'attached to {len(loggers)} logger(s) ({", ".join(sorted(loggers))}), recorded in the SSI ODMX database '
            f'as datastreams of type {r["datastream_type"]}. Values are the datastream values as stored in the '
            f'database, in {r["units"]}. Description generated from database metadata; review and expand before '
            f'publication.')


COLUMN_NAMING = ('variable-sensor', 'table')
FILE_NAMING = ('prefix-site', 'feeder-site')


def build_package(rows, *, prefix, title, fetch, quality_codes, sites=None, start=None, end=None,
                  config=None, column_naming='variable-sensor', file_naming='prefix-site', split_variables=False):
    """rows: ssiselect catalog rows for the selected datastreams.

    column_naming: 'variable-sensor' names measurement columns <variable>_<sensor>, e.g.
    soil_temperature_temp01; 'table' names them <datastream table>_data_value, e.g.
    influxdb_SNOD1_Node026_Temp01_meas_data_value (the value column of that table in the export).
    file_naming: 'prefix-site' names data files <prefix>_<site code>; 'feeder-site' names them
    <feeder table>_<site code>, e.g. influxdb_SNOD1_Node026_SG-DTP26 (one feeder table = one logger).
    split_variables: one data file per logger and variable (e.g. soil temperature and battery voltage of a
    node in separate files), with the variable appended to the file name: ..._SG-DTP26_soil_temperature."""
    if file_naming not in FILE_NAMING:
        raise ValueError(f'file_naming must be one of {FILE_NAMING}')
    if column_naming not in COLUMN_NAMING:
        raise ValueError(f'column_naming must be one of {COLUMN_NAMING}')
    config = config or {}
    sites = sites or {}
    rows = [r for r in rows if r['datastream_table']]

    # attributes: methods
    attributes, method_ids = [], OrderedDict()
    by_method = defaultdict(list)
    for r in rows:
        by_method[_method_id(r)].append(r)
    for mid, rs in by_method.items():
        cfg = config.get('methods', {}).get(mid, {})
        loggers = {x['feeder_table'].split('.', 1)[-1] if x['feeder_table'] else x['logger_name'] for x in rs}
        attributes.append(model.Attribute(
            attr_id=mid, attr_type='method_id',
            attr_description=cfg.get('description') or _default_method_description(
                rs[0], len({x['sensor_equipment_id'] for x in rs}), loggers),
            lower_bound=str(cfg.get('lower_bound', '')), upper_bound=str(cfg.get('upper_bound', '')),
            instrument_precision=str(cfg.get('instrument_precision', '')),
            sensor_model=rs[0]['sensor_model'] or ''))
        method_ids[mid] = True

    # attributes: sensors (one per equipment; several datastreams can share one, e.g. a relay logger)
    seen_sensors = set()
    for r in sorted(rows, key=lambda r: (r['site_code'], -(_num(r['sensor_z_offset_m']) or 0), r['sensor_name'] or '')):
        if r['sensor_equipment_id'] in seen_sensors:
            continue
        seen_sensors.add(r['sensor_equipment_id'])
        z = _num(r['sensor_z_offset_m'])
        attributes.append(model.Attribute(
            attr_id=f'sensor_{r["sensor_equipment_id"]}', attr_type='sensor_id',
            attr_description=(f'{r["sensor_model"]} "{r["sensor_name"]}" (SSI equipment {r["sensor_equipment_id"]}, '
                              f'manufacturer {r["sensor_manufacturer"] or "unknown"}) attached to logger '
                              f'{r["feeder_table"] or r["logger_name"]} (equipment {r["logger_equipment_id"]}) at '
                              f'site {r["site_code"]}, {position_text(z)}, installed '
                              f'{r["sensor_installed_utc"] or "unknown"} UTC'
                              + (f', removed {r["sensor_removed_utc"]} UTC' if r['sensor_removed_utc'] else '') + '.'),
            sensor_model=r['sensor_model'] or '', sensor_serial_number=''))

    # attributes: flags
    for code, q in sorted(quality_codes.items()):
        attributes.append(model.Attribute(
            attr_id=vocab.flag_id(code), attr_type='flag_id',
            attr_description=(f'SSI quality code "{code}" ({q["term"]}): {q["definition"]} '
                              f'Assigned by the SSI ingest (odmx.cv_quality_code).')))
    attributes.append(model.Attribute(
        attr_id=NO_RECORD, attr_type='flag_id',
        attr_description='The datastream table has no row for this time, although another sensor of the same '
                         'logger does; the value is reported as missing.'))

    # locations
    # locations: sites by code, then sensors by site and height (top to bottom), so the order does
    # not depend on the order of the catalog rows
    locations, seen = [], set()
    by_height = sorted(rows, key=lambda r: (r['site_code'], -(_num(r['sensor_z_offset_m']) or 0), r['sensor_name'] or ''))
    for r in by_height:
        code = r['site_code']
        if code not in seen:
            seen.add(code)
            locations.append(_site_location(code, sites.get(code), r))
    seen_locations = set()
    for r in by_height:
        loc = _sensor_location(r['site_code'], r)
        if loc.location_id not in seen_locations:     # one location per sensor position
            seen_locations.add(loc.location_id)
            locations.append(loc)

    # data files: one per logger (and per variable with split_variables)
    groups = OrderedDict()
    for r in sorted(rows, key=lambda r: (r['site_code'], r['feeder_table'] or '', -(_num(r['sensor_z_offset_m']) or 0),
                                         r['sensor_name'] or '')):
        groups.setdefault((r['site_code'], r['logger_equipment_id'], r['variable'] if split_variables else None),
                          []).append(r)
    if split_variables:      # within a logger: the main variable (most columns) first, then the others by name
        n_by = {k: len(v) for k, v in groups.items()}
        groups = OrderedDict(sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1], -n_by[kv[0]], kv[0][2] or '')))
    data_files = []
    for (code, logger, variable), rs in groups.items():
        if column_naming == 'table':     # database order: Temp01 .. Temp13, V_batt (natural sort of table names)
            rs = sorted(rs, key=lambda r: [int(x) if x.isdigit() else x.lower()
                                           for x in re.split(r'(\d+)', r['datastream_table'])])
        columns, names = [], set()
        for r in rs:
            if column_naming == 'table':
                name = f'{r["datastream_table"]}_data_value'
            else:
                name = f'{vocab.snake(r["variable"])}_{vocab.slug(r["sensor_name"]).lower()}'
            while name in names:
                name += '_x'
            names.add(name)
            mv, material = vocab.variable(r['variable'])
            z = _num(r['sensor_z_offset_m'])
            columns.append(model.MeasurementColumn(
                name=name, long_name=f'{r["variable_name"]} {r["sensor_name"]} {code}',
                definition=(f'{r["variable_name"]} measured by sensor {r["sensor_name"]} ({r["sensor_model"]}) at site '
                            f'{code}, {position_text(z)} (SSI datastream {r["datastream_id"]}, '
                            f'table datastreams.{r["datastream_table"]}).'),
                unit=vocab.unit_label(r['units']), measured_variable=mv, material_measured=material,
                method_id=_method_id(r), location_id=f'{code}_{vocab.slug(r["sensor_name"])}',
                sensor_id=f'sensor_{r["sensor_equipment_id"]}',
                notes=(f'Sensor is {z} m above the ground surface; the database labels the variable '
                       f'{r["variable"]}.' if z is not None and z > 0 and material == 'soil' else ''),
                source=r['datastream_table']))
        feeder = (rs[0]['feeder_table'] or '').split('.', 1)[-1]
        if file_naming == 'feeder-site' and feeder:
            stem = f'{feeder}_{vocab.slug(code)}'          # e.g. influxdb_SNOD1_Node026_SG-DTP26
        else:
            stem = f'{prefix}_{vocab.slug(code)}'
            if len({k[1] for k in groups if k[0] == code}) > 1:
                stem += f'_{logger}'
        if variable:
            stem += f'_{vocab.snake(variable)}'                # e.g. ..._soil_temperature, ..._battery_voltage
        data_files.append(model.DataFile(
            name=stem,
            description=(f'{title}: time series from logger {rs[0]["feeder_table"] or rs[0]["logger_name"]} at site {code} '
                         f'({rs[0]["site_name"]}), '
                         + (f'{rs[0]["variable_name"]}, ' if variable else '') + f'{len(columns)} sensors'
                         + (f', {start} to {end}' if start or end else '') + '.'),
            columns=columns, rows=_rows_factory(columns, fetch, start, end)))
    return model.Package(prefix=prefix, title=title, locations=locations, attributes=attributes,
                         data_files=data_files)


def _rows_factory(columns, fetch, start, end):
    def rows():
        series = {}
        for c in columns:
            series[c.name] = {t: (v, q) for t, v, q in fetch(c.source, start, end)}
        times = sorted(set().union(*[s.keys() for s in series.values()])) if series else []
        for t in times:
            out = {}
            for c in columns:
                hit = series[c.name].get(t)
                if hit is None:
                    out[c.name] = (None, (NO_RECORD,))
                else:
                    v, q = hit
                    out[c.name] = (v, (vocab.flag_id(q),) if q else ())
            yield iso_utc(t), out
    return rows

"""Problem checks for a written Sensor Time Series - Full package built from SSI datastreams.

run(pkg, pkg_dir, rows, sites, validation, config) -> list of problem dicts, saved in the
<prefix>_manifest.json beside the package. The checks only report; they never change values.

Each problem:
    {"id": "...", "severity": "error|warning|info", "category": "data|metadata|format",
     "file": "...", "columns": [...], "count": N, "description": "...", "examples": [...],
     "action": "..."}

severity: error   = values that are not what the column says they are (must be resolved before publication)
          warning = values or metadata that look wrong or are incomplete
          info    = facts a user of the package should know
"""
import bisect
import csv
import datetime
import json
import math
import re
from collections import Counter, defaultdict

from . import spec

LEFT = 'Values left exactly as stored in the database; to be resolved with the data provider.'

# Plausible ranges per SSI variable; values flagged good (qa_z) outside them are reported.
# A method's lower_bound / upper_bound in the --config file overrides these.
PLAUSIBLE = {
    'soilTemperature': (-40.0, 60.0),
    'snowTemperature': (-50.0, 40.0),
    'airTemperature': (-50.0, 50.0),
    'batteryVoltage': (0.0, 5.0),
}
# Optional per-cell flags (ssi2essdive.py --problem-flags): added after the database flag in the
# _flag column, separated by '; ' as the format allows, and defined as flag_id attributes.
PROBLEM_FLAGS = {
    'suspect_shifted_column': 'Suspect: this value matches the reading of another probe further down the profile in the '
                              'nearest complete reading (within 1 h); the partial reading appears to have been stored in '
                              'the wrong column by the ingest. Value left as stored in the database.',
    'suspect_battery_voltage': 'Suspect: in a shifted partial reading, this value lies in the range of the node battery '
                               'voltage and is most likely the battery voltage stored in a temperature column. Value left '
                               'as stored in the database.',
    'suspect_location_message': 'Suspect: this value equals the site latitude or longitude, or an elevation; a GPS / location '
                                'message stored as a temperature. Value left as stored in the database.',
    'suspect_zero_packet': 'Suspect: every value present in this row is exactly 0, probably an empty or corrupted packet. '
                           'Value left as stored in the database.',
    'suspect_implausible_value': 'Suspect: value flagged good by the SSI ingest but outside the plausible range for the '
                                 'variable. Value left as stored in the database.',
}


def _mark(cells, flag, file, t, column):
    if cells is not None:
        cells.setdefault(flag, set()).add((file, t, column))


MAX_EXAMPLES = 5
MATCH_TOL = 0.05          # degC: a value "matches" another reading if within this
NEAR_FULL_S = 3600        # compare a partial row with a full row at most this far away in time


def _f(x):
    if x in (None, '', spec.MISSING_NUMERIC):
        return None
    try:
        v = float(x)
    except ValueError:
        return None
    return None if math.isnan(v) else v


def _epoch(iso):
    return datetime.datetime.strptime(iso, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=datetime.timezone.utc).timestamp()


def _short(table):
    return re.sub(r'^influxdb_SNOD1_|_meas$', '', table)


def _problem(pid, severity, category, description, *, file=None, columns=None, count=None, examples=None,
             action=LEFT, **extra):
    p = {'id': pid, 'severity': severity, 'category': category}
    if file:
        p['file'] = file
    if columns:
        p['columns'] = columns
    if count is not None:
        p['count'] = count
    p['description'] = description
    if examples:
        p['examples'] = examples[:MAX_EXAMPLES]
    p.update(extra)
    p['action'] = action
    return p


def run(pkg, pkg_dir, rows, sites=None, validation=None, config=None, cells=None):
    """cells: optional dict, filled with {flag_id: {(file, datetime, column)}} for apply_problem_flags()."""
    sites, config = sites or {}, config or {}
    by_table = {r['datastream_table']: r for r in rows}
    brange = _battery_ranges(pkg, pkg_dir, by_table)
    out = []
    if validation is not None:
        out += _format_problems(validation)
    for df in pkg.data_files:
        out += _data_problems(df, pkg_dir / df.file_name, by_table, sites, config, cells, brange)
    out += _metadata_problems(rows, sites)
    order = {'error': 0, 'warning': 1, 'info': 2}
    out.sort(key=lambda p: order[p['severity']])
    return out


# ---------------------------------------------------------------- format (validator)

def _format_problems(rep):
    out = []
    for i, msg in enumerate(rep.errors):
        out.append(_problem(f'format_error_{i + 1}', 'error', 'format', msg, action='Fix before submission.'))
    for i, msg in enumerate(rep.warnings):
        sev = 'warning'
        desc = msg
        if 'not in the unit vocabulary' in msg:
            desc = (msg + '. The Sensor Time Series - Full unit vocabulary (controlled_vocabulary.md, based on UCUM) '
                    'has no entry for this unit; ESS-DIVE would need to add one, or the unit must be described otherwise.')
        out.append(_problem(f'format_warning_{i + 1}', sev, 'format', desc, action='Unit written as stored in the database.'))
    return out


# ---------------------------------------------------------------- data checks on the written file

def _battery_ranges(pkg, pkg_dir, by_table):
    """{feeder table: (min, max)} of the battery voltage of each logger, over all data files of the package
    (the battery may be in its own file when variables are split)."""
    vals = defaultdict(list)
    for df in pkg.data_files:
        bc = [(c.name, by_table[c.source]['feeder_table']) for c in df.columns
              if by_table.get(c.source, {}).get('variable') == 'batteryVoltage']
        if not bc:
            continue
        with open(pkg_dir / df.file_name, newline='', encoding='utf-8') as fh:
            for r in csv.DictReader(fh):
                for name, ft in bc:
                    v = _f(r[name])
                    if v is not None:
                        vals[ft].append(v)
    return {ft: (min(v), max(v)) for ft, v in vals.items() if v}


def _data_problems(df, path, by_table, sites, config, cells=None, brange=None):
    cols = [(c.name, by_table.get(c.source, {})) for c in df.columns]
    names = [n for n, _ in cols]
    with open(path, newline='', encoding='utf-8') as fh:
        data = [(r['datetime_measured'], [r[n] for n in names], [r[n + '_flag'] for n in names])
                for r in csv.DictReader(fh)]
    fname = df.file_name
    out = []
    n_rows = len(data)

    # columns of one variable in database order (the probe profile) and an optional battery column
    var = Counter(r.get('variable') for _, r in cols)
    main_var = var.most_common(1)[0][0] if var else None
    prof = [i for i, (_, r) in enumerate(cols) if r.get('variable') == main_var]
    batt = [i for i, (_, r) in enumerate(cols) if r.get('variable') == 'batteryVoltage']

    # 1. partial readings stored in the wrong columns (shifted along the probe profile)
    if len(prof) > 2:
        ft = cols[prof[0]][1].get('feeder_table')
        out += _shift_problems(data, prof, (brange or {}).get(ft), cols, fname, cells)

    # 2. values equal to the site's coordinates or close to its elevation (location packets)
    site = sites.get(next((r.get('site_code') for _, r in cols if r.get('site_code')), ''), {})
    out += _location_values(data, cols, site, fname, cells)

    # 3. rows where every reported value is exactly 0
    zeros = [(t, [cols[i][0] for i, v in enumerate(vals) if _f(v) is not None])
             for t, vals, _ in data
             if sum(_f(v) is not None for v in vals) >= 2 and all(_f(v) in (None, 0.0) for v in vals)]
    for t, cs in zeros:
        for c in cs:
            _mark(cells, 'suspect_zero_packet', fname, t, c)
    if zeros:
        out.append(_problem(
            'all_zero_rows', 'warning', 'data',
            'Rows in which every value present (at least two columns) is exactly 0, flagged good (qa_z). '
            'Probably empty or corrupted packets rather than real temperatures of 0 degC.',
            file=fname, count=len(zeros), examples=[{'datetime_measured': t, 'columns_equal_to_0': len(c)} for t, c in zeros]))

    # 4. good-flagged values outside plausible ranges
    for i, (name, r) in enumerate(cols):
        lo, hi = _bounds(r, config)
        if lo is None:
            continue
        bad = [(t, vals[i]) for t, vals, flags in data
               if flags[i] == 'qa_z' and _f(vals[i]) is not None and not lo <= _f(vals[i]) <= hi]
        for t, _ in bad:
            _mark(cells, 'suspect_implausible_value', fname, t, name)
        if bad:
            out.append(_problem(
                f'implausible_good_value_{_short(r["datastream_table"])}', 'warning', 'data',
                f'Values flagged good (qa_z) outside the plausible range {lo:g} to {hi:g} {r.get("units")} for '
                f'{r.get("variable")}.', file=fname, columns=[name], count=len(bad),
                examples=[{'datetime_measured': t, 'value': v} for t, v in bad]))

    # 5. out-of-range flags set by the SSI ingest
    f_cells = [(t, names[i], vals[i]) for t, vals, flags in data for i in range(len(names)) if flags[i] == 'qa_f']
    if f_cells:
        out.append(_problem(
            'qa_f_values', 'info', 'data',
            'Values flagged outOfRange (qa_f) by the SSI ingest. Kept, with their flag, as stored.',
            file=fname, columns=sorted({c for _, c, _ in f_cells}), count=len(f_cells),
            examples=[{'datetime_measured': t, 'column': c, 'value': v} for t, c, v in f_cells]))

    # 6. rows with no value at all, and timestamps off the 15-minute grid
    empty = [t for t, vals, _ in data if all(_f(v) is None for v in vals)]
    empty_set = set(empty)
    off = [t for t, _, _ in data if t[14:19] not in ('00:00', '15:00', '30:00', '45:00')]
    off_empty = sum(t in empty_set for t in off)
    if empty:
        out.append(_problem(
            'empty_rows', 'info', 'data',
            f'Rows in which every column is missing (-9999, flag qa_c): the feeder table has a row at that time but no '
            f'value in any column. {off_empty:,} of them are at times off the 15-minute grid (e.g. hh:09:07), '
            f'which suggests messages without measurements.',
            file=fname, count=len(empty), share_of_rows=round(len(empty) / n_rows, 4) if n_rows else None,
            examples=[{'datetime_measured': t} for t in empty]))
    if off:
        out.append(_problem(
            'off_grid_times', 'info', 'data',
            f'Rows whose time is not on the 15-minute grid; {off_empty:,} of the {len(off):,} have no value in any column.',
            file=fname, count=len(off), examples=[{'datetime_measured': t} for t in off if t not in empty_set][:MAX_EXAMPLES] or
            [{'datetime_measured': t} for t in off]))

    # 7. sparse columns
    for i in batt:
        n = sum(_f(vals[i]) is not None for _, vals, _ in data)
        out.append(_problem(
            f'sparse_{_short(cols[i][1]["datastream_table"])}', 'info', 'data',
            f'The battery voltage has a value in {n:,} of {n_rows:,} rows.', file=fname, columns=[cols[i][0]], count=n))
    return out


def _bounds(r, config):
    m = config.get('methods', {})
    for mid, cfg in m.items():
        if r.get('variable') and mid.startswith('method_' + re.sub(r'(?<!^)(?=[A-Z])', '_', r['variable']).lower()):
            if cfg.get('lower_bound') not in (None, '') and cfg.get('upper_bound') not in (None, ''):
                return float(cfg['lower_bound']), float(cfg['upper_bound'])
    return PLAUSIBLE.get(r.get('variable'), (None, None))


def _shift_problems(data, prof, brange, cols, fname, cells=None):
    """Rows where only the first few probe columns have values: do those values match other probes of
    the nearest row in which every probe reports (i.e. were they stored s columns too high)?"""
    n = len(prof)
    full = [(_epoch(t), [_f(vals[i]) for i in prof]) for t, vals, _ in data if all(_f(vals[i]) is not None for i in prof)]
    ft = [t for t, _ in full]
    blo, bhi = (brange[0] - 0.1, brange[1] + 0.1) if brange else (None, None)
    shifts, no_ref, examples, batt_like = Counter(), 0, defaultdict(list), []
    stored_cols = defaultdict(set)
    partial_rows = 0
    for t, vals, _ in data:
        pv = [_f(vals[i]) for i in prof]
        filled = [k for k, v in enumerate(pv) if v is not None]
        if not filled or len(filled) == n or max(filled) >= n // 2:
            continue                     # empty, full, or not a "first columns only" row
        partial_rows += 1
        te = _epoch(t)
        j = bisect.bisect_left(ft, te)
        near = [full[k] for k in (j - 1, j) if 0 <= k < len(full) and abs(full[k][0] - te) <= NEAR_FULL_S]
        if not near:
            no_ref += 1
            continue
        ref = min(near, key=lambda c: abs(c[0] - te))[1]
        best = None
        for s in range(n):
            ks = [k for k in filled if k + s < n]
            if len(ks) < 3:          # two values can match by chance
                continue
            err = sum(abs(pv[k] - ref[k + s]) for k in ks) / len(ks)
            if best is None or err < best[1]:
                best = (s, err, ks)
        if best is None or best[1] > MATCH_TOL:
            no_ref += 1
            continue
        s, _, ks = best
        shifts[s] += 1
        stored_cols[s] |= set(ks)
        if s:
            for k in ks:
                _mark(cells, 'suspect_shifted_column', fname, t, cols[prof[k]][0])
        beyond = [k for k in filled if k + s >= n]          # columns that would be past the last probe
        if s and beyond and blo is not None and all(blo <= pv[k] <= bhi for k in beyond):
            batt_like.append((t, [cols[prof[k]][0] for k in beyond], [vals[prof[k]] for k in beyond]))
            for k in beyond:
                _mark(cells, 'suspect_battery_voltage', fname, t, cols[prof[k]][0])
        if len(examples[s]) < MAX_EXAMPLES:
            examples[s].append({'datetime_measured': t,
                                'stored_as': {cols[prof[k]][0]: vals[prof[k]] for k in filled},
                                'matches': {cols[prof[k + s]][0]: ref[k + s] for k in ks},
                                'reference': 'nearest row in which every probe reports, within 1 h'})
    out = []
    for s, cnt in sorted(shifts.items()):
        if s == 0:
            continue
        out.append(_problem(
            f'partial_rows_shifted_by_{s}', 'error', 'data',
            f'Rows in which only the first probe columns have values: the values match the probes {s} positions further '
            f'down the profile in the nearest complete reading (e.g. the value stored in {cols[prof[0]][0]} is the reading '
            f'of {cols[prof[s]][0]}). The partial readings appear to have been stored in the wrong columns by the '
            f'ingest. Of {partial_rows:,} such rows, {cnt:,} match this shift; {no_ref:,} have no complete reading within '
            f'1 h to compare with (probably affected the same way).',
            file=fname, columns=[cols[prof[k]][0] for k in sorted(stored_cols[s])], count=cnt,
            should_be=[cols[prof[k + s]][0] for k in sorted(stored_cols[s])],
            rows_checked=partial_rows, rows_without_reference=no_ref,
            examples=examples[s]))
    if batt_like:
        bc = sorted({c for _, cs, _ in batt_like for c in cs})
        out.append(_problem(
            'battery_voltage_in_temperature_column', 'error', 'data',
            f'In the shifted partial rows, the value after the last shifted probe lies in the range of the node battery '
            f'voltage ({blo + 0.1:.3f} to {bhi - 0.1:.3f} V) and is most likely the battery voltage, stored in a '
            f'temperature column. Counted only in shifted rows that have a complete reading within 1 h to compare '
            f'with; rows without one are probably affected the same way.', file=fname, columns=bc, count=len(batt_like),
            examples=[{'datetime_measured': t, 'columns': c, 'values': v} for t, c, v in batt_like]))
    return out


def _location_values(data, cols, site, fname, cells=None):
    lat, lon, elev = (str(site.get(k) or '') for k in ('latitude', 'longitude', 'elevation_m'))
    hits = defaultdict(list)
    for t, vals, flags in data:
        for i, v in enumerate(vals):
            fv = _f(v)
            if fv is None:
                continue
            kind = ('latitude' if lat and v == lat else 'longitude' if lon and v == lon else
                    'elevation' if elev and _f(elev) and fv > 1000 and abs(fv - float(elev)) < 100 else None)
            if kind:
                hits[t].append({'column': cols[i][0], 'value': v, 'looks_like': kind, 'flag': flags[i]})
                _mark(cells, 'suspect_location_message', fname, t, cols[i][0])
    if not hits:
        return []
    cells = [c for cs in hits.values() for c in cs]
    good = sum(c['flag'] == 'qa_z' for c in cells)
    elevs = sorted({c['value'] for c in cells if c['looks_like'] == 'elevation'})
    return [_problem(
        'location_packet_values', 'error', 'data',
        f'Rows in which temperature columns hold the site latitude, longitude or an elevation (a GPS / location '
        f'message stored as temperatures). {good} of the {len(cells)} cells are flagged good (qa_z), the rest qa_f. '
        f'Latitude and longitude swap columns between messages.'
        + (f' The reported elevation ({", ".join(elevs)} m) differs from the registered site elevation ({elev} m).'
           if elevs and elev and all(abs(float(e) - float(elev)) > 1 for e in elevs) else ''),
        file=fname, columns=sorted({c['column'] for c in cells}), count=len(cells), rows=len(hits),
        examples=[{'datetime_measured': t, 'cells': cs} for t, cs in sorted(hits.items())])]


# ---------------------------------------------------------------- metadata checks

def _metadata_problems(rows, sites):
    out = []
    attr_mismatch, instr_unknown, no_serial, unknown_eq, counts, last_diff = [], [], [], [], [], []
    for r in rows:
        tab = r['datastream_table']
        try:
            a = json.loads(r.get('datastream_attribute') or '{}')
        except ValueError:
            a = {}
        z = r.get('sensor_z_offset_m')
        if 'sensor_elevation' in a and z not in (None, '') and float(a['sensor_elevation']) != float(z):
            attr_mismatch.append({'table': tab, 'datastream_attribute.sensor_elevation': a['sensor_elevation'],
                                  'equipment_position.equipment_z_offset_m': z})
        if a.get('instrument') == 'unknown sensor' and r.get('sensor_model') not in (None, '', 'unknown sensor'):
            instr_unknown.append({'table': tab, 'datastream_attribute.instrument': a['instrument'],
                                  'sensor_model': r['sensor_model']})
        s = r.get('_sensor') or {}
        if not s.get('equipment_serial_number'):
            no_serial.append(tab)
        if (r.get('sensor_model') in (None, '', 'unknown sensor') or
                (r.get('sensor_manufacturer') or '').lower().startswith('unknown')):
            unknown_eq.append({'table': tab, 'sensor_name': r.get('sensor_name'), 'sensor_model': r.get('sensor_model'),
                               'manufacturer': r.get('sensor_manufacturer'), 'equipment_id': r.get('sensor_equipment_id')})
        counts.append({'table': tab, 'total_measurement_numbers': r.get('n_measurements')})
        last_diff.append({'table': tab, 'last_measurement_date': r.get('last_measurement')})
    if attr_mismatch:
        out.append(_problem(
            'datastream_attribute_elevation', 'warning', 'metadata',
            'odmx.sampling_feature_timeseries_datastreams.datastream_attribute gives a sensor_elevation that differs from '
            'the sensor position in odmx.equipment_position. The package uses equipment_position.',
            count=len(attr_mismatch), examples=attr_mismatch, action='Package uses odmx.equipment_position.'))
    if instr_unknown:
        out.append(_problem(
            'datastream_attribute_instrument', 'info', 'metadata',
            'datastream_attribute names the instrument "unknown sensor" although the attached sensor model is known. '
            'The package uses the attached sensor.', count=len(instr_unknown), examples=instr_unknown,
            action='Package uses the attached sensor (odmx.related_equipment isAttachedTo).'))
    if unknown_eq:
        out.append(_problem(
            'unknown_equipment', 'warning', 'metadata',
            'Datastreams whose equipment is registered as unknown (model "unknown sensor" and/or Unknown Manufacturer); '
            'for the battery voltage this is the node logger, described in the package as a sensor at the site.',
            count=len(unknown_eq), examples=unknown_eq, action='Described as registered; provider to supply details.'))
    if no_serial:
        out.append(_problem(
            'no_serial_numbers', 'info', 'metadata',
            'No serial number (odmx.equipment.equipment_serial_number) and no calibration record for these sensors.',
            count=len(no_serial), columns=no_serial, action='sensor_serial_number left empty in the attributes file.'))
    out.append(_problem(
        'measurement_count_metadata', 'info', 'metadata',
        'odmx total_measurement_numbers and last_measurement_date count only rows flagged good (qa_z); the datastream '
        'tables (and the package) also contain the qa_c (missing) and qa_f rows.',
        count=len(counts), examples=counts, action='Not used by the package.'))
    for code, s in sorted(sites.items()):
        wkt = s.get('feature_geometry_wkt') or ''
        m = re.match(r'POINT\(\s*([-\d.]+)\s+([-\d.]+)\s*\)', wkt)
        if m and s.get('latitude') and float(m.group(1)) == float(s['latitude']):
            out.append(_problem(
                f'wkt_axis_order_{code}', 'info', 'metadata',
                f'odmx.sampling_features.feature_geometry_wkt for {code} is {wkt}, i.e. latitude first; WKT order is '
                f'POINT(longitude latitude).', action='Package uses the latitude and longitude columns.'))
    above = [r['datastream_table'] for r in rows
             if r.get('variable') == 'soilTemperature' and r.get('sensor_z_offset_m') not in (None, '')
             and float(r['sensor_z_offset_m']) > 0]
    if above:
        out.append(_problem(
            'soil_temperature_above_ground', 'info', 'metadata',
            'Probes registered as soil temperature but positioned above the ground surface.',
            count=len(above), columns=above, action='Noted in the data dictionary notes for these columns.'))
    return out


# ---------------------------------------------------------------- optional per-cell flags

def apply_problem_flags(pkg, pkg_dir, cells):
    """Append problem flags to the _flag cells listed in cells ({flag_id: {(file, datetime, column)}}) and
    define them in the attributes file. Values are not touched. Returns {flag_id: cells flagged}."""
    by_file = {}
    for flag, keys in cells.items():
        for f, t, c in keys:
            by_file.setdefault(f, {}).setdefault(t, {}).setdefault(c, []).append(flag)
    order = list(PROBLEM_FLAGS)
    for f, marks in by_file.items():
        path = pkg_dir / f
        tmp = path.with_name(path.name + '.tmp')
        with open(path, newline='', encoding='utf-8') as src, open(tmp, 'w', newline='', encoding='utf-8') as dst:
            r = csv.reader(src)
            w = csv.writer(dst, lineterminator='\n')
            header = next(r)
            w.writerow(header)
            idx = {h: i for i, h in enumerate(header)}
            for row in r:
                for c, flags in marks.get(row[0], {}).items():
                    j = idx[c + '_flag']
                    extra = sorted(set(flags), key=order.index)
                    row[j] = '; '.join([x for x in row[j].split('; ') if x and x != spec.MISSING_TEXT] + extra)
                w.writerow(row)
        tmp.replace(path)
    used = {f: len(k) for f, k in cells.items() if k}
    attr = pkg_dir / pkg.attr_file_name
    with open(attr, newline='', encoding='utf-8') as fh:
        rows = list(csv.reader(fh))
    header = rows[0]
    with open(attr, 'a', newline='', encoding='utf-8') as fh:
        w = csv.writer(fh, lineterminator='\n')
        for flag in order:
            if flag in used:
                row = {h: '' for h in header}
                row.update(attr_id=flag, attr_type='flag_id', attr_description=PROBLEM_FLAGS[flag])
                w.writerow([row[h] for h in header])
    return used

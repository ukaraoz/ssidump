"""Check a written Sensor Time Series - Full package against the reporting formats.

    python3 -m essdive.validate <package_dir>        # exit status 1 if there are errors

Reads the files back from disk (independent of the code that wrote them). Errors are rule
violations; warnings are things to review, such as units outside the controlled vocabulary.
"""
import csv
import re
import sys
from pathlib import Path

from . import spec

ISO_DT = re.compile(r'^\d{4}(-\d{2}(-\d{2}(T\d{2}(:\d{2}(:\d{2}(\.\d+)?)?)?(Z|[+-]\d{2}:\d{2}))?)?)?$')
NAME_OK = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]*$')


class Report:
    def __init__(self):
        self.errors, self.warnings = [], []

    def error(self, where, msg):
        self.errors.append(f'{where}: {msg}')

    def warn(self, where, msg):
        self.warnings.append(f'{where}: {msg}')

    @property
    def ok(self):
        return not self.errors

    def summary(self, limit=50):
        lines = [f'{len(self.errors)} errors, {len(self.warnings)} warnings']
        lines += [f'ERROR   {e}' for e in self.errors[:limit]]
        lines += [f'WARNING {w}' for w in self.warnings[:limit]]
        return '\n'.join(lines)


def _read(path):
    with open(path, newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def _header(path):
    with open(path, newline='', encoding='utf-8') as f:
        return next(csv.reader(f))


def _required(rep, where, row, terms):
    for t in terms:
        if (row.get(t) or '').strip() == '':
            rep.error(where, f'required term {t} is empty')


def _ids(value):
    return [] if value in ('', spec.NOT_APPLICABLE) else [v.strip() for v in value.split(';')]


def validate_package(pkg_dir, max_rows=None):
    pkg_dir = Path(pkg_dir)
    rep = Report()
    flmds = sorted(pkg_dir.glob('*flmd.csv'))
    if len(flmds) != 1:
        rep.error(str(pkg_dir), f'expected one *flmd.csv, found {len(flmds)}')
        return rep
    flmd = _read(flmds[0])
    if _header(flmds[0]) != spec.FLMD_COLUMNS:
        rep.error(flmds[0].name, 'columns differ from the template')

    # locations
    loc_files = [e['file_name'] for e in flmd if e.get('standard') == spec.STANDARD_LOCATION]
    locations = {}
    for lf in loc_files:
        rows = _read(pkg_dir / lf)
        if _header(pkg_dir / lf) != spec.LOCATION_COLUMNS:
            rep.error(lf, 'columns differ from the Location Metadata v2 template')
        for i, r in enumerate(rows, 2):
            w = f'{lf}:{i}'
            _required(rep, w, r, spec.LOCATION_REQUIRED)
            if r['location_id'] in locations:
                rep.error(w, f'duplicate location_id {r["location_id"]}')
            locations[r['location_id']] = r
            if r['location_shape'] not in spec.LOCATION_SHAPES:
                rep.error(w, f'location_shape {r["location_shape"]!r} not in vocabulary')
            if r['location_confidence'] not in spec.LOCATION_CONFIDENCE:
                rep.error(w, f'location_confidence {r["location_confidence"]!r} not in vocabulary')
            has_ll = r['latitude'] and r['longitude']
            has_xy = r['alt_coordinate_x'] and r['alt_coordinate_y']
            if not (has_ll or has_xy):
                rep.error(w, 'needs latitude/longitude or alt_coordinate_x/y')
            if has_ll and not (-90 <= float(r['latitude']) <= 90 and -180 <= float(r['longitude']) <= 180):
                rep.error(w, 'latitude/longitude out of range')
            if r['elevation'] and r['elevation_datum'] not in spec.ELEVATION_DATUMS:
                rep.error(w, f'elevation_datum {r["elevation_datum"]!r} not in vocabulary')
            if r['vertical_position_reference'] and r['vertical_position_reference'] not in spec.VERTICAL_POSITION_REFERENCES:
                rep.error(w, f'vertical_position_reference {r["vertical_position_reference"]!r} not in vocabulary')
    for lid, r in locations.items():
        if r['parent_location_id'] and r['parent_location_id'] not in locations:
            rep.error(loc_files[0], f'{lid}: parent_location_id {r["parent_location_id"]} not defined')

    # methods and attributes
    attrs = {}
    for af in [e['file_name'] for e in flmd if e['file_name'].endswith('sensor_attr.csv')]:
        if _header(pkg_dir / af) != spec.ATTR_COLUMNS:
            rep.error(af, 'columns differ from the template')
        for i, r in enumerate(_read(pkg_dir / af), 2):
            w = f'{af}:{i}'
            _required(rep, w, r, spec.ATTR_REQUIRED)
            if r['attr_id'] in attrs:
                rep.error(w, f'duplicate attr_id {r["attr_id"]}')
            if r['attr_type'] not in spec.ATTR_TYPES:
                rep.error(w, f'attr_type {r["attr_type"]!r} not in vocabulary')
            attrs[r['attr_id']] = r['attr_type']
    if not attrs:
        rep.error(str(pkg_dir), 'no methods and attributes file (*sensor_attr.csv) in the FLMD')

    # FLMD entries
    for i, e in enumerate(flmd, 2):
        w = f'{flmds[0].name}:{i}'
        _required(rep, w, e, spec.FLMD_REQUIRED)
        if not (pkg_dir / e['file_name']).exists():
            rep.error(w, f'{e["file_name"]} listed but missing')
        if not NAME_OK.match(e['file_name'].rsplit('.', 1)[0]):
            rep.warn(w, f'file name {e["file_name"]} has characters outside letters, numbers, - and _')
        if len(e.get('file_description', '')) < 10:
            rep.error(w, 'file_description shorter than 10 characters')
        dd = e['data_dictionary_file_name']
        if dd != spec.NOT_APPLICABLE:
            if not dd.endswith('dd.csv'):
                rep.error(w, f'data_dictionary_file_name {dd} must end with dd.csv')
            elif not (pkg_dir / dd).exists():
                rep.error(w, f'data dictionary {dd} missing')
    for f in pkg_dir.iterdir():
        if f.is_file() and f.name not in {e['file_name'] for e in flmd} and f != flmds[0]:
            rep.warn(f.name, 'file in package directory but not listed in the FLMD')

    # data files and their data dictionaries
    for e in flmd:
        if e['standard'] != spec.STANDARD_SENSOR_FULL or e['file_name'].endswith(('dd.csv', 'sensor_attr.csv')):
            continue
        _check_data_file(rep, pkg_dir, e['file_name'], e['data_dictionary_file_name'], attrs, locations, max_rows)
    return rep


def _check_dd(rep, ddf, rows, attrs, locations):
    dd = {}
    if _header(ddf) != spec.DD_COLUMNS:
        rep.error(ddf.name, 'columns differ from the template')
    for i, r in enumerate(rows, 2):
        w = f'{ddf.name}:{i}'
        name = r['column_or_row_name']
        if name in dd:
            rep.error(w, f'column {name} defined twice')
        dd[name] = r
        tmpl = spec.DD_TEMPLATE_ROWS.get(name) or (spec.DD_TEMPLATE_ROWS[spec.FLAG_TEMPLATE_KEY]
                                                   if name.endswith('_flag') else None)
        if tmpl:   # a reporting-format term: definition must be verbatim
            if r['definition'] != tmpl['definition']:
                rep.error(w, f'{name}: definition must be the template text')
            continue
        _required(rep, w, r, spec.DD_REQUIRED)
        if r['unit'] not in spec.UNITS and r['unit'] != spec.NOT_APPLICABLE:
            rep.warn(w, f'{name}: unit {r["unit"]!r} not in the unit vocabulary')
        if r['material_measured'] not in spec.MATERIALS:
            rep.error(w, f'{name}: material_measured {r["material_measured"]!r} not in vocabulary')
        if r['data_type'] and r['data_type'] not in spec.DATA_TYPES:
            rep.error(w, f'{name}: data_type {r["data_type"]!r} not in vocabulary')
        if r['representation_temporal'] not in ('', spec.NOT_APPLICABLE) and \
                r['representation_temporal'] not in spec.REPRESENTATION_TEMPORAL:
            rep.error(w, f'{name}: representation_temporal not in vocabulary')
        for term, kind in (('method_id', 'method_id'), ('sensor_id', 'sensor_id'), ('treatment_id', 'treatment_id')):
            for v in _ids(r.get(term, '')):
                if attrs.get(v) != kind:
                    rep.error(w, f'{name}: {term} {v} is not defined as {kind} in the attributes file')
        if r['location_id'] not in locations:
            rep.error(w, f'{name}: location_id {r["location_id"]} not in the locations file')
        if not NAME_OK.match(name):
            rep.warn(w, f'column name {name} has characters outside letters, numbers, - and _')
    return dd


def _check_data_file(rep, pkg_dir, name, dd_name, attrs, locations, max_rows):
    path = pkg_dir / name
    dd = _check_dd(rep, pkg_dir / dd_name, _read(pkg_dir / dd_name), attrs, locations)
    header = _header(path)
    time_cols = [c for c in header if c.startswith('datetime_measured')]
    if 'datetime_measured' not in header and not {'datetime_measured_start', 'datetime_measured_end'} <= set(header):
        rep.error(name, 'needs datetime_measured, or datetime_measured_start and datetime_measured_end')
    for c in header:
        if c not in dd:
            rep.error(name, f'column {c} is not in {dd_name}')
        if c.endswith('_flag') and c[:-5] not in header:
            rep.error(name, f'{c} has no matching measurement column')
    for c in dd:
        if c not in header:
            rep.warn(dd_name, f'{c} defined but not a column of {name}')
    measures = [c for c in header if c not in time_cols and not c.endswith('_flag') and c != 'notes']
    flags = [c for c in header if c.endswith('_flag')]
    seen, bad_time, bad_num, bad_flag, n = set(), 0, 0, set(), 0
    with open(path, newline='', encoding='utf-8') as f:
        for r in csv.DictReader(f):
            n += 1
            for tc in time_cols:
                if not ISO_DT.match(r[tc]):
                    bad_time += 1
            key = tuple(r[tc] for tc in time_cols)
            if key in seen:
                rep.error(name, f'timestamp {key} repeated (row {n + 1})')
            seen.add(key)
            for c in measures:
                v = r[c]
                if v != dd[c].get('missing_value_code', spec.MISSING_NUMERIC):
                    try:
                        float(v)
                    except ValueError:
                        bad_num += 1
            for c in flags:
                for code in _ids(r[c]):
                    if attrs.get(code) != 'flag_id':
                        bad_flag.add(code)
            if max_rows and n >= max_rows:
                rep.warn(name, f'only the first {max_rows:,} rows were checked')
                break
    if bad_time:
        rep.error(name, f'{bad_time} timestamps not ISO 8601 with Z or a UTC offset')
    if bad_num:
        rep.error(name, f'{bad_num} measurement values are neither numbers nor the missing value code')
    for code in sorted(bad_flag):
        rep.error(name, f'flag {code} is not defined as flag_id in the attributes file')


def main():
    rep = validate_package(sys.argv[1])
    print(rep.summary())
    sys.exit(0 if rep.ok else 1)


if __name__ == '__main__':
    main()

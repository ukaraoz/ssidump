"""Write an essdive.model.Package as Sensor Time Series - Full files.

    write_package(pkg, out_dir) -> list of written file names

Every file follows the CSV Reporting Format: UTF-8, comma delimited, one header row, no empty
lines, -9999 for missing numbers and N/A for missing text.
"""
import csv
import math
from pathlib import Path

from . import spec


def _writer(path):
    fh = open(path, 'w', newline='', encoding='utf-8')
    return fh, csv.writer(fh, lineterminator='\n')


def _fmt_number(v):
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return spec.MISSING_NUMERIC
    return repr(v) if isinstance(v, float) else str(v)


def _na(v):
    return spec.NOT_APPLICABLE if v in (None, '') else v


# ---------------------------------------------------------------- data file + its data dictionary

def write_data_file(df, out_dir):
    """Write <name>.csv. Returns (rows written, set of flag codes used)."""
    header = ['datetime_measured']
    for c in df.columns:
        header.append(c.name)
        if c.with_flag:
            header.append(c.flag_name)
    fh, w = _writer(Path(out_dir) / df.file_name)
    w.writerow(header)
    n, flags_used, last = 0, set(), None
    for when, values in df.rows():
        if last is not None and when <= last:
            fh.close()
            raise ValueError(f'{df.file_name}: timestamps must be unique and increasing ({when} after {last})')
        last = when
        row = [when]
        for c in df.columns:
            value, flags = values.get(c.name, (None, ()))
            row.append(_fmt_number(value))
            if c.with_flag:
                row.append('; '.join(flags) if flags else spec.MISSING_TEXT)
                flags_used.update(flags)
        w.writerow(row)
        n += 1
    fh.close()
    return n, flags_used


def dd_rows_for(df):
    """Data dictionary rows for one data file, in column order."""
    t = spec.DD_TEMPLATE_ROWS
    rows = [dict(t['datetime_measured'])]
    for c in df.columns:
        rows.append({
            'column_or_row_name': c.name, 'unit': c.unit, 'definition': c.definition,
            'measured_variable': c.measured_variable, 'material_measured': c.material_measured,
            'method_id': c.method_id, 'location_id': c.location_id, 'sensor_id': _na(c.sensor_id),
            'treatment_id': _na(c.treatment_id), 'unit_basis': _na(c.unit_basis),
            'column_or_row_long_name': _na(c.long_name), 'data_type': c.data_type,
            'missing_value_code': c.missing_value_code,
            'representation_temporal': _na(c.representation_temporal),
            'statistic_measurement': spec.NOT_APPLICABLE, 'statistic_measurement_number': spec.NOT_APPLICABLE,
            'statistic_spatial': spec.NOT_APPLICABLE, 'statistic_spatial_number': spec.NOT_APPLICABLE,
            'statistic_temporal': _na(c.statistic_temporal),
            'statistic_temporal_number': _na(c.statistic_temporal_number),
            'statistic_detail': spec.NOT_APPLICABLE, 'notes': _na(c.notes),
        })
        if c.with_flag:
            flag = dict(t[spec.FLAG_TEMPLATE_KEY])
            flag['column_or_row_name'] = c.flag_name
            flag['missing_value_code'] = spec.MISSING_TEXT
            rows.append(flag)
    return rows


def write_dd(rows, path):
    fh, w = _writer(path)
    w.writerow(spec.DD_COLUMNS)
    for r in rows:
        w.writerow([r.get(k, '') for k in spec.DD_COLUMNS])
    fh.close()


# ---------------------------------------------------------------- package-level files

def write_attributes(attrs, path):
    fh, w = _writer(path)
    w.writerow(spec.ATTR_COLUMNS)
    for a in attrs:
        r = a.as_row()
        w.writerow([r.get(k, '') or spec.NOT_APPLICABLE for k in spec.ATTR_COLUMNS])
    fh.close()


def attr_dd_rows():
    """Data dictionary for the methods and attributes file: the template rows, verbatim."""
    return [dict(spec.DD_TEMPLATE_ROWS[k]) for k in spec.ATTR_COLUMNS]


def write_locations(locations, path):
    fh, w = _writer(path)
    w.writerow(spec.LOCATION_COLUMNS)
    for loc in locations:
        r = loc.as_row()
        w.writerow([r.get(k, '') for k in spec.LOCATION_COLUMNS])
    fh.close()


def write_flmd(entries, path):
    fh, w = _writer(path)
    w.writerow(spec.FLMD_COLUMNS)
    for e in entries:
        w.writerow([e.get(k, '') for k in spec.FLMD_COLUMNS])
    fh.close()


def _flmd(file_name, description, standard, dd_name, notes=''):
    return {'file_name': file_name, 'file_description': description, 'standard': standard,
            'data_dictionary_file_name': dd_name, 'file_version': spec.NOT_APPLICABLE,
            'data_orientation': spec.FLMD_DATA_ORIENTATION, 'header_rows': spec.FLMD_HEADER_ROWS,
            'column_or_row_name_position': spec.FLMD_NAME_POSITION, 'notes': notes or spec.NOT_APPLICABLE}


def write_package(pkg, out_dir, log=print):
    """Write all files of the package. Returns {'files': [...], 'rows': {data file: n}}."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    flmd, rows, flags_used = [], {}, set()
    for df in pkg.data_files:
        n, used = write_data_file(df, out)
        rows[df.file_name] = n
        flags_used |= used
        write_dd(dd_rows_for(df), out / df.dd_file_name)
        flmd.append(_flmd(df.file_name, df.description, spec.STANDARD_SENSOR_FULL, df.dd_file_name,
                          df.notes or pkg.flmd_notes.get(df.file_name, '')))
        flmd.append(_flmd(df.dd_file_name, f'Data dictionary for {df.file_name}.', spec.STANDARD_SENSOR_FULL,
                          spec.NOT_APPLICABLE))
        log(f'  {df.file_name}: {n:,} rows, {len(df.columns)} measurement columns')
    write_attributes(pkg.attributes, out / pkg.attr_file_name)
    write_dd(attr_dd_rows(), out / pkg.attr_dd_file_name)
    write_locations(pkg.locations, out / pkg.locations_file_name)
    flmd += [
        _flmd(pkg.attr_file_name, f'Methods, sensors and flags used in {pkg.title}.', spec.STANDARD_SENSOR_FULL,
              pkg.attr_dd_file_name),
        _flmd(pkg.attr_dd_file_name, f'Data dictionary for {pkg.attr_file_name}.', spec.STANDARD_SENSOR_FULL,
              spec.NOT_APPLICABLE),
        _flmd(pkg.locations_file_name, f'Locations of the sites and sensors in {pkg.title}.',
              spec.STANDARD_LOCATION, spec.NOT_APPLICABLE),
    ]
    write_flmd(flmd, out / pkg.flmd_file_name)
    files = [e['file_name'] for e in flmd] + [pkg.flmd_file_name]
    return {'files': files, 'rows': rows, 'flags_used': sorted(flags_used)}

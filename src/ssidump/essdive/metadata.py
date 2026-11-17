"""All database metadata of a datastream table as one TSV row, read from an ssiexportall table export.

Layout (one row per datastream table, values exactly as stored, empty fields kept):
  - first column: odmx.sampling_feature_timeseries_datastreams:datastream_tablename
  - one-to-one groups as plain columns named <schema>.<table>:<column>
      datastream (odmx.sampling_feature_timeseries_datastreams), variable (odmx.variables),
      unit (odmx.cv_units), site (odmx.sampling_features)
  - one-to-many groups as one column each, holding a compact JSON list of the full joined rows
    (keys <schema>.<table>:<column>, values as text, sorted by id, [] when empty):
      odmx.sampling_feature_extension_property_values  site properties (+ odmx.extension_properties)
      odmx.related_features                            parent site(s) (+ odmx.sampling_features)
      odmx.equipment                                   logger, and the sensor attached to it whose name is
                                                       the feeder column (+ models, organizations
                                                       [manufacturer]/[vendor], positions, related equipment,
                                                       external_connection to the site); key "link" says how
                                                       each was reached
      odmx.equipment_persons_bridge                    people of that equipment (+ odmx.persons, odmx.affiliations)
      odmx.datastream_person_bridge                    people of the datastream
      odmx.cv_quality_code                             qa_flag codes used in the table; keys "link" and "rows"
                                                       (rows per code) are computed
"""
import csv
import gzip
import json
from collections import Counter
from pathlib import Path

DS = 'odmx.sampling_feature_timeseries_datastreams'


def _open(path):
    path = Path(path)
    if not path.exists():
        path = path.with_name(path.name + '.gz')
    return gzip.open(path, 'rt', encoding='utf-8', newline='') if path.suffix == '.gz' else \
        open(path, encoding='utf-8', newline='')


def _rows(export, schema, table):
    with _open(Path(export) / schema / f'{table}.csv') as fh:
        r = csv.reader(fh)
        header = next(r)
        return header, list(r)


def _p(prefix, d):
    return {f'{prefix}:{k}': x for k, x in d.items()}


def _dump(v):
    cell = json.dumps(v, ensure_ascii=False, separators=(',', ':'))
    assert '\t' not in cell and '\n' not in cell
    return cell


class Odmx:
    """The odmx tables needed, loaded once."""
    def __init__(self, export):
        self.export = Path(export)
        self.t = {}
        for name in ('sampling_feature_timeseries_datastreams', 'variables', 'cv_units', 'sampling_features',
                     'sampling_feature_extension_property_values', 'extension_properties', 'related_features',
                     'equipment', 'equipment_models', 'organizations', 'equipment_position', 'related_equipment',
                     'external_connection', 'equipment_persons_bridge', 'persons', 'affiliations',
                     'datastream_person_bridge', 'cv_quality_code'):
            h, rows = _rows(export, 'odmx', name)
            self.t[name] = (h, [dict(zip(h, r)) for r in rows])

    def header(self, name):
        return self.t[name][0]

    def rows(self, name):
        return self.t[name][1]

    def one(self, name, key, value):
        hits = [r for r in self.rows(name) if r[key] == value]
        if len(hits) != 1:
            raise ValueError(f'odmx.{name}: {len(hits)} rows with {key} = {value}')
        return hits[0]

    def where(self, name, sort_key, **eq):
        return sorted((r for r in self.rows(name) if all(r[k] == v for k, v in eq.items())),
                      key=lambda r: int(r[sort_key]))


def table_metadata(db, table, feeder_column=None):
    """Header and values of the metadata row for datastreams.<table>."""
    ds = db.one('sampling_feature_timeseries_datastreams', 'datastream_tablename', table)
    hdr = db.header('sampling_feature_timeseries_datastreams')
    order = ['datastream_tablename'] + [c for c in hdr if c != 'datastream_tablename']
    h = [f'{DS}:{c}' for c in order]
    v = [ds[c] for c in order]
    # one-to-one: variable, unit, site
    for name, key, ref in (('variables', 'variable_id', 'variable_id'), ('cv_units', 'units_id', 'units_id'),
                           ('sampling_features', 'sampling_feature_id', 'sampling_feature_id')):
        r = db.one(name, key, ds[ref])
        h += [f'odmx.{name}:{c}' for c in db.header(name)]
        v += [r[c] for c in db.header(name)]
    site = ds['sampling_feature_id']

    # site properties
    props = {r['property_id']: r for r in db.rows('extension_properties')}
    col = 'odmx.sampling_feature_extension_property_values'
    items = []
    for b in db.where('sampling_feature_extension_property_values', 'bridge_id', sampling_feature_id=site):
        o = _p(col, b)
        o.update(_p('odmx.extension_properties', props[b['property_id']]))
        items.append(o)
    h.append(col), v.append(_dump(items))

    # parent site(s)
    sf = {r['sampling_feature_id']: r for r in db.rows('sampling_features')}
    items = []
    for rel in db.where('related_features', 'relation_id', sampling_feature_id=site):
        o = _p('odmx.related_features', rel)
        o.update(_p('odmx.sampling_features', sf[rel['related_feature_id']]))
        items.append(o)
    h.append('odmx.related_features'), v.append(_dump(items))

    # equipment: logger, and the sensor attached to it named like the feeder column
    eq = {r['equipment_id']: r for r in db.rows('equipment')}
    models = {r['equipment_model_id']: r for r in db.rows('equipment_models')}
    orgs = {r['organization_id']: r for r in db.rows('organizations')}
    logger = ds['equipment_id']
    fcol = feeder_column or table.rsplit('_', 1)[0].split('_')[-1]

    def entry(eid, link):
        e = eq[eid]
        m = models[e['equipment_model_id']]
        o = {'link': link}
        o.update(_p('odmx.equipment', e))
        o.update(_p('odmx.equipment_models', m))
        o.update(_p('odmx.organizations[manufacturer]', orgs[m['equipment_model_manufacturer_id']]))
        if e['equipment_vendor_id'] in orgs:
            o.update(_p('odmx.organizations[vendor]', orgs[e['equipment_vendor_id']]))
        o['odmx.equipment_position'] = [_p('odmx.equipment_position', r) for r in
                                        db.where('equipment_position', 'equipment_position_id', equipment_id=eid)]
        o['odmx.related_equipment'] = [_p('odmx.related_equipment', r) for r in
                                       db.where('related_equipment', 'relation_id', equipment_id=eid)]
        o['odmx.external_connection'] = [_p('odmx.external_connection', r) for r in
                                         db.where('external_connection', 'connection_id', equipment_id=eid,
                                                  sampling_feature_id=site)]
        return o
    equipment = []
    if logger:
        equipment.append(entry(logger, f'logger: {DS}:equipment_id'))
        attached = [r['equipment_id'] for r in db.rows('related_equipment')
                    if r['related_equipment_id'] == logger and r['relationship_type_cv'] == 'isAttachedTo'
                    and eq[r['equipment_id']]['equipment_name'].lower() == fcol.lower()]
        if len(attached) == 1:
            equipment.append(entry(attached[0], f'sensor: odmx.related_equipment isAttachedTo {logger} with '
                                                f'odmx.equipment:equipment_name = feeder column {fcol}'))
    h.append('odmx.equipment'), v.append(_dump(equipment))

    # people of that equipment, and of the datastream
    ids = [e['odmx.equipment:equipment_id'] for e in equipment]
    persons = {r['person_id']: r for r in db.rows('persons')}
    items = []
    for b in sorted((r for r in db.rows('equipment_persons_bridge') if r['equipment_id'] in ids),
                    key=lambda r: int(r['bridge_id'])):
        o = _p('odmx.equipment_persons_bridge', b)
        o.update(_p('odmx.persons', persons[b['person_id']]))
        o['odmx.affiliations'] = [_p('odmx.affiliations', a) for a in
                                  db.where('affiliations', 'affiliation_id', person_id=b['person_id'])]
        items.append(o)
    h.append('odmx.equipment_persons_bridge'), v.append(_dump(items))
    h.append('odmx.datastream_person_bridge')
    v.append(_dump([_p('odmx.datastream_person_bridge', r) for r in
                    db.where('datastream_person_bridge', 'bridge_id', datastream_id=ds['datastream_id'])]))

    # quality codes used in the table
    _, data = _rows(db.export, 'datastreams', table)
    used = Counter(r[2] for r in data)
    codes = {r['qa_flag']: r for r in db.rows('cv_quality_code')}
    items = []
    for q in sorted(used):
        o = {'link': f'distinct datastreams.{table}:qa_flag = odmx.cv_quality_code:qa_flag', 'rows': used[q]}
        o.update(_p('odmx.cv_quality_code', codes.get(q, {'qa_flag': q})))
        items.append(o)
    h.append('odmx.cv_quality_code'), v.append(_dump(items))
    assert not any('\t' in x or '\n' in x for x in v)
    return h, v


def write_tsv(path, header, rows):
    with open(path, 'w', encoding='utf-8', newline='') as fh:
        fh.write('\t'.join(header) + '\n')
        for r in rows:
            fh.write('\t'.join(r) + '\n')

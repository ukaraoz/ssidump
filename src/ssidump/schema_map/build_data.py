import json, re, sys
from collections import Counter, defaultdict

schema_json, feeder_names, ds_names, out = sys.argv[1:5]
d = json.load(open(schema_json))

# Ordered rules: first match wins. Patterns are full-name regexes.
MODULES = [
    ('cv', 'Controlled vocabularies', [r'cv_.*']),
    ('genomics', 'Genomics & MAGs', [r'mags?', r'mag_.*', r'contigs', r'genes', r'gene_annotations', r'assembly.*',
                                    r'binning_event', r'coverage', r'extracted_dna', r'qcd_reads', r'raw_reads',
                                    r'sequencing_facility', r'bridge_.*']),
    ('streams', 'Datastreams & sensors', [r'datastream.*', r'.*_datastreams?', r'timeseries_datastream_template',
                                         r'calculated_datastream_expression', r'datalogger_.*', r'data_channel',
                                         r'variable_mapping', r'variable_qa_min_max', r'qaqc_.*', r'heartbeat',
                                         r'soilprobe_.*', r'external_connection', r'datasource_description',
                                         r'phenocam_.*', r'instrument_output_quantity_kind']),
    ('results', 'Results & values', [r'.*_result_values?', r'.*_results', r'.*_result_value_annotations', r'results.*',
                                    r'result_.*', r'related_results', r'derivation_equations', r'data_quality']),
    ('equipment', 'Equipment & calibration', [r'equipment.*', r'calibration_.*', r'maintenance_actions',
                                             r'instrument_actions', r'image_equipment_bridge', r'related_equipment',
                                             r'conversion_multiplier_offset']),
    ('features', 'Sampling features & specimens', [r'sampling_feature.*', r'sampled_features', r'specimen.*',
                                                  r'features_of_interest', r'related_features', r'spatial_.*',
                                                  r'organization_sampling_feature_bridge',
                                                  r'image_sampling_feature_bridge', r'images', r'image_persons_bridge']),
    ('geophys', 'Geophysics', [r'geoelectric_.*', r'survey_electrode_geometry']),
    ('sims', 'Simulations & models', [r'simulation.*', r'models', r'model_affiliations', r'related_models',
                                     r'related_simulations']),
    ('matrix', 'Matrix datasets', [r'matrix_.*']),
    ('people', 'People, projects & access', [r'persons', r'person_external_identifiers', r'organizations',
                                            r'affiliations', r'accounts', r'account_roles', r'roles',
                                            r'role_permissions', r'permissions', r'owned_entities', r'project.*',
                                            r'author_lists']),
    ('cite', 'Citations & datasets', [r'citation.*', r'dataset.*', r'related_citations', r'related_datasets',
                                     r'method_citations']),
    ('core', 'Actions, methods & variables', [r'actions', r'action_.*', r'directives', r'related_actions',
                                             r'feature_actions', r'methods', r'method_.*', r'variables',
                                             r'variables?_.*', r'processing_levels', r'units_quantity_kind_bridge',
                                             r'taxonomic_classifier.*', r'reference_material.*']),
    ('annot', 'Annotations & extensions', [r'annotations', r'related_annotations', r'extension_properties',
                                          r'external_identifier_systems', r'.*_annotations', r'.*_extension_property_values',
                                          r'.*_external_identifiers']),
]


def module_of(name):
    for key, _, pats in MODULES:
        if any(re.fullmatch(p, name) for p in pats):
            return key
    return 'other'


tables = []
for t in d['detailed']:
    mod = 'knowledge' if t['schema'] == 'knowledge' else module_of(t['name'])
    pk = set(t['pk'])
    fkcols = {c: f['ref'] for f in t['fks'] for c in f['cols']}
    tables.append({
        's': t['schema'], 'n': t['name'], 'm': mod,
        'c': [[c['n'], c['t'], 1 if c['nn'] else 0, 1 if c['n'] in pk else 0, fkcols.get(c['n'], '')] for c in t['cols']],
        'f': [[f['cols'], f['ref']] for f in t['fks']],
    })
unassigned = [t['n'] for t in tables if t['m'] == 'other']

feeder = [l.strip() for l in open(feeder_names) if l.strip()]
ds = [l.strip() for l in open(ds_names) if l.strip()]


def family(name):
    n = name.lower()
    if n.endswith('_streamflow'):
        return 'streamflow gauges'
    if 'cr1000' in n:
        return 'cr1000 loggers'
    if n.startswith('feeder_ess_dive'):
        return 'ess-dive'
    if n.startswith('feeder_'):
        return 'feeder (other)'
    return re.split(r'[_-]', n, maxsplit=1)[0]


def kind(name):
    if name.startswith('geochemdata_'):
        return 'Geochemistry, no depth' if name.endswith('_no_depth') else 'Geochemistry, at a depth'
    if name.endswith('_calc'):
        return 'Calculated'
    if name.endswith('_depth'):
        return 'Sensor, at a depth'
    if name.endswith('_meas'):
        return 'Sensor measurement'
    return 'Other'


# Map each datastream to the longest feeder table name it starts with.
by_len = sorted(feeder, key=len, reverse=True)
first = defaultdict(list)
for f in by_len:
    first[f[:3].lower()].append(f)
ds_to_feeder = {}
for n in ds:
    for f in first.get(n[:3].lower(), []):
        if n.lower().startswith(f.lower() + '_'):
            ds_to_feeder[n] = f
            break

fam_feeder = Counter(family(f) for f in feeder)
fam_ds = Counter(family(ds_to_feeder[n]) for n in ds_to_feeder)
unmatched = [n for n in ds if n not in ds_to_feeder]
unmatched_fam = Counter(family(n) for n in unmatched)
suffix = Counter(kind(n) for n in ds)

families = []
for fam, cnt in fam_feeder.most_common():
    ex = sorted(f for f in feeder if family(f) == fam)[:4]
    families.append({'fam': fam, 'tables': cnt, 'streams': fam_ds.get(fam, 0), 'ex': ex})

data = {
    'summary': d['schemas'],
    'modules': [[k, label] for k, label, _ in MODULES] + [['knowledge', 'Knowledge base']],
    'tables': tables,
    'odmx_views': d['odmx_views'],
    'ds_layouts': [{'count': g['count'], 'cols': [[c['n'], c['t']] for c in g['cols']], 'ex': g['examples'][:4]} for g in d['groups']['datastreams']],
    'feeder_layouts': [{'count': g['count'], 'ncols': g['ncols'], 'cols': [[c['n'], c['t']] for c in g['cols']], 'ex': g['examples'][:3]} for g in d['groups']['feeder']],
    'families': families,
    'ds_matched': len(ds_to_feeder), 'ds_total': len(ds),
    'ds_unmatched_fam': unmatched_fam.most_common(12),
    'ds_suffix': suffix.most_common(),
}
json.dump(data, open(out, 'w'), separators=(',', ':'))
print('unassigned:', unassigned)
print('module sizes:', Counter(t['m'] for t in tables))
print('matched', len(ds_to_feeder), 'of', len(ds), 'unmatched fams', unmatched_fam.most_common(8))
print('families', [(f['fam'], f['tables'], f['streams']) for f in families[:15]])
print('suffix', suffix)
import os; print(os.path.getsize(out))

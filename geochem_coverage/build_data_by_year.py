"""Aggregate a geochem datastreams export into per site/depth, year and analyte counts.

    python3 build_data_by_year.py geochemdata_all.tsv page_data.json
"""
import json, sys
import pandas as pd
from build_data import groups_for   # same analyte column groups as the all-years page

src, out = sys.argv[1:3]
d = pd.read_csv(src, sep='\t', low_memory=False)
d['dt'] = pd.to_datetime(d.datetime_utc)
d['year'] = d.dt.dt.year
d['dsort'] = d.depth.fillna(-1.0).astype(float)          # -1 = no depth recorded

GROUPS = groups_for(d)
cols = [c for _, vs in GROUPS for c in vs]
ci = {c: i for i, c in enumerate(cols)}
units = d.groupby('variable_name').units.first().to_dict()

rk = d[['site_code', 'site_name', 'site_type', 'dsort']].drop_duplicates(['site_code', 'dsort']).sort_values(['site_code', 'dsort'])
rows, ri = [], {}
for i, r in enumerate(rk.itertuples()):
    rows.append([r.site_code, r.site_name, r.site_type, None if r.dsort < 0 else r.dsort])
    ri[(r.site_code, r.dsort)] = i

yk = d[['site_code', 'dsort', 'year']].drop_duplicates().sort_values(['site_code', 'dsort', 'year'])
yrows, yi = [], {}
for i, r in enumerate(yk.itertuples()):
    yrows.append([ri[(r.site_code, r.dsort)], int(r.year)])
    yi[(r.site_code, r.dsort, r.year)] = i

g = (d.groupby(['site_code', 'dsort', 'year', 'variable_name'])
       .agg(n=('data_value', 'size'), t0=('dt', 'min'), t1=('dt', 'max')).reset_index())
cells = [[yi[(r.site_code, r.dsort, r.year)], ci[r.variable_name], int(r.n),
          r.t0.strftime('%m-%d'), r.t1.strftime('%m-%d')] for r in g.itertuples()]

json.dump(dict(rows=rows, yrows=yrows, cols=cols, units=[units[c] for c in cols],
               groups=[[n, len(vs)] for n, vs in GROUPS], cells=cells, total=len(d),
               span=[d.dt.min().strftime('%Y-%m-%d'), d.dt.max().strftime('%Y-%m-%d')]),
          open(out, 'w'), separators=(',', ':'))
print(f'{len(d)} data points -> {len(rows)} site/depth groups, {len(yrows)} year rows x {len(cols)} analytes ({len(cells)} filled cells)')

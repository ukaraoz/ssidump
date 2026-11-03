"""Aggregate a geochem datastreams export into per site/depth/analyte counts for the coverage page.

    python3 build_data.py geochemdata_all.tsv page_data.json
"""
import json, sys
import pandas as pd

# Column groups: everything not named below is an ICP-MS metal / trace element.
NAMED = [('Anions', ['Chloride', 'Fluoride', 'Nitrate', 'Nitrite', 'Phosphate', 'Sulfate']),
         ('N', ['TDN', 'Ammonia-N']),
         ('C', ['DIC', 'NPOC']),
         ('Iso', ['Delta D', 'Delta O18'])]


def groups_for(d):
    present = set(d.variable_name)
    named = [(n, [v for v in vs if v in present]) for n, vs in NAMED]
    used = {v for _, vs in named for v in vs}
    return [('Metals & trace elements (ICP-MS)', sorted(present - used))] + [x for x in named if x[1]]


def main():
    src, out = sys.argv[1:3]
    d = pd.read_csv(src, sep='\t', low_memory=False)
    d['dt'] = pd.to_datetime(d.datetime_utc)
    d['dsort'] = d.depth.fillna(-1.0).astype(float)          # -1 = no depth recorded

    g = (d.groupby(['site_code', 'site_name', 'site_type', 'dsort', 'variable_name'])
           .agg(n=('data_value', 'size'), t0=('dt', 'min'), t1=('dt', 'max')).reset_index())

    GROUPS = groups_for(d)
    cols = [c for _, vs in GROUPS for c in vs]
    ci = {c: i for i, c in enumerate(cols)}
    units = d.groupby('variable_name').units.first().to_dict()

    rk = g[['site_code', 'site_name', 'site_type', 'dsort']].drop_duplicates(['site_code', 'dsort']).sort_values(['site_code', 'dsort'])
    rows, ri = [], {}
    for i, r in enumerate(rk.itertuples()):
        rows.append([r.site_code, r.site_name, r.site_type, None if r.dsort < 0 else r.dsort])
        ri[(r.site_code, r.dsort)] = i

    cells = [[ri[(r.site_code, r.dsort)], ci[r.variable_name], int(r.n),
              r.t0.strftime('%Y-%m-%d'), r.t1.strftime('%Y-%m-%d')] for r in g.itertuples()]

    json.dump(dict(rows=rows, cols=cols, units=[units[c] for c in cols],
                   groups=[[n, len(vs)] for n, vs in GROUPS], cells=cells, total=len(d),
                   span=[d.dt.min().strftime('%Y-%m-%d'), d.dt.max().strftime('%Y-%m-%d')]),
              open(out, 'w'), separators=(',', ':'))
    print(f'{len(d)} data points -> {len(rows)} site/depth rows x {len(cols)} analytes ({len(cells)} filled cells)')


if __name__ == '__main__':
    main()

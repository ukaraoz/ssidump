"""Aggregate an ssiexport datastreams export into data-point counts for the coverage pages.

    python3 build_data.py DATASET export.tsv[.gz] all.json by_year.json meta.json

DATASET is a key of DATASETS below. Two page datasets are written: one cell per
site/depth x variable (all years), and one per site/depth x year x variable.
meta.json holds the page text the build script fills into the templates.
"""
import json, math, sys
import numpy as np
import pandas as pd

GEOCHEM_GROUPS = [('Anions', ['Chloride', 'Fluoride', 'Nitrate', 'Nitrite', 'Phosphate', 'Sulfate']),
                  ('N', ['TDN', 'Ammonia-N']),
                  ('C', ['DIC', 'NPOC']),
                  ('Iso', ['Delta D', 'Delta O18'])]

DATASETS = {
    'geochem': dict(
        label='Geochem', family='geochem', col='analyte', cols='analytes',
        groups=GEOCHEM_GROUPS, rest='Metals & trace elements (ICP-MS)',
        bins=[[1, 1], [2, 5], [6, 10], [11, 25], [26, 50], [51, 100], [101, 500], [501, None]],
        bins_year=[[1, 1], [2, 3], [4, 6], [7, 12], [13, 25], [26, 50], [51, 100], [101, None]],
        note='Rows without a depth are surface water or sites where the export records no depth.'),
    'decagon': dict(
        label='Decagon sensors', family='decagon', col='variable', cols='variables',
        groups=[('Weather (tower)', ['Absolute Atmospheric Pressure', 'Air Temperature', 'Gust Speed',
                                     'Incoming Broadband Solar Radiation', 'Lightning Average Distance',
                                     'Lightning Strike Count', 'Precipitation Rate', 'Rain Precipitation',
                                     'Relative Humidity', 'Vapor Pressure Deficit', 'Vapor Pressure in Air',
                                     'Wind Direction', 'Wind Speed']),
                ('Instrument', ['Battery Charge', 'Battery Voltage', 'Instrument Orientation X',
                                'Instrument Orientation Y', 'Instrument Reference Pressure',
                                'Instrument Reference Temperature'])],
        rest='Soil',
        note='Several sensors at one station can report the same variable (for example soil temperature '
             'from MPS-6, 5TE and ECT probes on different ports); their counts add up in one cell. '
             'The export records no sensor depths. The four Pumphouse and four Snodgrass towers also log '
             'weather and instrument readings.'),
    'streamflow': dict(
        label='Streamflow gauges', family='streamflow', col='variable', cols='variables',
        note=''),
    'mloggers': dict(
        label='Hillslope loggers M1–M4', select='--pattern "^m[1-4]_"', col='variable', cols='variables',
        groups=[('Groundwater', ['Vented Water Pressure', 'Water Electrical Conductivity', 'Water Height',
                                 'Water Level', 'Water Specific Conductance', 'Water Temperature'])],
        rest='Soil',
        note='M1–M4 are the CR1000 loggers (tables m1_10, m2_20, m3_30, m4_40) at the PLM hillslope boreholes '
             'and wells, ER-PLM1–4 and ER-PLM6. The export records no sensor depths.'),
    'metstation': dict(
        label='Met station', select='--pattern "^metstation_"', col='variable', cols='variables',
        groups=[('Soil', ['Soil Electrical Conductivity', 'Soil Temperature', 'Soil Volumetric Water Content'])],
        rest='Weather',
        note='One station, the Pumphouse weather station (ER-CSMWS).'),
    'snotel': dict(
        label='SNOTEL sites', family='snotel', col='variable', cols='variables',
        groups=[('Snow', ['Precipitation Snow Adjusted, daily', 'Snow Thickness', 'Snow Water Equivalent'])],
        rest='Weather',
        note='NRCS SNOTEL stations ingested into the SSI database. Records start in 1997.'),
    'gradient': dict(
        label='Soil-moisture gradient', family='gradient_sm', col='variable', cols='variables',
        note=''),
    'cr1000': dict(
        label='CR1000 weather stations', select='--pattern "^(am|bb|jf|kp|sm|mc|cb)_"', col='variable', cols='variables',
        groups=[('Soil', ['Soil Electrical Conductivity', 'Soil Temperature', 'Soil Volumetric Water Content']),
                ('Water quality (YSI sonde)', ['Absolute Water Pressure', 'FDOM', 'FDOM in QSU',
                    'NLF Water Electrical Conductivity', 'Optical Dissolved Oxygen in Water',
                    'Optical Dissolved Oxygen in Water percent air saturation', 'Water Electrical Conductivity',
                    'Water Height', 'Water ORP', 'Water Salinity', 'Water Specific Conductance', 'Water Temperature',
                    'Water Total Dissolved Solids', 'Water Total Suspended Solids', 'Water Turbidity',
                    'Water Turbidity in NTU', 'Water ph mv', 'pH'])],
        rest='Weather & radiation',
        note='Campbell CR1000 loggers at six weather stations (Almont, Billy Barr, Judd Falls, Kettle Ponds, '
             'Mexican Cut, Snodgrass Mountain), the YSI water-quality sonde at Pumphouse ISCO (ER-PHF0) and the '
             'PH_SOLINST barometric logger (ER-PHF2). M1–M4 have their own page.'),
    'towers': dict(
        label='Sensor towers', select='--pattern "^feeder_(er|sg|tr)_"', col='variable', cols='variables',
        groups=[('Fluxes', ['Carbon Dioxide Concentration', 'Evapotranspiration', 'Latent Heat Flux',
                            'Sensible Heat Flux']),
                ('Soil', ['Soil CO2 Concentration', 'Soil CO2 Flux', 'Soil Heat Flux',
                          'Soil Oxygen Concentration Percent', 'Soil Temperature'])],
        rest='Weather & radiation',
        note='The Pumphouse (ER-PHS1–4), Snodgrass (SG-EHS5–8) and Trail Creek (TR-EHS1–2) towers. '
             'Their Decagon soil sensors are on the Decagon page.'),
    'essdive': dict(
        label='ESS-DIVE datasets', family='ess_dive', col='variable', cols='variables',
        note='Water level and temperature datasets ingested from ESS-DIVE.'),
    'phenocam': dict(
        label='PhenoCam', family='phenocam', col='variable', cols='variables',
        note='Greenness indices from the Snodgrass and Trail Creek PhenoCams.'),
    'nwis': dict(
        label='USGS NWIS gauges', family='nwis', col='variable', cols='variables',
        note='USGS National Water Information System gauges ingested into the SSI database.'),
}


def nice(x):
    """Round up to 1, 2, 2.5, 5 x 10^k."""
    if x <= 1:
        return 1
    e = 10 ** math.floor(math.log10(x))
    for m in (1, 2, 2.5, 5, 10):
        if x <= m * e:
            return int(m * e)


def auto_bins(counts, n=8):
    """Up to n bins with round edges at the count quantiles, so each colour holds a similar share of cells."""
    edges = []
    for q in np.linspace(1 / n, 1 - 1 / n, n - 1):
        e = nice(float(np.quantile(counts, q)))
        if not edges or e > edges[-1]:
            edges.append(e)
    bins, a = [], 1
    for e in edges:
        bins.append([a, e]); a = e + 1
    bins.append([a, None])
    return bins


def label_bins(bins):
    k = len(bins)
    out = []
    for i, (a, b) in enumerate(bins):
        f = lambda v: f'{v:,}'
        lab = f(a) if a == b else (f'{f(a)}+' if b is None else f'{f(a)}–{f(b)}')
        out.append([a, b, lab, round(i * 7 / (k - 1)) if k > 1 else 7])   # spread over the 8-step ramp
    return out


def column_groups(cfg, present):
    if not cfg.get('groups'):
        return [('', sorted(present))]   # one unlabelled group
    named = [(n, [v for v in vs if v in present]) for n, vs in cfg['groups']]
    # unlisted columns go first, under cfg['rest']
    used = {v for _, vs in named for v in vs}
    return [(cfg['rest'], sorted(present - used))] + [x for x in named if x[1]]


AGG = dict(n=('t', 'size'), t0=('t', 'min'), t1=('t', 'max'))


def aggregate(src, chunksize=5_000_000):
    """Counts and first/last time per (site, depth, variable) and per (site, depth, year, variable), read in chunks."""
    cat = {c: 'category' for c in ('site_code', 'site_name', 'site_type', 'variable_name', 'units')}
    keys = ['site_code', 'dsort', 'variable_name']
    parts, yparts, sites, units, total, tmin, tmax = [], [], [], {}, 0, None, None
    for d in pd.read_csv(src, sep='\t', usecols=list(cat) + ['depth', 'utc_time'], dtype=cat, chunksize=chunksize):
        d['t'] = pd.to_datetime(d.utc_time, unit='s')
        d['year'] = d.t.dt.year
        d['dsort'] = d.depth.fillna(-1.0).astype(float)          # -1 = no depth recorded
        for c in cat:
            d[c] = d[c].astype(str)
        parts.append(d.groupby(keys).agg(**AGG))
        yparts.append(d.groupby(keys[:2] + ['year', 'variable_name']).agg(**AGG))
        sites.append(d[['site_code', 'site_name', 'site_type', 'dsort']].drop_duplicates())
        for v, u in d.groupby('variable_name').units.first().items():
            units.setdefault(v, u)
        total += len(d)
        tmin = d.t.min() if tmin is None else min(tmin, d.t.min())
        tmax = d.t.max() if tmax is None else max(tmax, d.t.max())
    comb = lambda ps: (pd.concat(ps).groupby(level=list(range(ps[0].index.nlevels)))
                         .agg(n=('n', 'sum'), t0=('t0', 'min'), t1=('t1', 'max')).reset_index())
    sites = pd.concat(sites).drop_duplicates(['site_code', 'dsort']).sort_values(['site_code', 'dsort'])
    return comb(parts), comb(yparts), sites, units, total, tmin, tmax


def main():
    key, src, out_all, out_year, out_meta = sys.argv[1:6]
    cfg = DATASETS[key]
    g, gy, rk, units, total, tmin, tmax = aggregate(src)

    GROUPS = column_groups(cfg, set(g.variable_name))
    cols = [c for _, vs in GROUPS for c in vs]
    ci = {c: i for i, c in enumerate(cols)}

    rows, ri = [], {}
    for i, r in enumerate(rk.itertuples()):
        rows.append([r.site_code, r.site_name, r.site_type, None if r.dsort < 0 else r.dsort])
        ri[(r.site_code, r.dsort)] = i
    common = dict(rows=rows, cols=cols, units=[units[c] for c in cols],
                  groups=[[n, len(vs)] for n, vs in GROUPS], total=total,
                  span=[tmin.strftime('%Y-%m-%d'), tmax.strftime('%Y-%m-%d')])

    cells = [[ri[(r.site_code, r.dsort)], ci[r.variable_name], int(r.n),
              r.t0.strftime('%Y-%m-%d'), r.t1.strftime('%Y-%m-%d')] for r in g.itertuples()]
    bins = cfg.get('bins') or auto_bins([c[2] for c in cells])
    json.dump(dict(common, cells=cells, bins=label_bins(bins)), open(out_all, 'w'), separators=(',', ':'))

    yk = gy[['site_code', 'dsort', 'year']].drop_duplicates().sort_values(['site_code', 'dsort', 'year'])
    yrows, yi = [], {}
    for i, r in enumerate(yk.itertuples()):
        yrows.append([ri[(r.site_code, r.dsort)], int(r.year)])
        yi[(r.site_code, r.dsort, r.year)] = i
    ycells = [[yi[(r.site_code, r.dsort, r.year)], ci[r.variable_name], int(r.n),
               r.t0.strftime('%m-%d'), r.t1.strftime('%m-%d')] for r in gy.itertuples()]
    ybins = cfg.get('bins_year') or auto_bins([c[2] for c in ycells])
    json.dump(dict(common, yrows=yrows, cells=ycells, bins=label_bins(ybins)), open(out_year, 'w'), separators=(',', ':'))

    sel = cfg.get('select') or f'--family {cfg["family"]}'
    json.dump(dict(cfg, key=key, file=src.rsplit('/', 1)[-1], select=sel), open(out_meta, 'w'))
    print(f'{key}: {total:,} data points -> {len(rows)} site/depth rows, {len(yrows)} year rows '
          f'x {len(cols)} {cfg["cols"]} ({len(cells)} / {len(ycells)} filled cells)')


if __name__ == '__main__':
    main()

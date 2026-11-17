"""Build the cross-source availability pages from the summary tables.

    python3 src/ssidump/coverage/build_site_variable.py [SUMMARIES_DIR] [PAGES_DIR] [--png]

Reads SUMMARIES_DIR/site_variable_pairs.tsv and unique_variables.tsv (default <repository>/exports/summaries)
and writes into PAGES_DIR (default <repository>/coverage):
  site_variable_heatmap.html       sampling features x variables, colour = years between first and last good value
  site_variable_date_ranges.html   columns = date ranges (first - last month), then the variables sharing each range
With --png, also renders each page to a PNG (full figure, no controls) with headless Chrome
($CHROME, default the macOS Google Chrome app).
"""
import csv
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
TYPE_ORDER = ['site', 'weatherStation', 'waterQualityStation', 'borehole', 'fieldArea']
CHROME = os.environ.get('CHROME', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
# print versions for the PNGs: whole figure, no controls / table view
PRINT_CSS = {
    'site_variable_heatmap.html': ('.scroll{max-height:none!important;overflow:visible!important}.controls,details{display:none!important}'
                                   'main{padding-bottom:12px;max-width:1460px!important}', (1500, 1680)),
    'site_variable_date_ranges.html': ('.right{overflow:visible!important}.frame{overflow:visible!important;display:inline-flex!important}'
                                       'main{max-width:none!important;padding-bottom:12px}.controls,details{display:none!important}',
                                       (6030, 1640)),
}


def data(summaries):
    rd = lambda f: list(csv.DictReader(open(summaries / f, encoding='utf-8'), delimiter='\t'))   # noqa: E731
    pairs, uv = rd('site_variable_pairs.tsv'), rd('unique_variables.tsv')
    cat = {r['variable']: r['category'] for r in uv}
    cat_order = list(dict.fromkeys(r['category'] for r in uv))

    def years(a, b):
        if not a or not b:
            return None
        return round((dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days / 365.25, 2)
    sites = {}
    for p in pairs:
        sites.setdefault(p['sampling_feature_code'], {'code': p['sampling_feature_code'], 'name': p['sampling_feature_name'],
                                                      'type': p['sampling_feature_type'], 'lat': p['latitude'], 'lon': p['longitude']})
    variables = sorted({p['variable'] for p in pairs}, key=lambda v: (cat_order.index(cat[v]), v))
    return {'sites': sorted(sites.values(), key=lambda s: (TYPE_ORDER.index(s['type']), s['code'])),
            'variables': [{'v': v, 'c': cat[v]} for v in variables],
            'cells': [{'s': p['sampling_feature_code'], 'v': p['variable'], 'u': p['units'], 'n': int(p['n_tables']),
                       'src': p['sources'], 'f': p['first'], 'l': p['last'], 'y': years(p['first'], p['last'])} for p in pairs],
            'types': TYPE_ORDER, 'cats': cat_order, 'built': dt.date.today().isoformat()}


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    summaries = Path(args[0]) if args else REPO / 'exports' / 'summaries'
    pages = Path(args[1]) if len(args) > 1 else REPO / 'coverage'
    d = json.dumps(data(summaries), separators=(',', ':')).replace('</', '<\\/')
    for name in PRINT_CSS:
        tpl = (HERE / name.replace('.html', '_template.html')).read_text(encoding='utf-8')
        out = pages / name
        out.write_text(tpl.replace('__DATA__', d), encoding='utf-8')
        print(f'wrote {out}')
        if '--png' in sys.argv:
            css, (w, h) = PRINT_CSS[name]
            html = out.read_text(encoding='utf-8').replace('</style>', css + '</style>').replace(' Scroll sideways for later ranges.', '')
            with tempfile.NamedTemporaryFile('w', suffix='.html', delete=False, encoding='utf-8') as fh:
                fh.write(html)
            png = out.with_suffix('.png')
            subprocess.run([CHROME, '--headless=new', '--disable-gpu', '--hide-scrollbars', '--force-device-scale-factor=2',
                            f'--window-size={w},{h}', f'--screenshot={png}', f'file://{fh.name}'], capture_output=True)
            os.unlink(fh.name)
            print(f'wrote {png}')


if __name__ == '__main__':
    main()

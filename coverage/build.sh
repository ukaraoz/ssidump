#!/usr/bin/env bash
# Build the two coverage pages (all years, and by year) for one datastreams export.
#
#   ssiexport.py export datastreams --family geochem --out geochemdata_all.tsv
#   coverage/build.sh geochem    geochemdata_all.tsv       geochem_coverage/
#   coverage/build.sh decagon    decagon_all.tsv.gz        coverage/
#   coverage/build.sh streamflow streamflow_all.tsv        coverage/
#
# Writes <dataset>_coverage.html and <dataset>_coverage_by_year.html into the output
# folder. Datasets and their page text are defined in build_data.py (DATASETS).
set -euo pipefail

key=${1:?usage: build.sh DATASET export.tsv[.gz] outdir}
tsv=${2:?usage: build.sh DATASET export.tsv[.gz] outdir}
outdir=${3:?usage: build.sh DATASET export.tsv[.gz] outdir}
here=$(cd "$(dirname "$0")" && pwd)
py=${PYTHON:-python3}
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$outdir"

"$py" "$here/build_data.py" "$key" "$tsv" "$tmp/all.json" "$tmp/year.json" "$tmp/meta.json"

"$py" - "$here" "$tmp" "$outdir" <<'PY'
import html, json, sys
here, tmp, outdir = sys.argv[1:4]
m = json.load(open(f'{tmp}/meta.json'))
k, label, col, cols = m['key'], m['label'], m['col'], m['cols']
code = lambda s: f'<code style="font-family:var(--f-data);font-size:12.5px">{html.escape(s)}</code>'
all_page, year_page = f'{k}_coverage.html', f'{k}_coverage_by_year.html'
link = lambda href, text: f'<a href="{href}" style="color:var(--accent)">{html.escape(text)}</a>'
cmd = f'ssiexport.py export datastreams {m["select"]}'
note = html.escape(m['note'])
pages = {
    'all': dict(tpl='template.html', data='all.json', out=all_page,
        TITLE=f'East River {label} Coverage',
        H1=f'Data points per site, depth and {col}',
        LEDE=f'Each cell counts the measurements in {code(m["file"])} for one {col} at one site and sampling depth. '
             f'{note} For a year-by-year breakdown see {link(year_page, label + " by year")}.',
        FOOT=f'Source: <code>{html.escape(m["file"])}</code>, exported from the SSI database with <code>{cmd}</code>. '
             'Hover or tap a cell for its date range. Depths are in meters as recorded by the datastream.'),
    'year': dict(tpl='template_by_year.html', data='year.json', out=year_page,
        TITLE=f'East River {label} by Year',
        H1=f'Data points per site, depth, year and {col}',
        LEDE=f'Each site and depth has one row per year with data. Each cell counts the measurements in {code(m["file"])} '
             f'for one {col} in that year. {note} Click a site row to collapse or expand its years. '
             f'The all-years version is {link(all_page, label + " coverage")}.',
        FOOT=f'Source: <code>{html.escape(m["file"])}</code>, exported from the SSI database with <code>{cmd}</code>. '
             'Hover or tap a cell for the first and last sample date in that year. Years with no data at a site are not shown. '
             'Depths are in meters as recorded by the datastream.'),
}
for p in pages.values():
    page = open(f'{here}/{p["tpl"]}', encoding='utf-8').read()
    for key in ('TITLE', 'H1', 'LEDE', 'FOOT'):
        page = page.replace(f'__{key}__', p[key])
    page = page.replace('__LABEL_LC__', html.escape(label.lower())).replace('__COLWORD_PL__', html.escape(cols)).replace('__COLWORD__', html.escape(col))
    page = page.replace('__DATA__', open(f'{tmp}/{p["data"]}').read().replace('</', '<\\/'))
    assert '__' not in page.split('<script>')[0].replace('__proto__', ''), 'unfilled placeholder'
    open(f'{outdir}/{p["out"]}', 'w', encoding='utf-8').write(page)
    print(f'wrote {outdir}/{p["out"]}')
PY

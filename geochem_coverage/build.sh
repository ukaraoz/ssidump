#!/usr/bin/env bash
# Build the geochem coverage pages (all years, and by year) from a geochem datastreams export.
#
#   ssiexport.py export datastreams --family geochem --out geochemdata_all.tsv
#   geochem_coverage/build.sh geochemdata_all.tsv geochem_coverage/
#
# Writes geochem_coverage.html and geochem_coverage_by_year.html into the output folder.
set -euo pipefail

tsv=${1:?usage: build.sh geochemdata_all.tsv outdir}
outdir=${2:?usage: build.sh geochemdata_all.tsv outdir}
here=$(cd "$(dirname "$0")" && pwd)
py=${PYTHON:-python3}
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$outdir"

"$py" "$here/build_data.py" "$tsv" "$tmp/all.json"
"$py" "$here/build_data_by_year.py" "$tsv" "$tmp/by_year.json"

fill() {
"$py" - "$1" "$2" "$3" <<'PY'
import sys
tpl, data, out = sys.argv[1:4]
page = open(tpl, encoding='utf-8').read().replace('__DATA__', open(data).read().replace('</', '<\\/'))
open(out, 'w', encoding='utf-8').write(page)
print(f'wrote {out}')
PY
}
fill "$here/template.html" "$tmp/all.json" "$outdir/geochem_coverage.html"
fill "$here/template_by_year.html" "$tmp/by_year.json" "$outdir/geochem_coverage_by_year.html"

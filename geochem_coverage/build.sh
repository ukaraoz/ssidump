#!/usr/bin/env bash
# Build the geochem coverage page from a geochem datastreams export.
#
#   ssiexport.py export datastreams --family geochem --out geochemdata_all.tsv
#   geochem_coverage/build.sh geochemdata_all.tsv geochem_coverage/geochem_coverage.html
set -euo pipefail

tsv=${1:?usage: build.sh geochemdata_all.tsv out.html}
out=${2:?usage: build.sh geochemdata_all.tsv out.html}
here=$(cd "$(dirname "$0")" && pwd)
py=${PYTHON:-python3}
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

"$py" "$here/build_data.py" "$tsv" "$tmp/page_data.json"

"$py" - "$here/template.html" "$tmp/page_data.json" "$out" <<'PY'
import sys
tpl, data, out = sys.argv[1:4]
page = open(tpl, encoding='utf-8').read().replace('__DATA__', open(data).read().replace('</', '<\\/'))
open(out, 'w', encoding='utf-8').write(page)
print(f'wrote {out}')
PY

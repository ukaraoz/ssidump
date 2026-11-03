#!/usr/bin/env bash
# Build the schema map page from a schema-only SQL file.
#
#   pg_restore -s -f schema_only.sql ssi_YYYY-MM-DD_HHMMSSZ.dump
#   schema_map/build.sh schema_only.sql ssi_schemas.html
set -euo pipefail

sql=${1:?usage: build.sh schema_only.sql out.html}
out=${2:?usage: build.sh schema_only.sql out.html}
here=$(cd "$(dirname "$0")" && pwd)
py=${PYTHON:-python3}
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

grep -o '^CREATE TABLE feeder\.[^ ]*' "$sql" | sed 's/CREATE TABLE feeder\.//; s/"//g' > "$tmp/feeder_names.txt"
grep -o '^CREATE TABLE datastreams\.[^ ]*' "$sql" | sed 's/CREATE TABLE datastreams\.//; s/"//g' > "$tmp/ds_names.txt"

"$py" "$here/parse_schema.py" "$sql" "$tmp/schema.json"
"$py" "$here/build_data.py" "$tmp/schema.json" "$tmp/feeder_names.txt" "$tmp/ds_names.txt" "$tmp/page_data.json"

"$py" - "$here/template.html" "$tmp/page_data.json" "$out" <<'EOF'
import sys
tpl, data, out = sys.argv[1:4]
page = open(tpl).read().replace('__DATA__', open(data).read().replace('</', '<\\/'))
open(out, 'w').write(page)
print(f'wrote {out}')
EOF

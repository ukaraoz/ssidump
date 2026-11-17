#!/bin/bash
# Build the database from the dump in the project root and export it. Everything stays in the root:
#   pgenv/   PostgreSQL 17 + PostGIS (conda)      pgdata/  database cluster     pgrun/  socket
#   logs/    restore logs                          exports/tables_<dump date>/   the export
# Project root: $SSIDUMP_ROOT, else the repository holding this script (src/ssidump/scripts/ -> ../../..).
# Run from anywhere:  src/ssidump/scripts/run_restore_export.sh > run.log 2>&1
# Uses the code in <root>/src without installing it (python3 -m ssidump.<module>).
set -euo pipefail
JOBS=${JOBS:-40}   # tables exported in parallel
ROOT="${SSIDUMP_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
export SSIDUMP_ROOT="$ROOT" SSIDUMP_PORT=54329 PGPORT=54329 PGHOST="$ROOT/pgrun" PGUSER=ssi PGDATABASE=odmx_lbnlsfa
cd "$ROOT"
DUMP=$(ls ssi_*.dump | tail -1)
TAG=$(echo "$DUMP" | sed -E 's/^ssi_([0-9-]+)_.*/\1/')
echo "== $(date) on $(hostname): setup ($ROOT, dump $DUMP)"
python3 -m ssidump.server setup
python3 -m ssidump.server start
if ! python3 -m ssidump.server status | grep -q '^odmx_lbnlsfa'; then
  echo "== $(date): restore"
  python3 -m ssidump.server restore "$DUMP" -j 4
fi
echo "== $(date): export ($JOBS tables in parallel)"
nice -n 10 python3 -m ssidump.exportall --schema datastreams --schema odmx --schema knowledge --schema feeder \
  --exclude 'datastreams:^sentinel_' --exclude 'feeder:^sentinel_' \
  --first 'datastreams:^influxdb_' --first 'feeder:^influxdb_' \
  --out "exports/tables_$TAG" -j "$JOBS" --table-timeout 900 || echo "== export finished with failures (rerun this script to retry them)"
echo "== $(date): stop"
python3 -m ssidump.server stop
echo "== $(date): done"

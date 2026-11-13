# SSI / ODMX database dumps

Tools for opening the nightly `ssi_*.dump` files of the `odmx_lbnlsfa` database
(LBNL SFA, East River watershed ODMX) on NERSC Perlmutter. They can:

- **restore** a dump into a private local PostgreSQL 17 + PostGIS server and query it with SQL;
- **export** tables from a dump straight to CSV, with no server needed.

**On Perlmutter everything lives in one self-contained folder on CFS:
`/global/cfs/cdirs/m4915/roelef_ssidump`** (the dump, these scripts, the module file, its own
PostgreSQL install in `pgenv/`, the database in `pgdata/`, and the exports in `exports/`). Nothing in
it is a symlink to anywhere else.

The dumps are written by pg_dump 16 (archive format 1.15). The system `pg_restore`
(version 12) can't read them, so these tools ship their own PostgreSQL 17 in `pgenv/`.

**[Browse the schema map](https://ukaraoz.github.io/ssidump/schema_map/ssi_schemas.html)**:
an interactive map of every schema and table in the 2026-10-26 dump. See [Schema map](#schema-map).

**[Browse data coverage](https://ukaraoz.github.io/ssidump/coverage/index.html)**: data-point counts per
site, depth, year and variable for geochem and 11 sensor sources. See [Coverage pages](#coverage-pages).

**[Related ESS-DIVE datasets](resources/FY27Q1_SSI_Data_Stream_Datasets.md)**: published ESS-DIVE
packages that hold some of the data in this dump, by source, with open questions on publication
status. Copied from a Google Doc in November 2026. See [Resources](#resources).

## Quick start

```bash
module use /global/cfs/cdirs/m4915/roelef_ssidump/modulefiles
module load ssidump

ssidump.py status        # is the server up?
ssidump.py start         # if not
psql                     # connects to odmx_lbnlsfa
```

You can add the `module use` line to `~/.bashrc` so that `module load ssidump` works in any shell.

## Loading a new dump

Copy the new `ssi_*.dump` into this directory, then run:

```bash
ssidump.py restore --replace
```

This picks the newest `ssi_*.dump`, unless you pass a path. It restores into a
temporary database and then swaps that in for `odmx_lbnlsfa`. If anything fails, the
existing database is left alone and the log is in `logs/`. A full restore takes
about 23 GB on disk.

Without `--replace`, the command refuses to overwrite an existing database. Other
useful options:

| Option | Effect |
|---|---|
| `ssidump.py restore path/to/x.dump` | Restore a specific dump |
| `--db NAME` | Restore under a different database name, e.g. to keep two dates side by side |
| `--schema odmx` | Restore only some schemas (repeatable). Foreign keys into schemas you leave out are skipped with a warning. |
| `-j 8` | Number of parallel restore jobs (default 4) |

`ssidump.py status` shows each database's size and which dump it came from.

## Exporting CSVs without a server

```bash
ssidump.py export-csv                                   # all odmx tables -> ./odmx_csv/
ssidump.py export-csv --table results --table sampling_features
ssidump.py export-csv --schema feeder --out /some/dir
```

Each table becomes `<table>.csv` with a header row. Row counts go to `_rowcounts.tsv`.
In the CSVs:

- NULL and empty strings both appear as empty fields.
- PostGIS geometry columns appear as hex-encoded WKB (binary).

## Connecting

Once the module is loaded, `PGHOST`, `PGPORT`, `PGUSER` and `PGDATABASE` are already
set, so `psql` and most client libraries connect with no extra arguments:

```python
import psycopg                      # or psycopg2 / sqlalchemy
conn = psycopg.connect("")          # picks up the PG* variables
```

If you don't load the module, use this connection string:

```
postgresql://ssi@/odmx_lbnlsfa?host=/global/cfs/cdirs/m4915/roelef_ssidump/pgrun&port=54329
```

You can also open a shell with `ssidump.py psql`. Arguments after `psql` are passed
through, e.g. `ssidump.py psql -c "select count(*) from odmx.results"`.

## What's in the database

| Schema | Contents |
|---|---|
| `odmx` | Core ODM2-style model (about 250 tables): sampling features, actions, results, equipment, controlled vocabularies (`cv_*`), citations, and genomics (assemblies, contigs, MAGs, KEGG) |
| `datastreams` | About 15,700 time-series tables, one per sensor variable (e.g. `am_4_cr1000_airtc_avg_meas`), plus views |
| `feeder` | About 1,500 tables of raw logger and ingest data: CR1000 loggers, Decagon sensors, stream gauges, ESS-DIVE, PhenoCam |
| `knowledge` | Document store (`source`, `chunk`) |

Good places to start are `odmx.sampling_features` and `odmx.sampling_feature_timeseries_datastreams`,
which maps sites to their `datastreams` tables.

Data quirk: `sampling_features.feature_geometry_wkt` stores `POINT(lat lon)`, which
is reversed from the usual order. Use the `latitude`/`longitude` columns or `feature_geometry` instead.

## Summaries and exports by table family (`ssiexport.py`)

`ssiexport.py` (standard library only; needs the server running) writes text summaries and
TSV exports for groups of tables. It knows three sources:

| Source | What it reads |
|---|---|
| `feeder` | wide raw tables in `feeder` (one per logger/source) |
| `datastreams` | per-variable tables in `datastreams`, joined to site / variable / units / depth from `odmx.sampling_feature_timeseries_datastreams`. Geochem is here as the `geochem` family; it has **no** feeder tables. |
| `samples` | the original geochem specimen results in `odmx` (value + standard deviation + nonDetect/quality codes, specimen and site) |

```bash
ssiexport.py families                                           # families, table counts, sizes
ssiexport.py summary feeder --family influxdb -o influxdb_summary.txt     # stats, column stats, 15 sample rows
ssiexport.py summary datastreams --family geochem -o geochem_datastreams_summary.txt
ssiexport.py summary samples --geochem-only -o geochem_samples_summary.txt
ssiexport.py export feeder --family snotel --out snotel_tables/            # one TSV per table + _manifest.tsv
ssiexport.py export feeder --family snotel --layout long --out snotel.tsv.gz   # table,index,timestamp,column,value
ssiexport.py export datastreams --family geochem --out geochemdata_all.tsv
ssiexport.py export datastreams --family geochem --variables Calcium --sites Evans --fill blank --out ca.tsv
ssiexport.py export samples --geochem-only --fill blank --out geochem_samples.tsv
```

Families: `geochem sentinel influxdb decagon ess_dive phenocam flux_stations streamflow nwis snotel
gradient_sm cr1000` (see `ssiexport.py families` for the patterns). Instead of `--family` you can
use `--pattern REGEX` or `--table NAME`. Other options: `--rows N` (sample rows, default 15),
`--start/--end` (time window), `--drop-empty-rows`, `--drop-nan`, `--fill keep|blank|drop` (-9999 codes),
`--nondetect keep|blank`. Output names ending in `.gz` are compressed. `ssiexport.py <cmd> <source> -h` for all options.

Geochem caveats: non-detects are stored as 0 (only `samples` carries `censor_code`); -9999 is a
fill code in some values and SDs; datastream times are local sample time + 7 h.

## Selecting data by owner, data type and sensor (`ssiselect.py`)

`ssiselect.py` (standard library only; needs the server running) picks datastreams by who owns
the equipment, what it measures and what sensor made it, and exports them in long format: one
row per measurement with its site, logger, sensor, owner and feeder table beside it. Every
export writes three files, so it can be checked and rerun:

| File | Contents |
|---|---|
| `NAME.tsv.gz` | the measurements: `datetime_utc`, `utc_time`, `value`, `qa_flag`, then the catalog columns |
| `NAME.catalog.tsv` | one row per selected datastream with all its metadata |
| `NAME.manifest.json` | the command, selection, SQL filters, database, host, script SHA-256, row count and file checksums |

```bash
ssiselect.py options --owner Wielandt                     # what this owner has: data types, sensors, sites
ssiselect.py catalog --owner Wielandt -o stijn_catalog.tsv
ssiselect.py hierarchy --owner Wielandt -o exports/stijn_wielandt/hierarchy.json   # site -> device -> sensors
ssiselect.py export  --owner Wielandt --data-type soilTemperature --sensor-type "TMP 117" \
                     -o exports/stijn_wielandt/soil_temperature.tsv.gz
ssiselect.py export  --config exports/stijn_wielandt/soil_temperature.manifest.json -o rerun.tsv.gz
```

Selection options are repeatable (OR within an option, AND between options):
`--owner` (person_id or name substring), `--data-type` (variable term or name, exact or glob such as
`'soil*'`), `--sensor-type` (sensor model, part number, equipment code or type; substring or glob),
`--site`, and `--role` (default `owner`). Export options: `--start`/`--end` (UTC), `--qa z`,
`--drop-missing`, `--valid-min`/`--valid-max`, `--slim` (only `datastream_id` beside each value).
Rows with no value (feeder NaN, `qa_flag` `c`, empty `value`) are **kept by default**; `--drop-missing`
removes them. The manifest counts rows per `qa_flag`.

How a datastream is tied to equipment: its `equipment_id` is the **logger** (e.g. a SNOD1 node);
the **sensor** is the device attached to that logger (`odmx.related_equipment`, `isAttachedTo`)
whose `equipment_name` equals the datastream's feeder column (e.g. probe `Temp05`). If none
matches, the logger is the sensor. `--owner` matches owners of either.

Things the database does not fix for you: each datastream table repeats every timestamp of its
feeder table, with empty values flagged `c` (kept by default; `--drop-missing` removes them); real values are all flagged `z`
(good), including known bad readings such as the SNOD1 error code 127.99 and a burst of
latitude/longitude/elevation values written into SNOD1 temperature columns on 2023-01-07
(off-minute timestamps). Filter those with `--valid-min/--valid-max` or after export.

## Full database export (`ssiexportall.py`, `run_restore_export.sh`)

`ssiexportall.py` writes every table of the chosen schemas to its own gzipped CSV, in parallel, with
row counts and checksums. `run_restore_export.sh` does the whole job from the dump in this folder:
set up PostgreSQL, restore the dump (skipped if the database already exists), export, stop the server.

```bash
cd /global/cfs/cdirs/m4915/roelef_ssidump
./run_restore_export.sh                # log in run.log; JOBS=16 ./run_restore_export.sh for fewer parallel tables
```

It exports `datastreams`, `odmx`, `knowledge` and `feeder` to `exports/full_<dump date>/`, skips the
Sentinel tables (`^sentinel_`) in `datastreams` and `feeder`, and exports InfluxDB tables first. Each
schema folder has `<table>.csv.gz` files (header row; NULL = empty field, empty string = `""`,
NaN = `NaN`, geometry = hex WKB) and a `_tables.tsv` of rows, bytes and SHA-256; `manifest.json` records
the command, dump, host and per-schema totals. A table that gives no result for 15 minutes is skipped
and listed as failed; rerunning resumes and retries only what is missing.

```bash
ssiexportall.py --schema odmx --out exports/odmx_only -j 8          # any subset by hand
ssiexportall.py --schema datastreams --exclude 'datastreams:^sentinel_' \
                --first 'datastreams:^influxdb_' --out exports/ds -j 40
```

The 2026-10-26 dump restored and exported in about 10 minutes on a login node with 40 parallel tables:
8,962 datastreams tables (338 M rows), 247 odmx, 4 knowledge and 150 feeder tables (22.6 M rows), 1.9 GB.

## The server: rules to know

- **Run it on one node at a time.** It accepts connections only through a socket file
  in `pgrun/` on the shared filesystem, with no network listener. So it's only reachable from
  the node it was started on, and two servers on the same data would corrupt it.
  `ssidump.py start` refuses if it was last started on another node and not stopped.
  If that node is gone (e.g. after a reboot), use `ssidump.py start --force`.
- **It stops when its node reboots.** Run `ssidump.py start` again; your data is safe in `pgdata/`.
- **Run `ssidump.py stop` when you're done for a while.** This matters especially on login nodes.
- **There are no passwords.** Access is limited by who can read this directory (you and your group).
- **Heavy jobs:** `-j 4` restores are fine on a login node. For long analyses, start the
  server inside an interactive or batch job (`ssidump.py start` on the compute node)
  and stop it before the job ends.

## Files

```
ssidump.py              the tool (Python 3 standard library only)
ssiexport.py            family summaries and TSV exports (feeder / datastreams / odmx samples)
ssiselect.py            selection by owner / data type / sensor type, long-format exports with manifests
ssiexportall.py         every table of chosen schemas to gzipped CSV, with row counts and checksums
run_restore_export.sh   setup + restore + full export + stop, from the dump in this folder (log: run.log)
exports/                ssiselect.py outputs (not in git)
modulefiles/ssidump.lua Lmod module: PATH + PG* connection variables
ssi_*.dump              dumps (input)
pgenv/                  conda env with PostgreSQL 17 + PostGIS
pgdata/                 database cluster (server.log is in here)
pgrun/                  server socket
logs/                   restore logs
```

## Setting up from scratch / for someone else

To set this up in a new directory, copy `ssidump.py` there and run:

```bash
export SSIDUMP_ROOT=/path/to/dir                 # defaults to the script's directory
export SSIDUMP_CONDA=/path/to/miniconda3/bin/conda
export SSIDUMP_PORT=54330                        # choose a free port if others run one too
python3 ssidump.py setup                         # creates pgenv/ and pgdata/
python3 ssidump.py restore
```

Then edit `root` (and `PGPORT`, if you changed the port) in a copy of `modulefiles/ssidump.lua`.

## Schema map

`schema_map/` builds a single HTML page that maps the five schemas in a dump, with a
clickable explorer for the `odmx` tables. It needs only Python 3 and the schema, not
a running server:

```bash
pg_restore -s -f schema_only.sql ssi_2026-10-26_083001Z.dump   # from pgenv/bin on Perlmutter
schema_map/build.sh schema_only.sql schema_map/ssi_schemas.html
```

`schema_map/table_inventory.py` writes per-schema table inventories (TSV): one row per table
with its description, columns and keys, and for `datastreams` the site, variable, units and dates
of each stream. It reads the schema file and a table export from `ssiexportall.py` (for the `odmx`
lookup tables and each schema's `_tables.tsv` row counts); tables missing from the export get an
empty row count.

```bash
python3 schema_map/table_inventory.py schema_only.sql exports/full_2026-10-26/ table_inventory/
```

Table lists, columns, keys and counts all come from the dump. The schema-flow diagram
and its stream counts at the top of `template.html` are written by hand for the
2026-10-26 dump, so update them if a new dump changes the structure. Theme groupings
for `odmx` tables are name rules in `build_data.py`.

The built page is published with GitHub Pages from the `main` branch at
<https://ukaraoz.github.io/ssidump/schema_map/ssi_schemas.html>. Commit and push a
rebuilt `schema_map/ssi_schemas.html` to update it.

## Coverage pages

`coverage/` builds heatmap pages of data-point counts from datastreams exports made
with `ssiexport.py`. Each source gets two pages: one row per site × depth
(`<dataset>_coverage.html`), and one that adds a row for each year under every
site × depth (`<dataset>_coverage_by_year.html`). Columns are variables (analytes for
geochem). **Start at the index: <https://ukaraoz.github.io/ssidump/coverage/index.html>**.

It needs Python 3 with pandas, not a running server. Exports are read in chunks, so the
58–65 million row exports build in about 1.5 minutes each.

```bash
# on Perlmutter (server running)
ssiexport.py export datastreams --family decagon --drop-nan --out decagon_all.tsv.gz
# locally, after copying the export
coverage/build.sh decagon decagon_all.tsv.gz coverage/
python3 coverage/build_index.py          # rebuild coverage/index.html
```

The export files named below were made on 2026-11-02 and have since been deleted; rebuilding a
page means re-exporting on Perlmutter (`ssiexport.py`, or the tables in `exports/full_<date>/`).

| Dataset | Export selection | Local export file |
|---|---|---|
| `geochem` | `--family geochem` (no `--drop-nan`) | `geochemdata_all.tsv` |
| `decagon` | `--family decagon` | `decagon_all.tsv.gz` |
| `streamflow` | `--family streamflow` | `streamflow_all.tsv` |
| `mloggers` | `--pattern "^m[1-4]_"` | `mloggers_all.tsv.gz` |
| `cr1000` | `--pattern "^(am\|bb\|jf\|kp\|sm\|mc\|cb)_"` | `cr1000_all.tsv.gz` |
| `metstation` | `--pattern "^metstation_"` | `metstation_all.tsv.gz` |
| `towers` | `--pattern "^feeder_(er\|sg\|tr)_"` | `towers_all.tsv.gz` |
| `snotel` | `--family snotel` | `snotel_all.tsv.gz` |
| `gradient` | `--family gradient_sm` | `gradient_all.tsv.gz` |
| `essdive` | `--family ess_dive` | `essdive_all.tsv.gz` |
| `phenocam` | `--family phenocam` | `phenocam_all.tsv.gz` |
| `nwis` | `--family nwis` | `nwis_all.tsv.gz` |

All but geochem use `--drop-nan`. Sentinel and InfluxDB have no pages yet. Page text,
column groups and colour bins per dataset are in `coverage/build_data.py` (`DATASETS`);
datasets without fixed bins get bins from the quantiles of their cell counts. To add a
source, add an entry there, export it, and run `build.sh` and `build_index.py`. Commit
and push the rebuilt HTML to update GitHub Pages.

## Resources

| File | Contents |
|---|---|
| [`resources/FY27Q1_SSI_Data_Stream_Datasets.md`](resources/FY27Q1_SSI_Data_Stream_Datasets.md) | Potential ESS-DIVE datasets that hold data from the SSI dump, grouped by source (CR1000, M1–M4, Decagon, met station, towers, streamflow, ESS-DIVE, PhenoCam, gradient, geochem). Each source links to its coverage page and the matching ESS-DIVE DOIs, and notes locations or publication status still to confirm. Markdown copy of the FY27Q1 Google Doc, November 2026; the Google Doc is the live version. |
| [East-Taylor Watershed Community Observatory Sensors](https://docs.google.com/spreadsheets/d/1bo6cMNUiqaIpHKeiXn_2ttl9FGDUw5Go70WRkmZ1iYM/edit) (Google Sheet) | Sensor registry for the Watershed Function SFA in the East River and Taylor River watersheds: one row per deployed sensor with its location, instrument model and the variables it records (`sensor_registration_template` sheet). The `data_dictionary` sheet defines each column and says whether it is required. Use it to look up the instruments behind the `feeder` and `datastreams` tables. |

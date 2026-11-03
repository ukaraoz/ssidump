# SSI / ODMX database dumps

Tools for opening the nightly `ssi_*.dump` files of the `odmx_lbnlsfa` database
(LBNL SFA, East River watershed ODMX) on NERSC Perlmutter. They can:

- **restore** a dump into a private local PostgreSQL 17 + PostGIS server and query it with SQL;
- **export** tables from a dump straight to CSV, with no server needed.

The dumps are written by pg_dump 16 (archive format 1.15). The system `pg_restore`
(version 12) can't read them, so these tools ship their own PostgreSQL 17 in `pgenv/`.

**[Browse the schema map](https://ukaraoz.github.io/ssidump/schema_map/ssi_schemas.html)**:
an interactive map of every schema and table in the 2026-10-26 dump. See [Schema map](#schema-map).

**[Browse geochem coverage](https://ukaraoz.github.io/ssidump/geochem_coverage/geochem_coverage.html)**:
data-point counts for every site, depth and analyte in the geochem export. See [Geochem coverage](#geochem-coverage).

## Quick start

```bash
module use /pscratch/sd/u/ukaraoz/roelef_ssidump/modulefiles
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
postgresql://ssi@/odmx_lbnlsfa?host=/pscratch/sd/u/ukaraoz/roelef_ssidump/pgrun&port=54329
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
modulefiles/ssidump.lua Lmod module: PATH + PG* connection variables
ssi_*.dump              dumps (input)
pgenv/                  conda env with PostgreSQL 17 + PostGIS
pgdata/                 database cluster (server.log is in here)
pgrun/                  server socket
logs/                   restore logs
odmx_csv/               CSV export of the odmx schema
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

Table lists, columns, keys and counts all come from the dump. The schema-flow diagram
and its stream counts at the top of `template.html` are written by hand for the
2026-10-26 dump, so update them if a new dump changes the structure. Theme groupings
for `odmx` tables are name rules in `build_data.py`.

The built page is published with GitHub Pages from the `main` branch at
<https://ukaraoz.github.io/ssidump/schema_map/ssi_schemas.html>. Commit and push a
rebuilt `schema_map/ssi_schemas.html` to update it.

## Geochem coverage

`geochem_coverage/` builds a heatmap page of data-point counts per site × depth (rows)
and analyte (columns) from a geochem datastreams export. It needs Python 3 with pandas,
not a running server:

```bash
ssiexport.py export datastreams --family geochem --out geochemdata_all.tsv
geochem_coverage/build.sh geochemdata_all.tsv geochem_coverage/geochem_coverage.html
```

Analyte column groups are set in `build_data.py`; any analyte not named there falls
under metals & trace elements. The page is published with GitHub Pages at
<https://ukaraoz.github.io/ssidump/geochem_coverage/geochem_coverage.html>. Commit and
push a rebuilt `geochem_coverage/geochem_coverage.html` to update it.

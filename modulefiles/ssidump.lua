-- module use /pscratch/sd/u/ukaraoz/roelef_ssidump/modulefiles; module load ssidump
whatis("Local PostgreSQL 17 + PostGIS server for SSI odmx_lbnlsfa dumps")
help([[
Puts ssidump.py and the PostgreSQL 17 client tools on PATH and sets PGHOST/PGPORT/
PGUSER/PGDATABASE so plain `psql` connects to odmx_lbnlsfa.

  ssidump.py status | start | stop
  ssidump.py restore --replace       newest ssi_*.dump in the root directory
  ssidump.py export-csv --schema odmx
]])

local root = "/pscratch/sd/u/ukaraoz/roelef_ssidump"
setenv("SSIDUMP_ROOT", root)
prepend_path("PATH", pathJoin(root, "pgenv/bin"))
prepend_path("PATH", root)
setenv("PGHOST", pathJoin(root, "pgrun"))
setenv("PGPORT", "54329")
setenv("PGUSER", "ssi")
setenv("PGDATABASE", "odmx_lbnlsfa")

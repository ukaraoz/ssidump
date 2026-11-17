-- module use /global/cfs/cdirs/m4915/roelef_ssidump/modulefiles; module load ssidump
whatis("Local PostgreSQL 17 + PostGIS server for SSI odmx_lbnlsfa dumps")
help([[
Makes the ssidump commands available (from <root>/src, no install needed), puts the
PostgreSQL 17 client tools on PATH and sets PGHOST/PGPORT/PGUSER/PGDATABASE so plain
`psql` connects to odmx_lbnlsfa.

  ssidump status | start | stop
  ssidump restore --replace          newest ssi_*.dump in the root directory
  ssidump export-csv --schema odmx
  ssiexport, ssiexportall, ssiselect, ssi2essdive   (each with --help)
]])

local root = "/global/cfs/cdirs/m4915/roelef_ssidump"
setenv("SSIDUMP_ROOT", root)
prepend_path("PATH", pathJoin(root, "pgenv/bin"))
prepend_path("PYTHONPATH", pathJoin(root, "src"))
setenv("PGHOST", pathJoin(root, "pgrun"))
setenv("PGPORT", "54329")
setenv("PGUSER", "ssi")
setenv("PGDATABASE", "odmx_lbnlsfa")

-- the commands, as shell functions running the modules in <root>/src
local commands = {ssidump = "server", ssiexport = "export", ssiexportall = "exportall",
                  ssiselect = "selection", ssi2essdive = "essdive_cli"}
for name, module in pairs(commands) do
  set_shell_function(name, 'python3 -m ssidump.' .. module .. ' "$@"', 'python3 -m ssidump.' .. module .. ' $*')
end

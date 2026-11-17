"""Where the project root is: the folder with the dump, pgenv/, pgdata/, pgrun/, logs/ and exports/.

1. $SSIDUMP_ROOT, if set (the Perlmutter modulefile and run_restore_export.sh set it);
2. else the repository the code sits in (src/ssidump/ -> two levels up), when it has a pyproject.toml,
   i.e. a checkout or an editable install;
3. else the current working directory.
"""
import os
from pathlib import Path


def project_root():
    env = os.environ.get('SSIDUMP_ROOT')
    if env:
        return Path(env)
    repo = Path(__file__).resolve().parents[2]
    if (repo / 'pyproject.toml').exists():
        return repo
    return Path.cwd()


ROOT = project_root()

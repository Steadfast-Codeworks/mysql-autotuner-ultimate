#!/usr/bin/env python3
"""
Single source of truth for locating my.cnf.

M10: three separate copies of this candidate list existed — in
``mysql-autotuner.py::_find_mycnf`` (7 paths, symlink-resolving), in
``safety/checks.py::_check_file_permissions`` (3 paths) and in
``core/collector.py::_get_config_file_info`` (4 paths). On Debian/Ubuntu they
could select *different files*, so the pre-flight permission check happily
validated a file the tool was never going to write, and the disk-space check
looked at partitions the backup would never touch.

Author: R.L. Burger (Steadfast Codeworks)
Date: 2026-08-24
Version: 1.0.4
Copyright (c) 2026 R.L. Burger
Project: Steadfast Tools
Website: https://www.steadfasttools.com
License: MIT License
"""

import os
from typing import List, Optional

# Ordered by precedence. The first that exists wins.
MYCNF_CANDIDATES: List[str] = [
    "/etc/my.cnf",
    "/etc/mysql/my.cnf",
    "/etc/mysql/mysql.conf.d/mysqld.cnf",       # Ubuntu/Debian MySQL
    "/etc/mysql/mariadb.conf.d/50-server.cnf",  # Debian/Ubuntu MariaDB
    "/etc/my.cnf.d/server.cnf",                 # RHEL family MariaDB
    "/usr/local/mysql/etc/my.cnf",
    "/var/lib/mysql/my.cnf",
]


def find_mycnf(resolve: bool = True) -> str:
    """Return the path to the active my.cnf.

    With ``resolve=True`` (the default) the path is passed through
    ``os.path.realpath``. That matters on Debian/Ubuntu where
    ``/etc/mysql/my.cnf`` is a symlink via ``/etc/alternatives``: writing the
    symlink itself would replace it with a regular file and orphan the
    alternatives mechanism.

    Raises ``FileNotFoundError`` when no candidate exists.
    """
    for path in MYCNF_CANDIDATES:
        if os.path.isfile(path):
            return os.path.realpath(path) if resolve else path
    raise FileNotFoundError(
        "Cannot locate my.cnf - checked: " + ", ".join(MYCNF_CANDIDATES)
    )


def find_mycnf_or_none(resolve: bool = True) -> Optional[str]:
    """``find_mycnf`` that returns None instead of raising."""
    try:
        return find_mycnf(resolve=resolve)
    except FileNotFoundError:
        return None

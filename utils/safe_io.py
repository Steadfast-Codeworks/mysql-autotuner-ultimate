#!/usr/bin/env python3
"""
Safe filesystem helpers (M1 hardening).

The auto-tuner frequently runs as root and writes logs, history, and reports.
On shared-hosting boxes — the tool's exact audience — a local user can
pre-create predictable paths in a world-writable directory (e.g. /tmp) as
symlinks, tricking a root process into writing through them. These helpers:

  * prefer a root-owned, non-world-writable base directory;
  * fall back to a UID-scoped subdirectory of the system temp dir rather than a
    shared, predictable path;
  * refuse to write through a symlink at the final path component (O_NOFOLLOW);
  * never chmod a directory that already existed (it may be attacker-owned).

Author: R.L. Burger (Steadfast Codeworks)
Date: 2025-09-07
Last Updated: 2026-08-24
Version: 1.0.4
Copyright (c) 2026 R.L. Burger
Project: Steadfast Tools
Website: https://www.steadfasttools.com
License: MIT License
"""

import os
import stat
import tempfile
import logging

logger = logging.getLogger(__name__)

# O_NOFOLLOW is POSIX-only; it does not exist on Windows. Fall back to 0 there
# (the attack surface is Linux servers anyway — Windows is dev/test only).
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def supports_unicode(sample: str = "─✓✗~💡") -> bool:
    """True when stdout can actually encode the report's decorative glyphs.

    Under a POSIX/C locale — the default for cron, systemd units and minimal
    containers — stdout resolves to ASCII and printing these raises
    UnicodeEncodeError. Callers use this to pick an ASCII glyph set so the
    output stays clean instead of degrading to a wall of '?'.
    """
    import sys as _sys

    encoding = getattr(_sys.stdout, "encoding", None) or "ascii"
    try:
        sample.encode(encoding)
        return True
    except (UnicodeEncodeError, LookupError, AttributeError):
        return False


def _uid_suffix() -> str:
    getuid = getattr(os, "getuid", None)
    return str(getuid()) if getuid is not None else "user"


def secure_dir(base_dir: str, require_owner: bool = False) -> str:
    """Return *base_dir* if it is a safe, usable directory, else ``None``.

    Creates it with mode 0o750 when missing. Refuses a symlinked directory and
    does NOT chmod a pre-existing one (which might be owned by someone else).
    Rejects any world-writable directory (N-18).
    When *require_owner* is True, verifies that the directory is owned by the
    current process EUID (on POSIX systems).
    """
    try:
        if os.path.islink(base_dir):
            logger.warning("Refusing to use symlinked directory: %s", base_dir)
            return None
        existed = os.path.isdir(base_dir)
        os.makedirs(base_dir, mode=0o750, exist_ok=True)
        if not existed and not os.path.islink(base_dir):
            # Only tighten perms on a directory we just created.
            try:
                os.chmod(base_dir, 0o750)
            except OSError:
                pass
        if not os.path.isdir(base_dir):
            return None

        st = os.stat(base_dir)
        # N-18: Reject world-writable directories (POSIX systems)
        if os.name != 'nt' and hasattr(stat, "S_IWOTH") and (st.st_mode & stat.S_IWOTH):
            logger.warning("Refusing world-writable directory: %s", base_dir)
            return None

        if require_owner and hasattr(os, "geteuid"):
            if st.st_uid != os.geteuid():
                logger.warning(
                    "Refusing directory not owned by current EUID (%s != %s): %s",
                    st.st_uid, os.geteuid(), base_dir,
                )
                return None
        return base_dir
    except (PermissionError, OSError) as exc:
        logger.debug("secure_dir(%s) unavailable: %s", base_dir, exc)
        return None


def temp_fallback_dir(name: str) -> str:
    """A UID-scoped subdirectory of the system temp dir (not a shared path)."""
    return os.path.join(tempfile.gettempdir(), f"{name}-{_uid_suffix()}")


def choose_writable_dir(preferred: str, fallback_name: str,
                        require_owner: bool = True) -> str:
    """Return the first usable of *preferred* or a UID-scoped temp fallback."""
    return (
        secure_dir(preferred, require_owner=require_owner)
        or secure_dir(temp_fallback_dir(fallback_name), require_owner=True)
    )


def secure_open_write(path: str, append: bool = False):
    """Open *path* for writing without following a symlink at the final
    component. Returns a text-mode file object (UTF-8). Raises ``OSError``
    (including if the final component is a symlink, when O_NOFOLLOW is honoured).
    """
    mode_flag = os.O_APPEND if append else os.O_TRUNC
    flags = os.O_CREAT | os.O_WRONLY | mode_flag | _O_NOFOLLOW
    fd = os.open(path, flags, 0o640)
    try:
        return os.fdopen(fd, "a" if append else "w", encoding="utf-8")
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise

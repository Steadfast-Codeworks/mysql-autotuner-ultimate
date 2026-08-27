#!/usr/bin/env python3
"""
Version Compatibility Layer - v1.0.4
====================================
Cross-version compatibility matrix for MySQL 8.0/8.4 and MariaDB 10.5/10.6/10.11/11.4.

Author: R.L. Burger (Steadfast Codeworks)
Date: 2025-09-07
Last Updated: 2026-08-24
Version: 1.0.4
Copyright (c) 2026 R.L. Burger
Project: Steadfast Tools
Website: https://www.steadfasttools.com
License: MIT License

Supported versions:
  - MariaDB: 10.5, 10.6, 10.11, 11.4
  - MySQL:   8.0, 8.4
"""

import re
import logging
from typing import Dict, List, Any, Optional, Tuple

logger = logging.getLogger(__name__)


# ======================================================================
# Version parsing
# ======================================================================

def parse_version_string(version_string: str) -> Dict[str, Any]:
    """
    Parse a MySQL/MariaDB version string into structured info.

    Examples:
        '10.11.8-MariaDB'  -> {is_mariadb: True, major: 10, minor: 11, patch: 8, branch: '10.11', version_unknown: False}
        '8.0.36'           -> {is_mariadb: False, major: 8, minor: 0, patch: 36, branch: '8.0', version_unknown: False}
        ''                 -> {is_mariadb: False, major: 0, minor: 0, patch: 0, branch: 'unknown', version_unknown: True}
    """
    ver_str = str(version_string or "").strip()
    info = {
        'version_string': ver_str,
        'is_mariadb': 'mariadb' in ver_str.lower(),
        'major': 0,
        'minor': 0,
        'patch': 0,
        'branch': 'unknown',
        'version_unknown': True,
    }

    if not ver_str:
        return info

    m = re.search(r'(\d+)\.(\d+)\.(\d+)', ver_str)
    if m:
        info['major'] = int(m.group(1))
        info['minor'] = int(m.group(2))
        info['patch'] = int(m.group(3))
        info['branch'] = f"{info['major']}.{info['minor']}"
        info['version_unknown'] = False
    else:
        m2 = re.search(r'(\d+)\.(\d+)', ver_str)
        if m2:
            info['major'] = int(m2.group(1))
            info['minor'] = int(m2.group(2))
            info['branch'] = f"{info['major']}.{info['minor']}"
            info['version_unknown'] = False

    return info


def version_tuple(ver: Dict[str, Any]) -> Tuple[int, int, int]:
    return (ver.get('major', 0), ver.get('minor', 0), ver.get('patch', 0))


# ======================================================================
# Deprecated / removed parameters per version
# ======================================================================

# Parameters that were removed or renamed in specific versions.
# Format: { 'parameter_name': { 'removed_in': (major, minor), 'replacement': 'new_name' | None, 'engine': 'mariadb'|'mysql'|'both' } }

DEPRECATED_PARAMS: Dict[str, Dict[str, Any]] = {
    # --- query_cache was removed in MySQL 8.0 ---
    'query_cache_type': {
        'removed_in_mysql': (8, 0),
        'removed_in_mariadb': None,  # Still available in MariaDB
        'replacement': None,
        'note': 'Query cache was removed in MySQL 8.0. MariaDB still supports it.',
    },
    'query_cache_size': {
        'removed_in_mysql': (8, 0),
        'removed_in_mariadb': None,
        'replacement': None,
        'note': 'Query cache was removed in MySQL 8.0. MariaDB still supports it.',
    },
    'query_cache_limit': {
        'removed_in_mysql': (8, 0),
        'removed_in_mariadb': None,
        'replacement': None,
        'note': 'Query cache was removed in MySQL 8.0. MariaDB still supports it.',
    },
    'query_cache_min_res_unit': {
        'removed_in_mysql': (8, 0),
        'removed_in_mariadb': None,
        'replacement': None,
        'note': 'Query cache was removed in MySQL 8.0.',
    },
    # --- innodb_log_file_size replaced by innodb_redo_log_capacity in MySQL 8.0.30+ ---
    'innodb_log_file_size': {
        'removed_in_mysql': (8, 4),  # Fully removed in 8.4; deprecated from 8.0.30
        'removed_in_mariadb': None,  # Still used in MariaDB
        'replacement': 'innodb_redo_log_capacity',
        'note': 'Deprecated in MySQL 8.0.30, removed in 8.4. Use innodb_redo_log_capacity instead.',
    },
    'innodb_log_files_in_group': {
        'removed_in_mysql': (8, 4),
        'removed_in_mariadb': None,
        'replacement': None,
        'note': 'Deprecated in MySQL 8.0.30, removed in 8.4. Managed automatically.',
    },
    # --- innodb_buffer_pool_instances ---
    # MariaDB: deprecated in 10.5, REMOVED in 10.6 — setting it aborts startup.
    # MySQL:   still present in 8.0/8.4 (deprecated in 8.4 but not fatal).
    'innodb_buffer_pool_instances': {
        'removed_in_mysql': None,
        'removed_in_mariadb': (10, 6),
        'replacement': None,
        'note': 'Removed in MariaDB 10.6 (the buffer pool is always a single '
                'instance there); setting it prevents the server from starting. '
                'Still valid in MySQL 8.0/8.4.',
    },
    # --- key_buffer_size ---
    # Still valid in all versions but irrelevant if no MyISAM tables
    # --- innodb_additional_mem_pool_size removed long ago ---
    'innodb_additional_mem_pool_size': {
        'removed_in_mysql': (5, 7),
        'removed_in_mariadb': (10, 5),
        'replacement': None,
        'note': 'Removed in MySQL 5.7 and MariaDB 10.5.',
    },
    # --- MariaDB 11.x changes ---
    'innodb_use_native_aio': {
        'removed_in_mysql': None,
        'removed_in_mariadb': (11, 0),
        'replacement': None,
        'note': 'Removed in MariaDB 11.0; AIO is always used when available.',
    },
}


# ======================================================================
# Version-specific default recommendations
# ======================================================================

# Parameters whose recommended values differ by version.
VERSION_SPECIFIC_DEFAULTS: Dict[str, Dict[str, Any]] = {
    'innodb_redo_log_capacity': {
        'applies_to': 'mysql',
        'min_version': (8, 0),
        'note': 'Replaces innodb_log_file_size * innodb_log_files_in_group in MySQL 8.0.30+/8.4',
    },
    'innodb_io_capacity': {
        'mariadb_default': 200,
        'mysql_default': 200,
        'note': 'Same default across versions; tune based on disk type.',
    },
    'innodb_io_capacity_max': {
        'mariadb_default': 2000,
        'mysql_default': 2000,
        'note': 'Same default; set higher for NVMe.',
    },
}


# ======================================================================
# Public API
# ======================================================================

class VersionCompatibility:
    """
    Ensures recommendations are compatible with the detected
    MySQL/MariaDB version.
    """

    # Absolute ceiling on innodb_redo_log_capacity, in MB. Overridden from
    # fallback_logic.safety_guardrails.max_log_file_size_gb by the caller.
    DEFAULT_MAX_REDO_CAPACITY_MB = 16 * 1024

    def __init__(self, version_info: Dict[str, Any] = None,
                 max_redo_capacity_mb: int = None,
                 datadir_free_mb: int = None):
        self.version_info = version_info or {}
        self.is_mariadb = self.version_info.get('is_mariadb', False)
        self.branch = self.version_info.get('branch', 'unknown')
        self.ver_tuple = version_tuple(self.version_info)
        self.max_redo_capacity_mb = (
            max_redo_capacity_mb or self.DEFAULT_MAX_REDO_CAPACITY_MB
        )
        self.datadir_free_mb = datadir_free_mb
        self.logger = logging.getLogger(__name__)

    @classmethod
    def from_version_string(cls, version_string: str,
                            max_redo_capacity_mb: int = None,
                            datadir_free_mb: int = None) -> 'VersionCompatibility':
        return cls(parse_version_string(version_string), max_redo_capacity_mb,
                   datadir_free_mb=datadir_free_mb)

    # ------------------------------------------------------------------
    # Filter recommendations
    # ------------------------------------------------------------------
    def filter_recommendations(
        self, recommendations: List[Dict[str, Any]],
        datadir_free_mb: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Remove or transform recommendations that are incompatible with
        the detected database version.
        """
        filtered: List[Dict[str, Any]] = []
        free_mb = datadir_free_mb if datadir_free_mb is not None else self.datadir_free_mb

        for rec in recommendations:
            param = rec.get('parameter', '')
            result = self._check_parameter(param, rec, datadir_free_mb=free_mb)
            if result is not None:
                filtered.append(result)

        return filtered

    def _check_parameter(
        self, param: str, rec: Dict[str, Any],
        datadir_free_mb: Optional[int] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Check a single parameter for version compatibility.
        Returns the (possibly transformed) recommendation, or None to skip.
        """
        dep = DEPRECATED_PARAMS.get(param)
        if dep is None:
            return rec  # Not in the deprecated list – keep as-is

        # Determine the removal version for the current engine
        if self.is_mariadb:
            removed_in = dep.get('removed_in_mariadb')
        else:
            removed_in = dep.get('removed_in_mysql')

        if removed_in is None:
            return rec  # Not removed for this engine

        # Compare versions
        if self.ver_tuple[:2] >= removed_in:
            replacement = dep.get('replacement')
            if replacement:
                # Transform the recommendation to use the replacement parameter
                new_rec = dict(rec)
                old_param = new_rec['parameter']
                new_rec['parameter'] = replacement
                new_rec['reason'] = (
                    f"{new_rec.get('reason', '')} "
                    f"[Note: '{old_param}' is deprecated/removed in "
                    f"{'MariaDB' if self.is_mariadb else 'MySQL'} {self.branch}; "
                    f"using '{replacement}' instead.]"
                ).strip()

                # Special handling: innodb_log_file_size -> innodb_redo_log_capacity
                if old_param == 'innodb_log_file_size' and replacement == 'innodb_redo_log_capacity':
                    new_rec = self._convert_log_file_to_redo_capacity(
                        new_rec, datadir_free_mb=datadir_free_mb
                    )
                    if new_rec is None:
                        return None

                self.logger.info(
                    f"Replaced deprecated '{old_param}' with '{replacement}' "
                    f"for {'MariaDB' if self.is_mariadb else 'MySQL'} {self.branch}"
                )
                return new_rec
            else:
                # No replacement – skip this recommendation
                self.logger.info(
                    f"Skipping '{param}' – removed in "
                    f"{'MariaDB' if self.is_mariadb else 'MySQL'} {self.branch}: "
                    f"{dep.get('note', '')}"
                )
                return None

        return rec  # Version is older than removal – keep as-is

    def _convert_log_file_to_redo_capacity(
        self, rec: Dict[str, Any], datadir_free_mb: Optional[int] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Convert innodb_log_file_size recommendation to innodb_redo_log_capacity.
        innodb_redo_log_capacity = innodb_log_file_size * innodb_log_files_in_group
        Default innodb_log_files_in_group was 2.
        """
        try:
            size_mb = self._parse_size_mb(rec.get('recommended_value', ''))
            # Multiply by 2 (default log files in group)
            capacity_mb = size_mb * 2

            # C1: the x2 happens AFTER every size guardrail has run, so without
            # a ceiling here a capped 16384M log_file_size becomes 32G of redo —
            # and on an unfiltered path it reached 64G, which MySQL happily
            # accepts (--validate-config approves it) and then PREALLOCATES on
            # disk. Clamp to the same guardrail the SafetyChecker enforces.
            capped = False
            if capacity_mb > self.max_redo_capacity_mb:
                self.logger.warning(
                    "innodb_redo_log_capacity %dM exceeds the %dM guardrail; "
                    "capping. MySQL preallocates this space on startup.",
                    capacity_mb, self.max_redo_capacity_mb,
                )
                capacity_mb = self.max_redo_capacity_mb
                capped = True

            # CRIT-3: Free-space guard after doubling.
            # MySQL preallocates redo capacity on startup. Keep proposed capacity
            # within half of the free space on the datadir filesystem (min 256MB floor).
            free_mb = datadir_free_mb if datadir_free_mb is not None else self.datadir_free_mb
            if free_mb is not None and free_mb > 0:
                budget_mb = int(free_mb * 0.5)
                if capacity_mb > budget_mb:
                    if budget_mb >= 256:
                        self.logger.warning(
                            "Capped innodb_redo_log_capacity from %dM to %dM: datadir has only "
                            "%dM free and redo capacity is preallocated.",
                            capacity_mb, budget_mb, free_mb,
                        )
                        capacity_mb = budget_mb
                        capped = True
                    else:
                        self.logger.warning(
                            "Dropping innodb_redo_log_capacity recommendation: datadir has only "
                            "%dM free, too little to grow the redo log safely.",
                            free_mb,
                        )
                        return None

            # L6: only express in G when it divides evenly, otherwise keep MB so
            # a value like 1536M is not silently floored to '1G' (a 33% loss).
            if capacity_mb >= 1024 and capacity_mb % 1024 == 0:
                rec['recommended_value'] = f'{capacity_mb // 1024}G'
            else:
                rec['recommended_value'] = f'{capacity_mb}M'
            rec['reason'] += (
                f' (Converted: innodb_log_file_size {size_mb}M x 2 files '
                f'= innodb_redo_log_capacity {capacity_mb}M'
                + (f', capped at the {self.max_redo_capacity_mb}M guardrail)'
                   if capped and capacity_mb == self.max_redo_capacity_mb
                   else (f', capped at {capacity_mb}M due to free space budget)' if capped else ')'))
            )
        except (ValueError, TypeError):
            # The rename to innodb_redo_log_capacity has ALREADY happened in
            # _check_parameter. Returning rec here would emit the replacement
            # parameter carrying an unconverted value: not doubled, not clamped
            # to max_redo_capacity_mb, and never checked against datadir free
            # space. A recommendation the guardrails never saw is worse than no
            # recommendation, so drop it and say why.
            self.logger.warning(
                "Could not parse innodb_log_file_size value %r; dropping the "
                "innodb_redo_log_capacity conversion rather than emitting a "
                "value that skipped the size and free-space guardrails.",
                rec.get('recommended_value'),
            )
            return None
        return rec

    # ------------------------------------------------------------------
    # Version-specific parameter adjustments
    # ------------------------------------------------------------------
    def get_version_adjustments(self) -> Dict[str, Any]:
        """
        Return version-specific notes and adjustments that should be
        communicated to the user.
        """
        adjustments = {
            'engine': 'MariaDB' if self.is_mariadb else 'MySQL',
            'version': self.branch,
            'notes': [],
            'unsupported_params': [],
        }

        # Check all deprecated params
        for param, dep in DEPRECATED_PARAMS.items():
            if self.is_mariadb:
                removed_in = dep.get('removed_in_mariadb')
            else:
                removed_in = dep.get('removed_in_mysql')

            if removed_in and self.ver_tuple[:2] >= removed_in:
                adjustments['unsupported_params'].append({
                    'parameter': param,
                    'replacement': dep.get('replacement'),
                    'note': dep.get('note', ''),
                })

        # Engine-specific notes
        if self.is_mariadb:
            if self.ver_tuple[:2] >= (11, 0):
                adjustments['notes'].append(
                    'MariaDB 11.x: Some InnoDB parameters have been removed or changed. '
                    'innodb_use_native_aio is no longer configurable.'
                )
            if self.ver_tuple[:2] >= (10, 11):
                adjustments['notes'].append(
                    'MariaDB 10.11+: Improved InnoDB redo log handling. '
                    'Consider larger innodb_log_file_size for write-heavy workloads.'
                )
            if self.ver_tuple[:2] >= (10, 6):
                adjustments['notes'].append(
                    'MariaDB 10.6+: InnoDB buffer pool dump/load is enabled by default.'
                )
        else:
            if self.ver_tuple[:2] >= (8, 4):
                adjustments['notes'].append(
                    'MySQL 8.4: innodb_log_file_size and innodb_log_files_in_group are removed. '
                    'Use innodb_redo_log_capacity instead.'
                )
                adjustments['notes'].append(
                    'MySQL 8.4: Query cache is not available. '
                    'Consider ProxySQL or application-level caching.'
                )
            elif self.ver_tuple[:2] >= (8, 0):
                adjustments['notes'].append(
                    'MySQL 8.0: Query cache has been removed. '
                    'All query_cache_* parameters will be skipped.'
                )
                if self.ver_tuple >= (8, 0, 30):
                    adjustments['notes'].append(
                        'MySQL 8.0.30+: innodb_log_file_size is deprecated. '
                        'innodb_redo_log_capacity is preferred.'
                    )

        return adjustments

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_size_mb(value) -> int:
        if isinstance(value, (int, float)):
            return int(value)
        s = str(value).strip().upper()
        if s.endswith('G'):
            return int(float(s[:-1]) * 1024)
        if s.endswith('M'):
            return int(float(s[:-1]))
        if s.endswith('K'):
            return max(1, int(float(s[:-1]) / 1024))
        return int(float(s))

    def is_supported(self) -> bool:
        """Check if the detected version is in the supported range."""
        if self.version_info.get('version_unknown') or self.branch == 'unknown' or self.ver_tuple == (0, 0, 0):
            return False
        if self.is_mariadb:
            return self.ver_tuple[:2] >= (10, 5)
        else:
            return self.ver_tuple[:2] >= (8, 0)

    def get_support_message(self) -> str:
        """Return a human-readable support status message."""
        if self.version_info.get('version_unknown') or self.branch == 'unknown' or self.ver_tuple == (0, 0, 0):
            return "Database version could not be identified with certainty (unknown branch/version). Recommendations may not be accurate."
        engine = 'MariaDB' if self.is_mariadb else 'MySQL'
        if self.is_supported():
            return f"{engine} {self.branch} is fully supported."
        else:
            min_ver = '10.5' if self.is_mariadb else '8.0'
            return (
                f"{engine} {self.branch} is below the minimum supported version ({min_ver}). "
                f"Recommendations may not be accurate."
            )

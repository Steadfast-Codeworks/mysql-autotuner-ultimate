#!/usr/bin/env python3
"""
Config <-> Code Consistency Audit

Every round of review on this tool has turned up the same class of bug: a name
declared in one place and read under a different name somewhere else, failing
silently because the reader used ``.get(key, default)`` and got the default.

  * Round 2 H1  — the recommendation engine read metric keys
                  (``innodb_data_size_mb``, ``swap_usage_gb``,
                  ``myisam_table_count``, ...) that ``_normalize_metrics`` never
                  produced, so whole features were inert and the buffer-pool
                  recommendation was computed from a permanent 0.
  * Round 3 M1  — ``detect_anomalies`` emitted ``aborted_connections`` /
                  ``connection_spikes`` while the YAML spelled them
                  ``high_aborted_connections`` / ``connection_overflow``, so two
                  of four confidence penalties never applied.
  * Round 3 M14 — four YAML sections (``parameter_rules``,
                  ``anomaly_detection.*.actions``, ``connection_headroom``,
                  ``connection_scaling_factor``) were read by nothing at all,
                  and ``parameter_rules``' formulas had already drifted from the
                  Python that superseded them.

None of these raise. Tests that assert "a recommendation was produced" pass
happily while the recommendation is wrong. The only reliable defence is to state
the contract explicitly and check it, which is what this module does:

  1. ``audit_config``  — every key the YAML defines is consumed by the code, and
                         every key the code consumes exists in the YAML.
  2. ``audit_metrics`` — every metric key the engine reads is produced by the
                         collector.
  3. ``audit_anomaly_contract`` — the anomaly names, their threshold sections and
                         their confidence penalties all agree.

Findings are warnings, never exceptions: a user's customised YAML carrying extra
keys must not break their run. ``--check-config`` surfaces them deliberately and
exits non-zero, so the contract can be enforced in CI without risking production.

Author: R.L. Burger (Steadfast Codeworks)
Date: 2026-08-24
Version: 1.0.4
Copyright (c) 2026 R.L. Burger
Project: Steadfast Tools
Website: https://www.steadfasttools.com
License: MIT License
"""

import logging
from typing import Any, Dict, List, Set

logger = logging.getLogger(__name__)


# ======================================================================
# 1. Config keys the code actually consumes
# ======================================================================
# Dotted paths. A trailing '.*' means "any child key here is consumed"
# (used for per-platform maps and per-anomaly sections, whose child names are
# data rather than contract).
#
# WHEN YOU ADD A config.get(...) LOOKUP, ADD ITS PATH HERE.
# `--check-config` fails otherwise, which is the entire point.
CONSUMED_CONFIG_KEYS: Set[str] = {
    # Informational metadata — read for display/reporting
    'metadata.*',
    'evidence_base.total_cases',
    'evidence_base.platform_distribution.*',
    'evidence_base.scenario_coverage.*',

    # Platform detection
    'platform_detection.signatures.*',

    # File-limit logic (per-platform blocks; block names are data, leaves are
    # contract — hence '*' for the platform and explicit leaf names below it)
    'file_limit_logic.*.table_count_threshold',
    'file_limit_logic.*.required_limit_nofile',
    'file_limit_logic.*.systemd_edit_required',
    'file_limit_logic.*.confidence',

    # Multi-pass logic (per-pass blocks)
    'multi_pass_logic.*.confidence_multiplier',
    'multi_pass_logic.*.buffer_pool_multiplier',
    'multi_pass_logic.*.log_file_ratio',
    'multi_pass_logic.*.table_cache_multiplier',

    # Post-migration adjustments
    'post_migration_logic.key_buffer_reduction',
    'post_migration_logic.buffer_pool_adjustment',
    'post_migration_logic.timeout_reduction',
    'post_migration_logic.confidence_modifier',

    # Peak-hour detection
    'peak_hour_logic.connection_spike_threshold',
    'peak_hour_logic.confidence_modifier',

    # Anomaly detection (per-anomaly blocks). NOTE there is deliberately no
    # 'anomaly_detection.*.risk_penalty' entry: penalties are read from
    # confidence_engine.risk_penalties, so a per-anomaly copy here is dead
    # config that reads as authoritative.
    'anomaly_detection.aborted_connections.threshold',
    'anomaly_detection.memory_pressure.swap_usage_threshold_gb',
    'anomaly_detection.xmlrpc_overload.tmp_disk_table_threshold',
    'anomaly_detection.xmlrpc_overload.select_full_join_threshold',
    'anomaly_detection.connection_spikes.utilisation_threshold',
    'anomaly_detection.*.actions',
    'anomaly_detection.*.actions.*',

    # Confidence engine
    'confidence_engine.base_confidence',
    'confidence_engine.evidence_modifiers.*',
    'confidence_engine.risk_penalties.*',

    # Safety guardrails
    'fallback_logic.safety_guardrails.max_buffer_pool_percent.*',
    'fallback_logic.safety_guardrails.max_connections.*',
    'fallback_logic.safety_guardrails.min_buffer_pool_mb',
    'fallback_logic.safety_guardrails.max_log_file_size_gb',
    'fallback_logic.safety_guardrails.max_buffer_pool_shrink_percent',
    'fallback_logic.max_buffer_pool_percentage',
    'fallback_logic.min_available_ram_mb',
    'fallback_logic.hard_max_connections',
    'fallback_logic.require_confirmation',
    'fallback_logic.backup_enabled',

    # Output / reporting
    'output.format',
    'output.detailed',
    'output.save_report',
    'output.max_saved_reports',
    'output.output_dir',

    # Optional MySQL connection overrides (M12)
    'mysql.host', 'mysql.port', 'mysql.user', 'mysql.password',
    'mysql.socket', 'mysql.defaults_file', 'mysql.timeout',
    'mysql.connect_timeout', 'mysql.read_timeout',
}

# Keys the code reads but that are legitimately optional — absent from the
# shipped YAML because the hardcoded default is the intended behaviour. Listed
# so the "consumed but missing" report stays signal, not noise.
OPTIONAL_CONFIG_KEYS: Set[str] = {
    'fallback_logic.max_buffer_pool_percentage',
    'fallback_logic.min_available_ram_mb',
    'fallback_logic.hard_max_connections',
    'fallback_logic.require_confirmation',
    'fallback_logic.backup_enabled',
    'output.format', 'output.detailed', 'output.save_report',
    'output.max_saved_reports', 'output.output_dir',
    'mysql.host', 'mysql.port', 'mysql.user', 'mysql.password',
    'mysql.socket', 'mysql.defaults_file', 'mysql.timeout',
    'mysql.connect_timeout', 'mysql.read_timeout',
    'metadata.*',
}


# ======================================================================
# 2. Metric keys the recommendation/decision code reads
# ======================================================================
# This is the Round 2 H1 contract. Any key here that _normalize_metrics does not
# produce becomes a silent zero-default somewhere in the engine.
CONSUMED_METRIC_KEYS: Set[str] = {
    # System
    'total_ram_mb', 'available_ram_mb', 'cpu_cores',
    'system_storage_type',
    # InnoDB / config variables
    'innodb_buffer_pool_size_mb', 'innodb_buffer_pool_instances',
    'innodb_log_file_size_mb', 'innodb_io_capacity', 'innodb_io_capacity_max',
    'innodb_flush_neighbors',
    'max_connections', 'table_definition_cache', 'thread_cache_size',
    'open_files_limit', 'key_buffer_size_mb', 'tmp_table_size_mb',
    'join_buffer_size_kb', 'sort_buffer_size_kb', 'read_buffer_size_kb',
    'read_rnd_buffer_size_kb', 'thread_stack_kb',
    # Status counters
    'max_used_connections', 'threads_created', 'connections', 'total_connects',
    'aborted_connects', 'created_tmp_tables', 'created_tmp_disk_tables',
    'select_full_join', 'uptime', 'queries_per_second',
    # Tables / engines
    'total_tables', 'innodb_tables', 'myisam_tables',
    'innodb_table_count', 'myisam_table_count',
    'innodb_data_size_mb', 'myisam_data_size_mb', 'myisam_index_size_mb',
    'database_names',
    # Datadir-derived (M5 storage class, C1 free-space guard)
    'datadir', 'datadir_free_mb',
    # Derived
    'swap_usage_gb', 'innodb_buffer_pool_hit_rate', 'mysql_version',
    'mysql_reachable',
    # Optional / Contextual metrics (MAJ-5, Tranche 4, C-2)
    'last_migration_timestamp',
    'table_stats_uncollected', 'table_count_authoritative',
}

# Metric keys that are consumed by specialized logic when provided, but are
# not required from the base collector on every run.
OPTIONAL_METRIC_KEYS: Set[str] = {
    'last_migration_timestamp',
    'table_stats_uncollected',
    'table_count_authoritative',
}


# ======================================================================
# Helpers
# ======================================================================

def _flatten(node: Any, prefix: str = '') -> List[str]:
    """Return every dotted leaf path in a nested mapping."""
    paths: List[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            path = f'{prefix}.{key}' if prefix else str(key)
            if isinstance(value, dict) and value:
                paths.extend(_flatten(value, path))
            else:
                paths.append(path)
    return paths


def _matches(pattern: str, path: str) -> bool:
    """Segment-wise match. '*' matches ONE segment; a trailing '*' matches any
    remaining depth.

    Single-segment wildcards matter: ``multi_pass_logic.*`` as a catch-all would
    hide a dead leaf like ``multi_pass_logic.pass_1.connection_headroom``, which
    is exactly the drift this module exists to find. Registering
    ``multi_pass_logic.*.buffer_pool_multiplier`` instead keeps the per-pass
    block names free-form while still pinning the leaf names down.
    """
    p_parts = pattern.split('.')
    v_parts = path.split('.')
    trailing = p_parts[-1] == '*'
    if trailing:
        p_parts = p_parts[:-1]
        if len(v_parts) < len(p_parts):
            return False
        v_parts = v_parts[:len(p_parts)]
    elif len(p_parts) != len(v_parts):
        return False
    return all(p == '*' or p == v for p, v in zip(p_parts, v_parts))


def _is_consumed(path: str, consumed: Set[str]) -> bool:
    """True if *path* is covered by any registry entry."""
    return any(_matches(pattern, path) for pattern in consumed)


# ======================================================================
# 3. The audits
# ======================================================================

def audit_anomaly_contract(config: Dict[str, Any],
                           anomaly_names) -> List[str]:
    """Check anomaly names, threshold sections and penalties agree (M1)."""
    problems: List[str] = []
    names = set(anomaly_names)

    penalties = set(
        (config.get('confidence_engine', {}) or {}).get('risk_penalties', {}) or {}
    )
    detection = set(config.get('anomaly_detection', {}) or {})

    for missing in sorted(names - penalties):
        problems.append(
            f"confidence_engine.risk_penalties has no entry for anomaly "
            f"'{missing}' — its confidence penalty will silently be 0"
        )
    for extra in sorted(penalties - names):
        problems.append(
            f"confidence_engine.risk_penalties defines '{extra}', which no "
            f"anomaly is ever named — dead config"
        )
    for missing in sorted(names - detection):
        problems.append(
            f"anomaly_detection has no section for anomaly '{missing}' — its "
            f"thresholds fall back to hardcoded defaults"
        )
    for extra in sorted(detection - names):
        problems.append(
            f"anomaly_detection defines '{extra}', which no anomaly is ever "
            f"named — dead config"
        )
    return problems


def audit_config(config: Dict[str, Any], anomaly_names=()) -> Dict[str, List[str]]:
    """Audit a loaded config against the consumed-key registry.

    Returns ``{'dead': [...], 'missing': [...], 'contract': [...]}``.
    """
    present = set(_flatten(config or {}))

    dead = sorted(p for p in present if not _is_consumed(p, CONSUMED_CONFIG_KEYS))

    # Only literal paths can be reported as missing — a pattern containing a
    # wildcard describes a shape, not a specific key that must exist.
    missing = []
    for key in sorted(CONSUMED_CONFIG_KEYS):
        if '*' in key or key in OPTIONAL_CONFIG_KEYS:
            continue
        if key not in present:
            missing.append(key)

    contract = audit_anomaly_contract(config or {}, anomaly_names)

    return {'dead': dead, 'missing': missing, 'contract': contract}


def audit_metrics(metrics: Dict[str, Any]) -> List[str]:
    """Report metric keys the engine consumes that the collector did not emit.

    This is the Round 2 H1 guard: a missing key here does not raise, it silently
    becomes a zero-default deep inside a recommendation formula.
    """
    if not metrics:
        return []
    required_keys = CONSUMED_METRIC_KEYS - OPTIONAL_METRIC_KEYS
    return sorted(required_keys - set(metrics))


def log_audit(config: Dict[str, Any], anomaly_names=(),
              log: logging.Logger = None) -> Dict[str, List[str]]:
    """Run audit_config and log anything it finds. Never raises."""
    log = log or logger
    try:
        result = audit_config(config, anomaly_names)
    except Exception as exc:               # never break a real run over this
        log.debug(f"Config audit skipped: {exc}")
        return {'dead': [], 'missing': [], 'contract': []}

    for problem in result['contract']:
        log.warning("Config contract: %s", problem)
    for key in result['dead']:
        log.debug("Config key defined but never read by any code path: %s", key)
    for key in result['missing']:
        log.debug("Config key expected by code but absent from YAML: %s", key)
    return result

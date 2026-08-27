#!/usr/bin/env python3
"""
Safety Checker Module - v1.0.4
==============================
Pre-flight verification, safety guardrails, and validation for MySQL configuration changes.

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
import re
import logging
import subprocess
from typing import Dict, List, Any, Tuple
from pathlib import Path


class SafetyChecker:
    """Performs safety checks before applying MySQL optimizations"""

    def __init__(self, safety_config: Dict[str, Any] = None):
        safety_config = safety_config or {}
        self.safety_config = safety_config
        self.logger = logging.getLogger(__name__)

        # Guardrails live under fallback_logic.safety_guardrails in the YAML.
        # (Previously the SafetyChecker looked for a flat 'max_buffer_pool_percentage'
        #  key that the config never defines, so every guardrail silently fell back
        #  to its hardcoded default — H5.)
        guardrails = safety_config.get('safety_guardrails', {})

        # max_buffer_pool_percent is a per-platform dict in the YAML; use its
        # 'default' as the flat backstop here. analyze_system() refines this to
        # the detected platform before validation runs.
        mbp = guardrails.get('max_buffer_pool_percent', {})
        default_pct = mbp.get('default', 0.8) if isinstance(mbp, dict) else (mbp or 0.8)

        # Default safety limits (honour YAML guardrails, fall back to hardcoded)
        self.max_buffer_pool_percentage = safety_config.get('max_buffer_pool_percentage', default_pct)
        self.min_available_ram_mb = safety_config.get('min_available_ram_mb', 2048)
        self.require_confirmation = safety_config.get('require_confirmation', True)
        self.backup_enabled = safety_config.get('backup_enabled', True)

        # Buffer-pool floor / anti-shrink guardrails (H2)
        self.min_buffer_pool_mb = guardrails.get('min_buffer_pool_mb', 1024)
        self.max_buffer_pool_shrink_percent = guardrails.get(
            'max_buffer_pool_shrink_percent', 0.20
        )
        self.max_log_file_size_gb = guardrails.get('max_log_file_size_gb', 16)

    # ------------------------------------------------------------------
    # RAM-proportional thresholds (H4)
    # ------------------------------------------------------------------
    # Both of these used to be flat numbers, which made the tool unusable on the
    # small VPSes it advertises support for: a fixed 2048 MB OS reserve on a
    # 2 GB server left 0 MB for the buffer pool, so the recommendation was
    # dropped entirely, and a fixed 1024 MB pool floor is nonsensical there too.
    # Scaling by total RAM keeps the guardrails identical on large servers
    # (where they were tuned) while behaving sanely on small ones.

    def os_reserve_mb(self, total_ram_mb: int) -> int:
        """RAM to keep away from the buffer pool for the OS and everything else.

        20% of total, floored at 512 MB, ceilinged at the configured value
        (default 2048 MB — the previous flat figure, now the maximum rather than
        a constant). 2 GB -> 512, 4 GB -> 819, 8 GB -> 1638, 16 GB+ -> 2048.
        """
        if not total_ram_mb or total_ram_mb <= 0:
            return self.min_available_ram_mb
        return max(512, min(self.min_available_ram_mb, int(total_ram_mb * 0.20)))

    def min_pool_mb(self, total_ram_mb: int) -> int:
        """Smallest buffer pool worth recommending, scaled to the server.

        The configured floor (default 1024 MB) exists so a mis-derived tiny
        value never replaces a healthy pool (H2). That intent is preserved on
        every server large enough for it; below that it scales to 25% of RAM
        with a 128 MB floor. 512 MB -> 128, 2 GB -> 512, 4 GB+ -> 1024.
        """
        if not total_ram_mb or total_ram_mb <= 0:
            return self.min_buffer_pool_mb
        return min(self.min_buffer_pool_mb, max(128, int(total_ram_mb * 0.25)))

    # ------------------------------------------------------------------
    # Public API – called by the main auto-tuner script
    # ------------------------------------------------------------------
    def validate_recommendations(
        self,
        recommendations: List[Dict[str, Any]],
        metrics: Dict[str, Any],
        allow_shrink: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Validate a list of recommendations against safety guardrails and
        return only those that are safe to apply.

        This is the method called by ``MySQLAutoTunerUltimate.analyze_system()``.

        Args:
            recommendations: list of recommendation dicts produced by the
                             decision engine.
            metrics: flat metrics dict (total_ram_mb, max_connections, …).

        Returns:
            A filtered list of recommendations that passed safety checks.
            Each returned recommendation has an extra ``safety_notes`` key.
        """
        total_ram_mb = metrics.get('total_ram_mb', 0)
        # H4: RAM-proportional, not flat — see os_reserve_mb / min_pool_mb.
        os_reserve_mb = self.os_reserve_mb(total_ram_mb)
        min_pool_mb = self.min_pool_mb(total_ram_mb)
        safe_recommendations: List[Dict[str, Any]] = []

        for rec in recommendations:
            notes: List[str] = []
            drop_rec = False
            param = rec.get('parameter', '')
            rec_value = rec.get('recommended_value', '')

            # --- Buffer pool size guard ---
            if param == 'innodb_buffer_pool_size':
                try:
                    bp_mb = self._parse_size_mb(rec_value)
                    cur_mb = self._parse_size_mb(rec.get('current_value', 0))

                    # 1. Cap to the maximum permitted % of RAM.
                    if total_ram_mb > 0:
                        max_allowed = int(total_ram_mb * self.max_buffer_pool_percentage)
                        if bp_mb > max_allowed:
                            notes.append(
                                f"Buffer pool capped from {bp_mb}M to {max_allowed}M "
                                f"({self.max_buffer_pool_percentage:.0%} of {total_ram_mb}M RAM)"
                            )
                            rec = dict(rec)
                            rec['recommended_value'] = f'{max_allowed}M'
                            bp_mb = max_allowed

                    # 2. Enforce the minimum floor — never recommend a tiny pool.
                    if bp_mb < min_pool_mb:
                        max_allowed = (
                            int(total_ram_mb * self.max_buffer_pool_percentage)
                            if total_ram_mb > 0 else min_pool_mb
                        )
                        if total_ram_mb == 0 or min_pool_mb <= max_allowed:
                            notes.append(
                                f"Buffer pool raised to minimum floor {min_pool_mb}M"
                            )
                            rec = dict(rec)
                            rec['recommended_value'] = f'{min_pool_mb}M'
                            bp_mb = min_pool_mb
                        else:
                            self.logger.warning(
                                "Dropping buffer pool recommendation: minimum floor "
                                "%sM exceeds the %.0f%% RAM cap on this %sM server.",
                                min_pool_mb,
                                self.max_buffer_pool_percentage * 100, total_ram_mb,
                            )
                            drop_rec = True

                    # 3. Refuse a large shrink of a HEALTHY pool (H2) unless the
                    #    operator explicitly opted in via --allow-buffer-pool-shrink.
                    #
                    #    "Healthy" is the load-bearing word. A pool that already
                    #    exceeds the RAM cap is not healthy — a 4 GB pool on a
                    #    1 GB server means the machine is swapping itself to
                    #    death — and refusing to shrink it does not protect the
                    #    operator, it preserves an outage. The guard exists to
                    #    stop a mis-derived value replacing a good one, so it
                    #    steps aside when the current value is itself unsafe.
                    #    (Found by tests/test_regressions.py on its first run.)
                    pool_already_unsafe = (
                        total_ram_mb > 0
                        and cur_mb > int(total_ram_mb * self.max_buffer_pool_percentage)
                    )
                    if pool_already_unsafe and bp_mb < cur_mb:
                        notes.append(
                            f"Current buffer pool {cur_mb}M exceeds "
                            f"{self.max_buffer_pool_percentage:.0%} of "
                            f"{total_ram_mb}M RAM — shrinking is a correction, "
                            f"not a downgrade"
                        )
                        self.logger.warning(
                            "innodb_buffer_pool_size is %sM on a %sM server "
                            "(over the %.0f%% cap). Recommending %sM; the "
                            "anti-shrink guard does not apply to a pool that "
                            "cannot fit in RAM.",
                            cur_mb, total_ram_mb,
                            self.max_buffer_pool_percentage * 100, bp_mb,
                        )
                    elif not drop_rec and cur_mb > 0 and bp_mb < cur_mb:
                        shrink_pct = (cur_mb - bp_mb) / cur_mb
                        if shrink_pct > self.max_buffer_pool_shrink_percent:
                            if not allow_shrink:
                                self.logger.warning(
                                    "Refusing to shrink innodb_buffer_pool_size "
                                    "%sM -> %sM (-%.0f%%); current pool preserved. "
                                    "Re-run with --allow-buffer-pool-shrink to override.",
                                    cur_mb, bp_mb, shrink_pct * 100,
                                )
                                drop_rec = True
                            else:
                                notes.append(
                                    f"Buffer pool shrink of {shrink_pct:.0%} applied "
                                    f"(--allow-buffer-pool-shrink set)"
                                )

                    # 4. Hard cap: guarantee OS RAM headroom (H5 — was a note only).
                    if not drop_rec and total_ram_mb > 0:
                        final_mb = self._parse_size_mb(rec['recommended_value'])
                        max_for_os = total_ram_mb - os_reserve_mb
                        if final_mb > max_for_os:
                            if max_for_os >= min_pool_mb:
                                notes.append(
                                    f"Buffer pool capped to {max_for_os}M to reserve "
                                    f"{os_reserve_mb}M RAM for the OS"
                                )
                                rec = dict(rec)
                                rec['recommended_value'] = f'{max_for_os}M'
                            else:
                                self.logger.warning(
                                    "Dropping buffer pool recommendation: cannot "
                                    "reserve %sM for the OS while keeping the pool "
                                    "above the %sM floor on this %sM server.",
                                    os_reserve_mb, min_pool_mb, total_ram_mb,
                                )
                                drop_rec = True
                except (ValueError, TypeError):
                    notes.append(f"Could not parse buffer pool value: {rec_value}")

            if drop_rec:
                continue

            # --- Max connections guard ---
            # MAJ-1: treat max_connections as raise-only relative to
            # max(current_value, max_used_connections). Lowering max_connections
            # below current setting or observed peak causes connection exhaustion
            # outages at peak traffic.
            if param == 'max_connections':
                try:
                    val = int(rec_value)
                    cur_val = int(rec.get('current_value', 0) or metrics.get('max_connections', 0) or 0)
                    max_used = int(metrics.get('max_used_connections', 0) or 0)
                    conn_floor = max(cur_val, max_used)

                    if conn_floor > 0 and val < conn_floor:
                        self.logger.warning(
                            "Refusing to downgrade max_connections from %d (observed peak: %d) "
                            "to %d; preserving connection floor to prevent outages.",
                            cur_val, max_used, val,
                        )
                        drop_rec = True
                        continue

                    hard_max = self.safety_config.get('hard_max_connections', 5000)
                    if val > hard_max:
                        rec = dict(rec)
                        rec['recommended_value'] = hard_max if hard_max >= conn_floor else conn_floor
                        notes.append(f"max_connections capped to hard limit {hard_max}")
                except (ValueError, TypeError):
                    pass

            # --- Log file size guard ---
            # C1: match BOTH names. `innodb_log_file_size` is what the base
            # recommender emits and what this checker normally sees; on MySQL
            # 8.4 the version-compat layer renames it to
            # `innodb_redo_log_capacity`. That rename is now applied after this
            # method runs, but accepting both names costs nothing and keeps the
            # cap effective if the ordering is ever changed back or a future
            # code path hands us an already-renamed recommendation. MySQL
            # PREALLOCATES redo capacity on startup, so an uncapped value here
            # consumes real disk (or refuses to start) — redundant safety is
            # correct safety on a tool that writes my.cnf.
            if param in ('innodb_log_file_size', 'innodb_redo_log_capacity'):
                try:
                    log_mb = self._parse_size_mb(rec_value)
                    max_log_gb = self.max_log_file_size_gb
                    if log_mb > max_log_gb * 1024:
                        rec = dict(rec)
                        rec['recommended_value'] = f'{int(max_log_gb * 1024)}M'
                        log_mb = int(max_log_gb * 1024)
                        notes.append(f"Log file size capped to {max_log_gb}G")
                        self.logger.warning(
                            "Capped %s from %dM to %dM (%dG guardrail)",
                            param, self._parse_size_mb(rec_value), log_mb, max_log_gb,
                        )

                    # C1 (deferred half) & N-2/N-9: the size guardrail bounds the damage
                    # but does not confirm the disk can hold it. MySQL
                    # PREALLOCATES redo capacity on startup, and
                    # `mysqld --validate-config` approves any legal value
                    # regardless of free space — so an oversized value stops the
                    # server from starting with no earlier warning. Keep the
                    # proposed log within half of the free space on the datadir
                    # filesystem, leaving room for the data itself to grow.
                    free_mb = metrics.get('datadir_free_mb')
                    version_info = metrics.get('version_compat', {})
                    is_mariadb = (
                        version_info.get('is_mariadb', False)
                        or 'mariadb' in str(metrics.get('mysql_version', '')).lower()
                    )
                    # For MySQL 8.0.x / non-MariaDB pre-8.4, innodb_log_files_in_group=2 by default
                    files_in_group = (
                        metrics.get('innodb_log_files_in_group', 2)
                        if (param == 'innodb_log_file_size' and not is_mariadb)
                        else 1
                    )
                    required_allocation_mb = log_mb * files_in_group

                    if free_mb is None:
                        self.logger.warning(
                            "Could not measure free space for datadir; log size recommendation (%dM) "
                            "will proceed without disk-budget safety cap.",
                            log_mb,
                        )
                    elif free_mb <= 0:
                        self.logger.warning(
                            "Dropping %s recommendation: datadir has 0MB free (disk full).",
                            param,
                        )
                        drop_rec = True
                    else:
                        budget_mb = int(free_mb * 0.5)
                        if required_allocation_mb > budget_mb:
                            allowed_log_mb = int(budget_mb / files_in_group)
                            if allowed_log_mb >= 256:
                                rec = dict(rec)
                                rec['recommended_value'] = f'{allowed_log_mb}M'
                                alloc_str = f" (total allocation: {allowed_log_mb * files_in_group}M across {files_in_group} files)" if files_in_group > 1 else ""
                                notes.append(
                                    f"Log size capped to {allowed_log_mb}M{alloc_str} — only "
                                    f"{free_mb}M free on the datadir filesystem "
                                    f"(redo is preallocated at startup)"
                                )
                                self.logger.warning(
                                    "Capped %s to %dM (total redo allocation: %dM): datadir has only %dM free "
                                    "and redo capacity is preallocated.",
                                    param, allowed_log_mb, allowed_log_mb * files_in_group, free_mb,
                                )
                            else:
                                self.logger.warning(
                                    "Dropping %s recommendation: datadir has only "
                                    "%dM free, too little to grow the redo log "
                                    "safely.", param, free_mb,
                                )
                                drop_rec = True
                except (ValueError, TypeError):
                    pass

            if drop_rec:
                continue

            # --- Restart-required flag ---
            if rec.get('restart_required', False):
                notes.append("Requires MySQL/MariaDB restart to take effect")

            # L3: 'safety_passed' was dropped. It was set from a `safe` variable
            # initialised True and never assigned again, so it was True on every
            # recommendation that existed — and nothing read it. Rejection now
            # happens by DROPPING the recommendation (drop_rec above), so
            # surviving this method IS the pass signal; a field that can never
            # be False only invites someone to trust it as a real check.
            rec_out = dict(rec)
            rec_out['safety_notes'] = notes
            safe_recommendations.append(rec_out)

        return safe_recommendations

    def validate_optimization_safety(
        self,
        recommendations: List[Dict[str, Any]],
        metrics: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Legacy method – kept for backward compatibility."""
        validation_result = {
            'safe': True,
            'issues': [],
            'warnings': [],
            'risky_changes': [],
        }
        total_ram_mb = metrics.get('total_ram_mb', 0)

        for rec in recommendations:
            parameter = rec.get('parameter', '')
            recommended_value = rec.get('recommended_value', '')

            if parameter == 'innodb_buffer_pool_size':
                try:
                    buffer_pool_mb = self._parse_size_mb(recommended_value)
                    buffer_pool_pct = (buffer_pool_mb / total_ram_mb) if total_ram_mb > 0 else 0
                    if buffer_pool_pct > self.max_buffer_pool_percentage:
                        validation_result['safe'] = False
                        validation_result['issues'].append(
                            f"Buffer pool {buffer_pool_mb}M ({buffer_pool_pct:.1%}) "
                            f"exceeds safety limit {self.max_buffer_pool_percentage:.0%}"
                        )
                    remaining = total_ram_mb - buffer_pool_mb
                    if remaining < self.min_available_ram_mb:
                        validation_result['safe'] = False
                        validation_result['issues'].append(
                            f"Only {remaining}M RAM left for OS "
                            f"(minimum {self.min_available_ram_mb}M)"
                        )
                except (ValueError, TypeError):
                    validation_result['warnings'].append(
                        f"Could not parse buffer pool size: {recommended_value}"
                    )

            if rec.get('restart_required', False):
                validation_result['risky_changes'].append({
                    'parameter': parameter,
                    'reason': 'Requires MySQL restart',
                    'impact': 'Service downtime',
                })

        return validation_result

    # ------------------------------------------------------------------
    # Pre-flight checks
    # ------------------------------------------------------------------
    def pre_flight_checks(self) -> Dict[str, Any]:
        """Perform comprehensive pre-flight safety checks"""
        self.logger.info("Performing pre-flight safety checks...")
        results = {
            'safe': True,
            'issues': [],
            'warnings': [],
            'checks_performed': [],
        }

        checks = [
            self._check_system_resources,
            self._check_mysql_status,
            self._check_file_permissions,
            self._check_disk_space,
            self._check_backup_capability,
            self._check_mysql_version_compatibility,
        ]

        for check in checks:
            try:
                check_result = check()
                results['checks_performed'].append(check_result['name'])
                if not check_result['passed']:
                    results['safe'] = False
                    results['issues'].extend(check_result.get('issues', []))
                results['warnings'].extend(check_result.get('warnings', []))
            except Exception as e:
                self.logger.error(f"Safety check failed: {e}")
                results['safe'] = False
                results['issues'].append(f"Safety check error: {e}")

        if results['safe']:
            self.logger.info("All safety checks passed")
        else:
            self.logger.warning(f"Safety checks failed with {len(results['issues'])} issues")
        return results

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------
    def _check_system_resources(self) -> Dict[str, Any]:
        result = {'name': 'System Resources', 'passed': True, 'issues': [], 'warnings': []}
        try:
            mem = self._get_memory_info()
            if mem:
                avail = mem.get('available_mb', 0)
                total = mem.get('total_mb', 0)

                # H4: this used to hard-fail whenever MemAvailable was below a
                # flat 2048 MB, which blocked --optimize on every 2 GB VPS AND
                # on any busy larger server — yet low available RAM is the
                # NORMAL state for a well-tuned database host, because MySQL has
                # already claimed it. Failing there punished exactly the servers
                # most in need of tuning.
                #
                # The real memory protection is the buffer-pool cap in
                # validate_recommendations (which reserves os_reserve_mb and can
                # never be exceeded) plus the worst-case footprint estimate. So
                # this check now only hard-fails when RAM is critically low in
                # absolute AND relative terms; otherwise it warns.
                # The critical floor is deliberately ABSOLUTE (128-256 MB), not
                # a percentage of total. A percentage re-creates the very bug
                # being fixed at a higher tier: 5% of a 128 GB server is 6.5 GB,
                # and a well-tuned 128 GB database host running with 3 GB free
                # is perfectly healthy — its buffer pool is supposed to have
                # claimed the rest. What is actually dangerous is having too
                # little RAM left for the OS to function, and that quantity does
                # not grow with machine size.
                critical = max(128, min(256, int(total * 0.05))) if total > 0 else 128
                reserve = self.os_reserve_mb(total)
                if avail < critical:
                    result['passed'] = False
                    result['issues'].append(
                        f"Critically low available RAM: {avail}MB "
                        f"(< {critical}MB). Applying changes now is unsafe."
                    )
                elif avail < reserve:
                    result['warnings'].append(
                        f"Low available RAM: {avail}MB (below the {reserve}MB OS "
                        f"reserve). Recommendations are capped accordingly."
                    )

                usage = ((total - avail) / total * 100) if total > 0 else 0
                if usage > 95:
                    result['warnings'].append(f"High memory usage: {usage:.1f}%")

            load = self._get_load_average()
            if load:
                cores = self._get_cpu_cores()
                if load.get('1min', 0) > cores * 3:
                    result['warnings'].append(
                        f"Very high load: {load['1min']:.2f} on {cores} cores"
                    )
        except Exception as e:
            result['warnings'].append(f"Could not check system resources: {e}")
        return result

    def _check_mysql_status(self) -> Dict[str, Any]:
        result = {'name': 'MySQL Status', 'passed': True, 'issues': [], 'warnings': []}
        try:
            mysql_host = str(self.safety_config.get('mysql_host') or self.safety_config.get('host') or '').lower()
            is_remote = bool(mysql_host) and mysql_host not in ('localhost', '127.0.0.1', '::1')

            if not self._is_mysql_running():
                if is_remote:
                    result['warnings'].append(
                        f"Local MySQL process not found (connecting to remote host '{mysql_host}')"
                    )
                else:
                    result['passed'] = False
                    result['issues'].append("MySQL service is not running")
        except Exception as e:
            result['warnings'].append(f"Could not check MySQL status: {e}")
        return result

    def _check_file_permissions(self) -> Dict[str, Any]:
        """M10: check the file the tool will ACTUALLY write.

        This used to iterate its own 3-entry list and `break` on the first hit,
        so on Debian/Ubuntu it could validate `/etc/mysql/my.cnf` while
        `_find_mycnf` resolved a different file under `conf.d` — a permission
        check on the wrong target is worse than none.
        """
        result = {'name': 'File Permissions', 'passed': True, 'issues': [], 'warnings': []}
        try:
            from utils.mycnf_paths import find_mycnf_or_none

            cfg = find_mycnf_or_none()
            if cfg is None:
                result['warnings'].append(
                    "No MySQL config file found in standard locations"
                )
                return result
            if not os.access(cfg, os.R_OK):
                result['passed'] = False
                result['issues'].append(f"Cannot read: {cfg}")
            if not os.access(cfg, os.W_OK):
                result['passed'] = False
                result['issues'].append(f"Cannot write: {cfg}")
        except Exception as e:
            result['warnings'].append(f"Could not check file permissions: {e}")
        return result

    def _check_disk_space(self) -> Dict[str, Any]:
        """M10: include the partition holding my.cnf.

        The backup and the atomic temp file are both written NEXT TO my.cnf —
        usually /etc on / — which was the one filesystem this never checked.
        """
        result = {'name': 'Disk Space', 'passed': True, 'issues': [], 'warnings': []}
        try:
            from utils.mycnf_paths import find_mycnf_or_none

            targets = ['/var/lib/mysql', '/var/backups']
            mycnf = find_mycnf_or_none()
            if mycnf:
                targets.insert(0, os.path.dirname(mycnf))

            for d in targets:
                if os.path.exists(d):
                    usage = self._get_disk_usage(d)
                    if usage and 'available_mb' in usage:
                        avail = usage.get('available_mb', 0)
                        if avail < 100:
                            result['passed'] = False
                            result['issues'].append(f"Disk space critical on {d}: {avail}MB")
                        elif avail < 500:
                            result['warnings'].append(f"Low disk on {d}: {avail}MB")
                    else:
                        result['warnings'].append(f"Could not determine disk usage for {d}")
        except Exception as e:
            result['warnings'].append(f"Could not check disk space: {e}")
        return result

    def _check_backup_capability(self) -> Dict[str, Any]:
        """MAJ-7: Validate backup writability in the directory containing my.cnf.

        The actual apply step writes the backup adjacent to my.cnf
        ({mycnf_path}.backup.{ts}). Validate that directory rather than creating
        an unused /var/backups/mysql-autotuner folder.
        """
        result = {'name': 'Backup Capability', 'passed': True, 'issues': [], 'warnings': []}
        if not self.backup_enabled:
            result['warnings'].append("Backup is disabled")
            return result
        try:
            from utils.mycnf_paths import find_mycnf_or_none

            mycnf = find_mycnf_or_none()
            if mycnf is None:
                result['warnings'].append(
                    "No MySQL config file found; skipping backup location check"
                )
                return result

            backup_dir = Path(os.path.dirname(mycnf))
            if not os.path.exists(backup_dir):
                result['passed'] = False
                result['issues'].append(f"Config directory does not exist: {backup_dir}")
                return result

            test_file_path = str(backup_dir / f".test_backup_write_{os.getpid()}")
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = os.open(test_file_path, flags, 0o600)
                try:
                    os.write(fd, b"test")
                finally:
                    os.close(fd)
                try:
                    os.unlink(test_file_path)
                except OSError:
                    pass
            except Exception as e:
                result['passed'] = False
                result['issues'].append(f"Cannot create backups in {backup_dir}: {e}")
        except Exception as e:
            result['passed'] = False
            result['issues'].append(f"Cannot verify backup capability: {e}")
        return result

    def _check_mysql_version_compatibility(self) -> Dict[str, Any]:
        result = {'name': 'MySQL Version', 'passed': True, 'issues': [], 'warnings': []}
        try:
            ver = self._get_mysql_version()
            if ver:
                is_mariadb = ver.get('is_mariadb', False)
                major = ver.get('major_version', '')
                # NB: compare as (major, minor) tuples, NOT float(major) — e.g.
                # float("10.11") == 10.11 < 10.5 would wrongly flag MariaDB 10.11
                # LTS as unsupported (M8).
                if is_mariadb and major:
                    if self._version_tuple(major) < (10, 5):
                        result['warnings'].append(
                            f"MariaDB {major} is below the minimum supported version (10.5)"
                        )
                elif major:
                    if self._version_tuple(major) < (8, 0):
                        result['warnings'].append(
                            f"MySQL {major} is below the minimum supported version (8.0)"
                        )
            else:
                result['warnings'].append("Could not detect MySQL/MariaDB version")
        except Exception as e:
            result['warnings'].append(f"Version check error: {e}")
        return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _version_tuple(version_str) -> Tuple[int, int]:
        """Parse '10.11' or '10.11.6-MariaDB' into a (major, minor) tuple."""
        nums = re.findall(r'\d+', str(version_str))
        if not nums:
            return (0, 0)
        major = int(nums[0])
        minor = int(nums[1]) if len(nums) > 1 else 0
        return (major, minor)

    @staticmethod
    def _parse_size_mb(value) -> int:
        """Parse a size string like '4096M', '8G', '512K', '2GB' into megabytes."""
        if isinstance(value, (int, float)):
            return int(value)
        s = str(value).strip().upper()
        if s.endswith(('G', 'GB')):
            num = s[:-2] if s.endswith('GB') else s[:-1]
            return int(float(num) * 1024)
        if s.endswith(('M', 'MB')):
            num = s[:-2] if s.endswith('MB') else s[:-1]
            return int(float(num))
        if s.endswith(('K', 'KB')):
            num = s[:-2] if s.endswith('KB') else s[:-1]
            return max(1, int(float(num) / 1024))
        try:
            val = float(s)
            if val > 10000000:
                return int(val / (1024 * 1024))
            return int(val)
        except (ValueError, TypeError):
            return 0

    def _get_memory_info(self) -> Dict[str, Any]:
        try:
            if os.path.exists('/proc/meminfo'):
                info = {}
                with open('/proc/meminfo') as f:
                    for line in f:
                        if ':' in line:
                            k, v = line.split(':', 1)
                            if 'kB' in v:
                                info[k.strip()] = int(v.replace('kB', '').strip())
                return {
                    'total_mb': info.get('MemTotal', 0) // 1024,
                    'available_mb': info.get('MemAvailable', 0) // 1024,
                }
        except Exception:
            pass
        return {}

    @staticmethod
    def _get_load_average() -> Dict[str, float]:
        try:
            if os.path.exists('/proc/loadavg'):
                with open('/proc/loadavg') as f:
                    parts = f.read().strip().split()
                    if len(parts) >= 3:
                        return {'1min': float(parts[0]), '5min': float(parts[1]), '15min': float(parts[2])}
        except Exception:
            pass
        return {}

    @staticmethod
    def _get_cpu_cores() -> int:
        try:
            if os.path.exists('/proc/cpuinfo'):
                with open('/proc/cpuinfo') as f:
                    return sum(1 for line in f if line.startswith('processor'))
        except Exception:
            pass
        return 1

    @staticmethod
    def _is_mysql_running() -> bool:
        """Check if MySQL or MariaDB daemon process is actively running (exact matching)."""
        try:
            r = subprocess.run(['pgrep', '-x', 'mysqld|mariadbd'], capture_output=True, timeout=5)
            if r.returncode == 0:
                return True
            for proc in ('mysqld', 'mariadbd'):
                r = subprocess.run(['pgrep', '-x', proc], capture_output=True, timeout=5)
                if r.returncode == 0:
                    return True
        except Exception:
            pass
        return False

    @staticmethod
    def _get_disk_usage(path: str) -> Dict[str, Any]:
        """Get disk usage stats for *path* using shutil.disk_usage (mirroring SystemInfo.get_free_space_mb)."""
        try:
            import shutil as _shutil
            probe = path
            while probe and not os.path.exists(probe):
                parent = os.path.dirname(probe)
                if parent == probe:
                    break
                probe = parent
            if probe and os.path.exists(probe):
                usage = _shutil.disk_usage(probe)
                return {
                    'total_mb': int(usage.total // (1024 * 1024)),
                    'used_mb': int(usage.used // (1024 * 1024)),
                    'available_mb': int(usage.free // (1024 * 1024)),
                }
        except (OSError, ValueError, AttributeError) as e:
            logging.getLogger(__name__).warning(f"Could not determine disk usage for {path}: {e}")
        return {}

    @staticmethod
    def _get_mysql_version() -> Dict[str, Any]:
        """L8: probe mariadbd as well as mysqld, by name and absolute path.

        MariaDB 11.x installs may ship only `mariadbd`, with no `mysqld` symlink
        — so this returned {} and the version check degraded to "Could not
        detect MySQL/MariaDB version" on exactly the newest supported versions.
        `_validate_config_file` already probes both; this now matches it.
        """
        import shutil as _shutil

        candidates = []
        for name in ('mariadbd', 'mysqld'):
            found = _shutil.which(name)
            if found:
                candidates.append(found)
        candidates += ['/usr/sbin/mariadbd', '/usr/sbin/mysqld',
                       '/usr/libexec/mysqld', '/usr/local/mysql/bin/mysqld']

        for binary in candidates:
            if os.path.isabs(binary) and not os.path.exists(binary):
                continue
            try:
                r = subprocess.run([binary, '--version'], capture_output=True,
                                   text=True, timeout=10)
            except Exception:
                continue
            if r.returncode == 0:
                out = r.stdout.strip()
                m = re.search(r'(\d+\.\d+)', out)
                return {
                    'version_string': out,
                    'is_mariadb': 'MariaDB' in out or 'mariadb' in binary,
                    'major_version': m.group(1) if m else 'Unknown',
                }
        return {}


def perform_safety_checks(safety_config: Dict[str, Any] = None) -> Dict[str, Any]:
    """Convenience function to perform safety checks"""
    if safety_config is None:
        safety_config = {
            'max_buffer_pool_percentage': 0.8,
            'min_available_ram_mb': 2048,
            'require_confirmation': True,
            'backup_enabled': True,
        }
    checker = SafetyChecker(safety_config)
    return checker.pre_flight_checks()

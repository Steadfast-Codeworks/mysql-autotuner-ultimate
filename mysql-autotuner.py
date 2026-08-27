#!/usr/bin/env python3
"""
MySQL Auto-Tuner Ultimate v1.0.4
================================
Evidence-Based Automated MySQL/MariaDB Performance Tuning Tool

Based on 66 real-world production optimization cases across cPanel, DirectAdmin,
and bare-metal environments.

Author: R.L. Burger (Steadfast Codeworks)
Date: 2025-09-07
Last Updated: 2026-08-24
Version: 1.0.4
Copyright (c) 2026 R.L. Burger
Project: Steadfast Tools
Website: https://www.steadfasttools.com
License: MIT License

Usage:
    ./mysql-autotuner.py --analyze --show-evidence --explain
    ./mysql-autotuner.py --optimize --pass-number 2 --dry-run
    ./mysql-autotuner.py --multi-pass --max-passes 3
    ./mysql-autotuner.py --file-limit-check --platform directadmin
    ./mysql-autotuner.py --dump-effective-config --output-format json
"""

import os
import sys
import json
import yaml
import signal
import argparse
import logging
from datetime import datetime
from pathlib import Path
from contextlib import contextmanager

# ---------------------------------------------------------------------------
# Project root & path setup
# ---------------------------------------------------------------------------
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from core.collector import DataCollector
from core.ultimate_decision_engine import UltimateDecisionEngine
from core.version_compat import VersionCompatibility
from core.explain_engine import ExplainEngine
from core.config_audit import audit_metrics
from safety.checks import SafetyChecker
from output.reporter import ReportGenerator
from utils.mysql_connector import MySQLConnector
from utils.system_info import SystemInfo


# ===================================================================
#  Helper: safe division (prevents ZeroDivisionError everywhere)
# ===================================================================
def safe_div(numerator, denominator, default=0.0):
    """Return numerator/denominator, or *default* when denominator is zero."""
    try:
        n = float(numerator)
        d = float(denominator)
        return n / d if d != 0 else default
    except (ValueError, TypeError):
        return default


@contextmanager
def _critical_section_trap(rollback_callback, logger=None):
    """Trap SIGINT, SIGTERM, and BaseException during the critical window so rollback is executed on interruption."""
    interrupted = []

    def _handler(signum, frame):
        sig_name = signal.Signals(signum).name if hasattr(signal, "Signals") else f"signal {signum}"
        if logger:
            logger.critical(f"Interrupted by {sig_name} during critical window! Triggering emergency rollback.")
        interrupted.append(signum)
        try:
            rollback_callback()
        except Exception as exc:
            if logger:
                logger.critical(f"Emergency rollback failed: {exc}")
        if signum == getattr(signal, "SIGINT", 2):
            raise KeyboardInterrupt("Interrupted during apply window")
        else:
            raise SystemExit(128 + signum)

    prev_int = None
    prev_term = None
    try:
        try:
            prev_int = signal.signal(signal.SIGINT, _handler)
        except (ValueError, AttributeError):
            pass
        try:
            if hasattr(signal, "SIGTERM"):
                prev_term = signal.signal(signal.SIGTERM, _handler)
        except (ValueError, AttributeError):
            pass
        yield
    except (Exception, KeyboardInterrupt) as exc:
        if not interrupted:
            if logger:
                logger.critical(f"Exception during critical apply window: {exc}. Triggering emergency rollback.")
            try:
                rollback_callback()
            except Exception as rollback_err:
                if logger:
                    logger.critical(f"Emergency rollback failed: {rollback_err}")
        raise
    finally:
        try:
            if prev_int is not None:
                signal.signal(signal.SIGINT, prev_int)
        except (ValueError, AttributeError):
            pass
        try:
            if prev_term is not None and hasattr(signal, "SIGTERM"):
                signal.signal(signal.SIGTERM, prev_term)
        except (ValueError, AttributeError):
            pass


class MySQLAutoTunerUltimate:
    """
    Ultimate MySQL Auto-Tuner with DirectAdmin file limit logic,
    educational --explain mode, and multi-version compatibility.
    """

    # Single source of truth lives in utils/version.py so the reports cannot
    # drift from the CLI (--version) or from each other.
    try:
        from utils.version import TOOL_VERSION as VERSION
    except ImportError:  # pragma: no cover
        VERSION = "1.0.4"

    # H2: --profile buffer-pool multipliers per optimisation pass. The
    # 'balanced' row is identical to the shipped config_ultimate.yaml values, so
    # applying it is a no-op and the flag's default stays faithful to the file.
    # Previously this table lived inside dump_effective_config() and was never
    # consulted by the real pipeline, making --profile a decorative flag.
    PROFILE_MULTIPLIERS = {
        "safe":       {"pass_1": 0.6, "pass_2": 0.75, "pass_3": 0.9},
        "balanced":   {"pass_1": 0.7, "pass_2": 0.85, "pass_3": 0.95},
        "aggressive": {"pass_1": 0.8, "pass_2": 0.95, "pass_3": 1.0},
    }

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------
    def __init__(self, config_file: str = None, mysql_overrides: dict = None):
        self.version = self.VERSION
        self.config_file = config_file or str(project_root / "config_ultimate.yaml")
        self.profile = "balanced"
        # M12: CLI connection overrides (host/port/user/password/socket/
        # defaults_file), applied on top of the config's `mysql:` block.
        self.mysql_overrides = mysql_overrides or {}

        # Set from the CLI: permit large buffer-pool shrinks (H2). Off by default.
        self.allow_bp_shrink = False
        # Set from the CLI: permit auto-restart on a cluster/replica node (M6).
        self.allow_cluster_restart = False

        self._setup_logging()

        # Load configuration
        with open(self.config_file, "r") as fh:
            self.config = yaml.safe_load(fh) or {}

        # Initialise components.
        #
        # M12: connection settings come from a dedicated `mysql:` block, merged
        # with any CLI overrides. Previously MySQLConnector was handed the WHOLE
        # config and looked for top-level `host`/`user`/`password`/`socket` keys
        # that config_ultimate.yaml never defined and no CLI flag could set — so
        # the auto-detection chain (/root/.my.cnf, debian.cnf, DirectAdmin conf,
        # socket auth) was the only way in. Any host with a non-standard socket,
        # a remote database, or credentials in a custom file simply could not
        # run the tool, with nothing the operator could do about it.
        mysql_cfg = dict(self.config.get("mysql", {}) or {})
        mysql_cfg.update({k: v for k, v in (self.mysql_overrides or {}).items()
                          if v is not None})
        self.mysql_connector = MySQLConnector(mysql_cfg)
        self.system_info = SystemInfo()
        self.data_collector = DataCollector(self.mysql_connector, self.system_info)
        self.decision_engine = UltimateDecisionEngine(self.config)
        self.version_compat = VersionCompatibility()
        self.explain_engine = ExplainEngine()

        # Safety checker expects a dict of safety-related settings.
        # L10: work on a COPY. `config.get(...)` returns the live sub-dict, so
        # setdefault() was mutating self.config — which then showed up as two
        # invented keys in --dump-effective-config that are not in the user's
        # file. Defaults belong to the checker, not to the reported config.
        safety_cfg = dict(self.config.get("fallback_logic", {}) or {})
        safety_cfg.setdefault("max_buffer_pool_percentage", 0.8)
        safety_cfg.setdefault("min_available_ram_mb", 2048)
        if mysql_cfg.get("host"):
            safety_cfg["mysql_host"] = mysql_cfg["host"]
        self.safety_checker = SafetyChecker(safety_cfg)

        # Reporter. M6: report files are opt-in; main() flips this on for
        # --save-report before any report is generated.
        output_cfg = self.config.get("output", {})
        self.report_generator = ReportGenerator(output_cfg)

        # Config <-> code consistency audit. Contract violations (an anomaly
        # with no penalty, a penalty for no anomaly) are logged as warnings;
        # dead/missing keys are DEBUG so a customised YAML stays quiet. Run
        # --check-config for the full report and a CI-usable exit code.
        try:
            from core.config_audit import log_audit
            log_audit(self.config, UltimateDecisionEngine.ANOMALY_NAMES, self.logger)
        except Exception as exc:
            self.logger.debug(f"Config audit unavailable: {exc}")

        self.logger.info(f"MySQL Auto-Tuner Ultimate v{self.version} initialised")
        total_cases = (
            self.config.get("evidence_base", {}).get("total_cases", 66)
        )
        self.logger.info(f"Evidence base: {total_cases} production cases")

    def _setup_logging(self):
        from utils.safe_io import choose_writable_dir, secure_open_write

        log_fmt = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        handlers = [logging.StreamHandler(sys.stdout)]

        # M1: write logs to a root-owned dir, or a UID-scoped temp dir — never a
        # predictable shared /tmp path — and open O_NOFOLLOW to defeat symlink
        # pre-creation attacks on shared hosts.
        log_dir = choose_writable_dir(
            "/var/log/mysql-autotuner", "mysql-autotuner-logs"
        )
        if log_dir:
            log_path = os.path.join(log_dir, "mysql-autotuner-ultimate.log")
            try:
                stream = secure_open_write(log_path, append=True)
                handlers.append(logging.StreamHandler(stream))
            except (PermissionError, OSError):
                pass

        logging.basicConfig(
            level=logging.INFO,
            format=log_fmt,
            handlers=handlers,
        )
        self.logger = logging.getLogger(__name__)

    # ------------------------------------------------------------------
    # Optimisation profile (--profile)
    # ------------------------------------------------------------------
    def apply_profile(self, profile: str) -> None:
        """Apply a --profile preset to the live config (H2).

        Writes the profile's buffer-pool multipliers into
        ``multi_pass_logic.pass_N``, which is where
        ``generate_multi_pass_recommendations`` actually reads them. The decision
        engine holds ``self.config`` by reference, so this reaches it.

        This is the ONLY place the profile is applied — ``dump_effective_config``
        now reports the already-applied state rather than applying it a second
        time.
        """
        if not profile or profile not in self.PROFILE_MULTIPLIERS:
            return
        self.profile = profile
        multipliers = self.PROFILE_MULTIPLIERS[profile]
        mp = self.config.setdefault("multi_pass_logic", {})
        changed = []
        for pass_key, value in multipliers.items():
            pass_cfg = mp.setdefault(pass_key, {})
            if pass_cfg.get("buffer_pool_multiplier") != value:
                changed.append(f"{pass_key}={value}")
            pass_cfg["buffer_pool_multiplier"] = value
        if changed:
            self.logger.info(
                "Profile '%s' applied — buffer pool multipliers: %s",
                profile, ", ".join(changed),
            )
        else:
            self.logger.info(
                "Profile '%s' applied (matches the configured defaults)", profile
            )

    # ------------------------------------------------------------------
    # Core analysis
    # ------------------------------------------------------------------
    def analyze_system(
        self,
        show_evidence: bool = False,
        platform: str = "auto",
        explain: bool = False,
    ) -> dict:
        """Analyse the MySQL system and return a results dict."""
        self.logger.info("Starting ultimate system analysis ...")

        try:
            # 1. Collect metrics
            metrics = self.data_collector.collect_all_metrics()

            # --- C4 guard: refuse to proceed on an unreachable database ---
            # If we never connected, SHOW VARIABLES/STATUS came back empty and
            # every metric is a zero-default. Generating (let alone applying)
            # recommendations from all-zero data is dangerous, so abort loudly.
            if not metrics.get("mysql_reachable"):
                raise ConnectionError(
                    "Could not collect MySQL/MariaDB metrics — the database "
                    "appears unreachable (empty SHOW VARIABLES/STATUS). Check "
                    "that the server is running and that credentials/socket are "
                    "correct. Aborting: recommendations will NOT be generated "
                    "from all-zero data."
                )

            # M5/C1: hand the real datadir to the system probe so storage class
            # and free space describe the filesystem MySQL actually uses.
            system_metrics = self.system_info.get_system_metrics(
                datadir=metrics.get("datadir") or None
            )
            metrics.update(system_metrics)

            # Metric contract audit (N-22): detect consumed keys not emitted by collector
            try:
                missing_metric_keys = audit_metrics(metrics)
                if missing_metric_keys:
                    self.logger.warning(
                        "Metric contract audit: missing required metric key(s): %s",
                        ", ".join(missing_metric_keys),
                    )
            except Exception as exc:
                self.logger.debug(f"Metric audit unavailable: {exc}")

            # 2. Detect MySQL/MariaDB version and apply compat layer
            db_version = metrics.get("mysql_version", "")
            # Build a version-aware VersionCompatibility instance. The redo-log
            # ceiling is handed in so the innodb_log_file_size ->
            # innodb_redo_log_capacity conversion (which multiplies by the log
            # files-in-group count) can never exceed the same guardrail the
            # SafetyChecker enforces on the pre-rename parameter (C1).
            max_log_gb = (
                self.config.get("fallback_logic", {})
                .get("safety_guardrails", {})
                .get("max_log_file_size_gb", 16)
            )
            self.version_compat = VersionCompatibility.from_version_string(
                db_version,
                max_redo_capacity_mb=int(max_log_gb) * 1024,
                datadir_free_mb=metrics.get("datadir_free_mb"),
            )
            compat_info = self.version_compat.version_info          # plain dict
            metrics["version_compat"] = compat_info

            if compat_info.get("version_unknown") or compat_info.get("branch") == "unknown":
                self.logger.warning(
                    "Database version could not be identified with certainty. %s",
                    self.version_compat.get_support_message(),
                )
            elif not self.version_compat.is_supported():
                self.logger.warning(self.version_compat.get_support_message())
            else:
                self.logger.info(
                    f"Detected: {'MariaDB' if compat_info.get('is_mariadb') else 'MySQL'} "
                    f"{compat_info.get('branch', 'unknown')}"
                )

            # 3. Platform detection / override
            # H1: set platform_override rather than platform_type — the engine
            # re-runs detect_platform() internally, which used to wipe a bare
            # platform_type assignment.
            self.decision_engine.platform_override = (
                platform if platform != "auto" else None
            )
            detected = self.decision_engine.detect_platform(metrics)
            if platform != "auto":
                self.logger.info(f"Platform override: {detected}")
            else:
                self.logger.info(f"Platform auto-detected: {detected}")

            # 4. Base recommendations (with safe division)
            base_recs = self._generate_base_recommendations(metrics)

            # 5. Ultimate decision engine enhancements
            ultimate_recs = self.decision_engine.generate_ultimate_recommendations(
                metrics, base_recs
            )

            # Refine the safety-checker's buffer-pool cap to the platform that
            # the decision engine settled on, so the per-platform guardrail
            # (e.g. 0.6 for DirectAdmin) applies as the final backstop.
            pt = self.decision_engine.platform_type
            mbp = (
                self.config.get("fallback_logic", {})
                .get("safety_guardrails", {})
                .get("max_buffer_pool_percent", {})
            )
            if isinstance(mbp, dict) and mbp:
                self.safety_checker.max_buffer_pool_percentage = mbp.get(
                    pt, mbp.get("default", self.safety_checker.max_buffer_pool_percentage)
                )

            # 6. Safety validation
            safe_recs = self.safety_checker.validate_recommendations(
                ultimate_recs, metrics, allow_shrink=self.allow_bp_shrink
            )

            # 7. Version-compatibility filtering — LAST, deliberately (C1).
            #
            # Every downstream guardrail matches on the *pre-rename* parameter
            # name (`innodb_log_file_size`, not `innodb_redo_log_capacity`), and
            # the decision engine appends recommendations of its own after the
            # base set is built. Filtering here means:
            #   * the engine's and SafetyChecker's caps always see the name they
            #     were written for, so they fire on MySQL 8.4 too;
            #   * recommendations ADDED by the engine (e.g. open_files_limit)
            #     are version-checked as well — previously they bypassed this
            #     step entirely;
            #   * the renamed/dropped result is what reaches my.cnf and the
            #     post-restart @@GLOBAL verification, which is what matters.
            pre_filter_count = len(safe_recs)
            safe_recs = self.version_compat.filter_recommendations(
                safe_recs, datadir_free_mb=metrics.get("datadir_free_mb")
            )
            if len(safe_recs) != pre_filter_count:
                self.logger.info(
                    "Version compatibility dropped %d recommendation(s) "
                    "unsupported on this server",
                    pre_filter_count - len(safe_recs),
                )

            # 8. Educational explanations (--explain)
            if explain:
                safe_recs = self.explain_engine.enrich_recommendations(
                    safe_recs, metrics
                )

            # 9. Build analysis results
            analysis_results = {
                "timestamp": datetime.now().isoformat(),
                "version": self.version,
                "platform_detected": self.decision_engine.platform_type,
                "db_version": compat_info,
                "migration_state": self.decision_engine.migration_state,
                "peak_hour_state": self.decision_engine.peak_hour_state,
                "detected_anomalies": self.decision_engine.detected_anomalies,
                "current_pass": self.decision_engine.current_pass,
                "file_limit_required": self.decision_engine.file_limit_required,
                "storage_type": metrics.get("system_storage_type", "unknown"),
                "memory_footprint": self._estimate_memory_footprint(metrics, safe_recs),
                "metrics": metrics,
                "recommendations": safe_recs,
                "evidence_base": self.config.get("evidence_base", {}),
                "safety_checks": {
                    "total_recommendations": len(ultimate_recs),
                    "safe_recommendations": len(safe_recs),
                    "filtered_count": len(ultimate_recs) - len(safe_recs),
                },
            }

            if show_evidence:
                analysis_results["evidence_details"] = (
                    self._generate_evidence_details(metrics, safe_recs)
                )

            self.logger.info(
                f"Analysis complete: {len(safe_recs)} recommendations generated"
            )
            return analysis_results

        except Exception as exc:
            self.logger.error(f"Analysis failed: {exc}", exc_info=True)
            raise

    # ------------------------------------------------------------------
    # File-limit check
    # ------------------------------------------------------------------
    def check_file_limits(self, platform: str = "auto") -> dict:
        """Check and recommend file limit adjustments."""
        self.logger.info("Checking file limit requirements ...")

        try:
            metrics = self.data_collector.collect_all_metrics()

            # --- MAJ-6 guard: refuse to proceed on an unreachable database ---
            # If we never connected, SHOW VARIABLES/STATUS came back empty and
            # every metric is a zero-default. Evaluating file limits from all-zero
            # data produces false-healthy "Adjustment required: False" reports.
            if not metrics.get("mysql_reachable"):
                raise ConnectionError(
                    "Could not collect MySQL/MariaDB metrics — the database "
                    "appears unreachable (empty SHOW VARIABLES/STATUS). Check "
                    "that the server is running and that credentials/socket are "
                    "correct. Aborting file limit check."
                )

            # --- C-2 guard: refuse to proceed on unmeasured table data ---
            # If table metrics could not be collected and no authoritative table count
            # is available (total_tables == 0), evaluating file limits produces false-healthy
            # reports. Refuse to report green when table counts cannot be determined.
            if metrics.get("table_stats_uncollected") and metrics.get("total_tables", 0) == 0:
                self.logger.warning(
                    "Table statistics uncollected and authoritative table count unavailable. "
                    "Cannot evaluate file limit requirements without table metrics."
                )
                raise RuntimeError(
                    "Could not collect table statistics and authoritative table count is unavailable. "
                    "Aborting file limit check: cannot evaluate file limits on unmeasured table data."
                )

            self.decision_engine.platform_override = (
                platform if platform != "auto" else None
            )
            self.decision_engine.detect_platform(metrics)

            file_limit_required = self.decision_engine.detect_file_limit_requirement(
                metrics
            )
            file_limit_rec = self.decision_engine.generate_file_limit_recommendation(
                metrics
            )

            results = {
                "timestamp": datetime.now().isoformat(),
                "platform": self.decision_engine.platform_type,
                "table_count": metrics.get("total_tables", 0),
                "file_limit_required": file_limit_required,
                "current_open_files_limit": metrics.get(
                    "open_files_limit", "unknown"
                ),
                "recommendation": file_limit_rec,
            }

            if file_limit_required and file_limit_rec:
                self.logger.info(
                    f"File limit adjustment required for "
                    f"{self.decision_engine.platform_type}"
                )
                self.logger.info(
                    f"Recommended limit: {file_limit_rec['recommended_value']}"
                )
                if file_limit_rec.get("systemd_edit_required"):
                    self.logger.info(
                        "Systemd service file modification required:"
                    )
                    for cmd in file_limit_rec.get("systemd_commands", []):
                        self.logger.info(f"  {cmd}")
            else:
                self.logger.info("No file limit adjustment required")

            return results

        except Exception as exc:
            self.logger.error(f"File limit check failed: {exc}", exc_info=True)
            raise

    # ------------------------------------------------------------------
    # Optimise (single pass)
    # ------------------------------------------------------------------
    def optimize_system(
        self,
        pass_number: int = 1,
        dry_run: bool = True,
        auto_apply: bool = False,
        explain: bool = False,
        dynamic_only: bool = False,
        platform: str = "auto",
    ) -> dict:
        """Optimise the MySQL system with ultimate logic."""
        if not dry_run and os.geteuid() != 0:
            raise PermissionError(
                "Root privileges required to apply changes. "
                "Run with sudo or use --dry-run for analysis only."
            )
        self.logger.info(f"Starting ultimate optimisation (Pass {pass_number}) ...")

        self.decision_engine.current_pass = pass_number
        # H1: --platform must reach the apply path too. This previously called
        # analyze_system() without it, so `--optimize --platform directadmin`
        # applied DEFAULT guardrails (0.8 RAM cap instead of 0.6, 1000
        # max_connections instead of 300) — the exact opposite of the caution
        # the flag implies.
        analysis_results = self.analyze_system(explain=explain, platform=platform)

        if dry_run:
            self.logger.info(
                "Dry-run mode: recommendations generated but not applied"
            )
            return {
                "mode": "dry_run",
                "pass_number": pass_number,
                "analysis": analysis_results,
                "recommendations_count": len(
                    analysis_results["recommendations"]
                ),
            }

        if auto_apply or self._confirm_application(
            analysis_results["recommendations"]
        ):
            app_results = self._apply_recommendations(
                analysis_results["recommendations"], dynamic_only=dynamic_only
            )
            self.decision_engine.save_optimization_history(
                analysis_results["metrics"],
                analysis_results["recommendations"],
            )
            return {
                "mode": "applied",
                "pass_number": pass_number,
                "analysis": analysis_results,
                "application": app_results,
            }

        self.logger.info("Optimisation cancelled by user")
        return {
            "mode": "cancelled",
            "pass_number": pass_number,
            "analysis": analysis_results,
        }

    # ------------------------------------------------------------------
    # Multi-pass optimisation
    # ------------------------------------------------------------------
    def multi_pass_optimization(
        self, max_passes: int = 3, dry_run: bool = True, explain: bool = False,
        auto_apply: bool = False, platform: str = "auto",
    ) -> dict:
        """Perform multi-pass optimisation with progressive enhancement."""
        self.logger.info(
            f"Starting multi-pass optimisation ({max_passes} passes) ..."
        )

        results = {"mode": "multi_pass", "max_passes": max_passes, "passes": []}

        # M13: when APPLYING, run only the final pass — one restart, not three.
        #
        # The passes differ solely by a buffer-pool multiplier applied to the
        # same base recommendation (0.7 / 0.85 / 0.95), all computed from the
        # same collected metrics. Applying them back-to-back therefore wrote
        # three configs in ~2 minutes where each was immediately superseded, and
        # restarted a production database three times to deliver only the last
        # one. It was actively counter-productive as well: every restart resets
        # the status counters, so passes 2 and 3 tripped the <24h uptime guard
        # and silently dropped their counter-driven recommendations.
        #
        # Genuine progressive tuning means observing between steps, which is
        # what `--optimize --pass-number N` across separate sessions is for.
        if not dry_run and max_passes > 1:
            self.logger.warning(
                "Multi-pass apply: running pass %d only. Passes 1-%d differ just "
                "by a buffer-pool multiplier on the same analysis, so applying "
                "them in sequence would restart the database %d times to deliver "
                "the pass-%d values anyway. To step up gradually with real "
                "observation in between, run '--optimize --pass-number 1' now "
                "and '--pass-number 2/3' in later sessions.",
                max_passes, max_passes, max_passes, max_passes,
            )
            pass_numbers = [max_passes]
        else:
            pass_numbers = list(range(1, max_passes + 1))

        for pass_num in pass_numbers:
            self.logger.info(f"=== OPTIMISATION PASS {pass_num} ===")
            pass_result = self.optimize_system(
                pass_number=pass_num,
                dry_run=dry_run,
                auto_apply=auto_apply,
                explain=explain,
                platform=platform,
            )
            results["passes"].append(pass_result)

            if not dry_run and pass_result["mode"] == "cancelled":
                self.logger.info(
                    f"Multi-pass optimisation stopped at pass {pass_num}"
                )
                break

        results["applied_pass"] = (
            pass_numbers[0] if (not dry_run and max_passes > 1) else None
        )
        self.logger.info("Multi-pass optimisation complete")
        return results

    # ------------------------------------------------------------------
    # Dump effective config
    # ------------------------------------------------------------------
    def dump_effective_config(
        self,
        platform_override: str = None,
        profile_override: str = None,
        output_format: str = "yaml",
    ) -> str:
        """Dump the effective configuration after applying all logic layers."""
        import copy

        effective = copy.deepcopy(self.config)

        if platform_override and platform_override != "auto":
            effective["platform_override"] = platform_override
            # H1: go through platform_override so the value survives any later
            # detect_platform() call, exactly as it does in the analysis path.
            self.decision_engine.platform_override = platform_override
            self.decision_engine.platform_type = platform_override

        # H2: the profile has already been applied to self.config by
        # apply_profile(), so the deepcopy above already carries the correct
        # multipliers. Only record which profile produced them.
        if profile_override:
            effective["profile_override"] = profile_override

        effective["runtime_info"] = {
            "detected_platform": self.decision_engine.platform_type,
            "platform_override": self.decision_engine.platform_override,
            "profile": self.profile,
            "config_file_used": self.config_file,
            "version": self.version,
            "timestamp": datetime.now().isoformat(),
        }

        pt = self.decision_engine.platform_type
        sg = effective.get("fallback_logic", {}).get("safety_guardrails", {})
        effective["effective_guardrails"] = {
            "max_buffer_pool_percent": sg.get("max_buffer_pool_percent", {}).get(pt, 0.8),
            "max_connections": sg.get("max_connections", {}).get(pt, 1000),
            "min_buffer_pool_mb": sg.get("min_buffer_pool_mb", 1024),
            "max_log_file_size_gb": sg.get("max_log_file_size_gb", 16),
        }

        fl_cfg = effective.get("file_limit_logic", {}).get(pt, {})
        if fl_cfg:
            effective["effective_file_limits"] = {
                "table_count_threshold": fl_cfg.get("table_count_threshold", 50000),
                "required_limit_nofile": fl_cfg.get("required_limit_nofile", 100000),
                "systemd_edit_required": fl_cfg.get("systemd_edit_required", False),
                "confidence": fl_cfg.get("confidence", 0.8),
            }

        # Redact credentials in config dump (Minor 8)
        if "mysql" in effective and isinstance(effective["mysql"], dict):
            if effective["mysql"].get("password"):
                effective["mysql"]["password"] = "******"
        if "password" in effective and effective["password"]:
            effective["password"] = "******"

        if output_format == "json":
            return json.dumps(effective, indent=2, sort_keys=False, default=str)
        try:
            return yaml.dump(effective, default_flow_style=False, sort_keys=False)
        except Exception:
            return json.dumps(effective, indent=2, sort_keys=False, default=str)

    # ------------------------------------------------------------------
    # Report generation (delegates to ReportGenerator)
    # ------------------------------------------------------------------
    def generate_report(
        self,
        analysis_results: dict,
        output_format: str = "text",
        explain: bool = False,
        top_databases=None,
    ) -> str:
        """Generate an optimisation report from analysis results."""
        metrics = analysis_results.get("metrics", {})
        recommendations = analysis_results.get("recommendations", [])
        analysis = {
            k: v
            for k, v in analysis_results.items()
            if k not in ("metrics", "recommendations")
        }
        return self.report_generator.generate_report(
            metrics=metrics,
            recommendations=recommendations,
            analysis=analysis,
            explain=explain,
            output_format=output_format,
            top_databases=top_databases,
        )

    # ------------------------------------------------------------------
    # Base recommendation generator (with safe division)
    # ------------------------------------------------------------------
    def _generate_base_recommendations(self, metrics: dict) -> list:
        """Generate base optimisation recommendations."""
        recommendations = []

        total_ram_mb = metrics.get("total_ram_mb", 0)
        current_bp = metrics.get("innodb_buffer_pool_size_mb", 0)
        innodb_data = metrics.get("innodb_data_size_mb", 0)

        # --- Uptime guard (improvement #3) ---
        # Status counters (Created_tmp_disk_tables, Threads_created, Select_full_join,
        # …) accumulate from server start, so their ratios are meaningless right
        # after a restart. Below 24h uptime we suppress the purely counter-driven
        # recommendations to avoid tuning on noise. uptime==0 means "unknown" —
        # don't suppress in that case.
        uptime_s = metrics.get("uptime", 0)
        uptime_ok = (uptime_s == 0) or (uptime_s >= 86400)
        if not uptime_ok:
            self.logger.warning(
                "Server uptime is only %.1fh (<24h). Counter-ratio-based "
                "recommendations (tmp tables, thread cache, join buffer) are "
                "suppressed until counters accumulate meaningfully.",
                uptime_s / 3600.0,
            )

        # --- InnoDB Buffer Pool Size ---
        if total_ram_mb > 0:
            if metrics.get("table_stats_uncollected"):
                rec_bp = int(total_ram_mb * 0.7)
            else:
                rec_bp = min(
                    int(total_ram_mb * 0.7),
                    max(int(innodb_data * 1.5), 1024),
                )
            if abs(rec_bp - current_bp) > 512:
                bp_rec = {
                    "parameter": "innodb_buffer_pool_size",
                    "current_value": f"{current_bp}M",
                    "recommended_value": f"{rec_bp}M",
                    "reason": (
                        f"Optimise for {total_ram_mb}MB RAM and "
                        f"{innodb_data}MB data size"
                    ),
                    "impact": "high",
                    "restart_required": True,
                }
                if metrics.get("table_stats_uncollected") or (innodb_data == 0 and metrics.get("total_tables", 0) > 0):
                    bp_rec["safety_note"] = (
                        "Table statistics could not be collected (e.g. scan timeout or empty stats); "
                        f"buffer pool sizing is based on system RAM baseline ({total_ram_mb}MB)."
                    )
                    bp_rec["reason"] = (
                        f"Optimise for {total_ram_mb}MB RAM (table statistics uncollected; "
                        "sizing based on system RAM baseline)"
                    )
                recommendations.append(bp_rec)

        # --- Max Connections ---
        max_used = metrics.get("max_used_connections", 0)
        cur_max = metrics.get("max_connections", 151)
        if cur_max > 0 and max_used >= cur_max * 0.8:
            rec_conn = max_used * 2
            pct = safe_div(max_used, cur_max) * 100
            recommendations.append(
                {
                    "parameter": "max_connections",
                    "current_value": cur_max,
                    "recommended_value": rec_conn,
                    "reason": (
                        f"Current usage {max_used} is {pct:.1f}% of limit"
                    ),
                    "impact": "medium",
                    "restart_required": False,
                }
            )

        # --- Table Definition Cache ---
        total_tables = metrics.get("total_tables", 0)
        cur_tdc = metrics.get("table_definition_cache", 400)
        if total_tables > cur_tdc:
            rec_tdc = int(total_tables * 1.1)
            recommendations.append(
                {
                    "parameter": "table_definition_cache",
                    "current_value": cur_tdc,
                    "recommended_value": rec_tdc,
                    "reason": (
                        f"Current cache {cur_tdc} is less than "
                        f"table count {total_tables}"
                    ),
                    "impact": "medium",
                    "restart_required": False,
                }
            )

        # --- Temporary Table Size (SAFE DIVISION) ---
        tmp_disk = metrics.get("created_tmp_disk_tables", 0)
        tmp_total = metrics.get("created_tmp_tables", 0)
        tmp_disk_ratio = safe_div(tmp_disk, tmp_total, 0.0)

        if uptime_ok and tmp_disk_ratio > 0.25:
            cur_tmp = metrics.get("tmp_table_size_mb", 16)
            rec_tmp = min(cur_tmp * 2, 512)
            # H3: only recommend when it raises the value — never downgrade a
            # server that already has a larger tmp_table_size configured.
            if rec_tmp > cur_tmp:
                recommendations.append(
                    {
                        "parameter": "tmp_table_size",
                        "current_value": f"{cur_tmp}M",
                        "recommended_value": f"{rec_tmp}M",
                        "reason": (
                            f"{tmp_disk_ratio * 100:.1f}% of temporary tables "
                            f"created on disk"
                        ),
                        "impact": "medium",
                        "restart_required": False,
                    }
                )

        # --- Join Buffer Size ---
        full_join = metrics.get("select_full_join", 0)
        if uptime_ok and full_join > 1000:
            cur_jb = metrics.get("join_buffer_size_kb", 256)
            rec_jb = min(cur_jb * 4, 8192)
            # H3: never downgrade an already-larger join_buffer_size.
            if rec_jb > cur_jb:
                recommendations.append(
                    {
                        "parameter": "join_buffer_size",
                        "current_value": f"{cur_jb}K",
                        "recommended_value": f"{rec_jb}K",
                        "reason": f"{full_join} joins without indexes detected",
                        "impact": "low",
                        "restart_required": False,
                    }
                )

        # --- InnoDB Log File Size ---
        if current_bp > 0:
            cur_log = metrics.get("innodb_log_file_size_mb", 48)
            rec_log = max(int(current_bp * 0.25), 256)
            if abs(rec_log - cur_log) > 128:
                recommendations.append(
                    {
                        "parameter": "innodb_log_file_size",
                        "current_value": f"{cur_log}M",
                        "recommended_value": f"{rec_log}M",
                        "reason": (
                            f"Optimise for {current_bp}MB buffer pool "
                            f"(25% ratio)"
                        ),
                        "impact": "medium",
                        "restart_required": True,
                    }
                )

        # --- Key Buffer Size (MyISAM) ---
        # M4: sized from the MyISAM INDEX size, not the data size. key_buffer_size
        # caches MyISAM index blocks only — row data is served from the OS page
        # cache — so data_size was the one number this buffer never holds.
        # Target ~1.2x the index size so the whole index fits with headroom,
        # which is the conventional MyISAM sizing rule.
        myisam_data = metrics.get("myisam_data_size_mb", 0)
        myisam_index = metrics.get("myisam_index_size_mb", 0)
        if myisam_data > 0 or myisam_index > 0:
            cur_kb = metrics.get("key_buffer_size_mb", 16)
            rec_kb = max(int(myisam_index * 1.2), 16)
            if abs(rec_kb - cur_kb) > 8:
                recommendations.append(
                    {
                        "parameter": "key_buffer_size",
                        "current_value": f"{cur_kb}M",
                        "recommended_value": f"{rec_kb}M",
                        "reason": (
                            f"Size to MyISAM index data ({myisam_index}MB of "
                            f"indexes across {myisam_data}MB of MyISAM tables)"
                        ),
                        "impact": "low",
                        "restart_required": False,
                    }
                )

        # --- Thread Cache Size ---
        threads_created = metrics.get("threads_created", 0)
        connections = metrics.get("connections", 0)
        thread_cache_miss = safe_div(threads_created, connections, 0.0)
        if uptime_ok and thread_cache_miss > 0.01:
            cur_tc = metrics.get("thread_cache_size", 8)
            rec_tc = min(max(cur_tc * 2, 16), 256)
            # H3: never downgrade an already-larger thread_cache_size.
            if rec_tc > cur_tc:
                recommendations.append(
                    {
                        "parameter": "thread_cache_size",
                        "current_value": cur_tc,
                        "recommended_value": rec_tc,
                        "reason": (
                            f"Thread cache miss ratio {thread_cache_miss:.4f} "
                            f"exceeds 1% threshold"
                        ),
                        "impact": "low",
                        "restart_required": False,
                    }
                )

        # --- InnoDB Buffer Pool Instances ---
        if current_bp >= 2048:
            cur_inst = metrics.get("innodb_buffer_pool_instances", 1)
            rec_inst = min(max(current_bp // 1024, 2), 16)
            if rec_inst != cur_inst:
                recommendations.append(
                    {
                        "parameter": "innodb_buffer_pool_instances",
                        "current_value": cur_inst,
                        "recommended_value": rec_inst,
                        "reason": (
                            f"Buffer pool {current_bp}MB benefits from "
                            f"{rec_inst} instances for concurrency"
                        ),
                        "impact": "medium",
                        "restart_required": True,
                    }
                )

        # --- Storage-aware I/O tuning (improvement #7) ---
        storage = metrics.get("system_storage_type", "unknown")
        if storage in ("ssd", "nvme"):
            target_io = 4000 if storage == "nvme" else 2000
            cur_io = metrics.get("innodb_io_capacity", 200)
            if cur_io < target_io:
                recommendations.append(
                    {
                        "parameter": "innodb_io_capacity",
                        "current_value": cur_io,
                        "recommended_value": target_io,
                        "reason": (
                            f"{storage.upper()} storage detected; default "
                            f"io_capacity ({cur_io}) underuses fast flash"
                        ),
                        "impact": "medium",
                        "restart_required": False,
                    }
                )
                cur_io_max = metrics.get("innodb_io_capacity_max", 2000)
                target_io_max = target_io * 2
                if cur_io_max < target_io_max:
                    recommendations.append(
                        {
                            "parameter": "innodb_io_capacity_max",
                            "current_value": cur_io_max,
                            "recommended_value": target_io_max,
                            "reason": f"{storage.upper()} storage burst headroom",
                            "impact": "low",
                            "restart_required": False,
                        }
                    )
            # On flash, neighbour-flushing wastes I/O — recommend disabling it.
            if metrics.get("innodb_flush_neighbors", 1) != 0:
                recommendations.append(
                    {
                        "parameter": "innodb_flush_neighbors",
                        "current_value": metrics.get("innodb_flush_neighbors", 1),
                        "recommended_value": 0,
                        "reason": (
                            f"{storage.upper()} storage has no seek penalty; "
                            f"neighbour flushing only adds write amplification"
                        ),
                        "impact": "low",
                        "restart_required": False,
                    }
                )

        return recommendations

    # ------------------------------------------------------------------
    # Evidence details
    # ------------------------------------------------------------------
    def _generate_evidence_details(self, metrics: dict, recs: list) -> dict:
        return {
            "platform_detection": {
                "detected_platform": self.decision_engine.platform_type,
                "detection_method": (
                    "signature_based"
                    if self.decision_engine.platform_type != "default"
                    else "heuristic"
                ),
                "confidence": (
                    0.9
                    if self.decision_engine.platform_type != "default"
                    else 0.6
                ),
            },
            "file_limit_analysis": {
                "file_limit_required": self.decision_engine.file_limit_required,
                "table_count": metrics.get("total_tables", 0),
                "platform_threshold": self.config.get(
                    "file_limit_logic", {}
                )
                .get(self.decision_engine.platform_type, {})
                .get("table_count_threshold", "N/A"),
            },
            "migration_analysis": {
                "migration_state": self.decision_engine.migration_state,
                "myisam_tables": metrics.get("myisam_table_count", 0),
                "innodb_tables": metrics.get("innodb_table_count", 0),
                "migration_indicators": [],
            },
            "anomaly_detection": {
                "detected_anomalies": self.decision_engine.detected_anomalies,
                "anomaly_details": self._get_anomaly_details(metrics),
            },
            "version_compatibility": metrics.get("version_compat", {}),
            "confidence_factors": {
                "base_confidence": self.config.get(
                    "confidence_engine", {}
                ).get("base_confidence", 0.7),
                "evidence_modifiers": self._calc_evidence_modifiers(metrics),
                "risk_factors": self._calc_risk_factors(metrics),
            },
            "evidence_sources": {
                "total_production_cases": self.config.get(
                    "evidence_base", {}
                ).get("total_cases", 66),
                "platform_cases": self.config.get(
                    "evidence_base", {}
                ).get("platform_distribution", {}),
                "scenario_coverage": self.config.get(
                    "evidence_base", {}
                ).get("scenario_coverage", {}),
            },
        }

    # ------------------------------------------------------------------
    # Anomaly details (with safe division)
    # ------------------------------------------------------------------
    def _get_anomaly_details(self, metrics: dict) -> dict:
        details = {}
        anomalies = self.decision_engine.detected_anomalies or []

        if "aborted_connections" in anomalies:
            aborted = metrics.get("aborted_connects", 0)
            total = metrics.get("total_connects", 0)
            ratio = safe_div(aborted, total, 0.0)
            details["aborted_connections"] = {
                "ratio": ratio,
                "threshold": 0.03,
                "severity": "high" if ratio > 0.05 else "medium",
            }

        if "memory_pressure" in anomalies:
            details["memory_pressure"] = {
                "swap_usage_gb": metrics.get("swap_usage_gb", 0),
                "available_memory_percent": metrics.get(
                    "available_memory_percent", 100
                ),
                "severity": (
                    "critical"
                    if metrics.get("swap_usage_gb", 0) > 20
                    else "high"
                ),
            }

        if "xmlrpc_overload" in anomalies:
            details["xmlrpc_overload"] = {
                "tmp_disk_tables": metrics.get("created_tmp_disk_tables", 0),
                "select_full_join": metrics.get("select_full_join", 0),
                "severity": "high",
            }

        return details

    # ------------------------------------------------------------------
    # Confidence helpers
    # ------------------------------------------------------------------
    def _calc_evidence_modifiers(self, metrics: dict) -> dict:
        mods = {}
        if self.decision_engine.current_pass > 1:
            mods["multi_pass"] = 0.2
        if self.decision_engine.migration_state == "post_migration":
            mods["post_migration"] = 0.15
        if self.decision_engine.peak_hour_state:
            mods["peak_hour"] = 0.1
        if self.decision_engine.file_limit_required:
            mods["file_limit_optimization"] = 0.1
        if metrics.get("total_ram_mb", 0) / 1024 > 32:
            mods["high_ram_system"] = 0.1
        return mods

    def _calc_risk_factors(self, metrics: dict) -> dict:
        """Report the risk penalties that were actually applied.

        M1: this used to hardcode its own penalty values under the OLD key names
        ('connection_overflow', 'high_aborted_connections'), so --show-evidence
        displayed a penalty the confidence engine had never applied. It now
        reads the same config the engine reads, keyed by the same canonical
        anomaly names, so the report cannot disagree with the calculation.
        """
        penalties = (
            self.config.get("confidence_engine", {}).get("risk_penalties", {}) or {}
        )
        return {
            anomaly: penalties.get(anomaly, 0)
            for anomaly in (self.decision_engine.detected_anomalies or [])
        }

    # ------------------------------------------------------------------
    # Application helpers
    # ------------------------------------------------------------------
    def _confirm_application(self, recommendations: list) -> bool:
        print(f"\n{'=' * 60}")
        print(f"  ULTIMATE OPTIMISATION RECOMMENDATIONS")
        print(f"{'=' * 60}")
        print(f"  Total recommendations : {len(recommendations)}")
        print(f"  Platform              : {self.decision_engine.platform_type}")
        print(f"  Migration state       : {self.decision_engine.migration_state}")
        print(f"  File limit required   : {self.decision_engine.file_limit_required}")
        anomalies = self.decision_engine.detected_anomalies
        print(
            f"  Anomalies             : "
            f"{', '.join(anomalies) if anomalies else 'None'}"
        )
        print(f"{'=' * 60}\n")

        for i, rec in enumerate(recommendations, 1):
            print(
                f"  {i}. {rec['parameter']}: "
                f"{rec['current_value']} -> {rec['recommended_value']}"
            )
            print(f"     Reason     : {rec['reason']}")
            conf = rec.get("confidence", 0)
            if isinstance(conf, (int, float)):
                print(f"     Confidence : {conf:.2f}")
            print(f"     Restart    : {rec.get('restart_required', False)}")
            if rec.get("explanation"):
                print(f"     Explain    : {rec['explanation'][:120]}...")
            if rec.get("systemd_edit_required"):
                print(f"     Systemd edit required:")
                for cmd in rec.get("systemd_commands", []):
                    print(f"       $ {cmd}")
            print()

        # L3: when stdin is not a TTY (e.g. run from cron), input() raises
        # EOFError. Treat the absence of an interactive "yes" as a safe "No"
        # rather than crashing with a generic error. Use --yes to apply
        # non-interactively.
        try:
            response = input("Apply these recommendations? [y/N]: ").strip().lower()
        except EOFError:
            self.logger.warning(
                "No interactive input available (non-TTY stdin); treating as "
                "'No'. Use --yes to apply without a prompt."
            )
            return False
        return response in ("y", "yes")

    # ------------------------------------------------------------------
    # my.cnf location helper
    # ------------------------------------------------------------------
    @staticmethod
    def _find_mycnf() -> str:
        """Return the real path to the active my.cnf file.

        M10: the candidate list now lives in ``utils/mycnf_paths`` so the
        pre-flight permission and disk-space checks resolve the SAME file this
        will write. They previously kept their own shorter copies and could pick
        a different one on Debian/Ubuntu.
        """
        from utils.mycnf_paths import find_mycnf

        return find_mycnf()

    # ------------------------------------------------------------------
    # MySQL/MariaDB service restart
    # ------------------------------------------------------------------
    def _detect_mysql_service(self, require_active: bool = True) -> str:
        """Return the name of the MySQL/MariaDB systemd unit.

        With ``require_active=True`` (default) only a unit reporting ``active``
        is returned — used to identify the running service *before* a restart.

        With ``require_active=False`` a unit that merely EXISTS is accepted even
        when it is currently ``failed``/``inactive``/``activating``. This is
        essential for the rollback path: a service that has just crashed on a
        bad config reports ``failed``, not ``active``, yet we still need its
        name in order to restart it.
        """
        import subprocess

        candidates = ("mariadb", "mysql", "mysqld")

        # Pass 1: prefer a unit that is actually running.
        for svc in candidates:
            try:
                result = subprocess.run(
                    ["systemctl", "is-active", svc],
                    capture_output=True, text=True, timeout=10,
                )
                if result.stdout.strip() == "active":
                    return svc
            except Exception:
                continue

        if require_active:
            return ""

        # Pass 2 (rollback): accept any INSTALLED unit, whatever its state.
        for svc in candidates:
            try:
                result = subprocess.run(
                    ["systemctl", "is-active", svc],
                    capture_output=True, text=True, timeout=10,
                )
                state = result.stdout.strip()
            except Exception:
                continue
            # 'failed'/'activating'/etc. only occur for units that exist.
            if state in ("failed", "activating", "deactivating", "reloading"):
                return svc
            # 'inactive'/'unknown' is ambiguous (missing units report it too),
            # so confirm the unit is installed before trusting it.
            if state in ("inactive", "unknown") and self._unit_exists(svc):
                return svc

        return ""

    @staticmethod
    def _unit_exists(svc: str) -> bool:
        """Return True if a systemd unit file is installed (in any state)."""
        import subprocess

        try:
            result = subprocess.run(
                ["systemctl", "list-unit-files", f"{svc}.service"],
                capture_output=True, text=True, timeout=10,
            )
            return f"{svc}.service" in result.stdout
        except Exception:
            return False

    def _restart_mysql_service(self, service: str = None) -> bool:
        """Restart the MySQL/MariaDB service via systemctl with a health check.

        Args:
            service: explicit unit name to restart. When ``None`` the unit is
                auto-detected. During rollback the caller MUST pass the name
                captured while the server was still healthy, because a crashed
                unit no longer reports ``active`` (see ``_detect_mysql_service``).

        Returns True only when the service is confirmed responding to
        health checks (via MySQLConnector, mysqladmin, or systemctl status) after the restart.
        """
        import shutil
        import subprocess
        import time

        svc = service or self._detect_mysql_service(require_active=False)
        if not svc:
            self.logger.error(
                "No MySQL/MariaDB service found — cannot restart"
            )
            return False

        self.logger.info(f"Restarting service: {svc}")
        try:
            result = subprocess.run(
                ["systemctl", "restart", svc],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0:
                self.logger.error(
                    f"systemctl restart {svc} failed: {result.stderr.strip()}"
                )
                return False
        except subprocess.TimeoutExpired:
            # A slow InnoDB startup (large buffer pool / redo-log resize) is not
            # itself a failure — the systemd job continues after the client is
            # killed. Fall through to the health check rather than declaring
            # defeat and triggering an unnecessary rollback.
            self.logger.warning(
                f"systemctl restart {svc} exceeded 120s; verifying via health check"
            )
        except Exception as exc:
            self.logger.error(f"Restart command failed: {exc}")
            return False

        # Post-restart health check — the authoritative success signal.
        # Check through MySQLConnector first, then parameter-aware mysqladmin,
        # with dynamic timeout scaling and unified systemd status.
        health_timeout = 45.0
        try:
            ram_mb = 0
            if getattr(self, "system_info", None):
                mem = self.system_info.get_memory_info()
                ram_mb = mem.get("total_mb", 0) if isinstance(mem, dict) else 0
            if ram_mb >= 65536:  # >= 64GB RAM
                health_timeout = 90.0
            elif ram_mb >= 32768:  # >= 32GB RAM
                health_timeout = 60.0
        except Exception:
            pass

        # Staged backoff delays (1s, 2s, 3s, 5s, 5s...)
        backoff_delays = [1, 2, 3, 5]
        attempt = 0
        total_waited = 0.0
        start_time = time.time()

        mysqladmin_bin = shutil.which("mysqladmin")
        mysqladmin_warned = False
        svc_active_state = None

        while True:
            attempt += 1

            # 1. Primary: Verify via active MySQLConnector
            if getattr(self, "mysql_connector", None):
                try:
                    self.mysql_connector.disconnect()
                    # M-10 (b): Use short per-attempt timeout (5s) for health check probe so supervision deadline controls
                    if self.mysql_connector.connect(connect_timeout=5, read_timeout=5):
                        res = self.mysql_connector.execute_query("SELECT 1", retry=False)
                        if res is not None:
                            self.logger.info(
                                f"Health check passed (attempt {attempt}, {total_waited:.0f}s elapsed)"
                            )
                            return True
                except Exception as exc:
                    self.logger.debug(
                        f"Connector health check attempt {attempt} failed: {exc}"
                    )

            # 2. Secondary fallback: Parameter-aware mysqladmin ping
            if mysqladmin_bin:
                try:
                    cmd = [mysqladmin_bin]
                    conn = getattr(self, "mysql_connector", None)
                    if conn:
                        if getattr(conn, "_user_defaults_file", None):
                            cmd.extend([f"--defaults-file={conn._user_defaults_file}"])
                        if conn.use_socket and conn.socket:
                            cmd.extend(["--socket", str(conn.socket)])
                        elif conn.host:
                            cmd.extend(["--host", str(conn.host), "--port", str(conn.port)])
                        if conn.user:
                            cmd.extend(["--user", str(conn.user)])
                    cmd.append("ping")

                    env = dict(os.environ)
                    if conn and conn.password:
                        env["MYSQL_PWD"] = str(conn.password)

                    ping = subprocess.run(
                        cmd,
                        env=env,
                        capture_output=True, text=True, timeout=10,
                    )
                    if ping.returncode == 0:
                        self.logger.info(
                            f"Health check passed (attempt {attempt}, {total_waited:.0f}s elapsed)"
                        )
                        return True
                except Exception as exc:
                    self.logger.debug(
                        f"mysqladmin health check attempt {attempt} failed: {exc}"
                    )
            elif not mysqladmin_warned:
                self.logger.warning(
                    "mysqladmin binary not found; using connection and systemd status for health check"
                )
                mysqladmin_warned = True

            # 3. Active grace-period signal: query systemctl is-active
            # If systemd reports "activating" or "active", MySQL is initializing or listening;
            # continue polling rather than aborting prematurely.
            try:
                status = subprocess.run(
                    ["systemctl", "is-active", svc],
                    capture_output=True, text=True, timeout=10,
                )
                svc_active_state = status.stdout.strip()
            except Exception:
                svc_active_state = None

            # Determine sleep duration from staged backoff
            sleep_idx = min(attempt - 1, len(backoff_delays) - 1)
            delay = backoff_delays[sleep_idx]

            # Check if timeout window has been exceeded
            if (time.time() - start_time >= health_timeout) or (total_waited + delay > health_timeout):
                break

            if svc_active_state in ("activating", "active"):
                self.logger.debug(
                    f"Service {svc} is '{svc_active_state}'; continuing health check polling..."
                )

            total_waited += delay
            time.sleep(delay)

        # Universal fallback: if connector/mysqladmin were inconclusive or couldn't authenticate,
        # but systemctl confirms the unit is active, stand down rather than triggering a false rollback.
        if svc_active_state != "active":
            try:
                status = subprocess.run(
                    ["systemctl", "is-active", svc],
                    capture_output=True, text=True, timeout=10,
                )
                svc_active_state = status.stdout.strip()
            except Exception:
                pass

        if svc_active_state == "active":
            self.logger.warning(
                f"Health check inconclusive via connector/CLI, but systemctl reports {svc} is active."
            )
            return True

        self.logger.error(
            f"Post-restart health check failed after {attempt} attempts ({total_waited:.0f}s elapsed)"
        )
        return False

    # ------------------------------------------------------------------
    # Pre-restart config validation
    # ------------------------------------------------------------------
    def _validate_config_file(self, candidate_path):
        """Validate a candidate my.cnf with the real server binary before it goes live.

        Runs the actual installed ``mariadbd``/``mysqld`` against the candidate
        file so the check reflects the true server version regardless of what
        our own version detection concluded. This catches removed/unknown
        variables (e.g. ``innodb_buffer_pool_instances`` on MariaDB 10.6+) that
        would otherwise abort startup and take the database down.

        Returns a ``(is_valid, detail, validation_ran)`` tuple:
            * is_valid=False       -> the binary reported a config error; do NOT apply.
            * validation_ran=False -> no binary available; result is inconclusive.
        """
        import shutil as _shutil
        import subprocess

        binary = None
        for cand in ("mariadbd", "mysqld"):
            found = _shutil.which(cand)
            if found:
                binary = found
                break
        if binary is None:
            for cand in ("/usr/sbin/mariadbd", "/usr/sbin/mysqld"):
                if os.path.exists(cand):
                    binary = cand
                    break
        if binary is None:
            return True, "no mariadbd/mysqld binary available to validate with", False

        name = os.path.basename(binary)

        # Attempt 1: --validate-config (MySQL 8.0.16+, most thorough).
        try:
            res = subprocess.run(
                [binary, f"--defaults-file={candidate_path}", "--validate-config"],
                capture_output=True, text=True, timeout=30,
            )
            out = (res.stdout + res.stderr).strip()
            if res.returncode == 0:
                return True, f"{name} --validate-config: OK", True
            low = out.lower()
            # Only treat as a config failure if the error is about the config
            # itself — not the flag being unsupported (MariaDB lacks it).
            if ("unknown variable" in low or "error" in low) and "--validate-config" not in low:
                return False, out[:400], True
        except Exception as exc:
            self.logger.debug(f"--validate-config attempt failed: {exc}")

        # Attempt 2: --help --verbose (portable; errors on unknown variables).
        try:
            res = subprocess.run(
                [binary, f"--defaults-file={candidate_path}", "--help", "--verbose"],
                capture_output=True, text=True, timeout=30,
            )
            out = (res.stdout + res.stderr).strip()
            if res.returncode != 0:
                err_line = next(
                    (l.strip() for l in out.splitlines()
                     if "unknown variable" in l.lower() or "[ERROR]" in l
                     or "error" in l.lower()),
                    out[:200],
                )
                return False, err_line, True
            return True, f"{name} --help --verbose: OK", True
        except Exception as exc:
            self.logger.debug(f"--help --verbose validation failed: {exc}")

        return True, "could not execute a config validator", False

    # ------------------------------------------------------------------
    # Concurrency lock (M5) — prevent overlapping cron runs
    # ------------------------------------------------------------------
    def _acquire_apply_lock(self):
        """Take an exclusive, non-blocking lock so two applies never interleave
        my.cnf writes and restarts. Held for the life of the process. No-op on
        platforms without ``fcntl`` (Windows dev/test only)."""
        try:
            import fcntl
        except ImportError:
            return

        from utils.safe_io import choose_writable_dir, secure_dir, temp_fallback_dir
        lock_dir = (
            choose_writable_dir("/run/mysql-autotuner", "mysql-autotuner-lock", require_owner=True)
            or choose_writable_dir("/var/lock", "mysql-autotuner-lock", require_owner=False)
            or secure_dir(temp_fallback_dir("mysql-autotuner-lock"), require_owner=True)
        )
        if not lock_dir:
            raise RuntimeError(
                "Could not find or create a secure directory for apply lock (must be owned by current user and non-world-writable)."
            )

        # Verify parent directory is safe and owned by current process EUID for non-system paths
        if hasattr(os, "geteuid"):
            st = os.stat(lock_dir)
            if st.st_uid != os.geteuid() and os.geteuid() != 0:
                raise RuntimeError(
                    f"Lock directory {lock_dir} is not owned by current EUID ({os.geteuid()})."
                )

        lock_path = os.path.join(lock_dir, "mysql-autotuner.lock")

        flags = os.O_CREAT | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(lock_path, flags, 0o640)
        except OSError as exc:
            raise RuntimeError(
                f"Failed to securely open apply lock file at {lock_path}: {exc}"
            )

        try:
            if hasattr(os, "fstat") and hasattr(os, "geteuid"):
                st = os.fstat(fd)
                if st.st_uid != os.geteuid() and os.geteuid() != 0:
                    os.close(fd)
                    raise RuntimeError(
                        f"Lock file {lock_path} is owned by UID {st.st_uid}, expected EUID {os.geteuid()}."
                    )
            fh = os.fdopen(fd, "w")
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            raise

        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            raise RuntimeError(
                "Another mysql-autotuner apply is already running (lock held at "
                f"{lock_path}). Refusing to run concurrently to avoid interleaved "
                "my.cnf writes and restarts."
            )
        # Keep a reference so the lock is held until the process exits.
        self._apply_lock_fh = fh

    # ------------------------------------------------------------------
    # Cluster / replication awareness (M6)
    # ------------------------------------------------------------------
    def _detect_cluster_role(self):
        """Best-effort detection of Galera / replication membership. Returns a
        human-readable reason string when a restart would affect more than this
        single node, else ``None``."""
        conn = self.mysql_connector

        # Galera / wsrep
        try:
            wsrep = conn.execute_query("SHOW VARIABLES LIKE 'wsrep_on'")
            if wsrep and str(wsrep[0].get("Value", "")).upper() == "ON":
                size = conn.execute_query("SHOW STATUS LIKE 'wsrep_cluster_size'")
                n = size[0].get("Value", "?") if size else "?"
                return f"Galera cluster node (wsrep_on=ON, cluster size {n})"
        except Exception as exc:
            self.logger.debug(f"wsrep check failed: {exc}")

        # This node is a replica
        for q in ("SHOW REPLICA STATUS", "SHOW SLAVE STATUS"):
            try:
                rows = conn.execute_query(q)
                if rows:
                    return "a replication replica (SHOW REPLICA/SLAVE STATUS returned a row)"
            except Exception:
                continue

        # This node is a primary with connected replicas
        for q in ("SHOW REPLICAS", "SHOW SLAVE HOSTS"):
            try:
                rows = conn.execute_query(q)
                if rows:
                    return "a replication primary (has connected replicas)"
            except Exception:
                continue

        return None

    # ------------------------------------------------------------------
    # my.cnf line rewriting (shared by apply and --diff)
    # ------------------------------------------------------------------
    @staticmethod
    def _build_mycnf_lines(original_lines: list, params_to_apply: dict) -> list:
        """Return the new my.cnf lines with *params_to_apply* set in [mysqld].

        Existing keys in [mysqld] are replaced in place; missing keys are
        appended to the section; if there is no [mysqld] section one is added.
        Pure function — no I/O — so it is reused by the --diff preview.
        """
        import re as _re

        param_patterns = {
            param: _re.compile(
                r"^\s*" + _re.escape(param).replace("_", "[-_]") + r"\s*=",
                _re.IGNORECASE,
            )
            for param in params_to_apply
        }

        new_lines = []
        in_mysqld = False
        written_params = set()

        for line in original_lines:
            stripped = line.strip()

            if stripped.startswith("["):
                if in_mysqld:
                    for p, v in params_to_apply.items():
                        if p not in written_params:
                            new_lines.append(f"{p} = {v}\n")
                            written_params.add(p)
                clean_sec = stripped.split("#", 1)[0].split(";", 1)[0].strip().lower()
                in_mysqld = clean_sec == "[mysqld]"

            if in_mysqld:
                matched = False
                for p, pat in param_patterns.items():
                    if pat.match(stripped):
                        if p not in written_params:
                            # M11: carry any trailing comment across. Operator
                            # annotations are often the only record of WHY a
                            # value was chosen ("# tuned by hand after the 2024
                            # incident"); replacing the whole line silently
                            # destroyed them.
                            comment = ""
                            hash_pos = line.find("#")
                            if hash_pos != -1:
                                comment = "  " + line[hash_pos:].rstrip("\n")
                            new_lines.append(
                                f"{p} = {params_to_apply[p]}{comment}\n"
                            )
                            written_params.add(p)
                        matched = True
                        break
                if matched:
                    continue

            new_lines.append(line)

        if in_mysqld:
            for p, v in params_to_apply.items():
                if p not in written_params:
                    new_lines.append(f"{p} = {v}\n")
                    written_params.add(p)

        if not any(p in written_params for p in params_to_apply):
            new_lines.append("\n[mysqld]\n")
            for p, v in params_to_apply.items():
                new_lines.append(f"{p} = {v}\n")

        return new_lines

    # ------------------------------------------------------------------
    # Shadowing-section detection (H6)
    # ------------------------------------------------------------------
    # Groups the server reads IN ADDITION to [mysqld]. Because later
    # definitions win, any of these appearing after [mysqld] and setting one of
    # our parameters silently overrides the value we just wrote.
    _SERVER_GROUP_RE = r"^\[(mysqld|mariadb|server|mysqld-[\d.]+|mariadb-[\d.]+|galera)\]$"

    @classmethod
    def _find_shadowing_sections(cls, lines: list, params) -> list:
        """Return ['param in [section]', ...] for parameters redefined in a
        server-read section that comes AFTER the [mysqld] we write to.

        The post-restart verification already DETECTS a mismatch; this is what
        turns "another config source is winning" into an actionable pointer. The
        hint previously named only `!includedir` directories, so the most common
        case — a duplicate in `[mariadb]` four lines further down the same file
        — was reported with no clue where to look.
        """
        import re as _re

        group_re = _re.compile(cls._SERVER_GROUP_RE, _re.IGNORECASE)
        param_res = {
            p: _re.compile(
                r"^\s*" + _re.escape(p).replace("_", "[-_]") + r"\s*=",
                _re.IGNORECASE,
            )
            for p in params
        }

        findings = []
        current = None
        seen_mysqld = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("["):
                clean_sec = stripped.split("#", 1)[0].split(";", 1)[0].strip().lower()
                current = clean_sec
                if current == "[mysqld]":
                    seen_mysqld = True
                continue
            # Only sections the server actually reads, and only after [mysqld].
            if not seen_mysqld or not current or not group_re.match(current):
                continue
            if current == "[mysqld]":
                continue
            for p, pat in param_res.items():
                if pat.match(stripped):
                    entry = f"{p} in {current}"
                    if entry not in findings:
                        findings.append(entry)
        return findings

    # ------------------------------------------------------------------
    # --diff preview (improvement #2)
    # ------------------------------------------------------------------
    def generate_mycnf_diff(self, recommendations: list) -> str:
        """Return a unified diff of the current my.cnf vs the proposed one.

        Read-only (no root needed, nothing written) — safe to run any time.
        """
        import difflib

        params_to_apply = {
            r["parameter"]: str(r["recommended_value"])
            for r in recommendations
            if r.get("parameter") is not None
        }
        if not params_to_apply:
            return "No changes proposed — my.cnf would be left unchanged."

        try:
            mycnf_path = self._find_mycnf()
            with open(mycnf_path, "r", encoding="utf-8", errors="surrogateescape") as fh:
                original_lines = fh.readlines()
        except FileNotFoundError as exc:
            return f"Cannot produce diff: {exc}"

        new_lines = self._build_mycnf_lines(original_lines, params_to_apply)
        diff = difflib.unified_diff(
            original_lines, new_lines,
            fromfile=f"{mycnf_path} (current)",
            tofile=f"{mycnf_path} (proposed)",
        )
        text = "".join(diff)
        return text or "No textual changes (values already present as written)."

    # ------------------------------------------------------------------
    # Post-restart verification (improvement #1)
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_to_bytes(value):
        """Parse '6000M' / '2G' / '262144' / '2000' to a comparable number.
        Suffixed sizes become bytes; bare counts stay as-is. None on failure."""
        try:
            s = str(value).strip().upper()
            if s.endswith("G"):
                return int(float(s[:-1]) * 1024 ** 3)
            if s.endswith("M"):
                return int(float(s[:-1]) * 1024 ** 2)
            if s.endswith("K"):
                return int(float(s[:-1]) * 1024)
            return int(float(s))
        except (ValueError, TypeError):
            return None

    def _compare_values(self, intended, effective) -> str:
        """Return 'applied' | 'adjusted' | 'mismatch' comparing intent vs live."""
        iv = self._parse_to_bytes(intended)
        ev = self._parse_to_bytes(effective)
        if iv is None or ev is None:
            return ("applied"
                    if str(intended).strip().lower() == str(effective).strip().lower()
                    else "mismatch")
        if iv == ev:
            return "applied"
        # Tolerance absorbs InnoDB chunk rounding / clamping (~2%).
        if abs(iv - ev) <= max(abs(iv) * 0.02, 1):
            return "adjusted"
        return "mismatch"

    def _verify_applied_parameters(self, params_to_apply: dict) -> list:
        """Read live @@GLOBAL values for each applied parameter and compare to
        intent. Returns a list of {parameter, intended, effective, status}."""
        verification = []
        for param, intended in params_to_apply.items():
            try:
                rows = self.mysql_connector.execute_query(
                    "SHOW GLOBAL VARIABLES WHERE Variable_name = %s", (param,)
                )
                if not rows:
                    verification.append({
                        "parameter": param, "intended": intended,
                        "effective": "(not found)", "status": "unknown",
                    })
                    continue
                effective = rows[0].get("Value")
                verification.append({
                    "parameter": param, "intended": intended,
                    "effective": effective,
                    "status": self._compare_values(intended, effective),
                })
            except Exception as exc:
                verification.append({
                    "parameter": param, "intended": intended,
                    "effective": f"(error: {exc})", "status": "unknown",
                })
        return verification

    @staticmethod
    def _render_verification_table(verification: list, include_dirs=None,
                                   shadowing=None) -> str:
        # Pick a glyph set stdout can actually encode. This table prints AFTER
        # the restart, so a UnicodeEncodeError here would turn a successful
        # apply into an error exit and lose the report entirely.
        try:
            from utils.safe_io import supports_unicode
            fancy = supports_unicode()
        except ImportError:
            fancy = False

        if fancy:
            badges = {"applied": "✓ APPLIED", "adjusted": "~ ADJUSTED",
                      "mismatch": "✗ MISMATCH", "unknown": "? UNKNOWN"}
            rule_char = "─"
        else:
            badges = {"applied": "[OK]       APPLIED", "adjusted": "[~]        ADJUSTED",
                      "mismatch": "[MISMATCH] OVERRIDDEN", "unknown": "[?]        UNKNOWN"}
            rule_char = "-"
        pw = max([len("Parameter")] + [len(v["parameter"]) for v in verification]) + 2
        iw = max([len("Intended")] + [len(str(v["intended"])) for v in verification]) + 2
        ew = max([len("Effective")] + [len(str(v["effective"])) for v in verification]) + 2
        line = rule_char * (pw + iw + ew + 22)
        out = ["", "POST-RESTART VERIFICATION", line,
               f"  {'Parameter':<{pw}}{'Intended':<{iw}}{'Effective':<{ew}}Status",
               line]
        for v in verification:
            out.append(
                f"  {v['parameter']:<{pw}}{str(v['intended']):<{iw}}"
                f"{str(v['effective']):<{ew}}{badges.get(v['status'], v['status'])}"
            )
        out.append(line)
        applied = sum(1 for v in verification if v["status"] in ("applied", "adjusted"))
        mism = [v for v in verification if v["status"] == "mismatch"]
        out.append(f"  {applied} of {len(verification)} applied as written"
                   f"{' / adjusted' if any(v['status']=='adjusted' for v in verification) else ''}.")
        if mism:
            out.append(f"  {len(mism)} overridden - another config source is winning.")
            # H6: name the SECTION first — the most common cause is a duplicate
            # a few lines further down the same file, not an included file.
            if shadowing:
                out.append("  Same file, later section (later definitions win):")
                for entry in shadowing:
                    out.append(f"    - {entry}")
            if include_dirs:
                out.append(
                    f"  Also check included files: {', '.join(include_dirs)}"
                )
            if not shadowing and not include_dirs:
                out.append(
                    "  No competing section or !includedir found in this file - "
                    "check command-line options in the systemd unit "
                    "(systemctl cat <unit>) or a persisted SET GLOBAL."
                )
        return "\n".join(out)

    # ------------------------------------------------------------------
    # Memory footprint estimate (improvement #4)
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_to_mb(value, default=0.0):
        """Parse a recommendation/config size value into megabytes."""
        if value is None:
            return default
        try:
            s = str(value).strip().upper()
            if not s:
                return default
            if s.endswith("G") or s.endswith("GB"):
                num = s.rstrip("GB")
                return float(num) * 1024.0
            if s.endswith("M") or s.endswith("MB"):
                num = s.rstrip("MB")
                return float(num)
            if s.endswith("K") or s.endswith("KB"):
                num = s.rstrip("KB")
                return float(num) / 1024.0
            val = float(s)
            if val >= 1048576:
                return val / (1024.0 * 1024.0)
            return val
        except (ValueError, TypeError):
            return default

    @staticmethod
    def _parse_to_kb(value, default=0.0):
        """Parse a recommendation/config size value into kilobytes."""
        if value is None:
            return default
        try:
            s = str(value).strip().upper()
            if not s:
                return default
            if s.endswith("G") or s.endswith("GB"):
                num = s.rstrip("GB")
                return float(num) * 1024.0 * 1024.0
            if s.endswith("M") or s.endswith("MB"):
                num = s.rstrip("MB")
                return float(num) * 1024.0
            if s.endswith("K") or s.endswith("KB"):
                num = s.rstrip("KB")
                return float(num)
            val = float(s)
            if val >= 1024:
                return val / 1024.0
            return val
        except (ValueError, TypeError):
            return default

    def _estimate_memory_footprint(self, metrics: dict, recommendations: list = None) -> dict:
        """Worst-case RAM = global buffers + max_connections × per-connection
        buffers. Evaluates both current baseline and proposed post-validation state."""
        total_ram_mb = metrics.get("total_ram_mb", 0) or 0

        # Current baseline values
        cur_bp_mb = float(metrics.get("innodb_buffer_pool_size_mb", 0) or 0)
        cur_key_buf_mb = float(metrics.get("key_buffer_size_mb", 0) or 0)
        cur_max_conn = int(metrics.get("max_connections", 0) or 0)

        cur_join_buf_kb = float(metrics.get("join_buffer_size_kb", 0) or 0)
        cur_sort_buf_kb = float(metrics.get("sort_buffer_size_kb", 0) or 0)
        cur_read_buf_kb = float(metrics.get("read_buffer_size_kb", 0) or 0)
        cur_read_rnd_buf_kb = float(metrics.get("read_rnd_buffer_size_kb", 0) or 0)
        cur_thread_stack_kb = float(metrics.get("thread_stack_kb", 0) or 0)
        cur_per_conn_mb = (
            cur_join_buf_kb + cur_sort_buf_kb + cur_read_buf_kb + cur_read_rnd_buf_kb + cur_thread_stack_kb
        ) / 1024.0

        cur_global_mb = cur_bp_mb + cur_key_buf_mb
        cur_worst_case_mb = cur_global_mb + (cur_max_conn * cur_per_conn_mb)
        cur_pct = (cur_worst_case_mb / total_ram_mb * 100) if total_ram_mb else 0

        # Proposed values from recommendations
        proposed = {
            r["parameter"]: r["recommended_value"]
            for r in (recommendations or [])
            if isinstance(r, dict) and "parameter" in r
        }

        prop_bp_mb = self._parse_to_mb(proposed.get("innodb_buffer_pool_size"), cur_bp_mb)
        prop_key_buf_mb = self._parse_to_mb(proposed.get("key_buffer_size"), cur_key_buf_mb)
        prop_max_conn = int(proposed.get("max_connections", cur_max_conn) or 0)

        prop_join_buf_kb = self._parse_to_kb(proposed.get("join_buffer_size"), cur_join_buf_kb)
        prop_sort_buf_kb = self._parse_to_kb(proposed.get("sort_buffer_size"), cur_sort_buf_kb)
        prop_read_buf_kb = self._parse_to_kb(proposed.get("read_buffer_size"), cur_read_buf_kb)
        prop_read_rnd_buf_kb = self._parse_to_kb(proposed.get("read_rnd_buffer_size"), cur_read_rnd_buf_kb)
        prop_thread_stack_kb = self._parse_to_kb(proposed.get("thread_stack"), cur_thread_stack_kb)
        prop_per_conn_mb = (
            prop_join_buf_kb + prop_sort_buf_kb + prop_read_buf_kb + prop_read_rnd_buf_kb + prop_thread_stack_kb
        ) / 1024.0

        prop_global_mb = prop_bp_mb + prop_key_buf_mb
        prop_worst_case_mb = prop_global_mb + (prop_max_conn * prop_per_conn_mb)
        prop_pct = (prop_worst_case_mb / total_ram_mb * 100) if total_ram_mb else 0

        verdict = "OK" if prop_pct < 80 else "WARNING" if prop_pct < 100 else "DANGER"

        return {
            # Legacy / Proposed primary fields
            "global_buffers_mb": round(prop_global_mb, 1),
            "per_connection_mb": round(prop_per_conn_mb, 2),
            "max_connections": prop_max_conn,
            "worst_case_mb": round(prop_worst_case_mb, 1),
            "total_ram_mb": total_ram_mb,
            "worst_case_pct_of_ram": round(prop_pct, 1),
            "verdict": verdict,
            # Explicit Current fields
            "current_global_buffers_mb": round(cur_global_mb, 1),
            "current_per_connection_mb": round(cur_per_conn_mb, 2),
            "current_max_connections": cur_max_conn,
            "current_footprint_mb": round(cur_worst_case_mb, 1),
            "current_worst_case_mb": round(cur_worst_case_mb, 1),
            "current_pct": round(cur_pct, 1),
            "current_worst_case_pct_of_ram": round(cur_pct, 1),
            # Explicit Proposed fields
            "proposed_global_buffers_mb": round(prop_global_mb, 1),
            "proposed_per_connection_mb": round(prop_per_conn_mb, 2),
            "proposed_max_connections": prop_max_conn,
            "proposed_footprint_mb": round(prop_worst_case_mb, 1),
            "proposed_worst_case_mb": round(prop_worst_case_mb, 1),
            "proposed_pct": round(prop_pct, 1),
            "proposed_worst_case_pct_of_ram": round(prop_pct, 1),
        }

    # ------------------------------------------------------------------
    # Rollback command (improvement #5)
    # ------------------------------------------------------------------
    def list_backups(self) -> list:
        """Return timestamped my.cnf backups, newest first."""
        import glob as _glob

        try:
            mycnf_path = self._find_mycnf()
        except FileNotFoundError:
            return []
        backups = _glob.glob(f"{mycnf_path}.backup.*")
        return sorted(backups, reverse=True)

    @staticmethod
    def _atomic_restore_file(src_path: str, dst_path: str):
        """Atomically restore src_path onto dst_path via tempfile.mkstemp + fsync + os.replace.

        Avoids truncating live dst_path in place (e.g. on ENOSPC or mid-write failure),
        preserving database integrity during rollbacks and preventing symlink attacks.
        """
        import os
        import tempfile

        dst_dir = os.path.dirname(os.path.abspath(dst_path))
        with open(src_path, "rb") as src_fh:
            content = src_fh.read()

        fd, tmp_path = tempfile.mkstemp(prefix="mycnf_restore_", dir=dst_dir)
        try:
            try:
                tmp_fh = os.fdopen(fd, "wb")
            except BaseException:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise

            with tmp_fh:
                tmp_fh.write(content)
                tmp_fh.flush()
                os.fsync(tmp_fh.fileno())

            try:
                if os.path.exists(dst_path):
                    st = os.stat(dst_path)
                else:
                    st = os.stat(src_path)
                os.chmod(tmp_path, st.st_mode)
                try:
                    os.chown(tmp_path, st.st_uid, st.st_gid)
                except (PermissionError, OSError, AttributeError):
                    pass
            except OSError:
                pass

            os.replace(tmp_path, dst_path)
        except Exception:
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            raise

        try:
            dirfd = os.open(dst_dir, getattr(os, "O_RDONLY", 0))
            try:
                os.fsync(dirfd)
            finally:
                os.close(dirfd)
        except (OSError, PermissionError, AttributeError):
            pass

    def rollback(self, backup_file: str = None, auto_apply: bool = False) -> dict:
        """Restore a previous my.cnf backup and restart (topology-aware).

        Without *backup_file* the newest backup is used. Requires root.
        """
        if not backup_file:
            backups = self.list_backups()
            if not backups:
                self.logger.error("No my.cnf backups found to roll back to")
                return {"mode": "rollback", "restored": False,
                        "reason": "no backups found"}
            backup_file = backups[0]

        if not os.path.isfile(backup_file):
            self.logger.error("Backup not found: %s", backup_file)
            return {"mode": "rollback", "restored": False,
                    "reason": f"backup not found: {backup_file}"}

        if hasattr(os, "geteuid") and os.geteuid() != 0:
            raise PermissionError("Root privileges required to roll back.")

        # MAJ-8: Acquire lock before any restore/rollback actions
        self._acquire_apply_lock()

        mycnf_path = self._find_mycnf()
        print(f"About to restore: {backup_file}")
        print(f"           onto: {mycnf_path}")
        if not auto_apply:
            try:
                if input("Proceed with rollback? [y/N]: ").strip().lower() not in ("y", "yes"):
                    return {"mode": "rollback", "restored": False,
                            "reason": "cancelled by user"}
            except EOFError:
                self.logger.warning("Non-TTY stdin; use --yes to roll back. Aborting.")
                return {"mode": "rollback", "restored": False, "reason": "no confirmation"}

        # Validate the backup before making it live.
        is_valid, detail, _ = self._validate_config_file(backup_file)
        if not is_valid:
            self.logger.error("Backup failed validation, not restoring: %s", detail)
            return {"mode": "rollback", "restored": False,
                    "reason": f"backup invalid: {detail}"}

        service_name = (
            self._detect_mysql_service(require_active=True)
            or self._detect_mysql_service(require_active=False)
        )

        # MAJ-2: Atomic restore
        self._atomic_restore_file(backup_file, mycnf_path)
        self.logger.info("Restored %s onto %s", backup_file, mycnf_path)

        # MAJ-8: Cluster / replication awareness
        cluster = self._detect_cluster_role()
        if cluster and not self.allow_cluster_restart:
            self.logger.warning(
                "Detected %s. The backup config was RESTORED to %s but the "
                "automatic restart was SKIPPED to avoid disrupting the "
                "cluster/replication topology. Perform a topology-aware "
                "restart manually, or re-run with --allow-cluster-restart.",
                cluster, mycnf_path,
            )
            return {
                "mode": "rollback",
                "restored": True,
                "backup_file": backup_file,
                "restart_performed": False,
                "restart_skipped_cluster": cluster,
                "manual_restart_required": True,
            }

        restarted = self._restart_mysql_service(service=service_name)
        if not restarted:
            self.logger.critical(
                "Rollback restore succeeded but the restart did NOT come up. "
                "Manual intervention required (service: %s).", service_name or "unknown",
            )
        return {"mode": "rollback", "restored": True,
                "backup_file": backup_file, "restart_performed": restarted}

    # ------------------------------------------------------------------
    # Apply recommendations
    # ------------------------------------------------------------------
    def _apply_recommendations(self, recommendations: list, dynamic_only: bool = False) -> dict:
        self.logger.info(f"Applying {len(recommendations)} recommendations ...")

        # --- M5: refuse to run concurrently with another apply ---
        self._acquire_apply_lock()

        # --- H5: pre-flight safety checks — abort before touching anything ---
        preflight = self.safety_checker.pre_flight_checks()
        for warning in preflight.get("warnings", []):
            self.logger.warning("Pre-flight warning: %s", warning)
        if not preflight.get("safe", True):
            issues = "; ".join(preflight.get("issues", [])) or "unknown"
            self.logger.error(
                "Pre-flight safety checks FAILED — aborting; no changes made. "
                "Issues: %s", issues,
            )
            raise RuntimeError(f"Pre-flight safety checks failed: {issues}")
        self.logger.info("Pre-flight safety checks passed")

        import filecmp
        import re as _re
        import shutil
        import tempfile

        mycnf_path = self._find_mycnf()

        # --- Fix #3 / N-13: verified atomic fsynced backup ---
        backup_file = f"{mycnf_path}.backup.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self._atomic_restore_file(mycnf_path, backup_file)
        if not filecmp.cmp(mycnf_path, backup_file, shallow=False):
            raise RuntimeError(
                f"Backup verification failed: {backup_file} does not match {mycnf_path}"
            )
        self.logger.info(f"Verified backup: {backup_file}")

        # --- Fix #18: warn on !includedir ---
        # M3: read with surrogateescape so a my.cnf containing non-UTF-8 bytes
        # (e.g. Latin-1 comments in older European hosting configs) does not
        # crash mid-apply; the matching write below restores the bytes verbatim.
        with open(mycnf_path, "r", encoding="utf-8", errors="surrogateescape") as fh:
            original_lines = fh.readlines()
        # H6: record competing sections up front, while we have the file.
        shadowing_sections = self._find_shadowing_sections(
            original_lines, [r.get("parameter") for r in recommendations
                             if r.get("parameter")]
        )
        if shadowing_sections:
            self.logger.warning(
                "my.cnf defines these parameters in a later section the server "
                "also reads, which will OVERRIDE the values written to "
                "[mysqld]: %s", "; ".join(shadowing_sections),
            )

        include_dirs = []
        for line in original_lines:
            stripped = line.strip()
            if stripped.startswith("!includedir"):
                parts = stripped.split(None, 1)
                if len(parts) > 1:
                    include_dirs.append(parts[1])
                self.logger.warning(
                    f"my.cnf uses '{stripped}' — overrides in included "
                    "files may take precedence over values written here"
                )

        # --- Fix #2: single-pass application (collect all changes, write once) ---
        applied = 0
        failed = 0
        restart_needed = False
        results = []
        params_to_apply = {}

        for rec in recommendations:
            try:
                param = rec["parameter"]
                value = str(rec["recommended_value"])
                # improvement #6: dynamic-only mode skips anything needing a restart.
                if dynamic_only and rec.get("restart_required"):
                    results.append({
                        "parameter": param,
                        "status": "skipped_needs_restart",
                    })
                    continue
                if rec.get("systemd_edit_required"):
                    self.logger.info(f"Systemd edit required for {param}:")
                    for cmd in rec.get("systemd_commands", []):
                        self.logger.info(f"  Execute: {cmd}")
                params_to_apply[param] = value
                if rec.get("restart_required"):
                    restart_needed = True
                applied += 1
                results.append({
                    "parameter": param,
                    "status": "applied",
                    "value": value,
                })
            except Exception as exc:
                failed += 1
                results.append({
                    "parameter": rec.get("parameter", "unknown"),
                    "status": "error",
                    "error": str(exc),
                })

        if not params_to_apply:
            self.logger.info("No parameters to apply")
            return {
                "backup_file": backup_file,
                "applied_count": 0,
                "failed_count": failed,
                "restart_performed": False,
                "results": results,
            }

        new_lines = self._build_mycnf_lines(original_lines, params_to_apply)

        self.logger.info(
            f"Writing {len(params_to_apply)} parameters in single pass"
        )

        # --- Fix #1: atomic write via tempfile + os.replace ---
        mycnf_dir = os.path.dirname(mycnf_path)
        fd, tmp_path = tempfile.mkstemp(dir=mycnf_dir, suffix=".tmp")
        try:
            try:
                fh = os.fdopen(
                    fd, "w", encoding="utf-8", errors="surrogateescape"
                )
            except BaseException:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise

            with fh:
                fh.writelines(new_lines)
                fh.flush()
                os.fsync(fh.fileno())
            original_stat = os.stat(mycnf_path)
            os.chmod(tmp_path, original_stat.st_mode)
            try:
                os.chown(tmp_path, original_stat.st_uid, original_stat.st_gid)
            except (PermissionError, OSError, AttributeError):
                pass

            # --- Pre-restart validation: never let a bad config go live ---
            is_valid, detail, validation_ran = self._validate_config_file(tmp_path)
            if not is_valid:
                os.unlink(tmp_path)
                self.logger.error(
                    "Config validation FAILED — refusing to apply. The live "
                    "my.cnf was left UNTOUCHED and no restart was performed. "
                    "Detail: %s", detail,
                )
                return {
                    "backup_file": backup_file,
                    "applied_count": 0,
                    "failed_count": len(recommendations),
                    "restart_performed": False,
                    "validation_failed": True,
                    "validation_detail": detail,
                    "results": [
                        {"parameter": r.get("parameter", "unknown"),
                         "status": "validation_failed"}
                        for r in recommendations
                    ],
                }
            if validation_ran:
                self.logger.info("Config validation passed (%s)", detail)
            else:
                # M-9: If version is unknown and validation did not run, refuse apply
                is_unknown_version = False
                if getattr(self, "version_compat", None):
                    vi = self.version_compat.version_info
                    if vi.get("version_unknown") or vi.get("branch") == "unknown" or self.version_compat.ver_tuple == (0, 0, 0):
                        is_unknown_version = True
                if is_unknown_version:
                    os.unlink(tmp_path)
                    self.logger.error(
                        "Database version is UNKNOWN and binary validation could not run (%s). "
                        "Refusing to apply unvalidated configuration changes to an unidentified "
                        "database branch.",
                        detail,
                    )
                    return {
                        "backup_file": backup_file,
                        "applied_count": 0,
                        "failed_count": len(recommendations),
                        "restart_performed": False,
                        "validation_failed": True,
                        "validation_detail": f"unknown database version without binary validation ({detail})",
                        "results": [
                            {"parameter": r.get("parameter", "unknown"),
                             "status": "validation_failed"}
                            for r in recommendations
                        ],
                    }
                self.logger.warning(
                    "New config could not be validated (%s); proceeding with a "
                    "verified backup at %s as the safety net.",
                    detail, backup_file,
                )
        except BaseException:
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            raise

        # Define the rollback closure for the critical window (M-10 a)
        def _critical_rollback():
            self.logger.critical(
                "Emergency rollback: restoring backup %s onto %s",
                backup_file, mycnf_path,
            )
            try:
                self._atomic_restore_file(backup_file, mycnf_path)
            except Exception as restore_err:
                self.logger.critical(
                    "Failed to restore backup file %s during emergency rollback: %s",
                    backup_file, restore_err,
                )
            svc = (
                self._detect_mysql_service(require_active=True)
                or self._detect_mysql_service(require_active=False)
            )
            recov = self._restart_mysql_service(service=svc)
            if recov:
                self.logger.info(
                    "Emergency rollback successful: MySQL/MariaDB restarted on previous config (%s)",
                    backup_file,
                )
            else:
                self.logger.critical(
                    "EMERGENCY ROLLBACK RESTART FAILED — MySQL/MariaDB is DOWN."
                )

        with _critical_section_trap(_critical_rollback, logger=self.logger):
            os.replace(tmp_path, mycnf_path)
            try:
                dirfd = os.open(mycnf_dir, getattr(os, "O_RDONLY", 0))
                try:
                    os.fsync(dirfd)
                finally:
                    os.close(dirfd)
            except (OSError, PermissionError, AttributeError):
                pass

            # improvement #6: dynamic-only mode applies live via SET GLOBAL, no restart
            live_applied = []
            if dynamic_only:
                live_applied = self._apply_dynamic_live(params_to_apply)

            # --- Fix #4: check restart result, with a VERIFIED rollback ---
            restart_performed = False
            if restart_needed and applied > 0:
                # --- M6: never auto-restart a cluster/replication node ---
                cluster = self._detect_cluster_role()
                if cluster and not self.allow_cluster_restart:
                    self.logger.warning(
                        "Detected %s. The new config was WRITTEN to %s but the "
                        "automatic restart was SKIPPED to avoid disrupting the "
                        "cluster/replication topology. Perform a topology-aware "
                        "restart manually, or re-run with --allow-cluster-restart.",
                        cluster, mycnf_path,
                    )
                    return {
                        "backup_file": backup_file,
                        "applied_count": applied,
                        "failed_count": failed,
                        "restart_performed": False,
                        "restart_skipped_cluster": cluster,
                        "manual_restart_required": True,
                        "results": results,
                    }

                # Capture the unit name NOW, while the server is still healthy and
                # reporting 'active'. A crashed unit no longer reports 'active', so
                # the rollback restart must reuse this exact name.
                service_name = (
                    self._detect_mysql_service(require_active=True)
                    or self._detect_mysql_service(require_active=False)
                )

                self.logger.info("Restarting MySQL/MariaDB to apply changes ...")
                if not self._restart_mysql_service(service=service_name):
                    self.logger.error(
                        "Restart FAILED on the new config — rolling back to backup: %s",
                        backup_file,
                    )
                    try:
                        self._atomic_restore_file(backup_file, mycnf_path)
                    except Exception as restore_err:
                        self.logger.critical(
                            "Failed to restore backup file %s: %s",
                            backup_file, restore_err,
                        )

                    # The database is currently DOWN. We must bring it back up on the
                    # known-good config and CONFIRM it — never restore the file and
                    # walk away silently.
                    recovered = self._restart_mysql_service(service=service_name)
                    if recovered:
                        self.logger.info(
                            "Rollback successful: MySQL/MariaDB restarted on the "
                            "previous config (%s)", backup_file,
                        )
                    else:
                        self.logger.critical(
                            "ROLLBACK RESTART FAILED — MySQL/MariaDB is DOWN and did "
                            "not come back up on the restored config. MANUAL "
                            "INTERVENTION REQUIRED. Restored config: %s (unit: %s). "
                            "Investigate with: systemctl restart %s ; journalctl -xeu %s",
                            backup_file, service_name or "unknown",
                            service_name or "mysql", service_name or "mysql",
                        )

                    return {
                        "backup_file": backup_file,
                        "applied_count": 0,
                        "failed_count": len(recommendations),
                        "restart_performed": False,
                        "rollback": True,
                        "rollback_recovered": recovered,
                        "service": service_name,
                        "results": [
                            {"parameter": r.get("parameter", "unknown"),
                             "status": "rolled_back"}
                            for r in recommendations
                        ],
                    }
                restart_performed = True

            # improvement #1: verify live @@GLOBAL values now that changes are active
            verification = None
            if restart_performed or (dynamic_only and live_applied):
                verification = self._verify_applied_parameters(params_to_apply)
                print(self._render_verification_table(
                    verification, include_dirs, shadowing_sections
                ))
                mism = [v for v in verification if v["status"] == "mismatch"]
                if mism:
                    self.logger.warning(
                        "%d parameter(s) did not take effect as written: %s",
                        len(mism), ", ".join(v["parameter"] for v in mism),
                    )

            self.logger.info(
                f"Application complete: {applied} applied, {failed} failed"
            )
            return {
                "backup_file": backup_file,
                "applied_count": applied,
                "failed_count": failed,
                "restart_performed": restart_performed,
                "dynamic_live_applied": live_applied if dynamic_only else None,
                "verification": verification,
                "results": results,
            }

    # ------------------------------------------------------------------
    # Live dynamic apply via SET GLOBAL (improvement #6)
    # ------------------------------------------------------------------
    def _apply_dynamic_live(self, params_to_apply: dict) -> list:
        """Apply each parameter live via SET GLOBAL (no restart). Size values are
        converted to bytes since SET does not accept K/M/G suffixes."""
        import re as _re

        applied = []
        for param, value in params_to_apply.items():
            # param comes from our own controlled recommendation set; guard anyway.
            if not _re.match(r"^[A-Za-z_]+$", param):
                self.logger.debug("Skipping non-identifier param for SET GLOBAL: %s", param)
                continue
            numeric = self._parse_to_bytes(value)
            try:
                if numeric is not None:
                    self.mysql_connector.execute_query(
                        f"SET GLOBAL {param} = {int(numeric)}"
                    )
                else:
                    self.mysql_connector.execute_query(
                        f"SET GLOBAL {param} = %s", (value,)
                    )
                applied.append(param)
                self.logger.info("SET GLOBAL %s = %s (live)", param, value)
            except Exception as exc:
                self.logger.warning("Could not SET GLOBAL %s: %s", param, exc)
        return applied


# ======================================================================
#  Exit codes (improvement #8)
# ======================================================================
EXIT_OK = 0        # nothing to do / clean success with no changes needed
EXIT_ERROR = 1     # error, OR a change that failed and was rolled back
EXIT_PENDING = 2   # recommendations exist / manual action required
EXIT_APPLIED = 3   # changes applied and confirmed live


def exit_code_for_application(app: dict) -> int:
    """Classify an ``_apply_recommendations`` result into an exit code.

    H7: previously every one of these paths returned ``applied_count == 0`` and
    was therefore reported as EXIT_OK — so a failed restart followed by a
    rollback exited 0 and any cron/monitoring wrapper saw a green tick while the
    database could still be down. The failure states are now distinguishable.
    """
    if not isinstance(app, dict):
        return EXIT_ERROR

    # A rollback is always a failure event, even when recovery succeeded: the
    # requested change did not take, and a human needs to know why.
    if app.get("rollback"):
        return EXIT_ERROR

    # The candidate config was rejected by the server binary. Nothing was
    # written and nothing was restarted, but the run did not do what was asked.
    if app.get("validation_failed"):
        return EXIT_ERROR

    # Config written, restart deliberately skipped on a cluster/replica node.
    # Not an error — the tool did exactly what it was told — but the change is
    # NOT live until someone performs a topology-aware restart.
    if app.get("manual_restart_required"):
        return EXIT_PENDING

    # N-6: If any parameter was skipped because it requires a restart (e.g. under --dynamic-only),
    # there is pending work that could not be applied live.
    results = app.get("results") or []
    if any(r.get("status") == "skipped_needs_restart" for r in results):
        return EXIT_PENDING

    applied = app.get("applied_count", 0)
    if not applied:
        return EXIT_OK

    # M-2: Inspect verification status if live verification was run
    verification = app.get("verification") or []
    if verification:
        if all(v.get("status") == "mismatch" for v in verification):
            return EXIT_ERROR          # Nothing took effect
        if any(v.get("status") == "mismatch" for v in verification):
            return EXIT_PENDING        # Partially live or blocked by external limits

    # Parameters were written. If a restart was required to make them live and
    # it was not confirmed, do not claim success.
    restart_confirmed = app.get("restart_performed", False)
    dynamic_live = app.get("dynamic_live_applied") or []
    if restart_confirmed or dynamic_live:
        return EXIT_APPLIED

    # Applied only parameters that needed no restart, via the file — they take
    # effect on the next restart. Pending, not applied.
    return EXIT_PENDING


# ======================================================================
#  CLI entry point
# ======================================================================
def _make_output_encoding_safe():
    """Never let an un-encodable character crash a run.

    The reports use ✓ / ✗ / ─ / 💡 for readability. Under a POSIX/C locale —
    the DEFAULT for cron jobs, systemd units and minimal containers, which is
    exactly where this tool is meant to run unattended — stdout resolves to
    ASCII and printing any of them raises UnicodeEncodeError.

    The dangerous case is the post-restart verification table: it prints AFTER
    my.cnf has been written and the service restarted, so the crash converts a
    genuinely SUCCESSFUL apply into `Error: …` and a failure exit code, and the
    operator loses the applied/mismatch report that justified the restart.

    Degrading a tick to '?' is always better than losing the run.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv=None):
    _make_output_encoding_safe()

    parser = argparse.ArgumentParser(
        description=f"MySQL Auto-Tuner Ultimate v{MySQLAutoTunerUltimate.VERSION}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --analyze
  %(prog)s --analyze --dry-run
  %(prog)s --analyze --show-evidence
  %(prog)s --analyze --explain
  %(prog)s --analyze --show-evidence --explain
  %(prog)s --analyze --explain --show-evidence --multi-pass --max-passes 3
  %(prog)s --analyze --top-databases --top-db-limit 5
  %(prog)s --analyze --top-databases --explain --show-evidence
  %(prog)s --analyze --top-databases --format json > report.json
  %(prog)s --optimize --pass-number 1 --dry-run
  %(prog)s --optimize --pass-number 1
  %(prog)s --multi-pass --max-passes 3 --dry-run
  %(prog)s --multi-pass --max-passes 3
  %(prog)s --file-limit-check --platform directadmin
  %(prog)s --platform directadmin --analyze
  %(prog)s --analyze --profile safe
  %(prog)s --optimize --profile aggressive --dry-run
  %(prog)s --dump-effective-config --platform directadmin --profile aggressive
  %(prog)s --dump-effective-config --output-format json > effective-config.json
        """,
    )

    # Actions
    parser.add_argument(
        "--analyze",
        action="store_true",
        help="Analyse system and generate recommendations",
    )
    parser.add_argument(
        "--optimize",
        action="store_true",
        help="Optimise system with recommendations",
    )
    parser.add_argument(
        "--multi-pass",
        action="store_true",
        help="Perform multi-pass optimisation",
    )
    parser.add_argument(
        "--file-limit-check",
        action="store_true",
        help="Check file limit requirements",
    )
    parser.add_argument(
        "--dump-effective-config",
        action="store_true",
        help="Print the final merged config and exit",
    )
    parser.add_argument(
        "--diff",
        action="store_true",
        help="Preview a unified diff of the proposed my.cnf changes (read-only)",
    )
    parser.add_argument(
        "--rollback",
        action="store_true",
        help="Restore a previous my.cnf backup and restart (newest unless --rollback-file)",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help=(
            "Audit the config file against the code: report settings nothing "
            "reads, settings the code expects but the file lacks, and broken "
            "name contracts (e.g. an anomaly with no confidence penalty). "
            "Exits 1 on a contract violation — usable in CI. Touches no database."
        ),
    )

    # Options
    parser.add_argument(
        "--rollback-file",
        help="Specific backup file to restore with --rollback (default: newest)",
    )
    parser.add_argument(
        "--dynamic-only",
        action="store_true",
        help=(
            "Cron-safe apply: only parameters that do NOT need a restart, applied "
            "live via SET GLOBAL. Never restarts the service."
        ),
    )
    parser.add_argument(
        "--pass-number",
        type=int,
        default=1,
        choices=[1, 2, 3],
        help="Optimisation pass number (1-3)",
    )
    parser.add_argument(
        "--max-passes",
        type=int,
        default=3,
        choices=[1, 2, 3],
        help="Maximum optimisation passes",
    )
    parser.add_argument(
        "--platform",
        choices=["auto", "directadmin", "cpanel", "litespeed"],
        default="auto",
        help="Platform type",
    )
    parser.add_argument(
        "--profile",
        choices=["safe", "balanced", "aggressive"],
        default="balanced",
        help="Optimisation profile",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate recommendations without applying",
    )
    parser.add_argument(
        "--yes", "-y",
        action="store_true",
        help=(
            "Apply recommendations without the interactive confirmation prompt "
            "(for automation/cron). Ignored in --dry-run."
        ),
    )
    parser.add_argument(
        "--allow-buffer-pool-shrink",
        action="store_true",
        help=(
            "Permit recommendations that shrink innodb_buffer_pool_size by more "
            "than the safety threshold. By default such shrinks are suppressed "
            "to protect a healthy production buffer pool."
        ),
    )
    parser.add_argument(
        "--allow-cluster-restart",
        action="store_true",
        help=(
            "Permit the automatic service restart even when a Galera cluster or "
            "replication topology is detected. By default the config is written "
            "but the restart is skipped so you can restart topology-aware."
        ),
    )
    parser.add_argument(
        "--show-evidence",
        action="store_true",
        help="Show evidence sources and confidence factors",
    )
    parser.add_argument(
        "--explain",
        action="store_true",
        help=(
            "Educational mode: show human-readable explanations for every "
            "recommendation, including the WHY behind each change"
        ),
    )
    parser.add_argument(
        "--top-databases",
        action="store_true",
        help=(
            "Show TOP DATABASE ACTIVITY table: per-schema queries/sec, "
            "data size, connections, and a composite impact score with "
            "actionable tip identifying the highest-load database."
        ),
    )
    parser.add_argument(
        "--top-db-limit",
        type=int,
        default=10,
        choices=range(1, 101),
        metavar="N",
        help="Maximum number of databases to show in --top-databases (1-100, default: 10)",
    )
    parser.add_argument(
        "--format",
        choices=["console", "text", "json", "html"],
        default="console",
        help="Report format",
    )
    parser.add_argument(
        "--output-format",
        choices=["yaml", "json"],
        default="yaml",
        help="Format for --dump-effective-config",
    )
    parser.add_argument(
        "--save-report",
        action="store_true",
        help=(
            "Also write the report to a timestamped file under "
            "/var/log/mysql-autotuner-reports (newest 30 kept). Off by default — "
            "console/redirected output is not affected."
        ),
    )
    # --- MySQL connection overrides (M12) ---
    conn_group = parser.add_argument_group(
        "MySQL connection",
        "Override the auto-detected connection. Without these the tool tries "
        "/root/.my.cnf, /etc/mysql/debian.cnf, the DirectAdmin config, then "
        "socket auth — which leaves no way in on a host with a non-standard "
        "socket, a remote database, or credentials in a custom file.",
    )
    conn_group.add_argument("--mysql-host", help="Hostname or IP (default: localhost)")
    conn_group.add_argument("--mysql-port", type=int, help="TCP port (default: 3306)")
    conn_group.add_argument("--mysql-user", help="Username")
    conn_group.add_argument("--mysql-socket", help="Path to the unix socket")
    conn_group.add_argument(
        "--defaults-file",
        help=(
            "Read credentials from a MySQL option file ([client]/[mysql] "
            "section). PREFERRED over --mysql-password: the password never "
            "appears in the process list."
        ),
    )
    conn_group.add_argument(
        "--mysql-password",
        help=(
            "Password. Visible to any local user via `ps` — prefer "
            "--defaults-file, or set the MYSQL_PWD environment variable."
        ),
    )

    parser.add_argument("--config", help="Configuration file path")
    parser.add_argument(
        "--verbose", action="store_true", help="Verbose output"
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"MySQL Auto-Tuner Ultimate v{MySQLAutoTunerUltimate.VERSION}",
    )

    args = parser.parse_args(argv)

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        # M12: assemble connection overrides. MYSQL_PWD is honoured as the
        # non-argv way to pass a password (the same variable the mysql client
        # uses), and an argv password earns a warning because `ps` exposes it.
        if args.mysql_password:
            print(
                "Warning: --mysql-password is visible to other local users via "
                "`ps`. Prefer --defaults-file or the MYSQL_PWD environment "
                "variable.",
                file=sys.stderr,
            )
        mysql_overrides = {
            "host": args.mysql_host,
            "port": args.mysql_port,
            "user": args.mysql_user,
            "socket": args.mysql_socket,
            "password": args.mysql_password or os.environ.get("MYSQL_PWD"),
            "defaults_file": args.defaults_file,
        }

        autotuner = MySQLAutoTunerUltimate(args.config, mysql_overrides)
        autotuner.allow_bp_shrink = args.allow_buffer_pool_shrink
        autotuner.allow_cluster_restart = args.allow_cluster_restart
        # H2: apply the profile ONCE, here, so every command honours it — not
        # just --dump-effective-config as before.
        autotuner.apply_profile(args.profile)
        # M6: report files are opt-in.
        if args.save_report:
            if autotuner.report_generator.output_dir is None:
                autotuner.logger.warning(
                    "--save-report requested but no writable reports directory "
                    "is available; the report will only be printed."
                )
            else:
                autotuner.report_generator.save_to_disk = True

        # Exit codes (improvement #8):
        #   0 = no changes needed | 1 = error | 2 = recommendations pending | 3 = applied
        exit_code = EXIT_OK

        # --- config audit (no database access) ---
        if args.check_config:
            from core.config_audit import audit_config
            report = audit_config(
                autotuner.config, UltimateDecisionEngine.ANOMALY_NAMES
            )
            print(f"\nConfig audit: {autotuner.config_file}")
            print("=" * 70)
            if report["contract"]:
                print("\nCONTRACT VIOLATIONS (these silently change behaviour):")
                for problem in report["contract"]:
                    print(f"  [VIOLATION] {problem}")
            if report["dead"]:
                print("\nDEFINED BUT NEVER READ (no effect):")
                for key in report["dead"]:
                    print(f"  - {key}")
            if report["missing"]:
                print("\nEXPECTED BY CODE BUT ABSENT (hardcoded default used):")
                for key in report["missing"]:
                    print(f"  ? {key}")
            if not any(report.values()):
                print("\n  OK - every setting is read, and every name contract "
                      "holds.")
            print()
            return EXIT_ERROR if report["contract"] else EXIT_OK

        # --- rollback (improvement #5) ---
        if args.rollback:
            backups = autotuner.list_backups()
            if not backups:
                print("No my.cnf backups found.")
                return EXIT_OK
            print("Available backups (newest first):")
            for b in backups:
                print(f"  {b}")
            rb = autotuner.rollback(
                backup_file=args.rollback_file, auto_apply=args.yes
            )
            if rb.get("restored"):
                # H7: restoring the file is only half the job. If the service did
                # not come back up, the database may be DOWN — never report that
                # as success to a cron wrapper or monitoring check.
                if rb.get("restart_performed"):
                    print("Rollback complete: config restored and MySQL/MariaDB "
                          "confirmed back up.")
                    return EXIT_OK
                if rb.get("manual_restart_required"):
                    print(
                        f"Rollback: config restored; restart SKIPPED ({rb.get('restart_skipped_cluster')}). "
                        "Perform a topology-aware restart, or re-run with --allow-cluster-restart."
                    )
                    return EXIT_PENDING
                print(
                    "ROLLBACK INCOMPLETE: the previous config was restored but the "
                    "service did NOT come back up. The database may be DOWN — "
                    "manual intervention required."
                )
                return EXIT_ERROR
            print(f"Rollback not performed: {rb.get('reason', 'unknown')}")
            return EXIT_ERROR

        # --- diff preview (improvement #2) ---
        if args.diff:
            results = autotuner.analyze_system(
                platform=args.platform, explain=False
            )
            diff_text = autotuner.generate_mycnf_diff(results["recommendations"])
            print(diff_text)
            return EXIT_PENDING if results["recommendations"] else EXIT_OK

        # --- dump-effective-config ---
        if args.dump_effective_config:
            out = autotuner.dump_effective_config(
                platform_override=(
                    args.platform if args.platform != "auto" else None
                ),
                profile_override=(
                    args.profile if args.profile != "balanced" else None
                ),
                output_format=args.output_format,
            )
            print(out)
            return EXIT_OK

        # --- analyze ---
        if args.analyze:
            results = autotuner.analyze_system(
                show_evidence=args.show_evidence,
                platform=args.platform,
                explain=args.explain,
            )

            # --top-databases: collect per-schema activity metrics
            top_db_data = None
            if args.top_databases:
                try:
                    from core.top_databases import collect_top_databases, mock_top_databases
                    connector = autotuner.data_collector.mysql_connector
                    if connector and getattr(connector, 'connection', None):
                        top_db_data = collect_top_databases(
                            connector, limit=args.top_db_limit
                        )
                    elif args.dry_run:
                        # In dry-run mode use realistic mock data
                        top_db_data = mock_top_databases()
                    else:
                        # Connector not available — report unavailable, do NOT fabricate mock data
                        autotuner.logger.warning(
                            "--top-databases: no live MySQL connection available; "
                            "metrics unavailable"
                        )
                        top_db_data = {
                            "status": "unavailable",
                            "reason": "No live MySQL connection available",
                            "rows": [],
                        }
                except Exception as _tdb_exc:
                    autotuner.logger.warning(
                        f"--top-databases collection failed: {_tdb_exc}"
                    )
                    top_db_data = {
                        "status": "unavailable",
                        "reason": str(_tdb_exc),
                        "rows": [],
                    }

            # 'console' is the default — ConsoleRenderer handles it;
            # other formats (json, html, text) fall through to ReportGenerator
            report = autotuner.generate_report(
                results,
                output_format=args.format,
                explain=args.explain,
                top_databases=top_db_data,
            )
            print(report)
            exit_code = EXIT_PENDING if results.get("recommendations") else EXIT_OK

        # --- file-limit-check ---
        elif args.file_limit_check:
            results = autotuner.check_file_limits(args.platform)
            print(f"\n{'=' * 50}")
            print(f"  FILE LIMIT CHECK RESULTS")
            print(f"{'=' * 50}")
            print(f"  Platform              : {results['platform']}")
            print(f"  Table count           : {results['table_count']}")
            print(
                f"  Current open files    : "
                f"{results['current_open_files_limit']}"
            )
            print(
                f"  Adjustment required   : "
                f"{results['file_limit_required']}"
            )
            rec = results.get("recommendation")
            if rec:
                print(f"\n  Recommendation:")
                print(f"    Parameter : {rec['parameter']}")
                print(f"    Value     : {rec['recommended_value']}")
                print(f"    Reason    : {rec['reason']}")
                print(f"    Confidence: {rec.get('confidence', 0):.2f}")
                if rec.get("systemd_edit_required"):
                    print(f"    Systemd modification required:")
                    for cmd in rec.get("systemd_commands", []):
                        print(f"      $ {cmd}")
            exit_code = EXIT_PENDING if results.get("file_limit_required") else EXIT_OK

        # --- optimize ---
        elif args.optimize:
            results = autotuner.optimize_system(
                args.pass_number, args.dry_run,
                auto_apply=args.yes, explain=args.explain,
                dynamic_only=args.dynamic_only,
                platform=args.platform,
            )
            print(
                f"Optimisation complete: {results['mode']} "
                f"(Pass {results['pass_number']})"
            )
            if results["mode"] == "applied":
                # H7: distinguish applied / pending / rolled-back / failed.
                exit_code = exit_code_for_application(results.get("application", {}))
            elif results["mode"] == "dry_run":
                exit_code = (
                    EXIT_PENDING if results.get("recommendations_count", 0) else EXIT_OK
                )
            elif results["mode"] == "cancelled":
                # The user declined at the prompt; recommendations still stand.
                exit_code = (
                    EXIT_PENDING
                    if results.get("analysis", {}).get("recommendations")
                    else EXIT_OK
                )

        # --- multi-pass ---
        elif args.multi_pass:
            results = autotuner.multi_pass_optimization(
                args.max_passes, args.dry_run, explain=args.explain,
                auto_apply=args.yes, platform=args.platform,
            )
            print(
                f"Multi-pass optimisation complete: "
                f"{len(results['passes'])} passes"
            )
            # H7: the worst outcome across all passes wins. A rollback in pass 2
            # must not be masked by a successful pass 1.
            pass_codes = []
            for p in results["passes"]:
                if p.get("mode") == "applied":
                    pass_codes.append(exit_code_for_application(p.get("application", {})))
                elif p.get("recommendations_count", 0) or p.get("analysis", {}).get("recommendations"):
                    pass_codes.append(EXIT_PENDING)
            if EXIT_ERROR in pass_codes:
                exit_code = EXIT_ERROR
            elif EXIT_APPLIED in pass_codes:
                exit_code = EXIT_APPLIED
            elif EXIT_PENDING in pass_codes:
                exit_code = EXIT_PENDING
            else:
                exit_code = EXIT_OK

        else:
            parser.print_help()

        return exit_code

    except KeyboardInterrupt:
        print("\nOperation cancelled by user")
        return EXIT_ERROR
    except Exception as exc:
        print(f"Error: {exc}")
        if args.verbose:
            import traceback

            traceback.print_exc()
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())

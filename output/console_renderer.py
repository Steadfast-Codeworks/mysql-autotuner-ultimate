"""
console_renderer.py  —  Rich human-friendly console output for MySQL Auto-Tuner
================================================================================
Produces the structured, ANSI-coloured terminal report shown in the design spec.

Features
--------
  * Health badges  [ OK ] / [ WARN ] / [ CRIT ] / [ INFO ]  with ANSI colour
  * Auto-detects TTY; strips colour codes when output is piped to a file
  * Structured sections: System Health, Database Profile, Recommendations,
    Evidence Base, footer
  * LTS / EOL / Maintenance tags on the DB version line
  * ASCII bar charts for scenario coverage
  * "What was checked" explanation when no recommendations are generated
  * ANSI stripped automatically when saving the report to disk

Author: R.L. Burger (Steadfast Codeworks)
Date: 2025-09-07
Last Updated: 2026-08-24
Version: 1.0.4
Copyright (c) 2026 R.L. Burger
Project: Steadfast Tools
Website: https://www.steadfasttools.com
License: MIT License
"""

import sys
import re
import os
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# ANSI helpers
# ---------------------------------------------------------------------------

_IS_TTY: bool = sys.stdout.isatty()


class _C:
    """ANSI colour codes — empty strings when not on a TTY."""

    if _IS_TTY:
        RESET   = "\033[0m"
        BOLD    = "\033[1m"
        DIM     = "\033[2m"
        # Foreground
        RED     = "\033[31m"
        GREEN   = "\033[32m"
        YELLOW  = "\033[33m"
        CYAN    = "\033[36m"
        WHITE   = "\033[37m"
        # Background (used for badges)
        BG_RED  = "\033[41m"
        BG_GRN  = "\033[42m"
        BG_YLW  = "\033[43m"
        BG_BLU  = "\033[44m"
    else:
        RESET = BOLD = DIM = ""
        RED = GREEN = YELLOW = CYAN = WHITE = ""
        BG_RED = BG_GRN = BG_YLW = BG_BLU = ""


def _strip_ansi(text: str) -> str:
    """Remove all ANSI escape sequences from *text*."""
    return re.sub(r"\033\[[0-9;]*m", "", text)


def _badge(level: str) -> str:
    """Return a coloured badge string for the given level."""
    level = level.upper()
    if level == "OK":
        return f"{_C.GREEN}{_C.BOLD}[ OK   ]{_C.RESET}"
    if level in ("WARN", "WARNING"):
        return f"{_C.YELLOW}{_C.BOLD}[ WARN ]{_C.RESET}"
    if level in ("CRIT", "DANGER", "CRITICAL"):
        return f"{_C.RED}{_C.BOLD}[ CRIT ]{_C.RESET}"
    # INFO / default
    return f"{_C.CYAN}[ INFO ]{_C.RESET}"


# ---------------------------------------------------------------------------
# LTS / EOL / Maintenance version tags
# ---------------------------------------------------------------------------

# MariaDB LTS branches and their status
_MARIADB_STATUS: Dict[str, str] = {
    "10.5":  "EOL",
    "10.6":  "LTS",
    "10.11": "LTS",
    "11.4":  "LTS",
    "11.2":  "Maintenance",
    "11.3":  "Maintenance",
}

# MySQL status
_MYSQL_STATUS: Dict[str, str] = {
    "8.0":  "LTS",
    "8.4":  "LTS",
    "9.0":  "Innovation",
}


def _version_tag(compat_info: Dict[str, Any]) -> str:
    """Return a coloured version tag string, e.g. '[LTS]' or '[EOL]'."""
    branch = compat_info.get("branch", "")
    is_mariadb = compat_info.get("is_mariadb", False)
    status_map = _MARIADB_STATUS if is_mariadb else _MYSQL_STATUS
    status = status_map.get(branch, "")
    if not status:
        return ""
    colour = {
        "LTS":         _C.GREEN,
        "EOL":         _C.RED,
        "Maintenance": _C.YELLOW,
        "Innovation":  _C.CYAN,
    }.get(status, _C.WHITE)
    return f"  {colour}{_C.BOLD}[{status}]{_C.RESET}"


# ---------------------------------------------------------------------------
# ASCII bar chart
# ---------------------------------------------------------------------------

# Glyph set — falls back to ASCII when stdout cannot encode the fancy
# characters (POSIX/C locale: cron, systemd units, minimal containers), so the
# report degrades gracefully instead of raising UnicodeEncodeError or printing a
# wall of '?'.
try:
    from utils.safe_io import supports_unicode as _supports_unicode
except ImportError:  # pragma: no cover - direct-script fallback
    def _supports_unicode(sample="\u2500\u2713\u2717"):
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        try:
            sample.encode(enc)
            return True
        except (UnicodeEncodeError, LookupError, AttributeError):
            return False

_UNICODE_OK = _supports_unicode("\u2588\u2500\u2713\U0001f4a1")
_BAR_CHAR  = "\u2588" if _UNICODE_OK else "#"
_THIN_CHAR = "\u2500" if _UNICODE_OK else "-"
_TICK      = "\u2713" if _UNICODE_OK else "OK"
_TIP_ICON  = "\U0001f4a1" if _UNICODE_OK else "TIP:"
_BAR_WIDTH = 20   # max bar width in characters


def _bar(value: int, max_value: int) -> str:
    """Return a coloured ASCII bar proportional to value/max_value."""
    if max_value == 0:
        return ""
    filled = int(round(_BAR_WIDTH * value / max_value))
    bar = _BAR_CHAR * filled
    return f"{_C.CYAN}{bar}{_C.RESET}"


# ---------------------------------------------------------------------------
# Layout helpers
# ---------------------------------------------------------------------------

_WIDTH = 72


def _rule(char: str = "-") -> str:
    return char * _WIDTH


def _section_header(title: str) -> str:
    return f"\n{_C.CYAN}{_C.BOLD}{title}{_C.RESET}\n{_rule()}"


def _kv(label: str, value: str, width: int = 18) -> str:
    return f"  {label:<{width}}: {value}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class ConsoleRenderer:
    """
    Renders a rich, human-friendly console report from analysis_results.

    Usage
    -----
        renderer = ConsoleRenderer()
        report   = renderer.render(analysis_results)
        print(report)
        # Save to disk (ANSI stripped automatically)
        renderer.save(report, "/var/log/mysql-autotuner/mysql-autotuner.log")
    """

    def render(
        self,
        analysis_results: Dict[str, Any],
        explain: bool = False,
        top_databases: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """Build and return the full console report string."""
        lines: List[str] = []

        metrics         = analysis_results.get("metrics", {})
        recommendations = analysis_results.get("recommendations", [])
        compat_info     = analysis_results.get("db_version", {})
        platform        = analysis_results.get("platform_detected", "unknown")
        migration_state = analysis_results.get("migration_state", "none")
        peak_hour       = analysis_results.get("peak_hour_state", False)
        anomalies       = analysis_results.get("detected_anomalies") or []
        current_pass    = analysis_results.get("current_pass", 1)
        file_limit_req  = analysis_results.get("file_limit_required", False)
        evidence_base   = analysis_results.get("evidence_base", {})
        safety_checks   = analysis_results.get("safety_checks", {})

        lines += self._render_system_health(metrics)
        lines += self._render_db_profile(
            metrics, compat_info, platform, migration_state,
            peak_hour, anomalies, current_pass, file_limit_req,
            recommendations=recommendations
        )
        lines += self._render_recommendations(
            recommendations, metrics, compat_info, platform,
            explain=explain, anomalies=anomalies, safety_checks=safety_checks,
        )
        # Memory footprint estimate (improvement #4)
        footprint = analysis_results.get("memory_footprint")
        if footprint:
            lines += self._render_memory_footprint(footprint)
        # Optional --top-databases section (inserted before evidence base)
        if top_databases is not None:
            lines += self._render_top_databases(top_databases)
        lines += self._render_evidence_base(evidence_base, platform)
        lines += self._render_footer()

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Memory footprint (improvement #4)
    # ------------------------------------------------------------------
    def _render_memory_footprint(self, fp: Dict[str, Any]) -> List[str]:
        lines = [_section_header("WORST-CASE MEMORY FOOTPRINT")]
        badge = _badge(fp.get("verdict", "OK"))

        cur_gb = fp.get("current_global_buffers_mb", fp.get("global_buffers_mb", 0))
        prop_gb = fp.get("proposed_global_buffers_mb", fp.get("global_buffers_mb", 0))

        cur_pc = fp.get("current_per_connection_mb", fp.get("per_connection_mb", 0))
        prop_pc = fp.get("proposed_per_connection_mb", fp.get("per_connection_mb", 0))

        cur_mc = fp.get("current_max_connections", fp.get("max_connections", 0))
        prop_mc = fp.get("proposed_max_connections", fp.get("max_connections", 0))

        cur_wc = fp.get("current_footprint_mb", fp.get("current_worst_case_mb", fp.get("worst_case_mb", 0)))
        cur_pct = fp.get("current_pct", fp.get("current_worst_case_pct_of_ram", fp.get("worst_case_pct_of_ram", 0)))

        prop_wc = fp.get("proposed_footprint_mb", fp.get("proposed_worst_case_mb", fp.get("worst_case_mb", 0)))
        prop_pct = fp.get("proposed_pct", fp.get("proposed_worst_case_pct_of_ram", fp.get("worst_case_pct_of_ram", 0)))

        total_ram = fp.get("total_ram_mb", 0)

        if cur_gb != prop_gb:
            lines.append(_kv(
                "Global buffers",
                f"{cur_gb:.0f} MB → {prop_gb:.0f} MB  (buffer pool + key buffer)"
            ))
        else:
            lines.append(_kv(
                "Global buffers",
                f"{prop_gb:.0f} MB  (buffer pool + key buffer)"
            ))

        if cur_pc != prop_pc or cur_mc != prop_mc:
            lines.append(_kv(
                "Per-connection",
                f"{cur_pc:.2f} MB × {cur_mc} max_connections → {prop_pc:.2f} MB × {prop_mc} max_connections"
            ))
        else:
            lines.append(_kv(
                "Per-connection",
                f"{prop_pc:.2f} MB × {prop_mc} max_connections"
            ))

        if cur_wc != prop_wc or cur_pct != prop_pct:
            lines.append(_kv(
                "Worst case",
                f"{badge}  Current: {cur_wc:.0f} MB ({cur_pct:.1f}%) → Proposed: {prop_wc:.0f} MB ({prop_pct:.1f}%) of {total_ram} MB RAM"
            ))
        else:
            lines.append(_kv(
                "Worst case",
                f"{badge}  {prop_wc:.0f} MB = {prop_pct:.1f}% of {total_ram} MB RAM"
            ))

        lines.append(
            "  (global + max_connections × per-connection buffers — the classic "
            "OOM-risk check)"
        )
        return lines

    # ------------------------------------------------------------------
    # Section 1 — System Health
    # ------------------------------------------------------------------
    def _render_system_health(self, metrics: Dict[str, Any]) -> List[str]:
        lines = [_rule()]

        total_ram_mb  = metrics.get("total_ram_mb", 0) or metrics.get("system_total_ram_mb", 0)
        free_ram_mb   = metrics.get("free_ram_mb", 0)  or metrics.get("system_available_ram_mb", 0)
        used_pct      = int(100 * (total_ram_mb - free_ram_mb) / total_ram_mb) if total_ram_mb else 0

        # Memory badge
        mem_level = "OK" if used_pct < 80 else ("WARN" if used_pct < 90 else "CRIT")
        lines.append(
            f"{_badge(mem_level)}  Memory        "
            f"{total_ram_mb:,} MB total | {free_ram_mb:,} MB free ({used_pct}% used)"
        )

        # CPU load
        load1  = metrics.get("system_load_1min",  0.0)
        load5  = metrics.get("system_load_5min",  0.0)
        load15 = metrics.get("system_load_15min", 0.0)
        cores  = metrics.get("system_cpu_cores",  1) or 1
        load_ratio = load1 / cores
        cpu_level = "OK" if load_ratio < 0.7 else ("WARN" if load_ratio < 1.2 else "CRIT")
        lines.append(
            f"{_badge(cpu_level)}  CPU Load      "
            f"{load1:.2f} / {load5:.2f} / {load15:.2f}  ({cores} cores)"
        )

        # Buffer pool
        bp_size_mb  = int(metrics.get("innodb_buffer_pool_size_mb", 0) or 0)
        bp_pct      = int(100 * bp_size_mb / total_ram_mb) if total_ram_mb else 0
        bp_level    = "OK" if 40 <= bp_pct <= 75 else "WARN"
        lines.append(
            f"{_badge(bp_level)}  Buffer Pool   "
            f"{bp_size_mb:,} MB allocated ({bp_pct}% of total RAM)"
        )

        # Connections
        max_conn     = int(metrics.get("max_connections", 0) or 0)
        peak_threads = int(metrics.get("max_used_connections", 0) or 0)
        conn_pct     = int(100 * peak_threads / max_conn) if max_conn else 0
        conn_level   = "OK" if conn_pct < 70 else ("WARN" if conn_pct < 85 else "CRIT")
        lines.append(
            f"{_badge(conn_level)}  Connections   "
            f"{peak_threads} peak / {max_conn} configured ({conn_pct}% peak utilization)"
        )

        return lines

    # ------------------------------------------------------------------
    # Section 2 — Database Profile
    # ------------------------------------------------------------------
    def _render_db_profile(
        self,
        metrics: Dict[str, Any],
        compat_info: Dict[str, Any],
        platform: str,
        migration_state: str,
        peak_hour: bool,
        anomalies: List[str],
        current_pass: int,
        file_limit_req: bool,
        recommendations: Optional[List[Dict[str, Any]]] = None,
    ) -> List[str]:
        lines = [_section_header("DATABASE PROFILE")]

        # Engine / version
        version_str = metrics.get("mysql_version", compat_info.get("version_string", "Unknown"))
        vtag = _version_tag(compat_info)
        lines.append(_kv("Engine", f"{version_str}{vtag}"))

        # Platform — resolve confidence from recommendations' platform_info
        raw_conf = None
        for r in (recommendations or []):
            if "platform_info" in r and "platform_confidence" in r["platform_info"]:
                raw_conf = r["platform_info"]["platform_confidence"]
                break
        if raw_conf is None:
            raw_conf = metrics.get("platform_confidence", 0.9 if platform not in ("default", "unknown") else 0.6)

        platform_conf_pct = int(raw_conf * 100) if raw_conf <= 1.0 else int(raw_conf)
        lines.append(_kv("Platform", f"{platform.upper()}  (detected with {platform_conf_pct}% confidence)"))

        # Tables
        total_t  = int(metrics.get("total_tables", 0) or 0)
        innodb_t = int(metrics.get("innodb_tables", 0) or 0)
        myisam_t = int(metrics.get("myisam_tables", 0) or 0)
        lines.append(_kv("Tables", f"{total_t} total  —  {innodb_t} InnoDB / {myisam_t} MyISAM"))

        # Peak hour
        ph_text = "Yes — peak-hour thresholds applied" if peak_hour else "No — normal thresholds applied"
        lines.append(_kv("Peak Hour", ph_text))

        # Migration
        if migration_state in ("post_migration", "migration_detected"):
            mig_text = f"Post-migration detected — InnoDB reallocation applied"
        elif total_t == 0:
            mig_text = "N/A — no user tables found"
        else:
            mig_text = "None detected"
        lines.append(_kv("Migration", mig_text))

        # Analysis pass + file limit
        fl_text = "required — systemd edit needed" if file_limit_req else "not required"
        lines.append(_kv("Analysis", f"Pass {current_pass}  |  File limit check: {fl_text}"))

        # Anomalies
        anom_text = ", ".join(anomalies) if anomalies else "None detected"
        lines.append(_kv("Anomalies", anom_text))

        return lines

    # ------------------------------------------------------------------
    # Section 3 — Recommendations
    # ------------------------------------------------------------------
    def _render_recommendations(
        self,
        recommendations: List[Dict[str, Any]],
        metrics: Dict[str, Any],
        compat_info: Dict[str, Any],
        platform: str,
        explain: bool = False,
        anomalies: Optional[List[str]] = None,
        safety_checks: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        lines = []
        safety_checks = safety_checks or {}
        filtered_count = safety_checks.get("filtered_count", 0)

        if recommendations:
            lines.append(_section_header(
                f"RECOMMENDATIONS  — {len(recommendations)} change(s) suggested"
            ))
            for rec in recommendations:
                lines += self._render_single_rec(rec, explain=explain)
        else:
            if filtered_count > 0:
                lines.append(_section_header(
                    f"RECOMMENDATIONS  — {_C.YELLOW}{filtered_count} recommendation(s) suppressed by safety guardrails{_C.RESET}"
                ))
                lines.append(
                    f"  {_badge('WARN')}  {safety_checks.get('total_recommendations', filtered_count)} recommendation(s) generated, {filtered_count} suppressed by safety guardrails (see log)."
                )
            else:
                lines.append(_section_header(
                    f"RECOMMENDATIONS  — {_C.GREEN}system looks healthy{_C.RESET}"
                ))
                lines.append(
                    f"  {_C.GREEN}{_TICK}{_C.RESET}  Your database configuration is healthy for the current workload."
                )
            lines.append("")
            lines.append("  What was checked:")
            lines.append(_rule())
            lines += self._render_what_was_checked(
                metrics, compat_info, platform,
                anomalies=anomalies, safety_checks=safety_checks,
            )
            lines.append("")
            lines.append(
                f"  {_C.DIM}TIP: Re-run after peak traffic hours for a more complete picture.{_C.RESET}"
            )
            lines.append(
                f"  {_C.DIM}     Use --multi-pass for progressive tuning across multiple passes.{_C.RESET}"
            )

        return lines

    def _render_single_rec(self, rec: Dict[str, Any], explain: bool = False) -> List[str]:
        """Render one recommendation with badge, parameter, values, and explanation."""
        lines = []
        param     = rec.get("parameter", "unknown")
        cur_val   = rec.get("current_value", "N/A")
        rec_val   = rec.get("recommended_value", "N/A")
        reason    = rec.get("reason", "")
        conf      = rec.get("confidence", 0.0)
        conf_pct  = int(conf * 100) if conf <= 1.0 else int(conf)
        # N-4: Support priority, with fallback to impact (used across engines) and legacy severity
        priority  = str(
            rec.get("priority")
            or rec.get("impact")
            or rec.get("severity")
            or "medium"
        ).upper()

        badge_level = {
            "CRITICAL": "CRIT", "HIGH": "CRIT",
            "WARNING": "WARN",  "MEDIUM": "WARN",
            "INFO": "INFO",     "LOW": "OK",
        }.get(priority, "INFO")
        lines.append(
            f"\n  {_badge(badge_level)}  {_C.BOLD}{param}{_C.RESET}"
        )
        lines.append(f"    Current   : {cur_val}")
        lines.append(f"    Suggested : {_C.GREEN}{rec_val}{_C.RESET}  (confidence: {conf_pct}%)")
        if reason:
            lines.append(f"    Reason    : {reason}")

        # Educational explanation (--explain output) — only shown when explain=True
        explanation = rec.get("explanation", "")
        if explain and explanation:
            lines.append("")
            for exp_line in explanation.splitlines():
                lines.append(f"    {_C.DIM}{exp_line}{_C.RESET}")

        return lines

    def _render_what_was_checked(
        self,
        metrics: Dict[str, Any],
        compat_info: Dict[str, Any],
        platform: str,
        anomalies: Optional[List[str]] = None,
        safety_checks: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        """Render the 'What was checked' table when there are no recommendations."""
        lines = []

        total_ram_mb  = metrics.get("total_ram_mb", 0) or metrics.get("system_total_ram_mb", 0)
        free_ram_mb   = metrics.get("free_ram_mb", 0)  or metrics.get("system_available_ram_mb", 0)
        used_pct      = int(100 * (total_ram_mb - free_ram_mb) / total_ram_mb) if total_ram_mb else 0

        bp_size_mb    = int(metrics.get("innodb_buffer_pool_size_mb", 0) or 0)
        bp_pct        = int(100 * bp_size_mb / total_ram_mb) if total_ram_mb else 0
        total_t       = int(metrics.get("total_tables", 0) or 0)
        max_conn      = int(metrics.get("max_connections", 0) or 0)
        peak_threads  = int(metrics.get("max_used_connections", 0) or 0)
        conn_pct      = int(100 * peak_threads / max_conn) if max_conn else 0
        load1         = metrics.get("system_load_1min", 0.0)
        cores         = metrics.get("system_cpu_cores", 1) or 1
        load_per_core = load1 / cores

        # M-6: Read anomalies from argument or fallback to metrics
        if anomalies is None:
            anomalies = metrics.get("detected_anomalies") or []
        safety_checks = safety_checks or {}
        filtered_count = safety_checks.get("filtered_count", 0)

        # Table Statistics scan check (M-6 / C-2)
        if metrics.get("table_stats_uncollected"):
            lines.append(
                f"  {_badge('WARN')}  Table Statistics    "
                "Size scan timed out / uncollected; table metrics may be incomplete"
            )

        # InnoDB Buffer Pool
        if 40 <= bp_pct <= 75:
            bp_badge = "OK"
            bp_note  = f"{bp_size_mb:,} MB ({bp_pct}% of RAM — within optimal 40–75% range)"
        else:
            bp_badge = "WARN"
            bp_note  = f"{bp_size_mb:,} MB (outside acceptable range for {total_t} tables)"
        lines.append(f"  {_badge(bp_badge)}  InnoDB Buffer Pool  {bp_note}")

        # Max Connections
        conn_badge = "OK" if conn_pct < 70 else "WARN"
        lines.append(
            f"  {_badge(conn_badge)}  Max Connections     "
            f"{peak_threads} / {max_conn} peak used ({conn_pct}% utilization)"
        )

        # CPU Load
        cpu_badge = "OK" if load_per_core < 0.7 else "WARN"
        lines.append(
            f"  {_badge(cpu_badge)}  CPU Load            "
            f"{load1:.2f} 1-min avg ({load_per_core:.2f}x per core)"
        )

        # Memory Pressure
        mem_badge = "OK" if used_pct < 80 else "WARN"
        lines.append(
            f"  {_badge(mem_badge)}  Memory Pressure     "
            f"{used_pct}% used — {free_ram_mb:,} MB available"
        )

        # Anomaly Detection (M-6)
        anom_badge = "WARN" if anomalies else "OK"
        anom_text  = ", ".join(anomalies) if anomalies else "None detected"
        lines.append(f"  {_badge(anom_badge)}  Anomaly Detection   {anom_text}")

        # Safety Guardrails (M-6)
        if filtered_count > 0:
            lines.append(
                f"  {_badge('WARN')}  Safety Guardrails   "
                f"{filtered_count} recommendation(s) generated but suppressed by safety guardrails (see log)"
            )

        # Migration State
        total_t = int(metrics.get("total_tables", 0) or 0)
        if total_t == 0:
            mig_note = "N/A — no user tables found"
        else:
            innodb_t = int(metrics.get("innodb_tables", 0) or 0)
            mig_note = f"{innodb_t}/{total_t} tables are InnoDB"
        lines.append(f"  {_badge('INFO')}  Migration State     {mig_note}")

        return lines

    # ------------------------------------------------------------------
    # Section 3b — Top Database Activity  (--top-databases)
    # ------------------------------------------------------------------

    def _render_top_databases(
        self, top_databases: Any
    ) -> List[str]:
        """
        Render the TOP DATABASE ACTIVITY table.
        Accepts structured contract dict {"status": ..., "rows": [...]} or legacy row list.
        """
        from core.top_databases import build_top_db_tip  # local import avoids circular deps

        lines = [_section_header("TOP DATABASE ACTIVITY")]

        if not top_databases:
            lines.append("  No user databases found or performance_schema is unavailable.")
            return lines

        status = top_databases.get("status", "ok") if isinstance(top_databases, dict) else "ok"
        rows = top_databases.get("rows", []) if isinstance(top_databases, dict) else top_databases
        reason = top_databases.get("reason", "") if isinstance(top_databases, dict) else ""

        if status == "unavailable":
            lines.append(
                f"  {_badge('WARN')}  {_C.YELLOW}Top database metrics unavailable: {reason or 'collection failed'}{_C.RESET}"
            )
            return lines

        if status == "mock":
            lines.append(f"  {_C.CYAN}[ MOCK DATA — DRY RUN ONLY ]{_C.RESET}")

        if not rows:
            lines.append("  No user databases found or performance_schema is unavailable.")
            return lines

        # ---- Column widths ----
        # Database name column: at least 22 chars, or longest name + 2
        db_w = max(22, max(len(r["database"]) for r in rows) + 2)
        # Header row
        hdr = (
            f"  {'Database':<{db_w}}  {'Queries/sec':>12}  {'Data Size':>10}"
            f"  {'Connections':>11}    Impact Score"
        )
        thin = "  " + _THIN_CHAR * (_WIDTH - 2)
        lines.append(f"{_C.BOLD}{hdr}{_C.RESET}")
        lines.append(thin)

        # Max values for proportional bar scaling
        max_qps   = max((r["queries_per_sec"] for r in rows), default=1) or 1
        max_score = max((r["impact_score"]    for r in rows), default=1) or 1

        # Impact label colours
        label_colour = {
            "HIGH":   _C.RED,
            "MEDIUM": _C.YELLOW,
            "LOW":    _C.GREEN,
        }

        for r in rows:
            db      = r["database"]
            qps     = r["queries_per_sec"]
            size_gb = r["data_size_gb"]
            conns   = r["connections"]
            score   = r["impact_score"]
            label   = r.get("impact_label", "LOW")

            qps_str  = f"{qps:,.0f}" if qps > 0 else "N/A"
            size_str = f"{size_gb:.1f} GB"
            lc       = label_colour.get(label, _C.WHITE)

            # Proportional bar (max 16 chars wide) based on impact_score
            bar_filled = max(1, int(round(16 * score / max_score)))
            bar_str    = f"{lc}{_C.BOLD}{_BAR_CHAR * bar_filled}{_C.RESET}"
            label_str  = f"{lc}{_C.BOLD}{label}{_C.RESET}"

            row_line = (
                f"  {db:<{db_w}}  {qps_str:>12}  {size_str:>10}"
                f"  {conns:>11}    {bar_str} {label_str}"
            )
            lines.append(row_line)

        lines.append("")

        # ---- Actionable tip ----
        tip = build_top_db_tip(rows)
        if tip:
            tip_icon = f"{_C.YELLOW}{_TIP_ICON}{_C.RESET}"  # 💡
            # Wrap tip text at ~65 chars
            tip_words  = tip.split()
            tip_lines: List[str] = []
            current    = ""
            for word in tip_words:
                if len(current) + len(word) + 1 > 65:
                    tip_lines.append(current)
                    current = word
                else:
                    current = (current + " " + word).strip()
            if current:
                tip_lines.append(current)

            lines.append(f"  {tip_icon} TIP: {tip_lines[0]}")
            for tl in tip_lines[1:]:
                lines.append(f"       {tl}")

        return lines

    # ------------------------------------------------------------------
    # Section 4 — Evidence Base
    # ------------------------------------------------------------------
    def _render_evidence_base(
        self, evidence_base: Dict[str, Any], platform: str
    ) -> List[str]:
        lines = [_section_header("EVIDENCE BASE")]

        total_cases = evidence_base.get("total_cases", 66)
        lines.append(f"  Recommendations drawn from {total_cases} real-world production cases.")
        lines.append("")

        # N-5: Platform distribution (fallback to platform_breakdown if present)
        platform_breakdown = evidence_base.get(
            "platform_distribution",
            evidence_base.get("platform_breakdown", {
                "directadmin": 28,
                "cpanel":      22,
                "litespeed":   16,
            })
        )
        lines.append("  Platform breakdown:")
        for plat, count in platform_breakdown.items():
            marker = f"  {_C.CYAN}◄ this server{_C.RESET}" if plat.lower() == platform.lower() else ""
            lines.append(f"    {plat:<14} {count:>3} cases{marker}")

        lines.append("")

        # Scenario coverage bar chart
        scenario_coverage = evidence_base.get("scenario_coverage", {
            "Anomaly Handling":  25,
            "Multi Pass":        18,
            "Peak Hour":         15,
            "Myisam Migration":  12,
            "File Limits":        8,
        })
        max_val = max(scenario_coverage.values()) if scenario_coverage else 1
        lines.append("  Scenario coverage:")
        for scenario, count in scenario_coverage.items():
            bar = _bar(count, max_val)
            lines.append(f"    {scenario:<22} {count:>3} cases  {bar}")

        return lines

    # ------------------------------------------------------------------
    # Footer
    # ------------------------------------------------------------------
    def _render_footer(self) -> List[str]:
        return [
            "",
            _rule("="),
            f"  {_C.BOLD}Steadfast Codeworks{_C.RESET}  |  Automate. Simplify. Steadfast.",
            _rule("="),
        ]

    # ------------------------------------------------------------------
    # Disk save (ANSI stripped)
    # ------------------------------------------------------------------
    def save(self, report: str, path: str) -> None:
        """Save *report* to *path*, stripping all ANSI codes first (safe-IO hardened)."""
        clean = _strip_ansi(report)
        try:
            from utils.safe_io import secure_open_write
            with secure_open_write(path) as fh:
                fh.write(clean)
        except ImportError:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(clean)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_size_mb(value: Any) -> int:
        """Convert a MySQL size string like '128M', '2G', '512K', '2GB' to MB (treating bare numbers as MB unless huge)."""
        if value is None:
            return 0
        if isinstance(value, (int, float)):
            val = float(value)
            if val > 10000000:
                return int(val / (1024 * 1024))
            return int(val)
        s = str(value).strip().upper()
        try:
            if s.endswith(("G", "GB")):
                num = s[:-2] if s.endswith("GB") else s[:-1]
                return int(float(num) * 1024)
            if s.endswith(("M", "MB")):
                num = s[:-2] if s.endswith("MB") else s[:-1]
                return int(float(num))
            if s.endswith(("K", "KB")):
                num = s[:-2] if s.endswith("KB") else s[:-1]
                return max(1, int(float(num) / 1024))
            val = float(s)
            if val > 10000000:
                return int(val / (1024 * 1024))
            return int(val)
        except (ValueError, ZeroDivisionError, TypeError):
            return 0

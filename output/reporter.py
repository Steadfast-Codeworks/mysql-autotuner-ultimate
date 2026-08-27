#!/usr/bin/env python3
"""
Report Generator Module - v1.0.4
================================
Consolidated reporting engine for MySQL Auto-Tuner Ultimate.
Generates consistent reports across text, JSON, and HTML formats.

Author: R.L. Burger (Steadfast Codeworks)
Date: 2026-08-04
Last Updated: 2026-08-24
Version: 1.0.4
Copyright (c) 2026 R.L. Burger
Project: Steadfast Tools
Website: https://www.steadfasttools.com
License: MIT License
"""

import html
import os
import json
import logging
import sys
from typing import Dict, List, Any, Optional
from datetime import datetime
from pathlib import Path

try:
    from utils.version import TOOL_VERSION
except ImportError:  # pragma: no cover - direct-script fallback
    TOOL_VERSION = "1.0.4"

# Rich console renderer (ANSI-coloured, TTY-aware)
try:
    from output.console_renderer import ConsoleRenderer, _strip_ansi
except ImportError:
    try:
        from console_renderer import ConsoleRenderer, _strip_ansi
    except ImportError:
        ConsoleRenderer = None
        def _strip_ansi(t): return t


class ReportGenerator:
    """Generates optimization reports in multiple formats"""

    def __init__(self, output_config: Dict[str, Any] = None):
        output_config = output_config or {}
        self.output_config = output_config
        self.logger = logging.getLogger(__name__)

        self.format = output_config.get('format', 'console')
        self.detailed = output_config.get('detailed', True)
        # M6: OFF by default. This used to default to True with no `output:`
        # section in the YAML to turn it off, so every single run — including
        # `--format json > report.json`, where the user had already chosen where
        # the output goes — silently dropped a timestamped file on disk with no
        # rotation and no mention in the README. An hourly cron produced ~8,760
        # files a year. Opt in with --save-report.
        self.save_to_disk = output_config.get('save_report', False)
        # Keep only the newest N reports when saving is enabled (0 = unlimited).
        self.max_saved_reports = int(output_config.get('max_saved_reports', 30))
        self._preferred_output_dir = output_config.get('output_dir', '/var/log/mysql-autotuner-reports')
        self._output_dir = None

    @property
    def output_dir(self) -> Optional[Path]:
        """Lazy resolution of reports directory (N-19) so /var/log is not touched on dry runs."""
        if self._output_dir is None:
            try:
                from utils.safe_io import choose_writable_dir
            except ImportError:
                from ..utils.safe_io import choose_writable_dir

            chosen = choose_writable_dir(self._preferred_output_dir, 'mysql-autotuner-reports')
            self._output_dir = Path(chosen) if chosen else None
            if self._output_dir is None and self.save_to_disk:
                self.logger.warning("No writable reports directory; disk saving disabled")
                self.save_to_disk = False
        return self._output_dir

    @output_dir.setter
    def output_dir(self, val: Any) -> None:
        self._output_dir = Path(val) if val else None

    # ------------------------------------------------------------------
    # Primary public API – called by the main auto-tuner script
    # ------------------------------------------------------------------
    def generate_report(
        self,
        metrics: Dict[str, Any],
        recommendations: List[Dict[str, Any]],
        analysis: Dict[str, Any] = None,
        explain: bool = False,
        output_format: str = 'console',
        top_databases=None,
    ) -> str:
        """
        Generate a full optimization report.

        Args:
            metrics: flat metrics dict.
            recommendations: list of recommendation dicts.
            analysis: optional analysis summary dict.
            explain: if True, include educational explanations.
            output_format: 'text', 'json', or 'html'.
            top_databases: optional list of per-db activity dicts (--top-databases).

        Returns:
            The report as a string.
        """
        analysis = analysis or {}

        # L5: 'console' and 'text' are advertised as distinct choices, so they
        # now differ. Previously BOTH routed to the ConsoleRenderer, leaving
        # _generate_text_report (100 lines) unreachable unless its import
        # failed — a documented CLI option that silently did nothing different.
        #   console -> rich renderer (boxes, badges, bar charts)
        #   text    -> plain, stable, ANSI-free layout suited to scripting,
        #              diffing between runs, and pasting into a ticket
        if output_format == 'json':
            report = self._generate_json_report(
                metrics, recommendations, analysis, explain, top_databases
            )
        elif output_format == 'html':
            report = self._generate_html_report(
                metrics, recommendations, analysis, explain, top_databases
            )
        elif output_format == 'console' and ConsoleRenderer is not None:
            # Build the full analysis_results dict the ConsoleRenderer expects
            analysis_results = dict(analysis)
            analysis_results['metrics'] = metrics
            analysis_results['recommendations'] = recommendations
            renderer = ConsoleRenderer()
            report = renderer.render(
                analysis_results, explain=explain, top_databases=top_databases
            )
        else:
            report = self._generate_text_report(
                metrics, recommendations, analysis, explain, top_databases
            )

        # Save to disk if configured — strip ANSI codes for file output.
        # M7: never let a disk-side failure destroy the report the user asked
        # for. secure_open_write raises OSError on a symlinked path (the safe-IO
        # hardening working correctly) and on ENOSPC; unguarded, that escaped to
        # main()'s catch-all and turned a completed analysis into `Error: …` and
        # exit 1, discarding the output entirely.
        if self.save_to_disk:
            try:
                self.save_report(report, fmt=output_format)
            except (OSError, PermissionError) as exc:
                self.logger.warning(
                    "Could not save report to disk (%s); the report above is "
                    "unaffected.", exc,
                )

        return report

    # Alias kept for backward compatibility
    def generate_analysis_report(self, metrics, analysis_results=None, recommendations=None, detailed=True):
        """Legacy method – wraps generate_report for backward compatibility."""
        analysis_results = analysis_results or {}
        recommendations = recommendations or []
        return self.generate_report(
            metrics=metrics,
            recommendations=recommendations,
            analysis=analysis_results,
            explain=False,
            output_format='text',
        )

    # ------------------------------------------------------------------
    # Text report
    # ------------------------------------------------------------------
    def _generate_text_report(
        self,
        metrics: Dict[str, Any],
        recommendations: List[Dict[str, Any]],
        analysis: Dict[str, Any],
        explain: bool,
        top_databases=None,
    ) -> str:
        lines: List[str] = []
        width = 80
        sep = '=' * width
        thin = '-' * width

        # Header
        lines.append(sep)
        lines.append(f'  STEADFAST MySQL/MariaDB Auto-Tuner  v{TOOL_VERSION}')
        lines.append(f'  Report generated: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
        lines.append(sep)
        lines.append('')

        # System overview
        lines.append('  SYSTEM OVERVIEW')
        lines.append(thin)
        lines.append(f'  Total RAM        : {metrics.get("total_ram_mb", 0)} MB')
        lines.append(f'  CPU Cores        : {metrics.get("cpu_cores", "N/A")}')
        lines.append(f'  Load Average     : {metrics.get("load_average_1m", 0):.2f} / '
                      f'{metrics.get("load_average_5m", 0):.2f} / '
                      f'{metrics.get("load_average_15m", 0):.2f}')
        lines.append(f'  Available RAM    : {metrics.get("available_ram_mb", 0)} MB')
        lines.append('')

        # MySQL overview
        raw = metrics.get('_raw_metrics', {})
        mysql_info = raw.get('mysql', {}) if isinstance(raw, dict) else {}
        ver = mysql_info.get('version', {}) if isinstance(mysql_info, dict) else {}
        lines.append('  MySQL / MariaDB OVERVIEW')
        lines.append(thin)
        lines.append(f'  Version          : {ver.get("version_string", "N/A")}')
        lines.append(f'  Buffer Pool      : {metrics.get("innodb_buffer_pool_size_mb", 0)} MB')
        lines.append(f'  Max Connections   : {metrics.get("max_connections", 0)}')
        lines.append(f'  Max Used Conns    : {metrics.get("max_used_connections", 0)}')
        lines.append(f'  Total Tables     : {metrics.get("total_tables", 0)}')
        lines.append(f'  InnoDB Tables    : {metrics.get("innodb_tables", 0)}')
        lines.append(f'  MyISAM Tables    : {metrics.get("myisam_tables", 0)}')
        lines.append('')

        # Recommendations
        lines.append('  RECOMMENDATIONS')
        lines.append(sep)

        if not recommendations:
            lines.append('  No recommendations generated.')
        else:
            for i, rec in enumerate(recommendations, 1):
                lines.append(f'  [{i}] {rec.get("parameter", "?")}')
                lines.append(f'      Current  : {rec.get("current_value", "N/A")}')
                lines.append(f'      Proposed : {rec.get("recommended_value", "N/A")}')
                lines.append(f'      Priority : {rec.get("priority") or rec.get("impact", "medium")}')
                conf = rec.get('confidence', 0)
                if isinstance(conf, (int, float)):
                    lines.append(f'      Confidence: {conf:.0%}')
                else:
                    lines.append(f'      Confidence: {conf}')
                lines.append(f'      Reason   : {rec.get("reason", "")}')

                # Safety notes
                for note in rec.get('safety_notes', []):
                    lines.append(f'      Safety   : {note}')

                # Educational explanation
                if explain:
                    explanation = rec.get('explanation', '')
                    if explanation:
                        lines.append('')
                        lines.append(f'      WHY THIS MATTERS:')
                        for para in explanation.split('\n'):
                            para = para.strip()
                            if para:
                                while len(para) > 68:
                                    idx = para[:68].rfind(' ')
                                    if idx == -1:
                                        idx = 68
                                    lines.append(f'      {para[:idx]}')
                                    para = para[idx:].strip()
                                lines.append(f'      {para}')

                lines.append(thin)

        # Top databases — the same data the console renderer shows. Without
        # this, `--top-databases --format text` would silently drop it, which is
        # exactly the gap M4 found in the JSON report.
        if top_databases:
            lines.append('')
            lines.append('  TOP DATABASE ACTIVITY')
            lines.append(thin)

            status = top_databases.get("status", "ok") if isinstance(top_databases, dict) else "ok"
            rows = top_databases.get("rows", []) if isinstance(top_databases, dict) else top_databases
            reason = top_databases.get("reason", "") if isinstance(top_databases, dict) else ""

            if status == "unavailable":
                lines.append(f'  [UNAVAILABLE] Top database metrics could not be collected: {reason or "Database unavailable"}')
            elif status == "mock":
                lines.append('  [MOCK DATA -- DRY RUN ONLY]')
                lines.append(
                    f'  {"Database":<32}{"Queries/s":>11}{"Size (GB)":>11}'
                    f'{"Conns":>7}{"Impact":>9}  Level'
                )
                lines.append(thin)
                for row in rows:
                    lines.append(
                        f'  {str(row.get("database", ""))[:31]:<32}'
                        f'{row.get("queries_per_sec", 0):>11}'
                        f'{row.get("data_size_gb", 0):>11}'
                        f'{row.get("connections", 0):>7}'
                        f'{row.get("impact_score", 0):>9}  '
                        f'{row.get("impact_label", "")}'
                    )
                try:
                    from core.top_databases import build_top_db_tip
                    tip = build_top_db_tip(rows)
                    if tip:
                        lines.append('')
                        lines.append(f'  TIP: {tip}')
                except Exception:
                    pass
            elif rows:
                lines.append(
                    f'  {"Database":<32}{"Queries/s":>11}{"Size (GB)":>11}'
                    f'{"Conns":>7}{"Impact":>9}  Level'
                )
                lines.append(thin)
                for row in rows:
                    lines.append(
                        f'  {str(row.get("database", ""))[:31]:<32}'
                        f'{row.get("queries_per_sec", 0):>11}'
                        f'{row.get("data_size_gb", 0):>11}'
                        f'{row.get("connections", 0):>7}'
                        f'{row.get("impact_score", 0):>9}  '
                        f'{row.get("impact_label", "")}'
                    )
                try:
                    from core.top_databases import build_top_db_tip
                    tip = build_top_db_tip(rows)
                    if tip:
                        lines.append('')
                        lines.append(f'  TIP: {tip}')
                except Exception:
                    pass
            else:
                lines.append('  No user databases found or performance_schema is unavailable.')

        # Analysis summary
        if analysis:
            lines.append('')
            lines.append('  ANALYSIS SUMMARY')
            lines.append(thin)
            for key, val in analysis.items():
                if not key.startswith('_'):
                    lines.append(f'  {key}: {val}')

        lines.append('')
        lines.append(sep)
        lines.append('  Steadfast Codeworks  |  Automate. Simplify. Steadfast.')
        lines.append(sep)
        return '\n'.join(lines)

    # ------------------------------------------------------------------
    # JSON report
    # ------------------------------------------------------------------
    def _generate_json_report(
        self,
        metrics: Dict[str, Any],
        recommendations: List[Dict[str, Any]],
        analysis: Dict[str, Any],
        explain: bool,
        top_databases=None,
    ) -> str:
        clean_metrics = {k: v for k, v in metrics.items() if not k.startswith('_')}

        report = {
            'meta': {
                'tool': 'Steadfast MySQL/MariaDB Auto-Tuner',
                'version': TOOL_VERSION,
                'timestamp': datetime.now().isoformat(),
                'explain_mode': explain,
            },
            'system': {
                'total_ram_mb': clean_metrics.get('total_ram_mb', 0),
                'cpu_cores': clean_metrics.get('cpu_cores', 0),
                'load_average': {
                    '1m': clean_metrics.get('load_average_1m', 0),
                    '5m': clean_metrics.get('load_average_5m', 0),
                    '15m': clean_metrics.get('load_average_15m', 0),
                },
            },
            'mysql': {
                'buffer_pool_mb': clean_metrics.get('innodb_buffer_pool_size_mb', 0),
                'max_connections': clean_metrics.get('max_connections', 0),
                'total_tables': clean_metrics.get('total_tables', 0),
                'innodb_tables': clean_metrics.get('innodb_tables', 0),
                'myisam_tables': clean_metrics.get('myisam_tables', 0),
            },
            'recommendations': [],
            'analysis': {k: v for k, v in analysis.items() if not k.startswith('_')},
        }

        for rec in recommendations:
            entry = {
                'parameter': rec.get('parameter', ''),
                'current_value': rec.get('current_value', ''),
                'recommended_value': rec.get('recommended_value', ''),
                'priority': rec.get('priority') or rec.get('impact', 'medium'),
                'confidence': rec.get('confidence', 0),
                'reason': rec.get('reason', ''),
                'restart_required': rec.get('restart_required', False),
                'safety_notes': rec.get('safety_notes', []),
            }
            if explain and rec.get('explanation'):
                entry['explanation'] = rec['explanation']
            report['recommendations'].append(entry)

        # M4: include per-database activity when --top-databases was requested,
        # so `--top-databases --format json` actually carries the data (the
        # README advertises exactly this pipeline).
        if top_databases:
            report['top_databases'] = top_databases

        return json.dumps(report, indent=2, default=str)

    # ------------------------------------------------------------------
    # HTML report
    # ------------------------------------------------------------------
    def _generate_html_report(
        self,
        metrics: Dict[str, Any],
        recommendations: List[Dict[str, Any]],
        analysis: Dict[str, Any],
        explain: bool,
        top_databases=None,
    ) -> str:
        html_parts: List[str] = []
        html_parts.append('<!DOCTYPE html><html lang="en"><head>')
        html_parts.append('<meta charset="UTF-8">')
        html_parts.append('<title>Steadfast MySQL Auto-Tuner Report</title>')
        html_parts.append('<style>')
        html_parts.append('body{font-family:monospace;background:#1a1a2e;color:#e0e0e0;padding:2em}')
        html_parts.append('h1{color:#00d4ff}h2{color:#00bfa6}')
        html_parts.append('table{border-collapse:collapse;width:100%;margin:1em 0}')
        html_parts.append('th,td{border:1px solid #333;padding:8px;text-align:left}')
        html_parts.append('th{background:#16213e;color:#00d4ff}')
        html_parts.append('tr:nth-child(even){background:#0f3460}')
        html_parts.append('.explain{background:#1a3a5c;padding:12px;border-left:4px solid #00d4ff;margin:8px 0}')
        html_parts.append('.high{color:#ff6b6b}.medium{color:#ffd93d}.low{color:#6bcb77}')
        html_parts.append('</style></head><body>')
        html_parts.append(f'<h1>Steadfast MySQL/MariaDB Auto-Tuner v{TOOL_VERSION}</h1>')
        html_parts.append(f'<p>Generated: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</p>')

        # System
        html_parts.append('<h2>System Overview</h2><table>')
        html_parts.append(f'<tr><td>Total RAM</td><td>{metrics.get("total_ram_mb", 0)} MB</td></tr>')
        html_parts.append(f'<tr><td>CPU Cores</td><td>{metrics.get("cpu_cores", "N/A")}</td></tr>')
        html_parts.append(f'<tr><td>Load Average</td><td>{metrics.get("load_average_1m", 0):.2f}</td></tr>')
        html_parts.append('</table>')

        # MySQL
        html_parts.append('<h2>MySQL / MariaDB</h2><table>')
        html_parts.append(f'<tr><td>Buffer Pool</td><td>{metrics.get("innodb_buffer_pool_size_mb", 0)} MB</td></tr>')
        html_parts.append(f'<tr><td>Max Connections</td><td>{metrics.get("max_connections", 0)}</td></tr>')
        html_parts.append(f'<tr><td>Total Tables</td><td>{metrics.get("total_tables", 0)}</td></tr>')
        html_parts.append('</table>')

        # Recommendations
        html_parts.append('<h2>Recommendations</h2>')
        if not recommendations:
            html_parts.append('<p>No recommendations generated.</p>')
        else:
            html_parts.append('<table><tr><th>#</th><th>Parameter</th><th>Current</th>'
                              '<th>Proposed</th><th>Priority</th><th>Confidence</th><th>Reason</th></tr>')
            _e = html.escape
            for i, rec in enumerate(recommendations, 1):
                prio = rec.get('priority') or rec.get('impact', 'medium')
                conf = rec.get('confidence', 0)
                conf_str = f'{conf:.0%}' if isinstance(conf, (int, float)) else str(conf)
                prio_safe = _e(str(prio))
                html_parts.append(
                    f'<tr><td>{i}</td><td>{_e(str(rec.get("parameter", "")))}</td>'
                    f'<td>{_e(str(rec.get("current_value", "")))}</td>'
                    f'<td>{_e(str(rec.get("recommended_value", "")))}</td>'
                    f'<td class="{prio_safe}">{prio_safe}</td>'
                    f'<td>{_e(conf_str)}</td>'
                    f'<td>{_e(str(rec.get("reason", "")))}</td></tr>'
                )
                if explain and rec.get('explanation'):
                    html_parts.append(
                        f'<tr><td colspan="7"><div class="explain">'
                        f'<strong>WHY THIS MATTERS:</strong><br>'
                        f'{_e(rec["explanation"]).replace(chr(10), "<br>")}'
                        f'</div></td></tr>'
                    )
            html_parts.append('</table>')

        # Top databases — the last format that silently dropped this data.
        if top_databases:
            _e = html.escape
            status = top_databases.get("status", "ok") if isinstance(top_databases, dict) else "ok"
            rows = top_databases.get("rows", []) if isinstance(top_databases, dict) else top_databases
            reason = top_databases.get("reason", "") if isinstance(top_databases, dict) else ""

            if status == "unavailable":
                html_parts.append('<h2>Top Database Activity</h2>')
                html_parts.append(
                    f'<p class="explain"><strong>UNAVAILABLE:</strong> '
                    f'{_e(reason or "Database unavailable")}</p>'
                )
            elif status == "mock":
                html_parts.append('<h2>Top Database Activity (Mock Data — Dry Run Only)</h2>')
                html_parts.append(
                    '<table><tr><th>Database</th><th>Queries/sec</th>'
                    '<th>Size (GB)</th><th>Connections</th><th>Impact</th>'
                    '<th>Level</th></tr>'
                )
                for row in rows:
                    level = _e(str(row.get('impact_label', '')))
                    html_parts.append(
                        f'<tr><td>{_e(str(row.get("database", "")))}</td>'
                        f'<td>{_e(str(row.get("queries_per_sec", 0)))}</td>'
                        f'<td>{_e(str(row.get("data_size_gb", 0)))}</td>'
                        f'<td>{_e(str(row.get("connections", 0)))}</td>'
                        f'<td>{_e(str(row.get("impact_score", 0)))}</td>'
                        f'<td class="{level.lower()}">{level}</td></tr>'
                    )
                html_parts.append('</table>')
                try:
                    from core.top_databases import build_top_db_tip
                    tip = build_top_db_tip(rows)
                    if tip:
                        html_parts.append(
                            f'<div class="explain"><strong>TIP:</strong> {_e(tip)}</div>'
                        )
                except Exception:
                    pass
            elif rows:
                html_parts.append('<h2>Top Database Activity</h2>')
                html_parts.append(
                    '<table><tr><th>Database</th><th>Queries/sec</th>'
                    '<th>Size (GB)</th><th>Connections</th><th>Impact</th>'
                    '<th>Level</th></tr>'
                )
                for row in rows:
                    level = _e(str(row.get('impact_label', '')))
                    html_parts.append(
                        f'<tr><td>{_e(str(row.get("database", "")))}</td>'
                        f'<td>{_e(str(row.get("queries_per_sec", 0)))}</td>'
                        f'<td>{_e(str(row.get("data_size_gb", 0)))}</td>'
                        f'<td>{_e(str(row.get("connections", 0)))}</td>'
                        f'<td>{_e(str(row.get("impact_score", 0)))}</td>'
                        f'<td class="{level.lower()}">{level}</td></tr>'
                    )
                html_parts.append('</table>')
                try:
                    from core.top_databases import build_top_db_tip
                    tip = build_top_db_tip(rows)
                    if tip:
                        html_parts.append(
                            f'<div class="explain"><strong>TIP:</strong> {_e(tip)}</div>'
                        )
                except Exception:
                    pass

        html_parts.append('<hr><p>Steadfast Codeworks | Automate. Simplify. Steadfast.</p>')
        html_parts.append('</body></html>')
        return '\n'.join(html_parts)

    # ------------------------------------------------------------------
    # File output
    # ------------------------------------------------------------------
    def save_report(self, report: str, filename: str = None, fmt: str = 'text') -> str:
        """Save report to file and return the path. ANSI codes are stripped automatically."""
        if self.output_dir is None:
            return ''
        try:
            if not self.output_dir.exists():
                self.output_dir.mkdir(parents=True, mode=0o750, exist_ok=True)
        except OSError:
            pass

        try:
            from utils.safe_io import secure_open_write
        except ImportError:
            from ..utils.safe_io import secure_open_write

        ext_map = {'text': 'txt', 'console': 'txt', 'json': 'json', 'html': 'html'}
        ext = ext_map.get(fmt, 'txt')
        if not filename:
            ts = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
            filename = f'autotuner_report_{ts}.{ext}'
        path = self.output_dir / filename
        # Strip ANSI colour codes so the saved file is clean plain text
        clean_report = _strip_ansi(report)
        # M1: O_NOFOLLOW write so a pre-created symlink in the reports dir can't
        # redirect a root-written report elsewhere.
        with secure_open_write(str(path)) as f:
            f.write(clean_report)
        self.logger.info(f"Report saved to {path}")
        self._prune_old_reports()
        return str(path)

    def _prune_old_reports(self) -> None:
        """Keep only the newest ``max_saved_reports`` generated reports (M6).

        Only files matching the tool's own ``autotuner_report_*`` naming are
        considered, so nothing a user put in the directory is ever removed.
        """
        if self.output_dir is None or self.max_saved_reports <= 0:
            return
        try:
            reports = sorted(
                self.output_dir.glob('autotuner_report_*'),
                key=lambda p: p.name,
                reverse=True,
            )
            for stale in reports[self.max_saved_reports:]:
                try:
                    stale.unlink()
                except OSError:
                    pass
        except OSError as exc:
            self.logger.debug(f"Could not prune old reports: {exc}")

"""
top_databases.py  —  Per-database activity collector for MySQL Auto-Tuner
=========================================================================
Collects per-schema metrics from information_schema and performance_schema
(where available) and computes an impact score for each database.

Metrics collected
-----------------
  * queries_per_sec   — derived from performance_schema.events_statements_summary_by_digest
                        or estimated from information_schema query counts
  * data_size_gb      — from information_schema.TABLES (data_length + index_length)
  * connections       — from information_schema.PROCESSLIST (active threads per schema)
  * impact_score      — composite: 0.5×queries_pct + 0.3×size_pct + 0.2×conn_pct

The collector gracefully degrades when performance_schema is unavailable:
  * queries_per_sec falls back to 0 (N/A shown in output)
  * All other metrics still work via information_schema

Author: R.L. Burger (Steadfast Codeworks)
Date: 2025-09-07
Last Updated: 2026-08-24
Version: 1.0.4
Copyright (c) 2026 R.L. Burger
Project: Steadfast Tools
Website: https://www.steadfasttools.com
License: MIT License
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Schemas that are always excluded from the report
_SYSTEM_SCHEMAS = frozenset({
    "information_schema", "performance_schema", "mysql",
    "sys", "innodb", "tmp", "test",
})


def _safe_float(val: Any, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _warn_if_large_scan(connector) -> None:
    """Pre-count tables and warn operator before running the expensive information_schema size scan."""
    try:
        res = connector.execute_query("""
            SELECT COUNT(*) AS n
            FROM information_schema.TABLES
            WHERE TABLE_SCHEMA NOT IN (
                'information_schema','performance_schema','mysql','sys','test'
            )
        """, retry=False)
        if res:
            table_count = int(_safe_float(res[0].get("n", 0)))
            if table_count >= 20000:
                logger.warning(
                    f"top_databases: scanning information_schema for {table_count:,} tables. "
                    "This may take significant time on large installations."
                )
            elif table_count > 0:
                logger.info(f"top_databases: scanning information_schema for {table_count:,} tables...")
    except Exception as exc:
        logger.debug(f"top_databases: pre-scan table count skipped: {exc}")


def collect_top_databases(
    connector,
    limit: int = 10,
    uptime_seconds: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Query the running MySQL/MariaDB instance and return a list of per-database
    activity dicts, sorted by impact_score descending.

    Args:
        connector : MySQLConnector instance (must already be connected).
        limit     : Maximum number of databases to return.
        uptime_seconds: Server uptime in seconds (used to normalise query counts).
                        If None, it is fetched automatically.

    Returns:
        Contract dict: {"status": "ok", "rows": [...]} or {"status": "unavailable", "reason": ...}
    """
    results: List[Dict[str, Any]] = []

    try:
        # ----------------------------------------------------------------
        # 1. Server uptime (for queries/sec normalisation)
        # ----------------------------------------------------------------
        if uptime_seconds is None:
            uptime_rows = connector.execute_query(
                "SHOW GLOBAL STATUS LIKE 'Uptime'",
                retry=False,
            )
            uptime_seconds = _safe_float(
                uptime_rows[0].get("Value", 1) if uptime_rows else 1, default=1.0
            )
        uptime_seconds = max(uptime_seconds, 1.0)

        # ----------------------------------------------------------------
        # Pre-scan warning for large databases (M-8)
        # ----------------------------------------------------------------
        _warn_if_large_scan(connector)

        # ----------------------------------------------------------------
        # 2. Data sizes from information_schema.TABLES
        # ----------------------------------------------------------------
        size_rows = connector.execute_query("""
            SELECT
                TABLE_SCHEMA                              AS db,
                SUM(DATA_LENGTH + INDEX_LENGTH) / 1073741824.0  AS size_gb
            FROM information_schema.TABLES
            WHERE TABLE_SCHEMA NOT IN (
                'information_schema','performance_schema','mysql','sys','test'
            )
            GROUP BY TABLE_SCHEMA
            ORDER BY size_gb DESC
        """, retry=False)

        size_map: Dict[str, float] = {}
        for row in (size_rows or []):
            db = row.get("db") or row.get("TABLE_SCHEMA") or ""
            if db and db.lower() not in _SYSTEM_SCHEMAS:
                # L9: the fallback used to re-read the same key ("size_gb" twice)
                # instead of the upper-case variant some drivers return.
                size_map[db] = _safe_float(
                    row.get("size_gb", row.get("SIZE_GB", 0))
                )

        if not size_map:
            logger.info("top_databases: no user databases found")
            return {"status": "ok", "rows": []}

        # ----------------------------------------------------------------
        # 3. Active connections per schema (PROCESSLIST)
        # ----------------------------------------------------------------
        conn_rows = connector.execute_query("""
            SELECT DB AS db, COUNT(*) AS conn_count
            FROM information_schema.PROCESSLIST
            WHERE DB IS NOT NULL
              AND DB NOT IN (
                  'information_schema','performance_schema','mysql','sys','test'
              )
            GROUP BY DB
        """, retry=False)
        conn_map: Dict[str, int] = {}
        for row in (conn_rows or []):
            db = row.get("db") or row.get("DB") or ""
            if db:
                conn_map[db] = int(_safe_float(row.get("conn_count", 0)))

        # ----------------------------------------------------------------
        # 4. Query counts — try performance_schema first, fall back gracefully
        # ----------------------------------------------------------------
        query_map: Dict[str, float] = {}
        try:
            perf_rows = connector.execute_query("""
                SELECT
                    SCHEMA_NAME                             AS db,
                    SUM(COUNT_STAR)                         AS total_queries
                FROM performance_schema.events_statements_summary_by_digest
                WHERE SCHEMA_NAME IS NOT NULL
                  AND SCHEMA_NAME NOT IN (
                      'information_schema','performance_schema','mysql','sys','test'
                  )
                GROUP BY SCHEMA_NAME
                ORDER BY total_queries DESC
            """, retry=False)
            for row in (perf_rows or []):
                db = row.get("db") or row.get("SCHEMA_NAME") or ""
                if db:
                    total_q = _safe_float(row.get("total_queries", 0))
                    query_map[db] = round(total_q / uptime_seconds, 1)
        except Exception as exc:
            logger.debug(f"top_databases: performance_schema unavailable ({exc}); queries_per_sec will be N/A")

        # ----------------------------------------------------------------
        # 5. Assemble per-database records
        # ----------------------------------------------------------------
        total_size  = max(sum(size_map.values()), 0.001)
        total_conn  = max(sum(conn_map.values()), 1)
        total_query = max(sum(query_map.values()), 0.001)

        for db, size_gb in size_map.items():
            qps       = query_map.get(db, 0.0)
            conns     = conn_map.get(db, 0)
            q_pct     = (qps / total_query * 100)  if total_query > 0 else 0.0
            s_pct     = (size_gb / total_size * 100)
            c_pct     = (conns / total_conn * 100)  if total_conn > 0 else 0.0

            # Composite impact score (0–100)
            impact = round(0.5 * q_pct + 0.3 * s_pct + 0.2 * c_pct, 1)

            results.append({
                "database":       db,
                "queries_per_sec": qps,
                "data_size_gb":   round(size_gb, 2),
                "connections":    conns,
                "impact_score":   impact,
                "queries_pct":    round(q_pct, 1),
                "size_pct":       round(s_pct, 1),
                "conn_pct":       round(c_pct, 1),
            })

        # Sort by impact score descending
        results.sort(key=lambda r: r["impact_score"], reverse=True)
        results = results[:limit]

        # ----------------------------------------------------------------
        # 6. Assign impact labels based on relative rank
        # ----------------------------------------------------------------
        _assign_impact_labels(results)

    except Exception as exc:
        logger.error(f"top_databases: collection failed — {exc}", exc_info=True)
        return {"status": "unavailable", "reason": str(exc), "rows": []}

    return {"status": "ok", "rows": results}


def _assign_impact_labels(rows: List[Dict[str, Any]]) -> None:
    """
    Assign HIGH / MEDIUM / LOW labels based on relative impact_score percentage of the top database.
    Databases with score >= 60% of max receive HIGH, >= 25% receive MEDIUM, and < 25% receive LOW.
    """
    if not rows:
        return

    scores = [r["impact_score"] for r in rows]
    max_s  = max(scores) or 1.0

    for r in rows:
        pct = r["impact_score"] / max_s * 100
        if pct >= 60:
            r["impact_label"] = "HIGH"
        elif pct >= 25:
            r["impact_label"] = "MEDIUM"
        else:
            r["impact_label"] = "LOW"


def build_top_db_tip(rows_or_contract: Any) -> Optional[str]:
    """
    Generate an actionable tip based on the top database's metrics.
    Accepts either a list of rows or a structured contract dict {"status": ..., "rows": [...]}.
    Returns None if there is nothing notable to report.
    """
    if not rows_or_contract:
        return None
    if isinstance(rows_or_contract, dict):
        rows = rows_or_contract.get("rows", [])
    else:
        rows = rows_or_contract
    if not rows:
        return None

    top = rows[0]
    db  = top["database"]
    q_pct = top.get("queries_pct", 0)
    s_gb  = top.get("data_size_gb", 0)
    label = top.get("impact_label", "")

    if label == "HIGH":
        if q_pct >= 50:
            return (
                f"{db} is generating {q_pct:.0f}% of query load. "
                f"Consider query optimization or a caching layer (e.g., Redis/Memcached)."
            )
        elif s_gb >= 5:
            return (
                f"{db} holds {s_gb:.1f} GB of data and dominates the impact score. "
                f"Review table indexes and consider archiving old records."
            )
        else:
            return (
                f"{db} has the highest combined impact score. "
                f"Profile slow queries with EXPLAIN and check index coverage."
            )
    elif label == "MEDIUM" and len(rows) >= 2:
        return (
            f"Load is distributed across multiple databases. "
            f"Monitor {db} and {rows[1]['database']} for query growth."
        )
    return None


# ---------------------------------------------------------------------------
# Offline / mock mode — used when no live MySQL connection is available
# (e.g., --dry-run, unit tests, demo mode)
# ---------------------------------------------------------------------------

def mock_top_databases() -> Dict[str, Any]:
    """Return realistic mock data for testing and --dry-run mode."""
    rows = [
        {
            "database":        "clientA_wordpress",
            "queries_per_sec": 847.0,
            "data_size_gb":    2.4,
            "connections":     12,
            "impact_score":    72.4,
            "queries_pct":     68.0,
            "size_pct":        22.0,
            "conn_pct":        52.0,
            "source":          "mock",
        },
        {
            "database":        "clientB_magento",
            "queries_per_sec": 312.0,
            "data_size_gb":    8.1,
            "connections":     8,
            "impact_score":    41.2,
            "queries_pct":     25.0,
            "size_pct":        74.0,
            "conn_pct":        35.0,
            "source":          "mock",
        },
        {
            "database":        "clientC_drupal",
            "queries_per_sec": 89.0,
            "data_size_gb":    0.3,
            "connections":     3,
            "impact_score":    10.8,
            "queries_pct":     7.0,
            "size_pct":        3.0,
            "conn_pct":        13.0,
            "source":          "mock",
        },
    ]
    _assign_impact_labels(rows)
    return {"status": "mock", "rows": rows}

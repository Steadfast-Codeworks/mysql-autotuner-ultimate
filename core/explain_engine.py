#!/usr/bin/env python3
"""
Explain Engine - v1.0.4
=======================
Educational engine explaining why MySQL parameters are recommended.

Author: R.L. Burger (Steadfast Codeworks)
Date: 2025-09-07
Last Updated: 2026-08-24
Version: 1.0.4
Copyright (c) 2026 R.L. Burger
Project: Steadfast Tools
Website: https://www.steadfasttools.com
License: MIT License
"""

import logging
from typing import Dict, List, Any, Optional

logger = logging.getLogger(__name__)


# ======================================================================
# Parameter knowledge base
# ======================================================================

PARAMETER_KNOWLEDGE: Dict[str, Dict[str, str]] = {
    'innodb_buffer_pool_size': {
        'what': (
            'The InnoDB buffer pool is the main memory area where InnoDB '
            'caches table data and indexes.  It is the single most important '
            'tuning parameter for InnoDB-heavy workloads.'
        ),
        'why': (
            'When the buffer pool is too small, InnoDB must read data from '
            'disk for every query that cannot find its pages in memory.  Disk '
            'I/O is orders of magnitude slower than RAM access, so an '
            'undersized buffer pool directly causes high disk reads, slow '
            'queries, and elevated server load.'
        ),
        'how_template': (
            'Your server has {total_ram_mb}MB total RAM.  The current buffer '
            'pool is {current_value}, which is {bp_pct:.0%} of total RAM.  '
            '{hit_rate_info}'
            'Based on {total_tables} tables ({innodb_tables} InnoDB, '
            '{data_size_info}), we recommend {recommended_value} '
            '({rec_pct:.0%} of RAM).  '
            'This follows the industry best practice of allocating 60-80% of '
            'RAM to the buffer pool on dedicated database servers, adjusted '
            'for shared hosting environments.'
        ),
        'impact': (
            'Increasing the buffer pool allows more data to be served from '
            'RAM, reducing disk I/O.  On servers where the buffer pool hit '
            'ratio is below 99%, even a modest increase can reduce disk reads '
            'by 30-70% and noticeably lower query latency and server load.'
        ),
    },
    'innodb_buffer_pool_instances': {
        'what': (
            'This parameter splits the InnoDB buffer pool into multiple '
            'instances to reduce contention when multiple threads access '
            'the buffer pool concurrently.'
        ),
        'why': (
            'On multi-core servers with a large buffer pool, a single '
            'instance creates a bottleneck because threads must acquire a '
            'mutex to access the pool.  Splitting it into multiple instances '
            'allows parallel access.'
        ),
        'how_template': (
            'Your server has {cpu_cores} CPU cores and a buffer pool of '
            '{current_value}.  The general rule is 1 instance per 1-2GB of '
            'buffer pool, with a maximum equal to the number of CPU cores.  '
            'We recommend {recommended_value} instances.'
        ),
        'impact': (
            'Reduces mutex contention on the buffer pool, improving '
            'throughput on multi-threaded workloads by 5-15%.'
        ),
    },
    'innodb_log_file_size': {
        'what': (
            'The InnoDB redo log (also called the transaction log) records '
            'all changes to InnoDB data.  innodb_log_file_size controls the '
            'size of each redo log file.'
        ),
        'why': (
            'When the redo log is too small, InnoDB must flush dirty pages '
            'from the buffer pool more frequently to make room for new log '
            'entries.  This causes checkpoint storms — sudden bursts of disk '
            'writes that spike I/O wait and slow down all queries.'
        ),
        'how_template': (
            'Your current redo log is {current_value}.  '
            '{log_writes_info}'
            'For a buffer pool of {bp_size_info} and your write workload, '
            'we recommend {recommended_value}.  A larger log allows InnoDB '
            'to batch more changes before flushing, smoothing out I/O.'
        ),
        'impact': (
            'A properly sized redo log reduces checkpoint frequency, '
            'smooths out write I/O, and can improve write-heavy workload '
            'performance by 20-40%.'
        ),
    },
    'innodb_redo_log_capacity': {
        'what': (
            'In MySQL 8.0.30+ and 8.4, innodb_redo_log_capacity replaces '
            'the combination of innodb_log_file_size and '
            'innodb_log_files_in_group.  It defines the total redo log '
            'capacity in bytes.'
        ),
        'why': (
            'Same as innodb_log_file_size — controls how much change data '
            'InnoDB can buffer before forcing a checkpoint flush.'
        ),
        'how_template': (
            'Your MySQL version uses innodb_redo_log_capacity instead of '
            'innodb_log_file_size.  Based on your write workload and buffer '
            'pool size, we recommend {recommended_value}.'
        ),
        'impact': (
            'Reduces checkpoint storms and smooths write I/O, improving '
            'write-heavy performance by 20-40%.'
        ),
    },
    'max_connections': {
        'what': (
            'max_connections sets the maximum number of simultaneous client '
            'connections MySQL/MariaDB will accept.'
        ),
        'why': (
            'If max_connections is too low, new connections are refused '
            'during traffic spikes, causing "Too many connections" errors '
            'and application downtime.  If set too high, each idle '
            'connection consumes memory (thread stack, sort buffer, etc.), '
            'wasting RAM.'
        ),
        'how_template': (
            'Your current max_connections is {current_value}.  '
            'The highest observed concurrent connections is '
            '{max_used_connections} ({conn_usage:.0%} utilisation).  '
            '{conn_analysis}'
            'We recommend {recommended_value} to provide adequate headroom '
            'without wasting memory.'
        ),
        'impact': (
            'Proper connection limits prevent "Too many connections" errors '
            'during peak hours while avoiding memory waste from excessive '
            'idle connections.'
        ),
    },
    'table_open_cache': {
        'what': (
            'table_open_cache controls how many table file descriptors '
            'MySQL keeps open simultaneously.  Each open table requires '
            'a file descriptor.'
        ),
        'why': (
            'When a query accesses a table that is not in the cache, MySQL '
            'must open the table file from disk.  On servers with many '
            'databases and tables (common in shared hosting), a small cache '
            'causes constant table open/close overhead.'
        ),
        'how_template': (
            'Your server has {total_tables} tables and the current cache '
            'is {current_value}.  '
            '{table_cache_analysis}'
            'We recommend {recommended_value} to reduce table open overhead.'
        ),
        'impact': (
            'Reduces the overhead of opening and closing table files, '
            'improving query performance on servers with many databases.'
        ),
    },
    'key_buffer_size': {
        'what': (
            'key_buffer_size is the buffer used for MyISAM index blocks.  '
            'It is only relevant if you have MyISAM tables.'
        ),
        'why': (
            'If you have migrated from MyISAM to InnoDB, the key buffer '
            'is wasting memory that could be used by the InnoDB buffer pool.  '
            'If you still have MyISAM tables, the key buffer needs to be '
            'large enough to cache their indexes.'
        ),
        'how_template': (
            'You have {myisam_tables} MyISAM tables and {innodb_tables} '
            'InnoDB tables.  Your current key_buffer_size is {current_value}.  '
            '{key_buffer_analysis}'
            'We recommend {recommended_value}.'
        ),
        'impact': (
            'After MyISAM-to-InnoDB migration, reducing key_buffer_size '
            'frees memory for the InnoDB buffer pool.  If MyISAM tables '
            'remain, proper sizing improves MyISAM index performance.'
        ),
    },
    'tmp_table_size': {
        'what': (
            'tmp_table_size (together with max_heap_table_size) controls '
            'the maximum size of in-memory temporary tables.  When a '
            'temporary table exceeds this limit, MySQL writes it to disk.'
        ),
        'why': (
            'Disk-based temporary tables are dramatically slower than '
            'in-memory ones.  If your server creates many disk-based temp '
            'tables, queries with GROUP BY, ORDER BY, or complex JOINs '
            'will be slow.'
        ),
        'how_template': (
            'Your server has created {created_tmp_tables} temporary tables, '
            'of which {created_tmp_disk_tables} ({tmp_disk_pct:.1%}) went '
            'to disk.  The current tmp_table_size is {current_value}.  '
            '{tmp_analysis}'
            'We recommend {recommended_value}.'
        ),
        'impact': (
            'Reducing the disk-based temp table ratio from above 25% to '
            'below 10% can significantly speed up complex queries.'
        ),
    },
    'max_heap_table_size': {
        'what': (
            'max_heap_table_size sets the maximum size for MEMORY (HEAP) '
            'tables and also caps in-memory temporary tables together with '
            'tmp_table_size.'
        ),
        'why': (
            'This should generally match tmp_table_size.  The effective '
            'limit for in-memory temp tables is the minimum of these two '
            'values.'
        ),
        'how_template': (
            'Your current max_heap_table_size is {current_value}.  '
            'We recommend {recommended_value} to match tmp_table_size.'
        ),
        'impact': 'Ensures tmp_table_size is fully effective.',
    },
    'thread_cache_size': {
        'what': (
            'thread_cache_size controls how many threads MySQL keeps in '
            'a cache for reuse.  When a client disconnects, its thread '
            'is placed in the cache instead of being destroyed.'
        ),
        'why': (
            'Creating and destroying threads is expensive.  On busy '
            'servers with many short-lived connections (typical in web '
            'hosting), a properly sized thread cache avoids this overhead.'
        ),
        'how_template': (
            'Your server has max_connections={max_connections} and '
            'the current thread_cache_size is {current_value}.  '
            'We recommend {recommended_value}.'
        ),
        'impact': (
            'Reduces thread creation overhead, improving connection '
            'handling speed by 5-10% on busy servers.'
        ),
    },
    'innodb_io_capacity': {
        'what': (
            'innodb_io_capacity tells InnoDB how many I/O operations per '
            'second (IOPS) the underlying storage can handle for background '
            'tasks like flushing dirty pages.'
        ),
        'why': (
            'If set too low, InnoDB does not flush dirty pages fast enough, '
            'leading to checkpoint storms.  If set too high on slow disks, '
            'InnoDB overwhelms the storage subsystem.'
        ),
        'how_template': (
            'Your current innodb_io_capacity is {current_value}.  '
            '{io_analysis}'
            'We recommend {recommended_value}.'
        ),
        'impact': (
            'Proper I/O capacity settings smooth out background flushing '
            'and prevent I/O spikes.'
        ),
    },
    'innodb_io_capacity_max': {
        'what': (
            'innodb_io_capacity_max sets the upper limit for I/O operations '
            'during urgent flushing (when the redo log is nearly full).'
        ),
        'why': (
            'During urgent flushing, InnoDB can burst up to this limit.  '
            'Setting it appropriately prevents I/O stalls.'
        ),
        'how_template': (
            'Your current innodb_io_capacity_max is {current_value}.  '
            'We recommend {recommended_value} (typically 2x innodb_io_capacity).'
        ),
        'impact': 'Prevents I/O stalls during urgent flushing operations.',
    },
    'join_buffer_size': {
        'what': (
            'join_buffer_size is allocated per-join for queries that cannot '
            'use indexes for joins.'
        ),
        'why': (
            'Queries with full table scans or non-indexed joins use this '
            'buffer.  Too small causes slow joins; too large wastes memory '
            '(allocated per-connection, per-join).'
        ),
        'how_template': (
            'Your current join_buffer_size is {current_value}.  '
            'We recommend {recommended_value}.'
        ),
        'impact': 'Improves performance of non-indexed join operations.',
    },
    'sort_buffer_size': {
        'what': (
            'sort_buffer_size is allocated per-session for sorting operations '
            '(ORDER BY, GROUP BY).'
        ),
        'why': (
            'Each connection that performs a sort allocates this buffer.  '
            'On servers with many connections, large sort buffers can '
            'consume significant memory.'
        ),
        'how_template': (
            'Your current sort_buffer_size is {current_value}.  '
            'With {max_connections} max connections, the worst-case memory '
            'usage for sort buffers alone is {worst_case_mb}MB.  '
            'We recommend {recommended_value}.'
        ),
        'impact': 'Balances sort performance against memory usage.',
    },
    'read_buffer_size': {
        'what': (
            'read_buffer_size is used for sequential table scans.  '
            'Each thread performing a sequential scan allocates this buffer.'
        ),
        'why': (
            'Larger buffers reduce the number of read system calls for '
            'sequential scans, but consume more memory per connection.'
        ),
        'how_template': (
            'Your current read_buffer_size is {current_value}.  '
            'We recommend {recommended_value}.'
        ),
        'impact': 'Improves sequential scan performance.',
    },
    'read_rnd_buffer_size': {
        'what': (
            'read_rnd_buffer_size is used for reading rows in sorted order '
            'after a sort operation (Multi-Range Read optimization).'
        ),
        'why': (
            'Helps convert random disk reads into sequential reads after '
            'sorting, improving performance of sorted queries.'
        ),
        'how_template': (
            'Your current read_rnd_buffer_size is {current_value}.  '
            'We recommend {recommended_value}.'
        ),
        'impact': 'Improves sorted query performance.',
    },
    'query_cache_type': {
        'what': (
            'query_cache_type enables or disables the MySQL query cache.  '
            'Note: The query cache was removed in MySQL 8.0 but is still '
            'available in MariaDB.'
        ),
        'why': (
            'The query cache stores the text and result of SELECT queries.  '
            'On write-heavy workloads, the cache invalidation overhead can '
            'actually hurt performance.  On read-heavy workloads with '
            'repetitive queries, it can help.'
        ),
        'how_template': (
            'Your current query_cache_type is {current_value}.  '
            '{qc_analysis}'
            'We recommend {recommended_value}.'
        ),
        'impact': 'Depends on workload; can improve or hurt performance.',
    },
    'query_cache_size': {
        'what': (
            'query_cache_size sets the amount of memory allocated for '
            'caching query results.'
        ),
        'why': (
            'If the cache is too small, queries are evicted frequently.  '
            'If too large, the cache management overhead increases.'
        ),
        'how_template': (
            'Your current query_cache_size is {current_value}.  '
            'We recommend {recommended_value}.'
        ),
        'impact': 'Optimizes memory allocation for query result caching.',
    },
    'innodb_flush_log_at_trx_commit': {
        'what': (
            'Controls when InnoDB flushes the redo log to disk.  '
            '1 = flush on every commit (safest), 2 = flush to OS cache '
            'on commit (good balance), 0 = flush every second (fastest, '
            'risk of 1-second data loss).'
        ),
        'why': (
            'Setting 1 guarantees ACID compliance but is the slowest.  '
            'Setting 2 provides a good balance for web hosting: data is '
            'safe against MySQL crashes but could lose up to 1 second of '
            'data on OS crash.'
        ),
        'how_template': (
            'Your current setting is {current_value}.  '
            'We recommend {recommended_value} for your workload profile.'
        ),
        'impact': (
            'Setting 2 instead of 1 can improve write performance by '
            '50-100% with minimal risk increase.'
        ),
    },
    'innodb_flush_method': {
        'what': (
            'Controls how InnoDB flushes data and log files to disk.  '
            'O_DIRECT bypasses the OS file cache, reducing double-buffering.'
        ),
        'why': (
            'Without O_DIRECT, data is cached in both the InnoDB buffer '
            'pool and the OS file cache, wasting RAM.  O_DIRECT tells the '
            'OS to skip its cache for InnoDB files.'
        ),
        'how_template': (
            'Your current innodb_flush_method is {current_value}.  '
            'We recommend {recommended_value}.'
        ),
        'impact': (
            'O_DIRECT reduces memory waste from double-buffering, '
            'freeing RAM for the buffer pool and other processes.'
        ),
    },
}

# Fallback for parameters not in the knowledge base
DEFAULT_KNOWLEDGE = {
    'what': 'This MySQL/MariaDB configuration parameter affects database performance.',
    'why': 'Proper tuning of this parameter can improve server performance.',
    'how_template': (
        'Your current value is {current_value}.  '
        'Based on your server metrics, we recommend {recommended_value}.'
    ),
    'impact': 'Optimizes database performance based on your workload.',
}


# ======================================================================
# Explain Engine
# ======================================================================

class ExplainEngine:
    """
    Generates human-readable, educational explanations for each
    recommendation.
    """

    def __init__(self):
        self.logger = logging.getLogger(__name__)

    def enrich_recommendations(
        self,
        recommendations: List[Dict[str, Any]],
        metrics: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """
        Add an 'explanation' key to each recommendation with a
        human-readable explanation of why the change was recommended.
        """
        enriched = []
        for rec in recommendations:
            enriched_rec = dict(rec)
            enriched_rec['explanation'] = self._generate_explanation(rec, metrics)
            enriched.append(enriched_rec)
        return enriched

    def _generate_explanation(
        self, rec: Dict[str, Any], metrics: Dict[str, Any]
    ) -> str:
        """Generate a full explanation for a single recommendation."""
        param = rec.get('parameter', '')
        knowledge = PARAMETER_KNOWLEDGE.get(param, DEFAULT_KNOWLEDGE)

        parts: List[str] = []

        # WHAT
        parts.append(f"WHAT: {knowledge['what']}")
        parts.append('')

        # WHY
        parts.append(f"WHY: {knowledge['why']}")
        parts.append('')

        # HOW (fill template with actual metrics)
        how_text = self._fill_how_template(knowledge['how_template'], rec, metrics)
        parts.append(f"HOW: {how_text}")
        parts.append('')

        # IMPACT
        parts.append(f"EXPECTED IMPACT: {knowledge['impact']}")

        return '\n'.join(parts)

    def _fill_how_template(
        self,
        template: str,
        rec: Dict[str, Any],
        metrics: Dict[str, Any],
    ) -> str:
        """Fill the HOW template with actual metric values."""
        total_ram_mb = metrics.get('total_ram_mb', 0)
        current_value = rec.get('current_value', 'N/A')
        recommended_value = rec.get('recommended_value', 'N/A')
        param = rec.get('parameter', '')

        # Build context dict for formatting
        ctx = {
            'current_value': current_value,
            'recommended_value': recommended_value,
            'total_ram_mb': total_ram_mb,
            'cpu_cores': metrics.get('cpu_cores', 1),
            'total_tables': metrics.get('total_tables', 0),
            'innodb_tables': metrics.get('innodb_tables', 0),
            'myisam_tables': metrics.get('myisam_tables', 0),
            'max_connections': metrics.get('max_connections', 0),
            'max_used_connections': metrics.get('max_used_connections', 0),
            'created_tmp_tables': metrics.get('created_tmp_tables', 0),
            'created_tmp_disk_tables': metrics.get('created_tmp_disk_tables', 0),
        }

        # Compute derived values
        # Buffer pool percentage
        try:
            bp_mb = self._parse_size_mb(current_value)
            ctx['bp_pct'] = bp_mb / total_ram_mb if total_ram_mb > 0 else 0
        except (ValueError, TypeError):
            ctx['bp_pct'] = 0

        try:
            rec_mb = self._parse_size_mb(recommended_value)
            ctx['rec_pct'] = rec_mb / total_ram_mb if total_ram_mb > 0 else 0
        except (ValueError, TypeError):
            ctx['rec_pct'] = 0

        # Hit rate info
        raw = metrics.get('_raw_metrics', {})
        perf = raw.get('performance', {}) if isinstance(raw, dict) else {}
        innodb_status = perf.get('innodb_status', {}) if isinstance(perf, dict) else {}
        hit_rate = innodb_status.get('buffer_pool_hit_rate', 0) if isinstance(innodb_status, dict) else 0
        if hit_rate and hit_rate < 0.99:
            ctx['hit_rate_info'] = (
                f'Your buffer pool hit rate is {hit_rate:.2%}, which means '
                f'{(1 - hit_rate):.2%} of data requests go to disk.  '
            )
        elif hit_rate:
            ctx['hit_rate_info'] = f'Your buffer pool hit rate is {hit_rate:.2%} (good).  '
        else:
            ctx['hit_rate_info'] = ''

        # Data size info
        data_size_mb = metrics.get('total_data_size_mb', 0)
        index_size_mb = metrics.get('total_index_size_mb', 0)
        total_db_mb = data_size_mb + index_size_mb
        if total_db_mb > 1024:
            ctx['data_size_info'] = f'{total_db_mb / 1024:.1f}GB total data+indexes'
        else:
            ctx['data_size_info'] = f'{total_db_mb}MB total data+indexes'

        # Buffer pool size info for log file recommendation
        bp_size_mb = metrics.get('innodb_buffer_pool_size_mb', 0)
        if bp_size_mb >= 1024:
            ctx['bp_size_info'] = f'{bp_size_mb / 1024:.1f}GB'
        else:
            ctx['bp_size_info'] = f'{bp_size_mb}MB'

        # Log writes info
        ctx['log_writes_info'] = ''

        # Connection analysis
        conn_usage = (
            metrics.get('max_used_connections', 0) / metrics.get('max_connections', 1)
            if metrics.get('max_connections', 0) > 0 else 0
        )
        ctx['conn_usage'] = conn_usage
        if conn_usage > 0.85:
            ctx['conn_analysis'] = (
                f'At {conn_usage:.0%} utilisation, you are dangerously close to '
                f'the limit and risk "Too many connections" errors.  '
            )
        elif conn_usage > 0.6:
            ctx['conn_analysis'] = (
                f'At {conn_usage:.0%} utilisation, you have moderate headroom '
                f'but should increase for peak-hour safety.  '
            )
        else:
            ctx['conn_analysis'] = (
                f'At {conn_usage:.0%} utilisation, you have good headroom.  '
            )

        # Table cache analysis
        table_open_cache = metrics.get('table_open_cache', 0)
        total_tables = metrics.get('total_tables', 0)
        if total_tables > 0 and table_open_cache > 0:
            ratio = table_open_cache / total_tables
            if ratio < 1.5:
                ctx['table_cache_analysis'] = (
                    f'Your cache-to-table ratio is {ratio:.1f}x, which is low.  '
                    f'Tables are being opened and closed frequently.  '
                )
            else:
                ctx['table_cache_analysis'] = (
                    f'Your cache-to-table ratio is {ratio:.1f}x.  '
                )
        else:
            ctx['table_cache_analysis'] = ''

        # Key buffer analysis
        myisam_tables = metrics.get('myisam_tables', 0)
        if myisam_tables == 0:
            ctx['key_buffer_analysis'] = (
                'You have no MyISAM tables, so the key buffer is wasting memory.  '
                'Reducing it frees RAM for the InnoDB buffer pool.  '
            )
        else:
            ctx['key_buffer_analysis'] = (
                f'You still have {myisam_tables} MyISAM tables that need the key buffer.  '
            )

        # Tmp table analysis
        tmp_tables = metrics.get('created_tmp_tables', 0)
        tmp_disk = metrics.get('created_tmp_disk_tables', 0)
        ctx['tmp_disk_pct'] = tmp_disk / tmp_tables if tmp_tables > 0 else 0
        if ctx['tmp_disk_pct'] > 0.25:
            ctx['tmp_analysis'] = (
                f'With {ctx["tmp_disk_pct"]:.1%} of temp tables going to disk, '
                f'complex queries are being slowed by disk I/O.  '
            )
        else:
            ctx['tmp_analysis'] = ''

        # Sort buffer worst case
        try:
            sort_mb = self._parse_size_mb(recommended_value)
            max_conn = metrics.get('max_connections', 0)
            ctx['worst_case_mb'] = int(sort_mb * max_conn) if max_conn > 0 else 0
        except (ValueError, TypeError):
            ctx['worst_case_mb'] = 0

        # I/O analysis
        ctx['io_analysis'] = ''

        # Query cache analysis
        ctx['qc_analysis'] = ''

        # Fill template
        try:
            return template.format(**ctx)
        except (KeyError, ValueError) as e:
            self.logger.debug(f"Template fill error for {param}: {e}")
            return (
                f'Your current value is {current_value}.  '
                f'Based on your server metrics, we recommend {recommended_value}.'
            )

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
        try:
            return int(float(s))
        except ValueError:
            return 0

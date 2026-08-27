#!/usr/bin/env python3
"""
Data Collector Module
Collects comprehensive system and MySQL metrics for analysis

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
import subprocess
import logging
from typing import Dict, Any, Optional
from datetime import datetime

def _safe_int(val, default=0):
    """Coerce a value from MySQL to int, handling None, 'NULL', and non-numeric strings."""
    if val is None:
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


class DataCollector:
    """Collects system and MySQL performance metrics"""
    
    def __init__(self, mysql_connector, system_info):
        self.mysql_connector = mysql_connector
        self.system_info = system_info
        self.logger = logging.getLogger(__name__)
    
    def collect_all_metrics(self) -> Dict[str, Any]:
        """Collect all system and MySQL metrics"""
        self.logger.info("Collecting comprehensive metrics...")
        
        metrics = {
            'timestamp': datetime.now().isoformat(),
            'system': self.collect_system_metrics(),
            'mysql': self.collect_mysql_metrics(),
            'tables': self.collect_table_metrics(),
            'performance': self.collect_performance_metrics()
        }
        
        # Flatten and normalize metrics for easier analysis
        normalized_metrics = self._normalize_metrics(metrics)
        
        self.logger.info("Metrics collection completed")
        return normalized_metrics
    
    def collect_system_metrics(self) -> Dict[str, Any]:
        """Collect system-level metrics"""
        self.logger.debug("Collecting system metrics...")
        
        try:
            return {
                'memory': self.system_info.get_memory_info(),
                'cpu': self.system_info.get_cpu_info(),
                'disk': self.system_info.get_disk_info(),
                'load': self.system_info.get_load_average(),
                'uptime': self.system_info.get_uptime(),
                'os': self.system_info.get_os_info()
            }
        except Exception as e:
            self.logger.error(f"Failed to collect system metrics: {e}")
            return {}
    
    def collect_mysql_metrics(self) -> Dict[str, Any]:
        """Collect MySQL configuration and status metrics"""
        self.logger.debug("Collecting MySQL metrics...")
        
        try:
            return {
                'version': self._get_mysql_version(),
                'variables': self._get_mysql_variables(),
                'status': self._get_mysql_status(),
                'engines': self._get_storage_engines(),
                'config_file': self._get_config_file_info()
            }
        except Exception as e:
            self.logger.error(f"Failed to collect MySQL metrics: {e}")
            return {}
    
    def collect_table_metrics(self) -> Dict[str, Any]:
        """Collect table and schema information.

        H5: this used to issue FOUR separate scans of information_schema —
        `GROUP BY ENGINE` twice (once for sizes, once for counts), a third
        `SUM(DATA_LENGTH)` over everything, and a scan of
        information_schema.STATISTICS. On MariaDB (every version) and MySQL 5.7,
        any query touching DATA_LENGTH forces a table-open of EVERY table, so on
        the 50k-200k-table cPanel/DirectAdmin servers this tool targets that was
        minutes of heavy I/O, four times over.

        It is now ONE scan. Engine counts and the size totals are derived from
        the same result set, and the STATISTICS scan — the most expensive of the
        four — is gone entirely because nothing ever consumed its output.
        """
        self.logger.debug("Collecting table metrics...")

        try:
            table_count = self._get_table_count()
            if table_count:
                self._warn_if_large_scan(table_count)

            table_stats = self._get_table_statistics()
            return {
                'table_stats': table_stats,
                'table_stats_uncollected': not bool(table_stats),
                'table_count_authoritative': table_count,
                # Derived from table_stats — no second scan (H5).
                'engine_distribution': {
                    engine: stats.get('table_count', 0)
                    for engine, stats in table_stats.items()
                    if engine != 'totals' and isinstance(stats, dict)
                },
                'data_size': {
                    'total_data_size': table_stats.get('totals', {}).get('total_data_size', 0),
                    'total_index_size': table_stats.get('totals', {}).get('total_index_size', 0),
                    'total_size': table_stats.get('totals', {}).get('total_size', 0),
                },
                'database_names': self._get_database_names()
            }
        except Exception as e:
            self.logger.error(f"Failed to collect table metrics: {e}")
            return {
                'table_stats': {},
                'table_stats_uncollected': True,
                'table_count_authoritative': 0,
            }

    def _get_table_count(self) -> int:
        """Cheap table count — no size columns, so no table-open storm.

        Used only to warn before the expensive scan; a failure here must not
        block collection.
        """
        try:
            result = self.mysql_connector.execute_query(
                """
                SELECT COUNT(*) AS n
                FROM information_schema.TABLES
                WHERE TABLE_SCHEMA NOT IN
                    ('information_schema', 'performance_schema', 'mysql', 'sys')
                """,
                retry=False,
            )
            if result:
                return _safe_int(result[0].get('n'), 0)
        except Exception as e:
            self.logger.debug(f"Could not pre-count tables: {e}")
        return 0

    def _warn_if_large_scan(self, table_count: int) -> None:
        """Tell the operator BEFORE a long scan, not after it times out (H5)."""
        if table_count >= 20000:
            self.logger.warning(
                "Scanning information_schema for %s tables. On MariaDB and "
                "MySQL 5.7 this opens every table to read its size and can take "
                "several minutes on a busy server — this is a read-only query "
                "and safe to interrupt.",
                f"{table_count:,}",
            )
        else:
            self.logger.info("Scanning information_schema for %s tables ...",
                             f"{table_count:,}")
    
    def collect_performance_metrics(self) -> Dict[str, Any]:
        """Collect performance-related metrics"""
        self.logger.debug("Collecting performance metrics...")
        
        try:
            return {
                'innodb_status': self._get_innodb_status(),
                'process_list': self._get_process_list(),
                'slow_queries': self._get_slow_query_info(),
                'error_log': self._analyze_error_log()
            }
        except Exception as e:
            self.logger.error(f"Failed to collect performance metrics: {e}")
            return {}
    
    def _get_mysql_version(self) -> Dict[str, str]:
        """Get MySQL version information with fallback to SHOW VARIABLES."""
        try:
            result = self.mysql_connector.execute_query("SELECT VERSION() as version", retry=False)
            if result and result[0].get('version'):
                version_string = str(result[0]['version']).strip()
                if version_string:
                    return {
                        'version_string': version_string,
                        'is_mariadb': 'MariaDB' in version_string,
                        'major_version': self._extract_major_version(version_string)
                    }
        except Exception as e:
            self.logger.debug(f"SELECT VERSION() failed: {e}")

        # Fallback to SHOW VARIABLES
        try:
            res_var = self.mysql_connector.execute_query(
                "SHOW VARIABLES WHERE Variable_name IN ('version', 'version_comment')",
                retry=False,
            )
            if res_var:
                var_map = {r.get('Variable_name'): r.get('Value') for r in res_var}
                ver = str(var_map.get('version') or '').strip()
                comment = str(var_map.get('version_comment') or '').strip()
                if ver:
                    if comment and 'MariaDB' in comment and 'MariaDB' not in ver:
                        ver = f"{ver}-{comment}"
                    return {
                        'version_string': ver,
                        'is_mariadb': 'MariaDB' in ver,
                        'major_version': self._extract_major_version(ver),
                    }
        except Exception as e:
            self.logger.debug(f"SHOW VARIABLES version fallback failed: {e}")

        return {}
    
    def _get_mysql_variables(self) -> Dict[str, Any]:
        """Get MySQL configuration variables"""
        try:
            result = self.mysql_connector.execute_query("SHOW VARIABLES")
            if result:
                return {row['Variable_name']: row['Value'] for row in result}
        except Exception as e:
            self.logger.error(f"Failed to get MySQL variables: {e}")
        
        return {}
    
    def _get_mysql_status(self) -> Dict[str, Any]:
        """Get MySQL status variables"""
        try:
            result = self.mysql_connector.execute_query("SHOW STATUS")
            if result:
                return {row['Variable_name']: row['Value'] for row in result}
        except Exception as e:
            self.logger.error(f"Failed to get MySQL status: {e}")
        
        return {}
    
    def _get_storage_engines(self) -> Dict[str, Any]:
        """Get available storage engines"""
        try:
            result = self.mysql_connector.execute_query("SHOW ENGINES")
            if result:
                engines = {}
                for row in result:
                    engines[row['Engine']] = {
                        'support': row['Support'],
                        'comment': row.get('Comment', ''),
                        'transactions': row.get('Transactions', ''),
                        'xa': row.get('XA', ''),
                        'savepoints': row.get('Savepoints', '')
                    }
                return engines
        except Exception as e:
            self.logger.error(f"Failed to get storage engines: {e}")
        
        return {}
    
    def _get_table_statistics(self) -> Dict[str, Any]:
        """Get table statistics from information_schema"""
        try:
            query = """
            SELECT 
                ENGINE,
                COUNT(*) as table_count,
                SUM(DATA_LENGTH) as data_size,
                SUM(INDEX_LENGTH) as index_size,
                SUM(DATA_LENGTH + INDEX_LENGTH) as total_size
            FROM information_schema.TABLES 
            WHERE TABLE_SCHEMA NOT IN ('information_schema', 'performance_schema', 'mysql', 'sys')
            GROUP BY ENGINE
            """

            # H5: no blind retry. A read_timeout on this scan surfaces as
            # "Lost connection to MySQL server during query" — indistinguishable
            # from a genuine disconnect — so the automatic retry re-ran a query
            # that had just consumed the entire timeout budget: double the load
            # on an already-struggling server, then the same failure.
            result = self.mysql_connector.execute_query(query, retry=False)
            if result:
                stats = {}
                total_tables = 0
                total_data_size = 0
                total_index_size = 0
                
                for row in result:
                    engine = row['ENGINE'] or 'Unknown'
                    stats[engine] = {
                        'table_count': _safe_int(row.get('table_count')),
                        'data_size': _safe_int(row.get('data_size')),
                        'index_size': _safe_int(row.get('index_size')),
                        'total_size': _safe_int(row.get('total_size')),
                    }
                    total_tables += stats[engine]['table_count']
                    total_data_size += stats[engine]['data_size']
                    total_index_size += stats[engine]['index_size']
                
                stats['totals'] = {
                    'total_tables': total_tables,
                    'total_data_size': total_data_size,
                    'total_index_size': total_index_size,
                    'total_size': total_data_size + total_index_size
                }
                
                return stats
        except Exception as e:
            self.logger.error(f"Failed to get table statistics: {e}")
        
        return {}
    
    def _get_engine_distribution(self) -> Dict[str, int]:
        """Get distribution of tables by storage engine"""
        try:
            query = """
            SELECT ENGINE, COUNT(*) as count
            FROM information_schema.TABLES 
            WHERE TABLE_SCHEMA NOT IN ('information_schema', 'performance_schema', 'mysql', 'sys')
            GROUP BY ENGINE
            """
            
            result = self.mysql_connector.execute_query(query)
            if result:
                return {row['ENGINE'] or 'Unknown': int(row['count']) for row in result}
        except Exception as e:
            self.logger.error(f"Failed to get engine distribution: {e}")
        
        return {}
    
    def _get_database_names(self) -> list:
        """Get the list of user (non-system) database names."""
        try:
            query = """
            SELECT SCHEMA_NAME
            FROM information_schema.SCHEMATA
            WHERE SCHEMA_NAME NOT IN
                ('information_schema', 'performance_schema', 'mysql', 'sys')
            """
            result = self.mysql_connector.execute_query(query)
            if result:
                return [row.get('SCHEMA_NAME') for row in result if row.get('SCHEMA_NAME')]
        except Exception as e:
            self.logger.debug(f"Could not get database names: {e}")
        return []

    def _get_data_size_info(self) -> Dict[str, int]:
        """Get database size information"""
        try:
            query = """
            SELECT 
                SUM(DATA_LENGTH) as total_data_size,
                SUM(INDEX_LENGTH) as total_index_size,
                SUM(DATA_LENGTH + INDEX_LENGTH) as total_size
            FROM information_schema.TABLES 
            WHERE TABLE_SCHEMA NOT IN ('information_schema', 'performance_schema', 'mysql', 'sys')
            """
            
            result = self.mysql_connector.execute_query(query)
            if result and result[0]:
                row = result[0]
                return {
                    'total_data_size': int(row['total_data_size'] or 0),
                    'total_index_size': int(row['total_index_size'] or 0),
                    'total_size': int(row['total_size'] or 0)
                }
        except Exception as e:
            self.logger.error(f"Failed to get data size info: {e}")
        
        return {}
    
    def _get_index_statistics(self) -> Dict[str, Any]:
        """Get index usage statistics"""
        try:
            # This would require performance_schema to be enabled
            query = """
            SELECT 
                COUNT(*) as total_indexes,
                SUM(CASE WHEN CARDINALITY = 0 THEN 1 ELSE 0 END) as unused_indexes
            FROM information_schema.STATISTICS 
            WHERE TABLE_SCHEMA NOT IN ('information_schema', 'performance_schema', 'mysql', 'sys')
            """
            
            result = self.mysql_connector.execute_query(query)
            if result and result[0]:
                return {
                    'total_indexes': int(result[0]['total_indexes'] or 0),
                    'unused_indexes': int(result[0]['unused_indexes'] or 0)
                }
        except Exception as e:
            self.logger.debug(f"Could not get index statistics: {e}")
        
        return {}
    
    def _get_innodb_status(self) -> Dict[str, Any]:
        """Get InnoDB engine status"""
        try:
            result = self.mysql_connector.execute_query("SHOW ENGINE INNODB STATUS")
            if result and result[0]:
                status_text = result[0]['Status']
                return self._parse_innodb_status(status_text)
        except Exception as e:
            self.logger.error(f"Failed to get InnoDB status: {e}")
        
        return {}
    
    def _get_process_list(self) -> Dict[str, Any]:
        """Get current process list"""
        try:
            result = self.mysql_connector.execute_query("SHOW PROCESSLIST")
            if result:
                processes = {
                    'total_processes': len(result),
                    'active_processes': len([p for p in result if p.get('Command') != 'Sleep']),
                    'sleeping_processes': len([p for p in result if p.get('Command') == 'Sleep']),
                    # L6: PROCESSLIST.Time is NULL for some system threads;
                    # int(None) raised TypeError and the outer handler then
                    # discarded EVERY process-list metric.
                    'longest_running': max(
                        [_safe_int(p.get('Time'), 0) for p in result], default=0
                    )
                }
                return processes
        except Exception as e:
            self.logger.error(f"Failed to get process list: {e}")
        
        return {}
    
    def _get_slow_query_info(self) -> Dict[str, Any]:
        """Get slow query log information"""
        try:
            # Check if slow query log is enabled
            slow_query_log = self.mysql_connector.execute_query(
                "SHOW VARIABLES LIKE 'slow_query_log'"
            )
            
            if slow_query_log and slow_query_log[0]['Value'] == 'ON':
                # Get slow query log file location
                log_file = self.mysql_connector.execute_query(
                    "SHOW VARIABLES LIKE 'slow_query_log_file'"
                )
                
                if log_file:
                    return {
                        'enabled': True,
                        'log_file': log_file[0]['Value'],
                        'long_query_time': self._get_variable_value('long_query_time')
                    }
            
            return {'enabled': False}
        except Exception as e:
            self.logger.error(f"Failed to get slow query info: {e}")
        
        return {}
    
    def _analyze_error_log(self) -> Dict[str, Any]:
        """Analyze MySQL error log for issues"""
        try:
            # Get error log location
            error_log = self._get_variable_value('log_error')
            if not error_log or not os.path.exists(error_log):
                return {'analyzed': False, 'reason': 'Error log not accessible'}
            
            # Read last 100 lines of error log
            try:
                result = subprocess.run(
                    ['tail', '-n', '100', error_log],
                    capture_output=True, text=True, timeout=10
                )
                
                if result.returncode == 0:
                    log_content = result.stdout
                    return self._parse_error_log(log_content)
            except subprocess.TimeoutExpired:
                self.logger.warning("Error log analysis timed out")
            except Exception as e:
                self.logger.warning(f"Could not read error log: {e}")
            
            return {'analyzed': False, 'reason': 'Could not read error log'}
        except Exception as e:
            self.logger.error(f"Failed to analyze error log: {e}")
        
        return {}
    
    def _get_config_file_info(self) -> Dict[str, Any]:
        """Get MySQL configuration file information.

        M10: the third and last private copy of the my.cnf candidate list. It
        had only 4 of the 7 paths, so on a Debian/Ubuntu box it could report a
        different file than the one the tool actually writes.
        """
        try:
            from utils.mycnf_paths import find_mycnf_or_none

            location = find_mycnf_or_none()
            if location:
                stat = os.stat(location)
                return {
                    'path': location,
                    'size': stat.st_size,
                    'modified': datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    'readable': os.access(location, os.R_OK),
                    'writable': os.access(location, os.W_OK)
                }

            return {'found': False}
        except Exception as e:
            self.logger.error(f"Failed to get config file info: {e}")
        
        return {}
    
    def _normalize_metrics(self, metrics: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize metrics into a flat structure for easier analysis"""
        normalized = {}
        
        # System metrics
        system = metrics.get('system', {})
        memory = system.get('memory', {})
        cpu = system.get('cpu', {})
        
        normalized.update({
            'total_ram_mb': memory.get('total_mb', 0),
            'available_ram_mb': memory.get('available_mb', 0),
            'used_ram_mb': memory.get('used_mb', 0),
            'cpu_cores': cpu.get('cores', 1),
            'cpu_model': cpu.get('model', 'Unknown'),
            'load_average_1m': system.get('load', {}).get('1min', 0),
            'load_average_5m': system.get('load', {}).get('5min', 0),
            'load_average_15m': system.get('load', {}).get('15min', 0)
        })
        
        # MySQL configuration
        mysql = metrics.get('mysql', {})
        variables = mysql.get('variables', {})
        status = mysql.get('status', {})

        # MySQL/MariaDB version string (used by version_compat layer)
        mysql_version_info = mysql.get('version', {})
        version_str = str(mysql_version_info.get('version_string') or '').strip()
        if not version_str:
            version_str = str(variables.get('version') or '').strip()
            comment = str(variables.get('version_comment') or '').strip()
            if comment and 'MariaDB' in comment and 'MariaDB' not in version_str:
                version_str = f"{version_str}-{comment}"
        normalized['mysql_version'] = version_str

        # M5/C1: the real data directory. Storage-class detection resolves the
        # device behind THIS path rather than guessing from all block devices,
        # and the redo-log guard checks free space on THIS filesystem.
        normalized['datadir'] = variables.get('datadir', '') or ''
        
        # Convert MySQL variables to appropriate types
        normalized.update({
            'innodb_buffer_pool_size_mb': self._convert_size_to_mb(
                variables.get('innodb_buffer_pool_size', '0')
            ),
            # L7: the only variable conversion that bypassed _safe_int — a
            # non-numeric value would raise inside _normalize_metrics and take
            # the whole collection down with it.
            'innodb_buffer_pool_instances': _safe_int(
                variables.get('innodb_buffer_pool_instances'), 1
            ),
            'innodb_log_file_size_mb': self._convert_size_to_mb(
                variables.get('innodb_log_file_size', '0')
            ),
            'max_connections': _safe_int(variables.get('max_connections'), 0),
            'table_open_cache': _safe_int(variables.get('table_open_cache'), 0),
            'table_definition_cache': _safe_int(variables.get('table_definition_cache'), 400),
            'thread_cache_size': _safe_int(variables.get('thread_cache_size'), 8),
            'open_files_limit': _safe_int(variables.get('open_files_limit'), 0),
            'join_buffer_size_kb': self._convert_size_to_kb(
                variables.get('join_buffer_size', '0')
            ),
            'key_buffer_size_mb': self._convert_size_to_mb(
                variables.get('key_buffer_size', '0')
            ),
            'tmp_table_size_mb': self._convert_size_to_mb(
                variables.get('tmp_table_size', '0')
            ),
            'max_heap_table_size_mb': self._convert_size_to_mb(
                variables.get('max_heap_table_size', '0')
            ),
            # Storage-aware I/O tuning (consumed by the storage-type recommender)
            'innodb_io_capacity': _safe_int(variables.get('innodb_io_capacity'), 200),
            'innodb_io_capacity_max': _safe_int(variables.get('innodb_io_capacity_max'), 2000),
            'innodb_flush_neighbors': _safe_int(variables.get('innodb_flush_neighbors'), 1),
            # Per-connection buffers (consumed by the memory-footprint estimate)
            'sort_buffer_size_kb': self._convert_size_to_kb(
                variables.get('sort_buffer_size', '0')
            ),
            'read_buffer_size_kb': self._convert_size_to_kb(
                variables.get('read_buffer_size', '0')
            ),
            'read_rnd_buffer_size_kb': self._convert_size_to_kb(
                variables.get('read_rnd_buffer_size', '0')
            ),
            'thread_stack_kb': self._convert_size_to_kb(
                variables.get('thread_stack', '0')
            ),
        })

        # MySQL status
        normalized.update({
            'max_used_connections': _safe_int(status.get('Max_used_connections'), 0),
            'threads_connected': _safe_int(status.get('Threads_connected'), 0),
            'threads_running': _safe_int(status.get('Threads_running'), 0),
            'threads_created': _safe_int(status.get('Threads_created'), 0),
            'connections': _safe_int(status.get('Connections'), 0),
            'total_connects': _safe_int(status.get('Connections'), 0),
            'aborted_connects': _safe_int(status.get('Aborted_connects'), 0),
            'created_tmp_tables': _safe_int(status.get('Created_tmp_tables'), 0),
            'created_tmp_disk_tables': _safe_int(status.get('Created_tmp_disk_tables'), 0),
            'select_full_join': _safe_int(status.get('Select_full_join'), 0),
            'slow_queries': _safe_int(status.get('Slow_queries'), 0),
            'uptime': _safe_int(status.get('Uptime'), 0),
            'questions': _safe_int(status.get('Questions'), 0)
        })
        
        # Table metrics
        tables = metrics.get('tables', {})
        table_stats = tables.get('table_stats', {})
        engine_dist = tables.get('engine_distribution', {})
        
        normalized.update({
            'total_tables': sum(engine_dist.values()) or tables.get('table_count_authoritative', 0),
            'table_count_authoritative': tables.get('table_count_authoritative', 0),
            'innodb_tables': engine_dist.get('InnoDB', 0),
            'myisam_tables': engine_dist.get('MyISAM', 0),
            # Aliases consumed by the decision engine (migration-state logic)
            'innodb_table_count': engine_dist.get('InnoDB', 0),
            'myisam_table_count': engine_dist.get('MyISAM', 0),
            # Per-engine sizes consumed by the recommendation engine.
            #
            # M4: innodb_data_size_mb is DATA + INDEX, because the InnoDB buffer
            # pool caches index pages exactly as it caches data pages — they are
            # the same 16K pages in the same pool. Using DATA_LENGTH alone
            # systematically undersized the buffer pool on any normalised
            # schema, where indexes routinely equal or exceed the data (2 GB
            # data + 3 GB indexes was sized as if the working set were 2 GB).
            'innodb_data_size_mb': (
                table_stats.get('InnoDB', {}).get('data_size', 0)
                + table_stats.get('InnoDB', {}).get('index_size', 0)
            ) // (1024 * 1024),
            # MyISAM is the opposite case and needs BOTH numbers: key_buffer_size
            # caches ONLY indexes (MyISAM row data is left to the OS page cache),
            # so the key-buffer recommendation must be sized from index_size —
            # it was previously sized from data_size, which is the one number
            # the key buffer never holds.
            'myisam_data_size_mb': table_stats.get('MyISAM', {}).get('data_size', 0) // (1024 * 1024),
            'myisam_index_size_mb': table_stats.get('MyISAM', {}).get('index_size', 0) // (1024 * 1024),
            'total_data_size_mb': table_stats.get('totals', {}).get('total_data_size', 0) // (1024 * 1024),
            'total_index_size_mb': table_stats.get('totals', {}).get('total_index_size', 0) // (1024 * 1024),
            'database_names': tables.get('database_names', []),
            'table_stats_uncollected': tables.get('table_stats_uncollected', not bool(table_stats))
        })
        
        # Calculate derived metrics
        if normalized['created_tmp_tables'] > 0:
            normalized['tmp_disk_table_percentage'] = (
                normalized['created_tmp_disk_tables'] / normalized['created_tmp_tables']
            )
        else:
            normalized['tmp_disk_table_percentage'] = 0
        
        if normalized['max_connections'] > 0:
            normalized['connection_usage_percentage'] = (
                normalized['max_used_connections'] / normalized['max_connections']
            )
        else:
            normalized['connection_usage_percentage'] = 0

        # Queries per second (consumed by peak-hour detection)
        if normalized['uptime'] > 0:
            normalized['queries_per_second'] = round(
                normalized['questions'] / normalized['uptime'], 2
            )
        else:
            normalized['queries_per_second'] = 0

        # Swap usage in GB (consumed by memory-pressure anomaly detection).
        # Derived from /proc/meminfo when available (SwapTotal/SwapFree in kB).
        swap_raw = memory.get('raw_data', {}) if isinstance(memory, dict) else {}
        swap_total_kb = swap_raw.get('SwapTotal', 0)
        swap_free_kb = swap_raw.get('SwapFree', 0)
        swap_used_kb = max(0, swap_total_kb - swap_free_kb)
        normalized['swap_usage_gb'] = round(swap_used_kb / (1024 * 1024), 2)

        # InnoDB buffer pool hit rate (consumed by the confidence engine)
        perf = metrics.get('performance', {})
        innodb_status = perf.get('innodb_status', {}) if isinstance(perf, dict) else {}
        normalized['innodb_buffer_pool_hit_rate'] = innodb_status.get(
            'buffer_pool_hit_rate', 0
        )

        # Reachability flag: True only if we actually spoke to the server.
        # SHOW VARIABLES/STATUS return rows on any successful connection, so an
        # empty pair here means the connection failed and every metric above is
        # a default (all-zero). Consumers MUST refuse to act on that (see C4).
        normalized['mysql_reachable'] = bool(variables) and bool(status)

        # Store original metrics for detailed analysis
        normalized['_raw_metrics'] = metrics

        return normalized
    
    def _convert_size_to_bytes(self, size_str) -> int:
        """Convert a MySQL size value (raw bytes or K/M/G/T suffixed) to bytes.

        M15: this is now the single conversion primitive; the MB and KB helpers
        derive from it. They used to convert independently, and the KB one went
        via MB — so any value of 1 MB or more was floored to a whole MB and then
        multiplied back up, e.g. a read_buffer_size of 1572864 (1.5M) came out
        as 1024 KB, a 33% undercount. Those five per-connection buffers feed the
        WORST-CASE MEMORY FOOTPRINT panel, whose entire purpose is precision.
        """
        if size_str is None:
            return 0
        s = str(size_str).upper().strip()
        if not s or s == '0':
            return 0

        multipliers = {
            'K': 1024,
            'M': 1024 ** 2,
            'G': 1024 ** 3,
            'T': 1024 ** 4,
        }
        try:
            if s.isdigit():
                return int(s)
            if s[-1] in multipliers:
                return int(float(s[:-1]) * multipliers[s[-1]])
            return int(float(s))
        except (ValueError, TypeError):
            pass

        # Last resort: pull the leading number out and treat it as bytes.
        match = re.search(r'(\d+)', s)
        return int(match.group(1)) if match else 0

    def _convert_size_to_mb(self, size_str) -> int:
        """Convert a MySQL size value to whole MB (floor)."""
        return self._convert_size_to_bytes(size_str) // (1024 * 1024)

    def _convert_size_to_kb(self, size_str) -> int:
        """Convert a MySQL size value to whole KB (floor).

        Derived straight from bytes — no MB round-trip — so sub-MB values and
        non-MB-multiple values are both exact to the kilobyte (M15).
        """
        return self._convert_size_to_bytes(size_str) // 1024

    def _get_variable_value(self, variable_name: str) -> Optional[str]:
        """Get a specific MySQL variable value"""
        try:
            result = self.mysql_connector.execute_query(
                "SHOW VARIABLES LIKE %s", (variable_name,)
            )
            if result and result[0]:
                return result[0]['Value']
        except Exception:
            pass
        return None
    
    def _extract_major_version(self, version_string: str) -> str:
        """Extract major version from version string"""
        import re
        match = re.search(r'(\d+\.\d+)', version_string)
        return match.group(1) if match else 'Unknown'
    
    def _parse_innodb_status(self, status_text: str) -> Dict[str, Any]:
        """Parse InnoDB status output"""
        # This is a simplified parser - could be expanded
        parsed = {}
        
        try:
            # Extract buffer pool hit rate
            import re
            hit_rate_match = re.search(r'Buffer pool hit rate (\d+) / (\d+)', status_text)
            if hit_rate_match:
                hits = int(hit_rate_match.group(1))
                total = int(hit_rate_match.group(2))
                if total > 0:
                    parsed['buffer_pool_hit_rate'] = hits / total
            
            # Extract other useful metrics
            # This could be expanded to parse more InnoDB status information
            
        except Exception as e:
            self.logger.debug(f"Error parsing InnoDB status: {e}")
        
        return parsed
    
    def _parse_error_log(self, log_content: str) -> Dict[str, Any]:
        """Parse error log content for issues"""
        parsed = {
            'analyzed': True,
            'warnings': 0,
            'errors': 0,
            'recent_issues': []
        }
        
        try:
            lines = log_content.split('\n')
            for line in lines:
                if '[Warning]' in line:
                    parsed['warnings'] += 1
                elif '[ERROR]' in line or '[Error]' in line:
                    parsed['errors'] += 1
                    # Store recent errors
                    if len(parsed['recent_issues']) < 5:
                        parsed['recent_issues'].append(line.strip())
        except Exception as e:
            self.logger.debug(f"Error parsing error log: {e}")
        
        return parsed


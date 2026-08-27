"""
Shared fixtures for the MySQL Auto-Tuner test suite.

The central piece is ``FakeConnector`` — a stand-in for ``MySQLConnector`` that
answers the exact queries the collector issues from a canned server profile.
That is deliberate: almost every serious bug found across three review rounds
was a *silent* one — a metric key that was never produced, a config key spelled
two ways, a guardrail matching the wrong parameter name. None of them raised.
A test that merely asserts "some recommendation came back" passes happily while
the recommendation is wrong, so these tests assert on VALUES.

Run with:  python -m pytest tests/ -v
(or:       python tests/run_tests.py   -- no pytest required)

Author: R.L. Burger (Steadfast Codeworks)
Date: 2026-08-24
Version: 1.0.4
Copyright (c) 2026 R.L. Burger
Project: Steadfast Tools
Website: https://www.steadfasttools.com
License: MIT License
"""

import importlib.util
import logging
import os
import sys

TOOL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TOOL_ROOT not in sys.path:
    sys.path.insert(0, TOOL_ROOT)

CONFIG_PATH = os.path.join(TOOL_ROOT, 'config_ultimate.yaml')

# The tool's entry point has a hyphen in its filename, so it cannot be imported
# normally. Load it once by path and reuse.
_spec = importlib.util.spec_from_file_location(
    'autotuner_main', os.path.join(TOOL_ROOT, 'mysql-autotuner.py')
)
autotuner_main = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(autotuner_main)

MySQLAutoTunerUltimate = autotuner_main.MySQLAutoTunerUltimate


# ---------------------------------------------------------------------------
# Canned server profiles
# ---------------------------------------------------------------------------

def server_profile(**overrides):
    """A realistic mid-size MariaDB server, overridable per test."""
    profile = {
        'version': '10.11.8-MariaDB',
        'total_ram_mb': 16384,
        'datadir': '/var/lib/mysql',
        'datadir_free_mb': 200000,
        'storage_type': 'ssd',
        'variables': {
            'version': '10.11.8-MariaDB',
            'datadir': '/var/lib/mysql',
            'innodb_buffer_pool_size': str(4 * 1024 ** 3),
            'innodb_buffer_pool_instances': '1',
            'innodb_log_file_size': str(48 * 1024 ** 2),
            'max_connections': '500',
            'table_open_cache': '4000',
            'table_definition_cache': '2000',
            'thread_cache_size': '8',
            'open_files_limit': '5000',
            'join_buffer_size': str(256 * 1024),
            'key_buffer_size': str(16 * 1024 ** 2),
            'tmp_table_size': str(16 * 1024 ** 2),
            'max_heap_table_size': str(16 * 1024 ** 2),
            'innodb_io_capacity': '200',
            'innodb_io_capacity_max': '2000',
            'innodb_flush_neighbors': '1',
            'sort_buffer_size': str(256 * 1024),
            'read_buffer_size': str(128 * 1024),
            'read_rnd_buffer_size': str(256 * 1024),
            'thread_stack': '299008',
        },
        'status': {
            'Max_used_connections': '120',
            'Threads_connected': '20',
            'Threads_running': '2',
            'Threads_created': '50',
            'Connections': '100000',
            'Aborted_connects': '10',
            'Created_tmp_tables': '10000',
            'Created_tmp_disk_tables': '100',
            'Select_full_join': '10',
            'Slow_queries': '5',
            'Uptime': str(300 * 3600),          # 12.5 days — past the 24h guard
            'Questions': '5000000',
        },
        # per engine: (table_count, data_bytes, index_bytes)
        'engines': {
            'InnoDB': (5000, 8 * 1024 ** 3, 4 * 1024 ** 3),
        },
        'databases': ['appdb', 'analytics'],
    }
    for key, value in overrides.items():
        if key in ('variables', 'status') and isinstance(value, dict):
            profile[key] = {**profile[key], **value}
        else:
            profile[key] = value
    return profile


class FakeConnector:
    """Answers exactly the queries DataCollector issues, from a profile dict."""

    def __init__(self, profile=None):
        self.profile = profile or server_profile()
        self.connection = True
        self.queries = []            # (kind, retry) for assertions about load

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _kind(query):
        q = ' '.join(query.split()).upper()
        if 'VERSION()' in q:
            return 'version'
        if q.startswith('SHOW VARIABLES') and 'LIKE' not in q:
            return 'variables'
        if q.startswith('SHOW STATUS'):
            return 'status'
        if 'COUNT(*) AS N' in q:
            return 'table_count'
        if 'GROUP BY ENGINE' in q:
            return 'engine_scan'
        if 'INFORMATION_SCHEMA.STATISTICS' in q:
            return 'statistics_scan'
        if 'SCHEMA_NAME' in q:
            return 'schemata'
        if 'SHOW ENGINES' in q:
            return 'engines'
        if 'PROCESSLIST' in q:
            return 'processlist'
        if 'INNODB STATUS' in q:
            return 'innodb_status'
        return 'other'

    def execute_query(self, query, params=None, retry=True):
        kind = self._kind(query)
        self.queries.append((kind, retry))
        p = self.profile

        if kind == 'version':
            return [{'version': p['version']}]
        if kind == 'variables':
            return [{'Variable_name': k, 'Value': v}
                    for k, v in p['variables'].items()]
        if kind == 'status':
            return [{'Variable_name': k, 'Value': v}
                    for k, v in p['status'].items()]
        if kind == 'table_count':
            return [{'n': sum(e[0] for e in p['engines'].values())}]
        if kind == 'engine_scan':
            return [
                {'ENGINE': engine, 'table_count': count,
                 'data_size': data, 'index_size': index,
                 'total_size': data + index}
                for engine, (count, data, index) in p['engines'].items()
            ]
        if kind == 'schemata':
            return [{'SCHEMA_NAME': d} for d in p['databases']]
        return []

    def count(self, kind):
        return sum(1 for k, _ in self.queries if k == kind)


class FakeSystemInfo:
    """SystemInfo stand-in — no /proc, no /sys, no subprocesses."""

    def __init__(self, profile=None):
        self.profile = profile or server_profile()
        self.logger = logging.getLogger(__name__)

    def get_memory_info(self):
        total = self.profile['total_ram_mb']
        avail = self.profile.get('available_ram_mb', int(total * 0.4))
        return {
            'total_mb': total, 'total_gb': total // 1024,
            'available_mb': avail, 'free_mb': avail,
            'used_mb': total - avail,
            'usage_percentage': (total - avail) / total * 100 if total else 0,
            'raw_data': {'SwapTotal': self.profile.get('swap_total_kb', 0),
                         'SwapFree': self.profile.get('swap_free_kb', 0)},
        }

    def get_cpu_info(self):
        return {'cores': 8, 'model': 'Test CPU',
                '1min': 1.0, '5min': 1.0, '15min': 1.0}

    def get_disk_info(self, datadir=None):
        return {'filesystems': [], 'total_space_gb': 500,
                'used_space_gb': 200, 'available_space_gb': 300,
                'measured_at': datadir or '/'}

    def get_load_average(self):
        return {'1min': 1.0, '5min': 1.0, '15min': 1.0}

    def get_uptime(self):
        return {'uptime_seconds': 300 * 3600, 'uptime_days': 12}

    def get_os_info(self):
        return {'os': 'TestOS', 'kernel': 'test', 'hostname': 'testhost'}

    def get_storage_type(self, datadir=None):
        return self.profile.get('storage_type', 'unknown')

    @staticmethod
    def get_free_space_mb(path):
        return 200000

    def get_system_metrics(self, datadir=None):
        mem = self.get_memory_info()
        return {
            'system_total_ram_mb': mem['total_mb'],
            'system_available_ram_mb': mem['available_mb'],
            'system_cpu_cores': 8,
            'system_load_1min': 1.0,
            'system_load_5min': 1.0,
            'system_load_15min': 1.0,
            'system_storage_type': self.get_storage_type(datadir),
            'datadir': datadir or '',
            'datadir_free_mb': self.profile.get('datadir_free_mb', 200000),
            'system_disk_total_gb': 500,
            'system_disk_avail_gb': 300,
        }


def build_tuner(profile=None, platform='auto', pass_number=1,
                profile_name='balanced'):
    """Assemble a fully wired tuner around the fakes — no DB, no filesystem."""
    from core.collector import DataCollector
    from core.ultimate_decision_engine import UltimateDecisionEngine
    from core.version_compat import VersionCompatibility
    from core.explain_engine import ExplainEngine
    from safety.checks import SafetyChecker
    from output.reporter import ReportGenerator
    import yaml

    profile = profile or server_profile()

    t = MySQLAutoTunerUltimate.__new__(MySQLAutoTunerUltimate)
    t.version = MySQLAutoTunerUltimate.VERSION
    t.config_file = CONFIG_PATH
    t.profile = 'balanced'
    t.allow_bp_shrink = False
    t.allow_cluster_restart = False
    t.mysql_overrides = {}
    t.logger = logging.getLogger('autotuner_test')
    t.config = yaml.safe_load(open(CONFIG_PATH))

    connector = FakeConnector(profile)
    system_info = FakeSystemInfo(profile)

    t.mysql_connector = connector
    t.system_info = system_info
    t.data_collector = DataCollector(connector, system_info)
    t.decision_engine = UltimateDecisionEngine(t.config)
    t.decision_engine.current_pass = pass_number
    t.version_compat = VersionCompatibility()
    t.explain_engine = ExplainEngine()

    safety_cfg = dict(t.config.get('fallback_logic', {}) or {})
    safety_cfg.setdefault('max_buffer_pool_percentage', 0.8)
    safety_cfg.setdefault('min_available_ram_mb', 2048)
    t.safety_checker = SafetyChecker(safety_cfg)
    t.report_generator = ReportGenerator({'save_report': False})
    t.apply_profile(profile_name)

    t._test_connector = connector
    t._test_platform = platform
    return t


def analyze(profile=None, platform='auto', pass_number=1,
            profile_name='balanced'):
    """Run the full analysis pipeline and return (results, tuner)."""
    t = build_tuner(profile, platform, pass_number, profile_name)
    return t.analyze_system(platform=platform), t


def rec_for(results, parameter):
    """Return the recommendation for *parameter*, or None."""
    for rec in results.get('recommendations', []):
        if rec.get('parameter') == parameter:
            return rec
    return None


def value_for(results, parameter):
    rec = rec_for(results, parameter)
    return rec.get('recommended_value') if rec else None

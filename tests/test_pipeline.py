"""
End-to-end pipeline tests.

These drive the real ``analyze_system`` against canned server profiles and
assert on the numbers it produces. They are the tests that would have caught
Round 2's H1 — the class of bug where the pipeline runs happily to completion
while a formula is quietly computing from a permanent zero.

Author: R.L. Burger (Steadfast Codeworks)
Date: 2026-08-24
Version: 1.0.4
License: MIT License
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from conftest import (                                    # noqa: E402
    CONFIG_PATH, analyze, build_tuner, rec_for, server_profile, value_for,
)


def _mb(value):
    return int(str(value).rstrip('M'))


# ----------------------------------------------------------------------
# The metric contract (Round 2 H1)
# ----------------------------------------------------------------------

def test_every_consumed_metric_key_is_produced():
    """R2 H1: the engine read keys _normalize_metrics never emitted.

    Each missing key silently became a zero-default inside a formula. This is
    the assertion that makes that class of bug loud.
    """
    from core.config_audit import audit_metrics

    tuner = build_tuner()
    metrics = tuner.data_collector.collect_all_metrics()
    metrics.update(tuner.system_info.get_system_metrics(metrics.get('datadir')))
    missing = audit_metrics(metrics)
    assert missing == [], f"collector does not produce: {missing}"


def test_config_contract_holds():
    from core.config_audit import audit_config
    from core.ultimate_decision_engine import UltimateDecisionEngine
    import yaml

    report = audit_config(yaml.safe_load(open(CONFIG_PATH)),
                          UltimateDecisionEngine.ANOMALY_NAMES)
    assert report['contract'] == [], '\n'.join(report['contract'])
    assert report['dead'] == [], f"config read by nothing: {report['dead']}"


# ----------------------------------------------------------------------
# Recommendation sanity
# ----------------------------------------------------------------------

def test_buffer_pool_stays_within_ram_and_reserves_headroom():
    for ram in (2048, 4096, 16384, 65536, 131072):
        results, tuner = analyze(server_profile(total_ram_mb=ram))
        value = value_for(results, 'innodb_buffer_pool_size')
        if value is None:
            continue
        pool = _mb(value)
        reserve = tuner.safety_checker.os_reserve_mb(ram)
        assert pool <= ram - reserve, \
            f"{ram}MB RAM: pool {pool}M leaves less than {reserve}M for the OS"
        assert pool > 0


def test_healthy_buffer_pool_is_never_shrunk_without_opt_in():
    """H2: a mis-derived value must not shrink a correctly tuned pool."""
    profile = server_profile(
        total_ram_mb=131072,
        variables={'innodb_buffer_pool_size': str(64 * 1024 ** 3)},
        engines={'InnoDB': (100, 10 * 1024 ** 2, 1024 ** 2)},   # tiny dataset
    )
    results, _ = analyze(profile)
    value = value_for(results, 'innodb_buffer_pool_size')
    if value is not None:
        current_mb = 64 * 1024
        assert _mb(value) >= current_mb * 0.8, \
            f"shrank a healthy 64G pool to {value} without --allow-buffer-pool-shrink"


def test_counter_driven_recommendations_suppressed_under_24h_uptime():
    """Status counters are meaningless right after a restart."""
    fresh = server_profile(status={'Uptime': str(2 * 3600),
                                   'Created_tmp_disk_tables': '9000',
                                   'Created_tmp_tables': '10000',
                                   'Select_full_join': '90000',
                                   'Threads_created': '9000'})
    results, _ = analyze(fresh)
    for param in ('tmp_table_size', 'join_buffer_size', 'thread_cache_size'):
        assert rec_for(results, param) is None, \
            f"{param} recommended from counters on a 2h-old server"

    settled = server_profile(status={'Uptime': str(300 * 3600),
                                     'Created_tmp_disk_tables': '9000',
                                     'Created_tmp_tables': '10000',
                                     'Select_full_join': '90000',
                                     'Threads_created': '9000'})
    results, _ = analyze(settled)
    assert rec_for(results, 'tmp_table_size') is not None


def test_raise_only_parameters_never_downgrade():
    """H3: several recommendations could LOWER an already-correct value."""
    profile = server_profile(variables={
        'tmp_table_size': str(256 * 1024 ** 2),
        'join_buffer_size': str(8 * 1024 ** 2),
        'thread_cache_size': '256',
        'table_definition_cache': '20000',
    }, status={'Created_tmp_disk_tables': '9000',
               'Created_tmp_tables': '10000',
               'Select_full_join': '90000',
               'Threads_created': '9000'})
    results, _ = analyze(profile)
    for param, current in (('tmp_table_size', 256),
                           ('join_buffer_size', 8 * 1024),
                           ('thread_cache_size', 256),
                           ('table_definition_cache', 20000)):
        rec = rec_for(results, param)
        if rec is None:
            continue
        proposed = int(str(rec['recommended_value']).rstrip('MK'))
        assert proposed >= current, \
            f"{param} downgraded {current} -> {proposed}"


def test_flash_io_tuning_only_on_flash():
    for storage, expect in (('nvme', True), ('ssd', True),
                            ('hdd', False), ('unknown', False)):
        results, _ = analyze(server_profile(storage_type=storage))
        has_io = rec_for(results, 'innodb_io_capacity') is not None
        assert has_io is expect, \
            f"{storage}: innodb_io_capacity recommended={has_io}"


def test_deprecated_parameters_are_filtered_per_version():
    """C2: innodb_buffer_pool_instances stops MariaDB 10.6+ from starting."""
    cases = [('10.5.20-MariaDB', True), ('10.6.16-MariaDB', False),
             ('11.4.2-MariaDB', False), ('8.4.0', True)]
    for version, should_keep in cases:
        profile = server_profile(
            version=version,
            variables={'version': version,
                       'innodb_buffer_pool_size': str(8 * 1024 ** 3)},
            total_ram_mb=131072,
        )
        results, _ = analyze(profile)
        present = rec_for(results, 'innodb_buffer_pool_instances') is not None
        assert present is should_keep, \
            f"{version}: innodb_buffer_pool_instances present={present}"


def test_unreachable_database_aborts_before_recommending():
    """C4: all-zero metrics used to flow straight into a config rewrite."""
    from conftest import FakeConnector

    class DeadConnector(FakeConnector):
        def execute_query(self, query, params=None, retry=True):
            return []

    tuner = build_tuner()
    tuner.data_collector.mysql_connector = DeadConnector()
    tuner.mysql_connector = tuner.data_collector.mysql_connector
    try:
        tuner.analyze_system()
        assert False, "must refuse to analyse an unreachable database"
    except ConnectionError:
        pass


# ----------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------

def test_every_format_carries_top_databases():
    """M4/L5: json, then html, each silently dropped this data."""
    from core.top_databases import mock_top_databases

    results, tuner = analyze()
    for fmt in ('console', 'text', 'json', 'html'):
        report = tuner.generate_report(results, output_format=fmt,
                                       top_databases=mock_top_databases())
        assert 'clientA_wordpress' in report, f"{fmt} dropped top-databases"


def test_text_format_is_ansi_free():
    results, tuner = analyze()
    assert '\033[' not in tuner.generate_report(results, output_format='text')


def test_json_report_is_valid_and_versioned():
    import json
    from utils.version import TOOL_VERSION

    results, tuner = analyze()
    payload = json.loads(tuner.generate_report(results, output_format='json'))
    assert payload['meta']['version'] == TOOL_VERSION
    assert isinstance(payload['recommendations'], list)


def test_reports_survive_a_non_utf8_stdout():
    """The verification table prints AFTER a restart — a crash there loses it."""
    from utils.safe_io import supports_unicode

    verification = [
        {'parameter': 'innodb_buffer_pool_size', 'intended': '4096M',
         'effective': '4096M', 'status': 'applied'},
        {'parameter': 'max_connections', 'intended': 300,
         'effective': '151', 'status': 'mismatch'},
    ]
    table = build_tuner()._render_verification_table(verification, ['/etc/my.cnf.d/'])
    assert 'APPLIED' in table and 'MISMATCH' in table
    if not supports_unicode():
        table.encode('ascii')          # must not raise


# ----------------------------------------------------------------------
# my.cnf rewriting
# ----------------------------------------------------------------------

def test_mycnf_rewrite_preserves_structure_and_comments():
    from conftest import MySQLAutoTunerUltimate as T

    original = ['# Managed by config management\n', '[client]\n',
                'port = 3306\n', '\n', '[mysqld]\n',
                'innodb_buffer_pool_size = 1G  # sized after the 2024 incident\n',
                'key_buffer_size = 16M\n', '\n', '[mysqldump]\n',
                'quick\n']
    new = T._build_mycnf_lines(original, {'innodb_buffer_pool_size': '4096M',
                                          'max_connections': '400'})
    text = ''.join(new)
    assert 'innodb_buffer_pool_size = 4096M' in text
    assert '# sized after the 2024 incident' in text, "M11: comment lost"
    assert 'max_connections = 400' in text
    assert '[client]' in text and 'port = 3306' in text
    assert '[mysqldump]' in text and 'quick' in text
    assert 'key_buffer_size = 16M' in text
    assert text.count('[mysqld]') == 1


def test_mycnf_rewrite_adds_section_when_absent():
    from conftest import MySQLAutoTunerUltimate as T

    original = ['[client]\n', 'port=3306\n', '\n',
                '!includedir /etc/mysql/conf.d/\n']
    text = ''.join(T._build_mycnf_lines(original,
                                        {'innodb_buffer_pool_size': '2048M'}))
    assert '[mysqld]' in text
    # Must land AFTER the includedir, otherwise the included files win.
    assert text.index('!includedir') < text.index('[mysqld]')

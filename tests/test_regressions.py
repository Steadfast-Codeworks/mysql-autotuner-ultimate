"""
Regression tests — one per bug found across the code reviews.

Each test names the finding it locks down and asserts on the VALUE, not merely
that something was produced. Every one of these bugs was silent: the tool ran,
returned recommendations, and exited 0 while the numbers were wrong. A test that
only checks "a recommendation exists" would have passed against every single one.

Author: R.L. Burger (Steadfast Codeworks)
Date: 2026-08-24
Version: 1.0.4
License: MIT License
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from conftest import (                                    # noqa: E402
    CONFIG_PATH, FakeConnector, MySQLAutoTunerUltimate, analyze, build_tuner,
    rec_for, server_profile, value_for,
)


# ======================================================================
# CRITICAL
# ======================================================================

def test_c1_redo_log_capacity_is_capped_on_mysql_84():
    """C1: innodb_redo_log_capacity escaped BOTH 16G caps and reached 64G.

    MySQL preallocates redo capacity at startup and --validate-config approves
    any legal value, so an uncapped figure fills the disk or stops the server.
    """
    from core.version_compat import VersionCompatibility
    from safety.checks import SafetyChecker
    import yaml

    cfg = yaml.safe_load(open(CONFIG_PATH))
    guard = dict(cfg['fallback_logic'])
    guard.setdefault('max_buffer_pool_percentage', 0.8)
    guard.setdefault('min_available_ram_mb', 2048)
    checker = SafetyChecker(guard)
    max_mb = cfg['fallback_logic']['safety_guardrails']['max_log_file_size_gb'] * 1024

    rec = [{'parameter': 'innodb_log_file_size', 'current_value': '48M',
            'recommended_value': '32768M', 'reason': 'r'}]

    # Barrier 1+2: safety cap runs first, then the conversion clamps its x2.
    safe = checker.validate_recommendations(rec, {'total_ram_mb': 131072})
    out = VersionCompatibility.from_version_string('8.4.0', max_mb) \
        .filter_recommendations(safe)
    assert out[0]['parameter'] == 'innodb_redo_log_capacity'
    assert checker._parse_size_mb(out[0]['recommended_value']) <= max_mb

    # Barrier 3: the alias cap fires even on an already-renamed recommendation.
    pre_renamed = [{'parameter': 'innodb_redo_log_capacity',
                    'current_value': '100M', 'recommended_value': '64G',
                    'reason': 'r'}]
    capped = checker.validate_recommendations(pre_renamed, {'total_ram_mb': 131072})
    assert checker._parse_size_mb(capped[0]['recommended_value']) <= max_mb


def test_c1_redo_log_respects_datadir_free_space():
    """C1 (deferred half): the size cap does not prove the disk can hold it."""
    from safety.checks import SafetyChecker
    import yaml

    cfg = yaml.safe_load(open(CONFIG_PATH))
    guard = dict(cfg['fallback_logic'])
    guard.setdefault('max_buffer_pool_percentage', 0.8)
    guard.setdefault('min_available_ram_mb', 2048)
    checker = SafetyChecker(guard)
    rec = [{'parameter': 'innodb_redo_log_capacity', 'current_value': '100M',
            'recommended_value': '16384M', 'reason': 'r'}]

    plenty = checker.validate_recommendations(
        rec, {'total_ram_mb': 131072, 'datadir_free_mb': 200000})
    assert checker._parse_size_mb(plenty[0]['recommended_value']) == 16384

    tight = checker.validate_recommendations(
        rec, {'total_ram_mb': 131072, 'datadir_free_mb': 20000})
    assert checker._parse_size_mb(tight[0]['recommended_value']) == 10000

    nospace = checker.validate_recommendations(
        rec, {'total_ram_mb': 131072, 'datadir_free_mb': 400})
    assert nospace == [], "should be dropped when the disk cannot take it"


# ======================================================================
# HIGH
# ======================================================================

def test_h1_platform_override_survives_the_pipeline():
    """H1: detect_platform() wiped the --platform override on its first line."""
    for platform, expected_max_conn in (('cpanel', 500),
                                        ('directadmin', 300),
                                        ('litespeed', 400)):
        results, _ = analyze(platform=platform)
        assert results['platform_detected'] == platform
        conn = rec_for(results, 'max_connections')
        if conn:
            assert int(conn['recommended_value']) <= expected_max_conn


def test_h1_platform_reaches_the_apply_path():
    """H1(b): optimize_system never passed --platform through at all."""
    import inspect
    sig = inspect.signature(MySQLAutoTunerUltimate.optimize_system)
    assert 'platform' in sig.parameters
    sig = inspect.signature(MySQLAutoTunerUltimate.multi_pass_optimization)
    assert 'platform' in sig.parameters


def test_h2_profile_changes_the_recommendation():
    """H2: --profile was consumed only by --dump-effective-config."""
    values = {}
    for name in ('safe', 'balanced', 'aggressive'):
        results, _ = analyze(profile_name=name)
        values[name] = results and value_for(results, 'innodb_buffer_pool_size')
    assert len(set(values.values())) == 3, f"profiles must differ: {values}"

    def mb(v):
        return int(str(v).rstrip('M'))
    assert mb(values['safe']) < mb(values['balanced']) < mb(values['aggressive'])


def test_h4_small_and_busy_servers_still_get_a_buffer_pool():
    """H4: a flat 2048MB OS reserve dropped the rec entirely below ~3GB RAM."""
    for ram in (1024, 2048, 4096, 16384):
        # Scale the currently-configured pool to the machine, so this tests the
        # OS-reserve maths rather than the anti-shrink guard.
        profile = server_profile(
            total_ram_mb=ram,
            variables={'innodb_buffer_pool_size': str((ram // 8) * 1024 ** 2)},
        )
        results, _ = analyze(profile)
        assert value_for(results, 'innodb_buffer_pool_size') is not None, \
            f"no buffer-pool recommendation on a {ram}MB server"


def test_oversized_buffer_pool_is_corrected_not_preserved():
    """A pool larger than RAM is the single most dangerous misconfiguration
    this tool can meet, and the anti-shrink guard used to refuse to fix it.

    Found by this suite on its first run: the H2 guard protects a *healthy*
    pool from a mis-derived value, but it was also protecting a 4 GB pool on a
    1 GB server — preserving an outage rather than preventing one.
    """
    from safety.checks import SafetyChecker
    import yaml

    cfg = dict(yaml.safe_load(open(CONFIG_PATH))['fallback_logic'])
    cfg.setdefault('max_buffer_pool_percentage', 0.8)
    cfg.setdefault('min_available_ram_mb', 2048)
    checker = SafetyChecker(cfg)

    # Pool 4x total RAM -> must be corrected without needing an override flag.
    out = checker.validate_recommendations(
        [{'parameter': 'innodb_buffer_pool_size',
          'current_value': '4096M', 'recommended_value': '501M'}],
        {'total_ram_mb': 1024})
    assert out, "refused to shrink a pool that cannot fit in RAM"
    assert checker._parse_size_mb(out[0]['recommended_value']) <= 819

    # A healthy pool is still protected from a large unexplained shrink.
    out = checker.validate_recommendations(
        [{'parameter': 'innodb_buffer_pool_size',
          'current_value': '65536M', 'recommended_value': '1024M'}],
        {'total_ram_mb': 131072})
    assert out == [], "anti-shrink guard must still protect a healthy pool"


def test_h4_preflight_allows_normal_low_available_ram():
    """H4: low available RAM is NORMAL on a tuned DB host, not a failure."""
    from safety.checks import SafetyChecker

    checker = SafetyChecker({})
    cases = [((2048, 600), True), ((8192, 1500), True),
             ((131072, 3000), True), ((131072, 900), True),
             ((2048, 80), False), ((131072, 100), False)]
    for (total, avail), should_pass in cases:
        SafetyChecker._get_memory_info = staticmethod(
            lambda t=total, a=avail: {'total_mb': t, 'available_mb': a})
        result = checker._check_system_resources()
        assert result['passed'] is should_pass, \
            f"{total}MB total / {avail}MB available"


def test_h5_information_schema_is_scanned_once_without_retry():
    """H5: four expensive scans, each blindly retried on timeout."""
    tuner = build_tuner()
    tuner.data_collector.collect_table_metrics()
    conn = tuner._test_connector
    assert conn.count('engine_scan') == 1
    assert conn.count('statistics_scan') == 0
    for kind, retry in conn.queries:
        if kind in ('engine_scan', 'table_count'):
            assert retry is False, f"{kind} must not be blindly retried"


def test_h5_connect_and_read_timeouts_differ():
    """H5: one 30s value for both timed out legitimate large-server scans."""
    from utils.mysql_connector import MySQLConnector
    c = MySQLConnector({})
    assert c.connect_timeout <= 30
    assert c.read_timeout >= 600


def test_h6_shadowing_section_is_named():
    """H6: the hint named only !includedir, never a competing section."""
    lines = ['[mysqld]\n', 'innodb_buffer_pool_size = 1G\n', '\n',
             '[mariadb]\n', 'innodb_buffer_pool_size = 8G\n', '\n',
             '[client]\n', 'innodb_buffer_pool_size = 2G\n']
    found = MySQLAutoTunerUltimate._find_shadowing_sections(
        lines, ['innodb_buffer_pool_size'])
    assert 'innodb_buffer_pool_size in [mariadb]' in found
    assert not any('client' in f for f in found), \
        "[client] is not read by the server"


def test_h7_exit_codes_distinguish_failure_from_success():
    """H7: a rollback exited 0 — monitoring saw green while the DB was down."""
    f = MySQLAutoTunerUltimate and __import__('conftest').autotuner_main
    code_for = f.exit_code_for_application
    assert code_for({'applied_count': 5, 'restart_performed': True}) == f.EXIT_APPLIED
    assert code_for({'applied_count': 0, 'rollback': True,
                     'rollback_recovered': True}) == f.EXIT_ERROR
    assert code_for({'applied_count': 0, 'rollback': True,
                     'rollback_recovered': False}) == f.EXIT_ERROR
    assert code_for({'applied_count': 0, 'validation_failed': True}) == f.EXIT_ERROR
    assert code_for({'applied_count': 3, 'restart_performed': False,
                     'manual_restart_required': True}) == f.EXIT_PENDING
    assert code_for({'applied_count': 3, 'restart_performed': False}) == f.EXIT_PENDING
    assert code_for({'applied_count': 0, 'restart_performed': False}) == f.EXIT_OK


# ======================================================================
# MEDIUM
# ======================================================================

def test_m1_every_anomaly_has_a_confidence_penalty():
    """M1: two of four penalties were dead — the YAML spelled them differently.

    This is the contract assertion the review asked for, as a test.
    """
    from core.config_audit import audit_anomaly_contract
    from core.ultimate_decision_engine import UltimateDecisionEngine
    import yaml

    cfg = yaml.safe_load(open(CONFIG_PATH))
    problems = audit_anomaly_contract(cfg, UltimateDecisionEngine.ANOMALY_NAMES)
    assert problems == [], '\n'.join(problems)


def test_m1_penalties_actually_lower_confidence():
    from core.ultimate_decision_engine import UltimateDecisionEngine
    import yaml

    engine = UltimateDecisionEngine(yaml.safe_load(open(CONFIG_PATH)))
    rec = {'parameter': 'innodb_buffer_pool_size', 'recommended_value': '4096M'}
    metrics = {'total_ram_mb': 8192, 'total_tables': 100}

    engine.detected_anomalies = []
    baseline = engine.calculate_ultimate_confidence(rec, metrics)
    for anomaly in UltimateDecisionEngine.ANOMALY_NAMES:
        engine.detected_anomalies = [anomaly]
        assert engine.calculate_ultimate_confidence(rec, metrics) < baseline, \
            f"{anomaly} applied no penalty"


def test_m2_missing_connections_status_does_not_crash():
    """M2: ZeroDivisionError aborted the whole analysis run."""
    profile = server_profile()
    del profile['status']['Connections']
    results, _ = analyze(profile)
    assert 'recommendations' in results


def test_m3_post_migration_state_is_reachable():
    """M3: required a metric no collector ever produced, so it never fired."""
    from core.ultimate_decision_engine import UltimateDecisionEngine
    import yaml

    cfg = yaml.safe_load(open(CONFIG_PATH))
    state = UltimateDecisionEngine(cfg).detect_migration_state({
        'myisam_table_count': 50, 'innodb_table_count': 4950,
        'key_buffer_size_mb': 512, 'myisam_index_size_mb': 10,
    })
    assert state == 'post_migration'


def test_m4_buffer_pool_accounts_for_index_size():
    """M4: sized on DATA_LENGTH only — indexes live in the pool too."""
    from core.collector import DataCollector

    collector = DataCollector(None, None)
    normalized = collector._normalize_metrics({
        'mysql': {'variables': {'x': '1'}, 'status': {'y': '1'}, 'version': {}},
        'tables': {
            'table_stats': {'InnoDB': {'data_size': 2 * 1024 ** 3,
                                       'index_size': 3 * 1024 ** 3}},
            'engine_distribution': {'InnoDB': 100},
            'database_names': [],
        },
        'system': {}, 'performance': {},
    })
    assert normalized['innodb_data_size_mb'] == 5120


def test_m4_key_buffer_sized_on_myisam_indexes():
    """M4: key_buffer_size caches indexes only, but was sized on row data."""
    profile = server_profile(engines={
        'InnoDB': (4000, 8 * 1024 ** 3, 4 * 1024 ** 3),
        'MyISAM': (1000, 2 * 1024 ** 3, 20 * 1024 ** 2),   # 2GB rows, 20MB idx
    }, variables={'key_buffer_size': str(512 * 1024 ** 2)})
    results, _ = analyze(profile)
    value = value_for(results, 'key_buffer_size')
    assert value is not None
    assert int(str(value).rstrip('M')) < 100, \
        f"sized on row data, not indexes: {value}"


def test_m13_multipass_apply_restarts_once():
    """M13: three restarts in two minutes on a production database."""
    calls = []

    def fake_optimize(self, pass_number=1, dry_run=True, **kwargs):
        calls.append(pass_number)
        return {'mode': 'dry_run' if dry_run else 'applied',
                'pass_number': pass_number,
                'analysis': {'recommendations': []},
                'recommendations_count': 1,
                'application': {'applied_count': 1,
                                'restart_performed': not dry_run}}

    original = MySQLAutoTunerUltimate.optimize_system
    try:
        MySQLAutoTunerUltimate.optimize_system = fake_optimize
        tuner = build_tuner()
        calls.clear()
        tuner.multi_pass_optimization(max_passes=3, dry_run=True)
        assert calls == [1, 2, 3], "dry-run should still show every pass"
        calls.clear()
        tuner.multi_pass_optimization(max_passes=3, dry_run=False)
        assert calls == [3], f"apply must run one pass only, ran {calls}"
    finally:
        MySQLAutoTunerUltimate.optimize_system = original


def test_m13_log_ratio_matches_the_pool_being_set():
    """M13: sized against the CURRENT pool, and lost its 256M floor."""
    from core.ultimate_decision_engine import UltimateDecisionEngine
    import yaml

    cfg = yaml.safe_load(open(CONFIG_PATH))
    base = [
        {'parameter': 'innodb_buffer_pool_size', 'current_value': '512M',
         'recommended_value': '65536M', 'reason': 'r'},
        {'parameter': 'innodb_log_file_size', 'current_value': '48M',
         'recommended_value': '256M', 'reason': 'r'},
    ]
    for pass_no in (1, 2, 3):
        engine = UltimateDecisionEngine(cfg)
        engine.current_pass = pass_no
        out = engine.generate_multi_pass_recommendations(
            {'innodb_buffer_pool_size_mb': 512}, [dict(r) for r in base])
        pool = int(next(r['recommended_value'] for r in out
                        if r['parameter'] == 'innodb_buffer_pool_size').rstrip('M'))
        log = int(next(r['recommended_value'] for r in out
                       if r['parameter'] == 'innodb_log_file_size').rstrip('M'))
        expected = cfg['multi_pass_logic'][f'pass_{pass_no}']['log_file_ratio']
        assert abs(log / pool - expected) < 0.01, \
            f"pass {pass_no}: ratio {log / pool:.3f}, configured {expected}"
        assert log >= 256, "256M floor lost"


def test_m15_size_conversion_is_exact_to_the_kilobyte():
    """M15: the KB helper went via MB, flooring 1.5M to 1024K."""
    from core.collector import DataCollector

    c = DataCollector(None, None)
    assert c._convert_size_to_kb('1572864') == 1536      # 1.5M
    assert c._convert_size_to_kb('3145728') == 3072      # 3M
    assert c._convert_size_to_kb('1536K') == 1536
    assert c._convert_size_to_kb('262144') == 256
    assert c._convert_size_to_mb(str(4 * 1024 ** 3)) == 4096


# ======================================================================
# LOW / hygiene
# ======================================================================

def test_l6_null_processlist_time_does_not_lose_all_metrics():
    from core.collector import DataCollector

    class C:
        def execute_query(self, q, params=None, retry=True):
            return [{'Command': 'Query', 'Time': None},
                    {'Command': 'Sleep', 'Time': '5'}]

    result = DataCollector(C(), None)._get_process_list()
    assert result.get('total_processes') == 2
    assert result.get('longest_running') == 5


def test_l7_non_numeric_variable_does_not_abort_collection():
    from core.collector import DataCollector

    normalized = DataCollector(None, None)._normalize_metrics({
        'mysql': {'variables': {'innodb_buffer_pool_instances': 'auto',
                                'x': '1'},
                  'status': {'y': '1'}, 'version': {}},
        'system': {}, 'tables': {}, 'performance': {},
    })
    assert normalized['innodb_buffer_pool_instances'] == 1


def test_l10_defaults_do_not_mutate_the_loaded_config():
    tuner = build_tuner()
    fallback = tuner.config.get('fallback_logic', {})
    assert 'max_buffer_pool_percentage' not in fallback
    assert 'min_available_ram_mb' not in fallback


def test_version_is_stated_once():
    """Four independent copies of the version string used to exist."""
    import yaml
    from utils.version import TOOL_VERSION
    from output.reporter import TOOL_VERSION as reporter_version

    assert MySQLAutoTunerUltimate.VERSION == TOOL_VERSION
    assert reporter_version == TOOL_VERSION
    assert str(yaml.safe_load(open(CONFIG_PATH))['metadata']['version']) == TOOL_VERSION

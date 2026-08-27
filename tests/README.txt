================================================================================
  MySQL Auto-Tuner — Test Suite
================================================================================

RUNNING
--------------------------------------------------------------------------------
No dependencies beyond Python 3.8+. From the tool's root directory:

    python3 tests/run_tests.py            # everything
    python3 tests/run_tests.py -v         # list each test
    python3 tests/run_tests.py m13        # only tests matching "m13"

pytest works too, if you have it:

    python3 -m pytest tests/ -v

Exit code 0 = all passed, 1 = at least one failure. Safe to run in CI.

NO DATABASE IS TOUCHED. Every test runs against a fake connector answering from
a canned server profile, so the suite is safe to run anywhere — including on a
production box while debugging.


WHAT IS TESTED, AND WHY THIS SHAPE
--------------------------------------------------------------------------------
Almost every serious bug found across three review rounds was SILENT. The tool
ran, produced recommendations, and exited 0 while the numbers were wrong:

  * metric keys the engine read that the collector never produced, so a formula
    computed from a permanent zero
  * config keys spelled one way in the YAML and another in the code, so a
    guardrail quietly evaluated to nothing
  * a size cap keyed to a parameter name that a rename had already changed
  * a flag documented in the README that no code path consumed

A test asserting "a recommendation was returned" passes against every one of
those. So these tests assert on VALUES — the actual number, the actual
parameter name, the actual exit code.

  tests/test_regressions.py   One test per finding, named for it. If a fix is
                              ever reverted, the test names the bug that comes
                              back.

  tests/test_pipeline.py      End-to-end runs of analyze_system() against canned
                              server profiles, plus the two contract checks:
                              every metric key the engine consumes is produced,
                              and every config key is read by something.

  tests/conftest.py           FakeConnector / FakeSystemInfo and the helpers
                              that assemble a fully wired tuner without a
                              database or a filesystem.


ADDING A TEST
--------------------------------------------------------------------------------
Most cases need only a server profile and an assertion:

    from conftest import analyze, server_profile, value_for

    def test_my_case():
        profile = server_profile(total_ram_mb=8192,
                                 variables={'max_connections': '1000'})
        results, _ = analyze(profile, platform='cpanel')
        assert value_for(results, 'max_connections') == 500

`server_profile(**overrides)` merges into a realistic MariaDB 10.11 baseline;
`variables` and `status` merge key-by-key rather than replacing wholesale.

When you fix a bug, add the test that fails before the fix and passes after,
and name it after the finding. That is what makes this suite worth keeping.


A NOTE ON WHAT THIS SUITE ALREADY FOUND
--------------------------------------------------------------------------------
On its very first run it caught a real bug that six rounds of review had missed:
the anti-shrink guard was refusing to correct a buffer pool LARGER than the
machine's total RAM. The guard exists to stop a mis-derived value replacing a
healthy one — but a 4 GB pool on a 1 GB server is not healthy, and "protecting"
it preserved an outage instead of preventing one.

See test_oversized_buffer_pool_is_corrected_not_preserved.

================================================================================
  Steadfast Codeworks  |  Automate. Simplify. Steadfast.
================================================================================

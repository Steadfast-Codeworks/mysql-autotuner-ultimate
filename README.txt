================================================================================
  Ultimate Steadfast MySQL Auto-Tuner v1.0.4
  Built by Steadfast Codeworks — https://www.steadfasttools.com
================================================================================

⚠️  BETA NOTICE
--------------------------------------------------------------------------------
This tool has been extensively tested in lab environments across MySQL 8.0/8.4
and MariaDB 10.5/10.6/10.11/11.4. However, production environments vary widely.

We recommend:
  1. ALWAYS run with --dry-run first to preview changes
  2. Back up your my.cnf before applying:
       cp /etc/my.cnf /etc/my.cnf.backup
  3. Test on a non-critical server if possible
  4. Report issues at: https://www.steadfasttools.com/contact

Your feedback helps improve this tool for the entire sysadmin community.

--------------------------------------------------------------------------------
  WHAT IT DOES
--------------------------------------------------------------------------------
The Ultimate Steadfast MySQL Auto-Tuner analyzes your server's hardware
specifications and active workload to generate an optimized MySQL/MariaDB
configuration. Built on evidence from 66 real-world production cases, it
performs multi-pass optimization and advanced anomaly detection to squeeze
maximum performance from your database server.

Key capabilities:
  - Hardware-aware tuning (RAM and CPU cores)
  - Multi-pass optimization (InnoDB buffer pool, connection pool, temp tables)
  - Anomaly detection (identifies misconfigured or dangerous settings)
  - Supports MySQL 8.0, 8.4 and MariaDB 10.5, 10.6, 10.11, 11.4
  - DirectAdmin file limit intelligence
  - Dry-run mode for safe previewing before any change is written

--------------------------------------------------------------------------------
  REQUIREMENTS
--------------------------------------------------------------------------------
  - Linux (CentOS 7+, AlmaLinux 8+, Ubuntu 20.04+, Debian 10+)
  - Python 3.8 or higher
  - MySQL 8.0+ or MariaDB 10.5+
  - Root or sudo access
  - Minimum 1 GB RAM (tool is most effective on 2 GB+ servers). Below 1 GB there
    is no room to both reserve headroom for the OS and recommend a sane buffer
    pool, so the buffer-pool recommendation is skipped and the reason is logged.
    Every other recommendation still applies.

--------------------------------------------------------------------------------
  INSTALLATION
--------------------------------------------------------------------------------
1. Extract the archive:
     sudo mkdir -p /opt/mysql-autotuner
     sudo tar -xzf mysql-autotuner-ultimate_v1.0.4.tar.gz -C /opt/mysql-autotuner
     cd /opt/mysql-autotuner

2. Make the installer executable:
     chmod +x mysql-autotuner.py install.sh

3. Run the installer as root (installs dependencies & sets permissions):
     sudo bash install.sh

4. (Optional) Install to system path for global access:
     sudo cp mysql-autotuner.py /usr/local/bin/mysql-autotuner

--------------------------------------------------------------------------------
  USAGE
--------------------------------------------------------------------------------
Basic analysis with rich console output:
      python3 mysql-autotuner.py --analyze

Always preview before applying — dry-run an optimization pass:
     python3 mysql-autotuner.py --optimize --dry-run

Show evidence base alongside recommendations:
     python3 mysql-autotuner.py --analyze --show-evidence

Educational mode explaining the WHY behind changes:
     python3 mysql-autotuner.py --analyze --explain

Combine explanations with evidence for maximum insight:
     python3 mysql-autotuner.py --analyze --explain --show-evidence

Combine all advanced options for a comprehensive analysis:
     python3 mysql-autotuner.py --analyze --explain --show-evidence --multi-pass --max-passes 3

Show TOP DATABASE ACTIVITY table: per-schema queries/sec, data size, connections, and a composite impact score with actionable tip identifying the highest-load database:
      python3 mysql-autotuner.py --analyze --top-databases --top-db-limit 5

Show TOP DATABASE ACTIVITY with detailed explanations and evidence for each recommendation:
      python3 mysql-autotuner.py --analyze --top-databases --explain --show-evidence

Export TOP DATABASE ACTIVITY report in JSON format for external analysis or monitoring tools:
      python3 mysql-autotuner.py --analyze --top-databases --format json > report.json

Perform a single-pass optimization with dry-run to preview changes:
     python3 mysql-autotuner.py --optimize --pass-number 1 --dry-run

Perform a single-pass optimization:
     python3 mysql-autotuner.py --optimize --pass-number 1

Check DirectAdmin file limit requirements:
      python3 mysql-autotuner.py --file-limit-check --platform directadmin

Perform 3-pass progressive optimization with dry-run to preview changes at each step:
     python3 mysql-autotuner.py --multi-pass --max-passes 3 --dry-run

Perform 3-pass progressive optimization:
      python3 mysql-autotuner.py --multi-pass --max-passes 3

Choose an optimisation profile (safe | balanced | aggressive) — controls how
much of RAM the buffer pool may claim on each pass. --profile is a modifier, so
combine it with an action:
      python3 mysql-autotuner.py --analyze --profile safe
      python3 mysql-autotuner.py --optimize --profile aggressive --dry-run

Dump effective configuration in JSON format:
      python3 mysql-autotuner.py --dump-effective-config --output-format json > effective-config.json

Preview the exact my.cnf changes as a unified diff — read-only, no root needed,
nothing is written:
      python3 mysql-autotuner.py --diff

Cron-safe apply: only parameters that do NOT require a restart, applied live via
SET GLOBAL. Never restarts the service:
      sudo python3 mysql-autotuner.py --optimize --dynamic-only --yes

Apply without the interactive prompt (automation / cron). Without it, a non-TTY
stdin is treated as "No" and nothing is applied:
      sudo python3 mysql-autotuner.py --optimize --yes

Undo: list the timestamped my.cnf backups and restore one. The backup is
validated with the server binary before it is restored, and the restart is
verified:
      sudo python3 mysql-autotuner.py --rollback
      sudo python3 mysql-autotuner.py --rollback --rollback-file /etc/my.cnf.backup.20260804_141530 --yes

Also write the report to a timestamped file (off by default; newest 30 kept):
      python3 mysql-autotuner.py --analyze --save-report

Override guardrails — both are deliberately awkward, use only when you mean it:
      # permit shrinking innodb_buffer_pool_size past the safety threshold
      sudo python3 mysql-autotuner.py --optimize --allow-buffer-pool-shrink
      # permit the automatic restart on a Galera / replication node
      sudo python3 mysql-autotuner.py --optimize --allow-cluster-restart

Connect to a database the auto-detection cannot reach — a non-standard socket, a
remote host, or credentials in a custom file. Without these the tool tries
/root/.my.cnf, /etc/mysql/debian.cnf, the DirectAdmin config, then socket auth:
      python3 mysql-autotuner.py --analyze --mysql-socket /opt/mysql/run/mysql.sock
      python3 mysql-autotuner.py --analyze --mysql-host db.internal --mysql-port 3307 --mysql-user tuner
      python3 mysql-autotuner.py --analyze --defaults-file /root/.mysql-autotuner.cnf

  Passing a password:
    --defaults-file is preferred. It reads a standard MySQL option file, which
    you can chmod 600:

        [client]
        user=tuner
        password=yourpassword

    --mysql-password works but puts the password in argv, where any local user
    can read it with `ps`. The MYSQL_PWD environment variable is also honoured.
    The same settings can live in the `mysql:` block of config_ultimate.yaml —
    but that file is world-readable in a typical install, so prefer an option
    file for anything with a password in it.

Audit the config file against the code — reports settings nothing reads,
settings the code expects but the file lacks, and broken name contracts (e.g. an
anomaly with no confidence penalty). Touches no database; exits 1 on a contract
violation, so it can gate a CI build:
      python3 mysql-autotuner.py --check-config

Full options:
     python3 mysql-autotuner.py --help

--------------------------------------------------------------------------------
  EXIT CODES
--------------------------------------------------------------------------------
Every command returns a meaningful exit code, so the tool can be driven from
cron, a monitoring check, or a CI pipeline:

  0   Success, nothing to do. Connected fine; no recommendations outstanding.

  1   Error — OR a change that did not take and was rolled back. Treat as an
      alert. This covers: the candidate config was rejected by the server
      binary; the restart failed and my.cnf was rolled back; a rollback whose
      restart did not come back up (the database may be DOWN).

  2   Action pending. Connected fine and there is something to do, but nothing
      is live yet. This covers: recommendations available (--analyze, --diff,
      --dry-run); a file-limit adjustment is required; changes were written to
      my.cnf but a restart is still needed; a cluster/replica node where the
      restart was deliberately skipped.

  3   Changes applied and confirmed live.

Note that 2 is a SUCCESS state — a healthy server that simply has room to
improve returns 2, not 0. A monitoring check should alert on 1, and treat 2 as
informational.

Example cron wrapper:

    #!/bin/bash
    rc=0
    /opt/mysql-autotuner/mysql-autotuner.py --analyze >/tmp/tuner.out 2>&1 || rc=$?
    case "$rc" in
      0) : ;;                                    # nothing to do
      2) mail -s "MySQL tuning available" you@example.com </tmp/tuner.out ;;
      *) mail -s "MySQL auto-tuner FAILED (rc=$rc)" you@example.com </tmp/tuner.out ;;
    esac

--------------------------------------------------------------------------------
  WHAT GETS OPTIMIZED
--------------------------------------------------------------------------------
  InnoDB Settings
    - innodb_buffer_pool_size       (sized to RAM and InnoDB data size)
    - innodb_buffer_pool_instances  (scaled to buffer pool size; MySQL only —
                                     removed in MariaDB 10.6+ and skipped there)
    - innodb_log_file_size          (workload-appropriate; auto-converted to
                                     innodb_redo_log_capacity on MySQL 8.4+)

  Connection Management
    - max_connections               (balanced against peak usage and RAM)
    - thread_cache_size

  Temp Tables & Joins
    - tmp_table_size                (raised when temp tables spill to disk)
    - join_buffer_size              (raised on unindexed-join pressure)

  MyISAM
    - key_buffer_size               (sized to MyISAM data)
    - table_definition_cache        (raised toward the table count)

  DirectAdmin / Platform Specific
    - open_files_limit alignment with system ulimits (large table counts)

  Values are guardrail-checked before being written: buffer pool is capped to a
  platform-appropriate share of RAM with an OS headroom reserve, and a
  recommendation never shrinks a healthy buffer pool without --allow-buffer-pool-shrink.

--------------------------------------------------------------------------------
  SAFETY FEATURES
--------------------------------------------------------------------------------
  - Dry-run mode shows all changes before applying anything
  - Automatic, byte-verified backup of my.cnf before any modification, written
    next to the config as:  <my.cnf-path>.backup.YYYYMMDD_HHMMSS
  - New config is validated with the server binary; an invalid config is never
    written or restarted
  - Failed restart triggers an automatic, verified rollback to the backup
  - Cluster/replication nodes are detected — the restart is skipped unless you
    pass --allow-cluster-restart
  - Every applied parameter is re-read from @@GLOBAL after the restart and
    shown as APPLIED / ADJUSTED / MISMATCH, so you can see what actually took
    effect rather than what was intended
  - --rollback restores any previous backup, validating it first and verifying
    the service comes back up
  - Anomaly detection warns about dangerous existing settings
  - All activity is logged to: /var/log/mysql-autotuner/mysql-autotuner-ultimate.log
    (falls back to a per-user temp directory if /var/log is not writable)
  - Report FILES are not written unless you ask for them with --save-report;
    normal console output and shell redirection are untouched

--------------------------------------------------------------------------------
  TESTING
--------------------------------------------------------------------------------
A test suite ships with the tool. It needs no dependencies beyond Python 3.8+
and NEVER touches a database — every test runs against a fake connector and a
canned server profile, so it is safe to run anywhere, including on a production
box while you are debugging:

      python3 tests/run_tests.py            # everything
      python3 tests/run_tests.py -v         # list each test
      python3 tests/run_tests.py m13        # only tests matching "m13"

Exit code 0 = all passed, 1 = at least one failure, so it can gate a CI build.

The suite is built around regressions: one test per bug ever found, named after
it, asserting on the actual numbers rather than merely that a recommendation
appeared. See tests/README.txt for the reasoning and for how to add your own.

--------------------------------------------------------------------------------
  SUPPORT
--------------------------------------------------------------------------------
  Documentation:   https://www.steadfasttools.com/products/mysql-autotuner
  Report Issues:   https://www.steadfasttools.com/contact
  Email:           support@steadfasttools.com
  Response time:   24-48 hours (GMT+2)

--------------------------------------------------------------------------------
  LICENSE
--------------------------------------------------------------------------------
This software is released under the MIT License. You are free to use, copy,
modify, merge, publish, distribute, sublicense, and/or sell copies of it,
subject to the conditions of the MIT License. The software is provided
"as is", without warranty of any kind.

Full license: https://www.steadfasttools.com/legal/licensing

Copyright (c) 2026 Steadfast Codeworks (R.L. Burger)
Automate. Simplify. Steadfast.
================================================================================

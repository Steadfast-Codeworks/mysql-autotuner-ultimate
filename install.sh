#!/bin/bash
# MySQL Auto-Tuner Installation Script
#
# Author: R.L. Burger (Steadfast Codeworks)
# Date: 2025-09-07
# Last Updated: 2026-08-24
# Version: 1.0.4
# Copyright (c) 2026 R.L. Burger
# Project: Steadfast Tools
# Website: https://www.steadfasttools.com
# License: MIT License

set -e

echo "MySQL Auto-Tuner Installation"
echo "=============================="

# Check if Python 3 is available
if ! command -v python3 &> /dev/null; then
    echo "Error: Python 3 is required but not installed."
    echo "Please install Python 3.8 or higher and try again."
    exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
echo "Found Python $PYTHON_VERSION"

# Check Python version
if python3 -c 'import sys; exit(0 if sys.version_info >= (3, 8) else 1)'; then
    echo "✓ Python version is compatible"
else
    echo "Error: Python 3.8 or higher is required"
    exit 1
fi

# Check if pip is available
if ! command -v pip3 &> /dev/null; then
    echo "Error: pip3 is required but not installed."
    echo "Please install pip3 and try again."
    exit 1
fi

echo "✓ pip3 is available"

# Install Python dependencies
echo ""
echo "Installing Python dependencies..."
install_ok=0

# On Debian/Ubuntu 23.04+ (PEP 668) the system Python is "externally managed"
# and a bare `pip3 install` fails. Prefer distro packages there, then fall back
# to a user-site pip install, then finally a plain pip install.
if command -v apt-get &> /dev/null; then
    if sudo apt-get install -y python3-pymysql python3-yaml &> /dev/null; then
        echo "✓ Dependencies installed via apt (python3-pymysql, python3-yaml)"
        install_ok=1
    fi
fi

if [ "$install_ok" -eq 0 ]; then
    if pip3 install -r requirements.txt 2> /dev/null \
       || pip3 install --user -r requirements.txt 2> /dev/null; then
        echo "✓ Dependencies installed via pip"
        install_ok=1
    fi
fi

if [ "$install_ok" -eq 0 ]; then
    echo "Error: Failed to install dependencies automatically."
    echo "Try one of the following:"
    echo "  sudo apt install python3-pymysql python3-yaml        # Debian/Ubuntu"
    echo "  python3 -m venv venv && . venv/bin/activate && pip install -r requirements.txt"
    exit 1
fi

# Make the main script executable
echo ""
echo "Setting up executable permissions..."
chmod +x mysql-autotuner.py
echo "✓ Made mysql-autotuner.py executable"

# Create directories
echo ""
echo "Creating directories..."
sudo mkdir -p /var/backups/mysql-autotuner
sudo mkdir -p /var/log/mysql-autotuner
# NOTE: reports are NOT written by default (use --save-report). When enabled the
# tool creates its own directory under /var/log, so nothing is pre-created here.
# Earlier versions created /tmp/mysql-autotuner-reports, which is exactly the
# predictable, world-writable path the safe-IO hardening moved away from.

echo "✓ Created necessary directories"

# Check MySQL connectivity (optional)
#
# Exit-code contract (see README):
#   0 = connected, nothing to tune   2 = connected, recommendations available
#   1 = error                        3 = changes applied
# A healthy server that needs tuning returns 2 — treating that as a failure told
# almost every new user their database connection was broken.
echo ""
echo "Testing MySQL connectivity..."
# `|| rc=$?` keeps this a compound list so `set -e` does not abort the installer
# on a non-zero exit code.
rc=0
./mysql-autotuner.py --analyze >/dev/null 2>&1 || rc=$?
if [ "$rc" -eq 0 ]; then
    echo "✓ MySQL connection test successful — no tuning recommendations at this time"
elif [ "$rc" -eq 2 ]; then
    echo "✓ MySQL connection test successful — tuning recommendations are available"
    echo "  See them with: ./mysql-autotuner.py --analyze --explain"
else
    echo "⚠ MySQL connection test did not complete (exit code $rc)"
    echo "  This is normal if MySQL is not running or credentials are needed."
    echo "  Diagnose with: ./mysql-autotuner.py --analyze --verbose"
fi

echo ""
echo "Installation completed successfully!"
echo ""
echo "Quick start:"
echo ""
echo "  # Basic analysis with rich console output"
echo "  ./mysql-autotuner.py --analyze"
echo ""
echo "  # Dry-run (preview only, no changes applied)"
echo "  ./mysql-autotuner.py --analyze --dry-run"
echo ""
echo "  # Show evidence base alongside recommendations"
echo "  ./mysql-autotuner.py --analyze --show-evidence"
echo ""
echo "  # Educational mode explaining the WHY behind changes"
echo "  ./mysql-autotuner.py --analyze --explain"
echo ""
echo "  # Combine explanations with evidence for maximum insight"
echo "  ./mysql-autotuner.py --analyze --explain --show-evidence"
echo ""
echo "  # Combine all advanced options for a comprehensive analysis"
echo "  ./mysql-autotuner.py --analyze --explain --show-evidence --multi-pass --max-passes 3"
echo ""
echo "  # Show TOP DATABASE ACTIVITY table: per-schema queries/sec, data size, connections,"
echo "  # and a composite impact score with actionable tip identifying the highest-load database"
echo "  ./mysql-autotuner.py --analyze --top-databases --top-db-limit 5"
echo ""
echo "  # Show TOP DATABASE ACTIVITY with detailed explanations and evidence for each recommendation"
echo "  ./mysql-autotuner.py --analyze --top-databases --explain --show-evidence"
echo ""
echo "  # Export TOP DATABASE ACTIVITY report in JSON format for external analysis or monitoring tools"
echo "  ./mysql-autotuner.py --analyze --top-databases --format json > report.json"
echo ""
echo "  # Perform a single-pass optimization with dry-run to preview changes"
echo "  ./mysql-autotuner.py --optimize --pass-number 1 --dry-run"
echo ""
echo "  # Perform a single-pass optimization"
echo "  ./mysql-autotuner.py --optimize --pass-number 1"
echo ""
echo "  # Check DirectAdmin file limit requirements"
echo "  ./mysql-autotuner.py --file-limit-check --platform directadmin"
echo ""
echo "  # Perform 3-pass progressive optimization with dry-run to preview changes at each step"
echo "  ./mysql-autotuner.py --multi-pass --max-passes 3 --dry-run"
echo ""
echo "  # Perform 3-pass progressive optimization"
echo "  ./mysql-autotuner.py --multi-pass --max-passes 3"
echo ""
echo "  # Choose an optimisation profile (safe | balanced | aggressive)"
echo "  ./mysql-autotuner.py --analyze --profile safe"
echo ""
echo "  # Dump effective configuration in JSON format"
echo "  ./mysql-autotuner.py --dump-effective-config --output-format json > effective-config.json"
echo ""
echo "  # Preview the exact my.cnf changes as a diff (read-only, no root needed)"
echo "  ./mysql-autotuner.py --diff"
echo ""
echo "  # Cron-safe: apply only no-restart parameters live via SET GLOBAL"
echo "  sudo ./mysql-autotuner.py --optimize --dynamic-only --yes"
echo ""
echo "  # Undo: restore a previous my.cnf backup (validated, restart verified)"
echo "  sudo ./mysql-autotuner.py --rollback"
echo ""
echo "  # Show all options"
echo "  ./mysql-autotuner.py --help"
echo ""
echo "Exit codes:  0 = nothing to do   1 = error/rolled back"
echo "             2 = action pending  3 = applied and confirmed live"
echo "             (2 is a SUCCESS state — alert on 1, treat 2 as informational)"
echo ""
echo "For detailed usage instructions, see README.txt"


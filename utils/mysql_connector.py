#!/usr/bin/env python3
"""
MySQL Connector Utility - v1.0.4
================================
Database connectivity, query execution, and credential auto-detection for MySQL/MariaDB.

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
import logging
import subprocess
import configparser
from typing import Dict, List, Any, Optional

try:
    import pymysql
    import pymysql.cursors
    PYMYSQL_AVAILABLE = True
except ImportError:
    PYMYSQL_AVAILABLE = False

try:
    import mysql.connector
    MYSQL_CONNECTOR_AVAILABLE = True
except ImportError:
    MYSQL_CONNECTOR_AVAILABLE = False


class MySQLConnector:
    """MySQL/MariaDB connection handler with smart credential auto-detection"""

    # Common socket locations ordered by likelihood
    SOCKET_PATHS = [
        '/var/lib/mysql/mysql.sock',        # RHEL/CentOS/cPanel default
        '/var/run/mysqld/mysqld.sock',      # Debian/Ubuntu default
        '/tmp/mysql.sock',                  # macOS / some custom installs
        '/run/mysqld/mysqld.sock',          # Newer Debian/Ubuntu
        '/var/run/mysql/mysql.sock',        # Some DirectAdmin setups
    ]

    # Credential file locations ordered by priority
    CREDENTIAL_PATHS = [
        '/root/.my.cnf',                    # Root user MySQL config (cPanel, DA)
        '/etc/mysql/debian.cnf',            # Debian maintenance credentials
        '/usr/local/directadmin/conf/mysql.conf',  # DirectAdmin MySQL config
    ]

    def __init__(self, config: Dict[str, Any] = None):
        config = config or {}
        self.logger = logging.getLogger(__name__)
        self.connection = None

        # User-supplied overrides take priority
        self._user_host = config.get('host')
        self._user_port = config.get('port')
        self._user_user = config.get('user')
        self._user_password = config.get('password')
        self._user_socket = config.get('socket')
        # An explicit MySQL option file to read credentials from, ahead of the
        # auto-detection chain (M12) — e.g. --defaults-file=/root/.tuner.cnf
        self._user_defaults_file = config.get('defaults_file')

        # H5: connect and read timeouts are different problems. Connecting
        # should fail fast (an unreachable server is not worth waiting on), but
        # a legitimate information_schema scan on a 200k-table server routinely
        # runs for minutes. A single 30s value for both meant those scans timed
        # out on exactly the servers the tool is for.
        self.timeout = config.get('timeout', 30)
        self.connect_timeout = config.get('connect_timeout', min(self.timeout, 30))
        self.read_timeout = config.get('read_timeout', max(self.timeout, 600))

        # Resolved values (filled by _resolve_credentials)
        self.host = 'localhost'
        self.port = 3306
        self.user = 'root'
        self.password = ''
        self.socket = None
        self.use_socket = False
        self.credential_source = 'defaults'

        # Resolve credentials
        self._resolve_credentials()

        self.logger.info(
            f"MySQL connector initialised  host={self.host}  "
            f"method={'socket' if self.use_socket else 'tcp'}  "
            f"cred_source={self.credential_source}"
        )

    # ------------------------------------------------------------------
    # Credential resolution
    # ------------------------------------------------------------------
    def _resolve_credentials(self):
        """Resolve MySQL credentials using a priority chain."""
        # 1. Detect socket
        self.socket = self._user_socket or self._detect_socket()
        self.host = self._user_host or 'localhost'
        self.port = int(self._user_port or 3306)
        self.use_socket = bool(self.socket) and self.host in ('localhost', '127.0.0.1')

        # 2. If the user explicitly supplied user, use it (N-7)
        # Check if user was explicitly supplied (even without password)
        if self._user_user:
            self.user = self._user_user
            self.password = self._user_password if self._user_password is not None else ''
            self.credential_source = 'user_supplied'
            if self._user_password is None and self._user_defaults_file:
                creds = self._read_credential_file(self._user_defaults_file)
                if creds and creds.get('password'):
                    self.password = creds.get('password', '')
                    self.credential_source = self._user_defaults_file
            return

        # 2b. M12: an explicitly supplied option file wins over auto-detection.
        #     This is the supported way to hand the tool credentials without
        #     putting a password in argv (where `ps` would expose it).
        if self._user_defaults_file:
            creds = self._read_credential_file(self._user_defaults_file)
            if creds:
                self.user = creds.get('user', self._user_user or 'root')
                self.password = creds.get('password', '')
                self.credential_source = self._user_defaults_file
                if creds.get('socket') and not self._user_socket:
                    self.socket = creds['socket']
                    self.use_socket = True
                self.logger.info(
                    f"Credentials loaded from {self._user_defaults_file}"
                )
                return
            self.logger.warning(
                "No usable [client]/[mysql] credentials in %s; falling back to "
                "auto-detection.", self._user_defaults_file,
            )

        # 3. Try auto-detection from credential files (N-8: local connections only)
        is_local = self.use_socket or self.host in ('localhost', '127.0.0.1', '::1')
        if not is_local:
            self.logger.warning(
                f"Connecting to remote host '{self.host}' without explicit credentials. "
                "Skipping local root credential files to prevent shipping local credentials remotely."
            )
        else:
            for cred_path in self.CREDENTIAL_PATHS:
                creds = self._read_credential_file(cred_path)
                if creds:
                    self.user = creds.get('user', 'root')
                    self.password = creds.get('password', '')
                    self.credential_source = cred_path
                    self.logger.info(f"Credentials loaded from {cred_path}")
                    # If the file also specifies a socket, honour it
                    if creds.get('socket') and not self._user_socket:
                        self.socket = creds['socket']
                        self.use_socket = True
                    return

        # 4. Try unix socket auth as root (no password) — common on modern
        #    MariaDB installs with unix_socket plugin
        if self.use_socket:
            self.user = self._user_user or 'root'
            self.password = ''
            self.credential_source = 'socket_auth_attempt'
            return

        # 5. Fallback
        self.user = self._user_user or 'root'
        self.password = self._user_password or ''
        self.credential_source = 'defaults'

    def _detect_socket(self) -> Optional[str]:
        """Detect the MySQL socket file."""
        # Check common paths
        for path in self.SOCKET_PATHS:
            if os.path.exists(path):
                return path

        # Try to find from running process
        try:
            result = subprocess.run(
                ['bash', '-c', "ps aux | grep -E 'mysqld|mariadbd' | grep -v grep"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                match = re.search(r'--socket=([^\s]+)', result.stdout)
                if match and os.path.exists(match.group(1)):
                    return match.group(1)
        except Exception:
            pass

        # Try mysqladmin
        try:
            result = subprocess.run(
                ['mysqladmin', 'variables'],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                match = re.search(r'socket\s*\|\s*([^\s|]+)', result.stdout)
                if match and os.path.exists(match.group(1)):
                    return match.group(1)
        except Exception:
            pass

        return None

    def _read_credential_file(self, path: str) -> Optional[Dict[str, str]]:
        """Read MySQL credentials from a configuration file."""
        if not os.path.exists(path):
            return None

        try:
            # DirectAdmin stores credentials differently
            if 'directadmin' in path:
                return self._parse_directadmin_conf(path)

            # Standard MySQL option files (.my.cnf, debian.cnf).
            # M7: real MySQL option files contain bare options (e.g. `quick`,
            # `no-auto-rehash`) and sometimes duplicate keys — both of which make
            # a strict ConfigParser throw, silently discarding valid credentials.
            cfg = configparser.ConfigParser(
                interpolation=None, allow_no_value=True, strict=False
            )
            cfg.read(path)

            # Try [client] section first, then [mysql], then [mysqladmin]
            for section in ('client', 'mysql', 'mysqladmin'):
                if cfg.has_section(section):
                    creds = {}

                    def _extract_opt_val(opt_name: str) -> str:
                        if not cfg.has_option(section, opt_name):
                            return ""
                        val = cfg.get(section, opt_name) or ""
                        val = val.strip()
                        # If quoted, return inside of quotes directly (N-11)
                        if (val.startswith('"') and val.endswith('"') and len(val) >= 2) or \
                           (val.startswith("'") and val.endswith("'") and len(val) >= 2):
                            return val[1:-1]
                        # Otherwise, strip inline comments starting with # or ;
                        for delim in (' #', '\t#', ' ;', '\t;'):
                            if delim in val:
                                val = val.split(delim, 1)[0].strip()
                        if val.startswith('#') or val.startswith(';'):
                            return ""
                        return val.strip('"').strip("'")

                    u = _extract_opt_val('user')
                    p = _extract_opt_val('password')
                    s = _extract_opt_val('socket')
                    if u:
                        creds['user'] = u
                    if p:
                        creds['password'] = p
                    if s:
                        creds['socket'] = s
                    if creds.get('user') or creds.get('password'):
                        return creds

        except Exception as e:
            self.logger.debug(f"Could not parse {path}: {e}")

        return None

    def _parse_directadmin_conf(self, path: str) -> Optional[Dict[str, str]]:
        """Parse DirectAdmin mysql.conf (key=value format)."""
        try:
            creds = {}
            with open(path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if '=' in line and not line.startswith('#'):
                        key, _, value = line.partition('=')
                        key = key.strip().lower()
                        value = value.split('#', 1)[0].split(';', 1)[0].strip().strip('"').strip("'")
                        if key in ('user', 'passwd', 'password'):
                            creds['user' if key == 'user' else 'password'] = value
                        elif key == 'socket':
                            creds['socket'] = value
            if creds.get('user') or creds.get('password'):
                return creds
        except Exception as e:
            self.logger.debug(f"Could not parse DirectAdmin config {path}: {e}")
        return None

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------
    def connect(self, connect_timeout: Optional[int] = None,
                read_timeout: Optional[int] = None) -> bool:
        """Establish connection to MySQL/MariaDB with optional timeout overrides."""
        if self.connection:
            try:
                # Verify existing connection is still alive
                if PYMYSQL_AVAILABLE and hasattr(self.connection, 'ping'):
                    self.connection.ping(reconnect=False)
                return True
            except Exception:
                self.connection = None

        # Attempt connection with detected credentials
        if not self._attempt_connect(connect_timeout=connect_timeout, read_timeout=read_timeout):
            # If socket auth failed, try TCP without password
            if self.credential_source == 'socket_auth_attempt':
                self.logger.info("Socket auth failed, trying TCP connection...")
                self.use_socket = False
                if not self._attempt_connect(connect_timeout=connect_timeout, read_timeout=read_timeout):
                    return False
            else:
                return False

        return True

    def _attempt_connect(self, connect_timeout: Optional[int] = None,
                         read_timeout: Optional[int] = None) -> bool:
        """Single connection attempt."""
        try:
            if PYMYSQL_AVAILABLE:
                self.connection = self._connect_pymysql(
                    connect_timeout=connect_timeout, read_timeout=read_timeout
                )
            elif MYSQL_CONNECTOR_AVAILABLE:
                self.connection = self._connect_mysql_connector(
                    connect_timeout=connect_timeout, read_timeout=read_timeout
                )
            else:
                self.logger.error(
                    "No MySQL connector library available. "
                    "Install with: pip3 install PyMySQL"
                )
                return False

            if self.connection:
                self.logger.info("MySQL connection established successfully")
                return True

        except Exception as e:
            self.logger.error(f"Connection attempt failed: {e}")
            self.connection = None

        return False

    def _connect_pymysql(self, connect_timeout: Optional[int] = None,
                         read_timeout: Optional[int] = None):
        """Connect using PyMySQL."""
        c_timeout = connect_timeout if connect_timeout is not None else self.connect_timeout
        r_timeout = read_timeout if read_timeout is not None else self.read_timeout
        params = {
            'user': self.user,
            'password': self.password,
            'charset': 'utf8mb4',
            'connect_timeout': c_timeout,
            'read_timeout': r_timeout,
            'write_timeout': r_timeout,
            'autocommit': True,
        }
        if self.use_socket and self.socket:
            params['unix_socket'] = self.socket
        else:
            params['host'] = self.host
            params['port'] = self.port

        return pymysql.connect(**params)

    def _connect_mysql_connector(self, connect_timeout: Optional[int] = None,
                                read_timeout: Optional[int] = None):
        """Connect using mysql-connector-python."""
        c_timeout = connect_timeout if connect_timeout is not None else self.connect_timeout
        params = {
            'user': self.user,
            'password': self.password,
            'charset': 'utf8mb4',
            'connection_timeout': c_timeout,
            'autocommit': True,
        }
        if self.use_socket and self.socket:
            params['unix_socket'] = self.socket
        else:
            params['host'] = self.host
            params['port'] = self.port

        return mysql.connector.connect(**params)

    def disconnect(self):
        """Close MySQL connection."""
        if self.connection:
            try:
                self.connection.close()
            except Exception:
                pass
            finally:
                self.connection = None

    # ------------------------------------------------------------------
    # Query execution
    # ------------------------------------------------------------------
    def execute_query(self, query: str, params: Optional[tuple] = None,
                      retry: bool = True) -> Optional[List[Dict[str, Any]]]:
        """Execute a query and return results as list of dicts.

        Args:
            retry: reconnect and re-run once on failure. Correct for the cheap
                status/variable queries, where a dropped connection is the
                likely cause — and WRONG for expensive information_schema scans
                (H5). A read_timeout surfaces as ``OperationalError(2013, 'Lost
                connection …')``, indistinguishable from a genuine disconnect,
                so a blind retry re-ran a query that had just spent the whole
                timeout budget: double the load on an already-struggling server,
                then the same failure. Callers issuing heavy scans pass
                ``retry=False``.
        """
        if not self.connection and not self.connect():
            raise Exception("Cannot establish MySQL connection")

        cursor = None
        try:
            if PYMYSQL_AVAILABLE:
                cursor = self.connection.cursor(pymysql.cursors.DictCursor)
            elif MYSQL_CONNECTOR_AVAILABLE:
                cursor = self.connection.cursor(dictionary=True)
            else:
                cursor = self.connection.cursor()

            if params:
                cursor.execute(query, params)
            else:
                cursor.execute(query)

            # Strip comments, optimizer hints and match SELECT/SHOW/DESCRIBE/EXPLAIN and CTEs (N-16)
            cleaned_query = re.sub(r'/\*.*?\*/', '', query, flags=re.DOTALL)
            cleaned_query = re.sub(r'(--|#).*$', '', cleaned_query, flags=re.MULTILINE).strip().upper()
            if re.match(r'^(WITH\s+.*?\s+SELECT|SELECT|SHOW|DESCRIBE|EXPLAIN)', cleaned_query, flags=re.DOTALL):
                return cursor.fetchall()
            return []

        except Exception as e:
            self.logger.error(f"Query failed: {e}")
            if not retry:
                self.logger.error(
                    "Not retrying (expensive query). If this was a timeout, "
                    "raise 'timeout' in the config or reduce the scan scope."
                )
                raise
            # Try to reconnect once on connection-lost errors
            # MAJ-4: explicitly disconnect before reconnecting to prevent FD/connection leak
            retry_cursor = None
            try:
                self.disconnect()
                if self.connect():
                    if PYMYSQL_AVAILABLE:
                        retry_cursor = self.connection.cursor(pymysql.cursors.DictCursor)
                    elif MYSQL_CONNECTOR_AVAILABLE:
                        retry_cursor = self.connection.cursor(dictionary=True)
                    else:
                        retry_cursor = self.connection.cursor()
                    if params:
                        retry_cursor.execute(query, params)
                    else:
                        retry_cursor.execute(query)
                    cleaned_query = re.sub(r'/\*.*?\*/', '', query, flags=re.DOTALL)
                    cleaned_query = re.sub(r'(--|#).*$', '', cleaned_query, flags=re.MULTILINE).strip().upper()
                    if re.match(r'^(WITH\s+.*?\s+SELECT|SELECT|SHOW|DESCRIBE|EXPLAIN)', cleaned_query, flags=re.DOTALL):
                        return retry_cursor.fetchall()
                    return []
            except Exception as retry_err:
                self.logger.error(f"Retry also failed: {retry_err}")
                raise
            finally:
                if retry_cursor:
                    try:
                        retry_cursor.close()
                    except Exception:
                        pass
            raise
        finally:
            if cursor:
                try:
                    cursor.close()
                except Exception:
                    pass

    def test_connection(self) -> Dict[str, Any]:
        """Test MySQL connection and return status."""
        result = {
            'connected': False,
            'error': None,
            'version': None,
            'user': None,
            'credential_source': self.credential_source,
        }

        try:
            if self.connect():
                result['connected'] = True
                ver = self.execute_query("SELECT VERSION() AS version")
                if ver:
                    result['version'] = ver[0]['version']
                usr = self.execute_query("SELECT CURRENT_USER() AS user")
                if usr:
                    result['user'] = usr[0]['user']
        except Exception as e:
            result['error'] = str(e)

        return result

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------
    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.disconnect()

    def __del__(self):
        try:
            self.disconnect()
        except Exception:
            pass

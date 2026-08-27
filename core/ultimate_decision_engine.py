#!/usr/bin/env python3
"""
Ultimate Decision Engine for MySQL Auto-Tuner v1.0.4
===================================================
Evidence-based decision engine incorporating insights from 66 real-world production cases.

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
import json
import logging
import subprocess
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime, timedelta

class UltimateDecisionEngine:
    """
    Ultimate decision engine with file limit logic and advanced patterns
    """

    # M1: the single source of truth for anomaly names. These strings are the
    # contract between three places that previously drifted apart:
    #   * detect_anomalies()               — emits them
    #   * anomaly_detection.<name> (YAML)  — thresholds and actions
    #   * confidence_engine.risk_penalties — confidence penalties
    # The YAML used to spell two of them differently ('high_aborted_connections',
    # 'connection_overflow'), so `risk_penalties.get(anomaly, 0)` silently
    # returned 0 and those penalties never applied — confidence was INFLATED on
    # exactly the stressed servers where it should have dropped. config_audit
    # now asserts all three stay in step at startup.
    ANOMALY_NAMES = (
        'aborted_connections',
        'memory_pressure',
        'xmlrpc_overload',
        'connection_spikes',
    )


    def __init__(self, config: Dict[str, Any]):
        """Initialize the ultimate decision engine"""
        self.config = config
        self.logger = logging.getLogger(__name__)
        
        # Platform state.
        # H1: platform_override is set from --platform and, when present, wins
        # over auto-detection permanently. Previously the CLI override was
        # written straight into platform_type, and detect_platform() — the FIRST
        # thing generate_ultimate_recommendations() calls — silently overwrote
        # it, so --platform changed nothing downstream (guardrails, file-limit
        # thresholds, or even the platform printed in the report).
        self.platform_override = None
        self.platform_type = 'default'
        self.migration_state = 'unknown'
        self.peak_hour_state = False
        self.detected_anomalies = []
        self.current_pass = 1
        self.file_limit_required = False
        
        # Evidence tracking
        self.optimization_history = []
        self.confidence_factors = {}
        
        self.logger.info("Ultimate Decision Engine v1.0.4 initialized")
        self.logger.info(f"Evidence base: {self.config.get('evidence_base', {}).get('total_cases', 66)} production cases")
    
    def detect_platform(self, metrics: Dict[str, Any]) -> str:
        """
        Detect platform type with enhanced logic
        
        Args:
            metrics: System and MySQL metrics
            
        Returns:
            Platform type string
        """
        # H1: an explicit --platform override short-circuits detection entirely.
        # This method is called from several places (analyze_system, and again
        # from generate_ultimate_recommendations), so the guard has to live here
        # rather than at the call sites.
        if self.platform_override:
            self.platform_type = self.platform_override
            self.logger.info(f"Platform override in effect: {self.platform_override}")
            return self.platform_override

        signatures = self.config.get('platform_detection', {}).get('signatures', {})

        # Check for DirectAdmin signatures
        if self._check_platform_signatures(signatures.get('directadmin', []), metrics):
            self.platform_type = 'directadmin'
            self.logger.info("Platform detected: DirectAdmin")
            return 'directadmin'
        
        # Check for cPanel signatures
        if self._check_platform_signatures(signatures.get('cpanel', []), metrics):
            self.platform_type = 'cpanel'
            self.logger.info("Platform detected: cPanel")
            return 'cpanel'
        
        # Check for LiteSpeed signatures
        if self._check_platform_signatures(signatures.get('litespeed', []), metrics):
            self.platform_type = 'litespeed'
            self.logger.info("Platform detected: LiteSpeed")
            return 'litespeed'
        
        # Fallback to heuristic detection
        database_names = metrics.get('database_names', [])
        total_tables = metrics.get('total_tables', 0)
        
        # DirectAdmin heuristic: Many databases with user_db pattern and high table count
        if len(database_names) > 100 and total_tables > 30000:
            self.platform_type = 'directadmin'
            self.logger.info("Platform detected: DirectAdmin (heuristic)")
            return 'directadmin'
        
        # cPanel heuristic: Moderate databases with user_db pattern
        if len(database_names) > 10 and any('_' in db for db in database_names[:10]):
            self.platform_type = 'cpanel'
            self.logger.info("Platform detected: cPanel (heuristic)")
            return 'cpanel'
        
        self.platform_type = 'default'
        self.logger.info("Platform detected: Default (unknown)")
        return 'default'
    
    def _check_platform_signatures(self, signatures: List[str], metrics: Dict[str, Any]) -> bool:
        """Check if platform signatures match"""
        for signature in signatures:
            if signature.startswith('/'):
                # File path signature
                if os.path.exists(signature):
                    return True
            elif ':' in signature:
                # Metric signature
                key, expected_value = signature.split(':', 1)
                if key.strip() in metrics:
                    actual_value = str(metrics[key.strip()])
                    if expected_value.strip().strip("'\"") in actual_value:
                        return True
            else:
                # Process signature
                try:
                    result = subprocess.run(['pgrep', signature], capture_output=True, text=True, timeout=5)
                    if result.returncode == 0:
                        return True
                except Exception:
                    pass
        return False
    
    def detect_file_limit_requirement(self, metrics: Dict[str, Any]) -> bool:
        """
        Detect if file limit adjustment is required
        
        Args:
            metrics: System and MySQL metrics
            
        Returns:
            True if file limit adjustment is required
        """
        self.file_limit_required = False
        file_limit_config = self.config.get('file_limit_logic', {}).get(self.platform_type, {})
        
        if not file_limit_config:
            return False
        
        table_count = metrics.get('total_tables', 0)
        threshold = file_limit_config.get('table_count_threshold', 50000)
        
        if table_count >= threshold:
            self.file_limit_required = True
            self.logger.info(f"File limit adjustment required: {table_count} tables >= {threshold} threshold")
            return True
        
        return False
    
    def generate_file_limit_recommendation(self, metrics: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Generate file limit recommendation
        
        Args:
            metrics: System and MySQL metrics
            
        Returns:
            File limit recommendation or None
        """
        if not self.file_limit_required:
            return None
        
        file_limit_config = self.config.get('file_limit_logic', {}).get(self.platform_type, {})
        
        if not file_limit_config:
            return None
        
        required_limit = file_limit_config.get('required_limit_nofile', 100000)
        systemd_edit_required = file_limit_config.get('systemd_edit_required', False)
        confidence = file_limit_config.get('confidence', 0.8)
        
        recommendation = {
            'parameter': 'open_files_limit',
            'current_value': metrics.get('open_files_limit', 'unknown'),
            'recommended_value': required_limit,
            'reason': f'{self.platform_type.title()} file limit optimization for {metrics.get("total_tables", 0)} tables',
            'impact': 'critical',
            'restart_required': True,
            'systemd_edit_required': systemd_edit_required,
            'systemd_commands': [
                'sudo systemctl edit mariadb',
                'Add: [Service]\\nLimitNOFILE=' + str(required_limit),
                'sudo systemctl daemon-reload',
                'sudo systemctl restart mariadb'
            ] if systemd_edit_required else [],
            'confidence': confidence,
            'evidence_source': f'{self.config.get("evidence_base", {}).get("total_cases", 66)} production cases'
        }
        
        return recommendation
    
    def detect_migration_state(self, metrics: Dict[str, Any]) -> str:
        """
        Detect MyISAM migration state with enhanced logic
        
        Args:
            metrics: System and MySQL metrics
            
        Returns:
            Migration state string
        """
        myisam_tables = metrics.get('myisam_table_count', 0)
        innodb_tables = metrics.get('innodb_table_count', 0)
        total_tables = myisam_tables + innodb_tables
        
        if total_tables == 0:
            self.migration_state = 'unknown'
            return 'unknown'
        
        myisam_ratio = myisam_tables / total_tables
        
        # M3: this used to depend ENTIRELY on a 'last_migration_timestamp'
        # metric that no collector has ever produced, so `recent_migration` was
        # permanently False and the 'post_migration' branch below was
        # unreachable — taking generate_post_migration_recommendations() and the
        # post_migration confidence modifier down with it, while the README
        # advertised "enhanced migration intelligence".
        #
        # The timestamp is still honoured if anything ever supplies it, but the
        # state is now also derivable from evidence actually on hand: a server
        # that has moved to InnoDB but still carries MyISAM leftovers AND still
        # has a key_buffer_size sized for the MyISAM era. That oversized key
        # buffer is the tell — it is memory reserved for tables that no longer
        # need it, which is precisely what the post-migration path reclaims.
        migration_timestamp = metrics.get('last_migration_timestamp')
        recent_migration = False

        if migration_timestamp:
            try:
                migration_time = datetime.fromisoformat(str(migration_timestamp).replace('Z', '+00:00'))
                recent_migration = (datetime.now() - migration_time) < timedelta(days=7)
            except Exception:
                pass

        if not recent_migration:
            key_buffer_mb = metrics.get('key_buffer_size_mb', 0)
            myisam_index_mb = metrics.get('myisam_index_size_mb', 0)
            # Oversized = the key buffer could hold several times the MyISAM
            # INDEX data that is actually left (and is not merely the 8-16M
            # default). Compared against index size, not row data, for the same
            # reason the key_buffer_size recommendation is (M4): the key buffer
            # caches MyISAM index blocks only. Using row data here would have
            # kept these two decisions disagreeing about what the buffer holds.
            key_buffer_oversized = (
                key_buffer_mb > 32 and key_buffer_mb > max(myisam_index_mb, 1) * 2
            )
            recent_migration = (
                0 < myisam_ratio < 0.1 and key_buffer_oversized
            )
            if recent_migration:
                self.logger.info(
                    "Post-migration signature: %.1f%% MyISAM tables remain but "
                    "key_buffer_size is %dM for only %dM of MyISAM index data",
                    myisam_ratio * 100, key_buffer_mb, myisam_index_mb,
                )

        # Enhanced migration state detection
        if myisam_ratio > 0.8:
            self.migration_state = 'pre_migration'
        elif myisam_ratio < 0.1 and recent_migration:
            self.migration_state = 'post_migration'
        elif myisam_ratio < 0.05:
            self.migration_state = 'innodb_only'
        else:
            self.migration_state = 'mixed_engines'

        self.logger.info(f"Migration state detected: {self.migration_state} (MyISAM: {myisam_ratio:.1%})")
        return self.migration_state
    
    def detect_peak_hour_state(self, metrics: Dict[str, Any]) -> bool:
        """
        Detect peak hour state with enhanced logic
        
        Args:
            metrics: System and MySQL metrics
            
        Returns:
            True if peak hour state detected
        """
        peak_hour_config = self.config.get('peak_hour_logic', {})
        
        max_used_connections = metrics.get('max_used_connections', 0)
        max_connections = metrics.get('max_connections', 151)
        connection_ratio = max_used_connections / max_connections if max_connections > 0 else 0
        
        # M-5: Wire up configured connection_spike_threshold (default 0.8)
        connection_spike_threshold = peak_hour_config.get('connection_spike_threshold', 0.8)
        connection_spike = connection_ratio > connection_spike_threshold
        
        self.peak_hour_state = connection_spike
        
        if self.peak_hour_state:
            self.logger.info(
                f"Peak hour state detected: connection_ratio={connection_ratio:.2f} "
                f"(threshold={connection_spike_threshold})"
            )
        
        return self.peak_hour_state
    
    def detect_anomalies(self, metrics: Dict[str, Any]) -> List[str]:
        """
        Detect system anomalies with enhanced patterns
        
        Args:
            metrics: System and MySQL metrics
            
        Returns:
            List of detected anomalies
        """
        anomalies = []
        anomaly_config = self.config.get('anomaly_detection', {})
        
        # Aborted connections anomaly
        # M2: the collector ALWAYS sets 'total_connects' (defaulting to 0 when
        # SHOW STATUS has no 'Connections' row), so the .get() default of 1
        # never applies and a 0 here raised ZeroDivisionError, aborting the
        # entire analysis run. `or 1` is the guard the default was meant to be.
        aborted_connects = metrics.get('aborted_connects', 0)
        total_connects = metrics.get('total_connects', 1) or 1
        aborted_ratio = aborted_connects / total_connects
        
        aborted_threshold = anomaly_config.get('aborted_connections', {}).get('threshold', 0.03)
        if aborted_ratio > aborted_threshold:
            anomalies.append('aborted_connections')
            self.logger.warning(f"Aborted connections anomaly: {aborted_ratio:.1%} > {aborted_threshold:.1%}")
        
        # Memory pressure anomaly
        swap_usage_gb = metrics.get('swap_usage_gb', 0)
        swap_threshold = anomaly_config.get('memory_pressure', {}).get('swap_usage_threshold_gb', 10)
        
        if swap_usage_gb > swap_threshold:
            anomalies.append('memory_pressure')
            self.logger.warning(f"Memory pressure anomaly: {swap_usage_gb}GB swap usage > {swap_threshold}GB threshold")
        
        # XMLRPC overload anomaly
        tmp_disk_tables = metrics.get('created_tmp_disk_tables', 0)
        select_full_join = metrics.get('select_full_join', 0)
        
        tmp_disk_threshold = anomaly_config.get('xmlrpc_overload', {}).get('tmp_disk_table_threshold', 300000)
        join_threshold = anomaly_config.get('xmlrpc_overload', {}).get('select_full_join_threshold', 100000)
        
        if tmp_disk_tables > tmp_disk_threshold and select_full_join > join_threshold:
            anomalies.append('xmlrpc_overload')
            self.logger.warning(f"XMLRPC overload anomaly: {tmp_disk_tables} tmp disk tables, {select_full_join} full joins")
        
        # Connection spikes anomaly
        max_used_connections = metrics.get('max_used_connections', 0)
        max_connections = metrics.get('max_connections', 151)

        spike_ratio = anomaly_config.get('connection_spikes', {}).get(
            'utilisation_threshold', 0.95
        )
        if max_connections > 0 and max_used_connections >= max_connections * spike_ratio:
            anomalies.append('connection_spikes')
            self.logger.warning(f"Connection spike anomaly: {max_used_connections}/{max_connections} connections")

        self.detected_anomalies = anomalies
        return anomalies
    
    def calculate_ultimate_confidence(self, recommendation: Dict[str, Any], metrics: Dict[str, Any]) -> float:
        """
        Calculate ultimate confidence score with enhanced factors
        
        Args:
            recommendation: Parameter recommendation
            metrics: System and MySQL metrics
            
        Returns:
            Confidence score (0.0-1.0)
        """
        confidence_config = self.config.get('confidence_engine', {})
        base_confidence = confidence_config.get('base_confidence', 0.7)
        
        # Start with base confidence
        confidence = base_confidence
        
        # Apply evidence modifiers
        evidence_modifiers = confidence_config.get('evidence_modifiers', {})
        
        # Table count evidence
        if metrics.get('total_tables', 0) > 10000:
            confidence += evidence_modifiers.get('table_count', 0.1)
        
        # Memory usage evidence
        total_ram_gb = metrics.get('total_ram_mb', 0) / 1024
        if total_ram_gb > 32:
            confidence += evidence_modifiers.get('memory_usage', 0.15)
        
        # Performance metrics evidence
        if metrics.get('innodb_buffer_pool_hit_rate', 0) > 0.99:
            confidence += evidence_modifiers.get('performance_metrics', 0.2)
        
        # Platform-specific evidence
        if self.platform_type != 'default':
            confidence += evidence_modifiers.get('platform_specific', 0.1)
        
        # Multi-pass confidence boost
        if self.current_pass > 1:
            pass_boost = (self.current_pass - 1) * 0.1
            confidence += pass_boost
        
        # Post-migration confidence boost
        if self.migration_state == 'post_migration':
            confidence += self.config.get('post_migration_logic', {}).get('confidence_modifier', 0.15)
        
        # Peak hour confidence boost
        if self.peak_hour_state:
            confidence += self.config.get('peak_hour_logic', {}).get('confidence_modifier', 0.1)
        
        # Apply risk penalties
        risk_penalties = confidence_config.get('risk_penalties', {})
        
        for anomaly in self.detected_anomalies:
            penalty = risk_penalties.get(anomaly, 0)
            confidence += penalty  # penalties are negative
        
        # Ensure confidence is within bounds
        confidence = max(0.0, min(1.0, confidence))
        
        return confidence
    
    def generate_multi_pass_recommendations(self, metrics: Dict[str, Any], base_recommendations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Generate multi-pass optimization recommendations
        
        Args:
            metrics: System and MySQL metrics
            base_recommendations: Base recommendations
            
        Returns:
            Enhanced multi-pass recommendations
        """
        multi_pass_config = self.config.get('multi_pass_logic', {})
        pass_config = multi_pass_config.get(f'pass_{self.current_pass}', {})
        
        if not pass_config:
            return base_recommendations

        # M13 / N-12: resolve the buffer pool this pass will actually SET before the
        # loop, so innodb_log_file_size is sized against the post-multiplier
        # value. Reading it inside the loop saw the pre-multiplier number (the
        # input list is not mutated), which silently turned pass 1's configured
        # 0.15 log ratio into an effective 0.25 of the pool being applied.
        # When falling back to active pool, use the current pool unscaled (N-12).
        bp_multiplier = pass_config.get('buffer_pool_multiplier', 1.0)
        target_pool_mb = self._recommended_buffer_pool_mb(
            base_recommendations, metrics, multiplier=bp_multiplier
        )

        enhanced_recommendations = []

        for rec in base_recommendations:
            enhanced_rec = rec.copy()
            
            # Apply multi-pass enhancements
            if rec['parameter'] == 'innodb_buffer_pool_size':
                current_value = self._parse_size_value(rec['recommended_value'])
                enhanced_value = int(current_value * bp_multiplier)
                enhanced_rec['recommended_value'] = f'{enhanced_value}M'
                enhanced_rec['reason'] += f' (Pass {self.current_pass} enhancement: {bp_multiplier}x)'
            
            elif rec['parameter'] == 'innodb_log_file_size':
                # M13: size against the buffer pool this run is RECOMMENDING,
                # not the one currently configured, and keep the 256M floor the
                # base recommender applies. Previously this overwrote the base
                # value with `current_pool * ratio`, which both discarded the
                # floor and tracked a buffer pool the tool was about to change —
                # so the log was sized for the old pool, not the new one.
                ratio = pass_config.get('log_file_ratio', 0.25)
                log_size_mb = max(int(target_pool_mb * ratio), 256)
                enhanced_rec['recommended_value'] = f'{log_size_mb}M'
                enhanced_rec['reason'] += (
                    f' (Pass {self.current_pass}: {ratio:.0%} of the '
                    f'{target_pool_mb}M target buffer pool)'
                )
            
            elif rec['parameter'] == 'table_definition_cache':
                multiplier = pass_config.get('table_cache_multiplier', 1.0)
                current_value = int(rec['recommended_value'])
                enhanced_value = int(current_value * multiplier)
                enhanced_rec['recommended_value'] = enhanced_value
                enhanced_rec['reason'] += f' (Pass {self.current_pass} scaling: {multiplier}x)'
            
            # Apply confidence multiplier
            confidence_multiplier = pass_config.get('confidence_multiplier', 1.0)
            base_confidence = self.calculate_ultimate_confidence(enhanced_rec, metrics)
            enhanced_rec['confidence'] = min(1.0, base_confidence * confidence_multiplier)
            
            enhanced_recommendations.append(enhanced_rec)
        
        return enhanced_recommendations
    
    def generate_post_migration_recommendations(self, metrics: Dict[str, Any], base_recommendations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Generate post-migration optimization recommendations
        
        Args:
            metrics: System and MySQL metrics
            base_recommendations: Base recommendations
            
        Returns:
            Post-migration enhanced recommendations
        """
        if self.migration_state != 'post_migration':
            return base_recommendations
        
        post_migration_config = self.config.get('post_migration_logic', {})
        enhanced_recommendations = []
        
        for rec in base_recommendations:
            enhanced_rec = rec.copy()
            
            # Key buffer size reduction
            if rec['parameter'] == 'key_buffer_size':
                reduction_factor = post_migration_config.get('key_buffer_reduction', 0.8)
                current_value = self._parse_size_value(rec['recommended_value'])
                reduced_value = int(current_value * reduction_factor)
                enhanced_rec['recommended_value'] = f'{reduced_value}M'
                enhanced_rec['reason'] += f' (Post-migration reduction: {reduction_factor:.0%})'
            
            # Buffer pool adjustment for memory pressure
            elif rec['parameter'] == 'innodb_buffer_pool_size':
                swap_usage_gb = metrics.get('swap_usage_gb', 0)
                if swap_usage_gb > 10:  # Memory pressure detected
                    adjustment_factor = post_migration_config.get('buffer_pool_adjustment', 0.9)
                    current_value = self._parse_size_value(rec['recommended_value'])
                    adjusted_value = int(current_value * adjustment_factor)
                    enhanced_rec['recommended_value'] = f'{adjusted_value}M'
                    enhanced_rec['reason'] += f' (Memory pressure relief: {adjustment_factor:.0%})'
            
            # Timeout reductions
            elif 'timeout' in rec['parameter']:
                reduction_factor = post_migration_config.get('timeout_reduction', 0.5)
                current_value = int(rec['recommended_value'])
                reduced_value = int(current_value * reduction_factor)
                enhanced_rec['recommended_value'] = reduced_value
                enhanced_rec['reason'] += f' (Post-migration timeout reduction: {reduction_factor:.0%})'
            
            enhanced_recommendations.append(enhanced_rec)
        
        return enhanced_recommendations
    
    def generate_platform_specific_recommendations(self, metrics: Dict[str, Any], base_recommendations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Generate platform-specific optimization recommendations
        
        Args:
            metrics: System and MySQL metrics
            base_recommendations: Base recommendations
            
        Returns:
            Platform-specific enhanced recommendations
        """
        enhanced_recommendations = []
        
        for rec in base_recommendations:
            enhanced_rec = rec.copy()
            
            # Apply platform-specific adjustments
            if rec['parameter'] == 'innodb_buffer_pool_size':
                enhanced_rec = self._apply_platform_buffer_pool_limits(enhanced_rec, metrics)
            
            elif rec['parameter'] == 'max_connections':
                enhanced_rec = self._apply_platform_connection_limits(enhanced_rec, metrics)
            
            enhanced_recommendations.append(enhanced_rec)
        
        # Add file limit recommendation if required
        file_limit_rec = self.generate_file_limit_recommendation(metrics)
        if file_limit_rec:
            enhanced_recommendations.append(file_limit_rec)
        
        return enhanced_recommendations
    
    def _apply_platform_buffer_pool_limits(self, recommendation: Dict[str, Any], metrics: Dict[str, Any]) -> Dict[str, Any]:
        """Apply platform-specific buffer pool limits"""
        safety_guardrails = self.config.get('fallback_logic', {}).get('safety_guardrails', {})
        max_percent = safety_guardrails.get('max_buffer_pool_percent', {}).get(self.platform_type, 0.8)
        
        total_ram_mb = metrics.get('total_ram_mb', 0)
        max_buffer_pool_mb = int(total_ram_mb * max_percent)
        
        current_value = self._parse_size_value(recommendation['recommended_value'])
        
        if current_value > max_buffer_pool_mb:
            recommendation['recommended_value'] = f'{max_buffer_pool_mb}M'
            recommendation['reason'] += f' ({self.platform_type.title()} {max_percent:.0%} RAM limit applied)'
        
        return recommendation
    
    def _apply_platform_connection_limits(self, recommendation: Dict[str, Any], metrics: Dict[str, Any]) -> Dict[str, Any]:
        """Apply platform-specific connection limits while respecting raise-only floor."""
        safety_guardrails = self.config.get('fallback_logic', {}).get('safety_guardrails', {})
        max_connections = safety_guardrails.get('max_connections', {}).get(self.platform_type, 1000)
        
        current_rec = int(recommendation['recommended_value'])
        cur_configured = int(metrics.get('max_connections', 0) or recommendation.get('current_value', 0) or 0)
        max_used = int(metrics.get('max_used_connections', 0) or 0)
        floor = max(cur_configured, max_used)

        if max_connections < floor:
            # Platform cap sits below configured setting or observed peak — never recommend downgrade
            self.logger.warning(
                "Platform %s max_connections limit (%d) is below current/peak floor (%d); "
                "preserving connection floor to prevent outages.",
                self.platform_type, max_connections, floor,
            )
            recommendation['recommended_value'] = max(current_rec, floor)
            recommendation['reason'] += (
                f' ({self.platform_type.title()} limit {max_connections} noted; '
                f'floor of {floor} preserved)'
            )
        elif current_rec > max_connections:
            recommendation['recommended_value'] = max_connections
            recommendation['reason'] += f' ({self.platform_type.title()} connection limit applied)'
        
        return recommendation
    
    # ------------------------------------------------------------------
    # Anomaly-driven adjustments (M14)
    # ------------------------------------------------------------------
    # Maps an action name from anomaly_detection.<anomaly>.actions to the
    # parameter it scales. Previously the whole `actions:` block was read by
    # NOTHING — detecting an anomaly produced a log line and a confidence
    # penalty, but changed no recommendation, while the README advertised
    # "anomaly detection" as a headline feature.
    _ANOMALY_ACTION_PARAMS = {
        'reduce_buffer_pool':            'innodb_buffer_pool_size',
        'reduce_max_connections':        'max_connections',
        'reduce_per_connection_buffers': 'join_buffer_size',
        'increase_tmp_table_size':       'tmp_table_size',
        'increase_join_buffer_size':     'join_buffer_size',
    }

    # Parameter bound constraints (M-4): maximum ceilings and minimum floors in natural units
    _PARAM_BOUNDS = {
        'tmp_table_size': (None, 512),       # Max 512 MB
        'join_buffer_size': (None, 8192),     # Max 8192 KB
    }

    @staticmethod
    def _parse_size_to_unit(val: Any, target_suffix: str = '') -> float:
        """Parse val into numeric quantity in units of target_suffix ('', 'K', 'M', 'G')."""
        if val is None:
            return 0.0
        s = str(val).strip().upper()
        if not s:
            return 0.0
        if s.endswith('G'):
            bytes_val = float(s[:-1]) * 1024.0 * 1024.0 * 1024.0
        elif s.endswith('M'):
            bytes_val = float(s[:-1]) * 1024.0 * 1024.0
        elif s.endswith('K'):
            bytes_val = float(s[:-1]) * 1024.0
        else:
            try:
                return float(s)
            except ValueError:
                return 0.0

        target = target_suffix.upper()
        if target == 'G':
            return bytes_val / (1024.0 * 1024.0 * 1024.0)
        elif target == 'M':
            return bytes_val / (1024.0 * 1024.0)
        elif target == 'K':
            return bytes_val / 1024.0
        else:
            return bytes_val

    @staticmethod
    def _iter_actions(actions) -> List[Tuple[str, float]]:
        """Normalise the YAML actions list into (action_name, factor) pairs.

        The YAML shape is a list of single-key mappings:
            actions:
              - reduce_buffer_pool: 0.85
              - reduce_per_connection_buffers: 0.5
              A plain mapping is accepted too, so either style works.
        """
        pairs: List[Tuple[str, float]] = []
        if isinstance(actions, dict):
            items = actions.items()
        elif isinstance(actions, list):
            items = []
            for entry in actions:
                if isinstance(entry, dict):
                    items.extend(entry.items())
        else:
            return pairs
        for name, factor in items:
            try:
                pairs.append((str(name), float(factor)))
            except (TypeError, ValueError):
                continue
        return pairs

    def apply_anomaly_actions(self, recommendations: List[Dict[str, Any]],
                              metrics: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Scale recommendations according to the detected anomalies (M14).

        Every factor is a bounded multiplier on a value the recommender already
        produced, so this can never introduce a parameter that was not already
        vetted, and the safety guardrails still run afterwards.
        """
        if not self.detected_anomalies:
            return recommendations

        anomaly_config = self.config.get('anomaly_detection', {})
        # Collect the strongest factor per parameter so two anomalies touching
        # the same parameter compound predictably rather than by dict order.
        factors: Dict[str, float] = {}
        sources: Dict[str, List[str]] = {}
        for anomaly in self.detected_anomalies:
            for action, factor in self._iter_actions(
                anomaly_config.get(anomaly, {}).get('actions')
            ):
                param = self._ANOMALY_ACTION_PARAMS.get(action)
                if not param or factor <= 0:
                    continue
                factors[param] = factors.get(param, 1.0) * factor
                sources.setdefault(param, []).append(f"{anomaly}/{action}")

        if not factors:
            return recommendations

        adjusted = []
        for rec in recommendations:
            param = rec.get('parameter')
            factor = factors.get(param)
            if factor is None or abs(factor - 1.0) < 1e-9:
                adjusted.append(rec)
                continue
            new_rec = dict(rec)
            try:
                old = rec['recommended_value']
                is_sized = isinstance(old, str) and old[-1:].upper() in ('K', 'M', 'G')
                suffix = old[-1:].upper() if is_sized else ''
                base = (
                    float(str(old)[:-1]) if is_sized else float(old)
                )
                scaled = int(base * factor)
                if scaled <= 0:
                    adjusted.append(rec)
                    continue

                # 1. Floor check against current/peak for max_connections
                if param == 'max_connections':
                    cur_configured = int(metrics.get('max_connections', 0) or rec.get('current_value', 0) or 0)
                    max_used = int(metrics.get('max_used_connections', 0) or 0)
                    floor = max(cur_configured, max_used)
                    if scaled < floor:
                        self.logger.warning(
                            "Anomaly adjustment would scale max_connections to %d, below "
                            "current/peak floor (%d); bounding by floor to prevent connection exhaustion.",
                            scaled, floor,
                        )
                        scaled = floor
                # 2. Floor check for per-connection buffers & general parameters (H3 raise-only invariant)
                elif 'current_value' in rec:
                    cur = int(self._parse_size_to_unit(rec.get('current_value', 0), suffix))
                    if cur > 0 and scaled < cur:
                        self.logger.warning(
                            "Anomaly adjustment would scale %s to %d%s, below "
                            "current configured value (%d%s); bounding by floor to maintain raise-only invariant.",
                            param, scaled, suffix, cur, suffix,
                        )
                        scaled = cur

                # 3. Ceiling & bounds checks from _PARAM_BOUNDS (M-4)
                lo, hi = self._PARAM_BOUNDS.get(param, (None, None))
                if hi is not None and scaled > hi:
                    self.logger.info(
                        "Anomaly adjustment for %s (%d%s) capped at maximum ceiling %d%s",
                        param, scaled, suffix, hi, suffix,
                    )
                    scaled = hi
                if lo is not None and scaled < lo:
                    scaled = lo

                new_rec['recommended_value'] = (
                    f'{scaled}{suffix}' if is_sized else scaled
                )
                new_rec['reason'] = (
                    f"{rec.get('reason', '')} "
                    f"(Anomaly adjustment x{factor:.2f}: {', '.join(sources[param])})"
                ).strip()
                new_rec.setdefault('anomaly_adjustments', []).extend(sources[param])
                self.logger.info(
                    "Anomaly adjustment: %s %s -> %s (x%.2f from %s)",
                    param, old, new_rec['recommended_value'], factor,
                    ', '.join(sources[param]),
                )
            except (TypeError, ValueError, KeyError):
                adjusted.append(rec)
                continue
            adjusted.append(new_rec)
        return adjusted

    def _apply_ultimate_safety_guardrails(self, recommendations: List[Dict[str, Any]], metrics: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Apply ultimate safety guardrails"""
        safe_recommendations = []
        
        for rec in recommendations:
            safe_rec = rec.copy()
            
            # Apply parameter-specific guardrails
            if rec['parameter'] == 'innodb_buffer_pool_size':
                safe_rec = self._apply_platform_buffer_pool_limits(safe_rec, metrics)
            
            elif rec['parameter'] == 'max_connections':
                safe_rec = self._apply_platform_connection_limits(safe_rec, metrics)
            
            elif rec['parameter'] == 'innodb_log_file_size':
                max_log_size_gb = self.config.get('fallback_logic', {}).get('safety_guardrails', {}).get('max_log_file_size_gb', 16)
                current_value = self._parse_size_value(safe_rec['recommended_value'])
                max_log_size_mb = max_log_size_gb * 1024
                
                if current_value > max_log_size_mb:
                    safe_rec['recommended_value'] = f'{max_log_size_mb}M'
                    safe_rec['reason'] += f' (Maximum log file size limit: {max_log_size_gb}GB)'
            
            safe_recommendations.append(safe_rec)
        
        return safe_recommendations
    
    def generate_ultimate_recommendations(self, metrics: Dict[str, Any], base_recommendations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Generate ultimate optimization recommendations
        
        Args:
            metrics: System and MySQL metrics
            base_recommendations: Base recommendations
            
        Returns:
            Ultimate recommendations with all enhancements
        """
        self.logger.info("Generating ultimate recommendations...")
        
        # Detect system state
        self.detect_platform(metrics)
        self.detect_migration_state(metrics)
        self.detect_peak_hour_state(metrics)
        self.detect_anomalies(metrics)
        self.detect_file_limit_requirement(metrics)
        
        # Apply enhancements in order
        recommendations = base_recommendations.copy()
        
        # 1. Multi-pass enhancements
        recommendations = self.generate_multi_pass_recommendations(metrics, recommendations)
        
        # 2. Post-migration adjustments
        recommendations = self.generate_post_migration_recommendations(metrics, recommendations)
        
        # 3. Platform-specific optimizations
        recommendations = self.generate_platform_specific_recommendations(metrics, recommendations)

        # 3b. Anomaly-driven adjustments (M14) — before the guardrails, so any
        # increase an anomaly asks for is still subject to every cap.
        recommendations = self.apply_anomaly_actions(recommendations, metrics)

        # 4. Apply safety guardrails
        recommendations = self._apply_ultimate_safety_guardrails(recommendations, metrics)
        
        # 5. Add ultimate metadata
        pass_config = (
            self.config.get('multi_pass_logic', {})
            .get(f'pass_{self.current_pass}', {})
        )
        conf_multiplier = pass_config.get('confidence_multiplier', 1.0)

        for rec in recommendations:
            base_conf = self.calculate_ultimate_confidence(rec, metrics)
            rec['confidence'] = min(1.0, round(base_conf * conf_multiplier, 2))
            rec['ultimate_metadata'] = {
                'platform': self.platform_type,
                'migration_state': self.migration_state,
                'peak_hour_state': self.peak_hour_state,
                'detected_anomalies': self.detected_anomalies,
                'current_pass': self.current_pass,
                'file_limit_required': self.file_limit_required,
                'evidence_base': self.config.get('evidence_base', {})
            }
            rec['platform_info'] = {
                'platform_type': self.platform_type,
                'platform_confidence': 0.9 if self.platform_type != 'default' else 0.6
            }
            rec['safety_guardrails'] = {
                'applied': True,
                'platform_limits': True,
                'memory_limits': True
            }
        
        self.logger.info(f"Generated {len(recommendations)} ultimate recommendations")
        return recommendations
    
    def _parse_size_value(self, value: str) -> int:
        """Parse size value string to MB"""
        if isinstance(value, (int, float)):
            return int(value)

        value_str = str(value).strip().upper()
        if value_str.endswith(('G', 'GB')):
            num_str = value_str[:-2] if value_str.endswith('GB') else value_str[:-1]
            return int(float(num_str) * 1024)
        elif value_str.endswith(('M', 'MB')):
            num_str = value_str[:-2] if value_str.endswith('MB') else value_str[:-1]
            return int(float(num_str))
        elif value_str.endswith(('K', 'KB')):
            num_str = value_str[:-2] if value_str.endswith('KB') else value_str[:-1]
            return int(float(num_str) / 1024)
        else:
            return int(float(value_str))
    
    def _get_buffer_pool_size_mb(self, metrics: Dict[str, Any]) -> int:
        """Get current buffer pool size in MB"""
        return metrics.get('innodb_buffer_pool_size_mb', 1024)

    def _recommended_buffer_pool_mb(self, recommendations: List[Dict[str, Any]],
                                    metrics: Dict[str, Any],
                                    multiplier: float = 1.0) -> int:
        """Buffer pool this run is targeting: the recommended value if one was
        generated (scaled by multiplier), else the currently configured value unscaled (N-12/M13)."""
        for rec in recommendations:
            if rec.get('parameter') == 'innodb_buffer_pool_size':
                try:
                    val = self._parse_size_value(rec['recommended_value'])
                    return int(val * multiplier)
                except (KeyError, TypeError, ValueError):
                    break
        return self._get_buffer_pool_size_mb(metrics)
    
    def save_optimization_history(self, metrics: Dict[str, Any], recommendations: List[Dict[str, Any]]):
        """Save optimization history for future analysis, appending to historical records."""
        history_entry = {
            'timestamp': datetime.now().isoformat(),
            'platform': self.platform_type,
            'migration_state': self.migration_state,
            'current_pass': self.current_pass,
            'metrics_summary': {
                'total_ram_mb': metrics.get('total_ram_mb', 0),
                'total_tables': metrics.get('total_tables', 0),
                'max_connections': metrics.get('max_connections', 0),
                'innodb_buffer_pool_size_mb': metrics.get('innodb_buffer_pool_size_mb', 0)
            },
            'recommendations_count': len(recommendations),
            'avg_confidence': sum(r.get('confidence', 0) for r in recommendations) / len(recommendations) if recommendations else 0
        }
        
        # Save to file — M1: root-owned dir or a UID-scoped temp dir (never a
        # predictable shared /tmp path), written O_NOFOLLOW to defeat symlink
        # pre-creation attacks on shared hosts.
        try:
            from utils.safe_io import choose_writable_dir, secure_open_write
        except ImportError:
            from ..utils.safe_io import choose_writable_dir, secure_open_write

        history_dir = choose_writable_dir(
            "/var/log/mysql-autotuner", "mysql-autotuner-history"
        )
        if history_dir:
            history_path = os.path.join(history_dir, "optimization_history.json")
            existing_history = []
            if os.path.isfile(history_path):
                try:
                    with open(history_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        if isinstance(data, list):
                            existing_history = data
                except (json.JSONDecodeError, OSError) as e:
                    self.logger.warning(f"Could not load existing optimization history from {history_path}: {e}")

            existing_history.append(history_entry)
            # MAJ-3: Prune to keep last 100 entries to prevent unbounded growth
            self.optimization_history = existing_history[-100:]

            try:
                import tempfile
                payload = json.dumps(self.optimization_history, indent=2, default=str)
                fd, tmp = tempfile.mkstemp(dir=history_dir, prefix=".history_tmp_")
                try:
                    try:
                        f = os.fdopen(fd, "w", encoding="utf-8")
                    except BaseException:
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                        raise

                    with f:
                        f.write(payload)
                        f.flush()
                        os.fsync(f.fileno())

                    try:
                        os.chmod(tmp, 0o640)
                    except OSError:
                        pass

                    os.replace(tmp, history_path)
                except Exception:
                    if os.path.exists(tmp):
                        try:
                            os.unlink(tmp)
                        except OSError:
                            pass
                    raise
            except (PermissionError, OSError, TypeError, ValueError) as e:
                self.logger.error(f"Failed to save optimization history: {e}")
        else:
            self.optimization_history.append(history_entry)
            self.optimization_history = self.optimization_history[-100:]
            self.logger.error("Failed to save optimization history: no writable directory")


#!/usr/bin/env python3
"""
System Information Utility
Collects system-level metrics and information

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
from typing import Dict, Any, Optional, List
from pathlib import Path

class SystemInfo:
    """System information collector"""
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
    
    def get_memory_info(self) -> Dict[str, Any]:
        """Get system memory information"""
        try:
            # Try to read from /proc/meminfo (Linux)
            if os.path.exists('/proc/meminfo'):
                return self._parse_proc_meminfo()
            else:
                # Fallback to other methods
                return self._get_memory_fallback()
        except Exception as e:
            self.logger.error(f"Failed to get memory info: {e}")
            return {}
    
    def _parse_proc_meminfo(self) -> Dict[str, Any]:
        """Parse /proc/meminfo for memory information"""
        memory_info = {}
        
        with open('/proc/meminfo', 'r') as f:
            for line in f:
                if ':' in line:
                    key, value = line.split(':', 1)
                    key = key.strip()
                    value = value.strip()
                    
                    # Extract numeric value (remove 'kB' suffix)
                    if 'kB' in value:
                        numeric_value = int(value.replace('kB', '').strip())
                        memory_info[key] = numeric_value
        
        # Calculate derived values
        total_kb = memory_info.get('MemTotal', 0)
        free_kb = memory_info.get('MemFree', 0)
        buffers_kb = memory_info.get('Buffers', 0)
        cached_kb = memory_info.get('Cached', 0)
        available_kb = memory_info.get('MemAvailable', 0)
        
        # If MemAvailable is not available, calculate it
        if available_kb == 0:
            available_kb = free_kb + buffers_kb + cached_kb
        
        used_kb = total_kb - available_kb
        
        return {
            'total_kb': total_kb,
            'total_mb': total_kb // 1024,
            'total_gb': total_kb // (1024 * 1024),
            'free_kb': free_kb,
            'free_mb': free_kb // 1024,
            'available_kb': available_kb,
            'available_mb': available_kb // 1024,
            'used_kb': used_kb,
            'used_mb': used_kb // 1024,
            'buffers_kb': buffers_kb,
            'cached_kb': cached_kb,
            'usage_percentage': (used_kb / total_kb * 100) if total_kb > 0 else 0,
            'raw_data': memory_info
        }
    
    def _get_memory_fallback(self) -> Dict[str, Any]:
        """Fallback method for getting memory info using 'free' command"""
        try:
            # Try using 'free' command
            result = subprocess.run(
                ['free', '-b'], 
                capture_output=True, 
                text=True, 
                timeout=5
            )
            
            if result.returncode == 0:
                lines = result.stdout.strip().split('\n')
                if len(lines) >= 2:
                    # Parse the memory line
                    mem_line = lines[1].split()
                    if len(mem_line) >= 4:
                        total_bytes = int(mem_line[1])
                        used_bytes = int(mem_line[2])
                        free_bytes = int(mem_line[3])
                        available_bytes = int(mem_line[6]) if len(mem_line) > 6 else free_bytes
                        
                        swap_total_bytes = 0
                        swap_free_bytes = 0
                        if len(lines) >= 3 and "swap" in lines[2].lower():
                            swap_line = lines[2].split()
                            if len(swap_line) >= 4:
                                swap_total_bytes = int(swap_line[1])
                                swap_free_bytes = int(swap_line[3])

                        return {
                            'total_kb': total_bytes // 1024,
                            'total_mb': total_bytes // (1024 * 1024),
                            'total_gb': total_bytes // (1024 * 1024 * 1024),
                            'used_kb': used_bytes // 1024,
                            'used_mb': used_bytes // (1024 * 1024),
                            'free_kb': free_bytes // 1024,
                            'free_mb': free_bytes // (1024 * 1024),
                            'available_kb': available_bytes // 1024,
                            'available_mb': available_bytes // (1024 * 1024),
                            'usage_percentage': (used_bytes / total_bytes * 100) if total_bytes > 0 else 0,
                            'raw_data': {
                                'SwapTotal': swap_total_bytes // 1024,
                                'SwapFree': swap_free_bytes // 1024,
                            }
                        }
        except Exception as e:
            self.logger.debug(f"Free command failed: {e}")
        
        return {}
    
    def get_cpu_info(self) -> Dict[str, Any]:
        """Get CPU information"""
        try:
            cpu_info = {}
            
            # Try to read from /proc/cpuinfo (Linux)
            if os.path.exists('/proc/cpuinfo'):
                cpu_info = self._parse_proc_cpuinfo()
            
            # Get load average
            load_avg = self.get_load_average()
            cpu_info.update(load_avg)
            
            return cpu_info
        except Exception as e:
            self.logger.error(f"Failed to get CPU info: {e}")
            return {}
    
    def _parse_proc_cpuinfo(self) -> Dict[str, Any]:
        """Parse /proc/cpuinfo for CPU information"""
        cpu_info = {
            'cores': 0,
            'processors': [],
            'model': 'Unknown',
            'vendor': 'Unknown',
            'cache_size': 'Unknown'
        }
        
        try:
            with open('/proc/cpuinfo', 'r') as f:
                processor_info = {}
                
                for line in f:
                    if ':' in line:
                        key, value = line.split(':', 1)
                        key = key.strip()
                        value = value.strip()
                        
                        if key == 'processor':
                            if processor_info:
                                cpu_info['processors'].append(processor_info)
                            processor_info = {'processor': int(value)}
                        elif key == 'model name':
                            processor_info['model_name'] = value
                            if cpu_info['model'] == 'Unknown':
                                cpu_info['model'] = value
                        elif key == 'vendor_id':
                            processor_info['vendor_id'] = value
                            if cpu_info['vendor'] == 'Unknown':
                                cpu_info['vendor'] = value
                        elif key == 'cache size':
                            processor_info['cache_size'] = value
                            if cpu_info['cache_size'] == 'Unknown':
                                cpu_info['cache_size'] = value
                        elif key == 'cpu cores':
                            processor_info['cores'] = int(value)
                        elif key == 'siblings':
                            processor_info['siblings'] = int(value)
                
                # Add the last processor
                if processor_info:
                    cpu_info['processors'].append(processor_info)
                
                # Calculate total cores
                cpu_info['cores'] = len(cpu_info['processors'])
                
                # Get physical cores if available
                if cpu_info['processors']:
                    first_proc = cpu_info['processors'][0]
                    if 'cores' in first_proc:
                        # Physical cores per processor
                        physical_cores = first_proc['cores']
                        # Count unique physical processors
                        physical_processors = len(set(
                            proc.get('physical id', proc['processor']) 
                            for proc in cpu_info['processors']
                        ))
                        cpu_info['physical_cores'] = physical_cores * physical_processors
        
        except Exception as e:
            self.logger.debug(f"Error parsing /proc/cpuinfo: {e}")
        
        return cpu_info
    
    def get_load_average(self) -> Dict[str, float]:
        """Get system load average"""
        try:
            # Try to read from /proc/loadavg (Linux)
            if os.path.exists('/proc/loadavg'):
                with open('/proc/loadavg', 'r') as f:
                    load_data = f.read().strip().split()
                    if len(load_data) >= 3:
                        return {
                            '1min': float(load_data[0]),
                            '5min': float(load_data[1]),
                            '15min': float(load_data[2])
                        }
            
            # Fallback to uptime command
            result = subprocess.run(
                ['uptime'], 
                capture_output=True, 
                text=True, 
                timeout=5
            )
            
            if result.returncode == 0:
                # Parse uptime output
                uptime_output = result.stdout.strip()
                load_match = re.search(r'load average[s]?:\s*([0-9.]+),?\s*([0-9.]+),?\s*([0-9.]+)', uptime_output)
                if load_match:
                    return {
                        '1min': float(load_match.group(1)),
                        '5min': float(load_match.group(2)),
                        '15min': float(load_match.group(3))
                    }
        
        except Exception as e:
            self.logger.debug(f"Failed to get load average: {e}")
        
        return {'1min': 0.0, '5min': 0.0, '15min': 0.0}
    
    def get_disk_info(self, datadir: str = None) -> Dict[str, Any]:
        """Get disk information.

        L4: ``total_space_gb`` / ``used_space_gb`` / ``available_space_gb`` were
        initialised to 0 and never populated — only ``filesystems[]`` was filled
        — so ``system_disk_total_gb`` and friends reported a permanent 0 and any
        report showing them said "Disk: 0 GB". They are now filled from the
        filesystem that actually matters: MySQL's datadir, falling back to the
        root filesystem when the datadir is unknown.
        """
        try:
            disk_info = {
                'filesystems': [],
                'total_space_gb': 0,
                'used_space_gb': 0,
                'available_space_gb': 0,
                'measured_at': '',
            }

            try:
                import shutil as _shutil
                probe = datadir if datadir and os.path.exists(datadir) else '/'
                if os.path.exists(probe):
                    usage = _shutil.disk_usage(probe)
                    gb = 1024 ** 3
                    disk_info['total_space_gb'] = round(usage.total / gb, 1)
                    disk_info['used_space_gb'] = round(usage.used / gb, 1)
                    disk_info['available_space_gb'] = round(usage.free / gb, 1)
                    disk_info['measured_at'] = probe
            except (OSError, ValueError, AttributeError) as exc:
                self.logger.debug(f"Could not measure disk usage: {exc}")
            
            # Use df command to get filesystem information
            result = subprocess.run(
                ['df', '-h'], 
                capture_output=True, 
                text=True, 
                timeout=10
            )
            
            if result.returncode == 0:
                lines = result.stdout.strip().split('\n')[1:]  # Skip header
                
                for line in lines:
                    parts = line.split()
                    if len(parts) >= 6:
                        filesystem = {
                            'device': parts[0],
                            'size': parts[1],
                            'used': parts[2],
                            'available': parts[3],
                            'use_percentage': parts[4],
                            'mount_point': parts[5]
                        }
                        disk_info['filesystems'].append(filesystem)
            
            # Get disk I/O statistics if available
            disk_io = self._get_disk_io_stats()
            if disk_io:
                disk_info['io_stats'] = disk_io
            
            # Detect storage type (SSD/HDD)
            storage_type = self._detect_storage_type()
            if storage_type:
                disk_info['storage_type'] = storage_type
            
            return disk_info
        
        except Exception as e:
            self.logger.error(f"Failed to get disk info: {e}")
            return {}
    
    def _get_disk_io_stats(self) -> Dict[str, Any]:
        """Get disk I/O statistics"""
        try:
            # Try to read from /proc/diskstats (Linux)
            if os.path.exists('/proc/diskstats'):
                io_stats = {}
                
                with open('/proc/diskstats', 'r') as f:
                    for line in f:
                        parts = line.strip().split()
                        if len(parts) >= 14:
                            device = parts[2]
                            # Skip loop devices and partitions for main stats
                            if not device.startswith('loop') and not re.search(r'\d+$', device):
                                io_stats[device] = {
                                    'reads_completed': int(parts[3]),
                                    'reads_merged': int(parts[4]),
                                    'sectors_read': int(parts[5]),
                                    'time_reading': int(parts[6]),
                                    'writes_completed': int(parts[7]),
                                    'writes_merged': int(parts[8]),
                                    'sectors_written': int(parts[9]),
                                    'time_writing': int(parts[10])
                                }
                
                return io_stats
        
        except Exception as e:
            self.logger.debug(f"Failed to get disk I/O stats: {e}")
        
        return {}
    
    def _detect_storage_type(self) -> Dict[str, str]:
        """Detect storage type (SSD/HDD) for main devices"""
        storage_types = {}
        
        try:
            # Check /sys/block for rotational devices (Linux)
            block_path = Path('/sys/block')
            if block_path.exists():
                for device_path in block_path.iterdir():
                    if device_path.is_dir():
                        device_name = device_path.name
                        rotational_file = device_path / 'queue' / 'rotational'
                        
                        if rotational_file.exists():
                            try:
                                with open(rotational_file, 'r') as f:
                                    rotational = f.read().strip()
                                    storage_types[device_name] = 'HDD' if rotational == '1' else 'SSD'
                            except Exception:
                                pass
        
        except Exception as e:
            self.logger.debug(f"Failed to detect storage type: {e}")
        
        return storage_types
    
    def get_uptime(self) -> Dict[str, Any]:
        """Get system uptime"""
        try:
            # Try to read from /proc/uptime (Linux)
            if os.path.exists('/proc/uptime'):
                with open('/proc/uptime', 'r') as f:
                    uptime_seconds = float(f.read().split()[0])
                    
                    days = int(uptime_seconds // 86400)
                    hours = int((uptime_seconds % 86400) // 3600)
                    minutes = int((uptime_seconds % 3600) // 60)
                    
                    return {
                        'uptime_seconds': uptime_seconds,
                        'uptime_days': days,
                        'uptime_hours': hours,
                        'uptime_minutes': minutes,
                        'uptime_formatted': f"{days}d {hours}h {minutes}m"
                    }
            
            # Fallback to uptime command
            result = subprocess.run(
                ['uptime'], 
                capture_output=True, 
                text=True, 
                timeout=5
            )
            
            if result.returncode == 0:
                uptime_output = result.stdout.strip()
                # Parse uptime output for time information
                up_match = re.search(r'up\s+(.+?),\s+\d+\s+users?', uptime_output)
                if up_match:
                    return {'uptime_string': up_match.group(1)}
        
        except Exception as e:
            self.logger.debug(f"Failed to get uptime: {e}")
        
        return {}
    
    def get_os_info(self) -> Dict[str, Any]:
        """Get operating system information"""
        try:
            os_info = {}
            
            # Try to read from /etc/os-release (Linux)
            if os.path.exists('/etc/os-release'):
                with open('/etc/os-release', 'r') as f:
                    for line in f:
                        if '=' in line:
                            key, value = line.strip().split('=', 1)
                            # Remove quotes from value
                            value = value.strip('"\'')
                            os_info[key.lower()] = value
            
            # Get kernel information
            try:
                result = subprocess.run(
                    ['uname', '-a'], 
                    capture_output=True, 
                    text=True, 
                    timeout=5
                )
                if result.returncode == 0:
                    os_info['kernel'] = result.stdout.strip()
            except Exception:
                pass
            
            # Get architecture
            try:
                result = subprocess.run(
                    ['uname', '-m'], 
                    capture_output=True, 
                    text=True, 
                    timeout=5
                )
                if result.returncode == 0:
                    os_info['architecture'] = result.stdout.strip()
            except Exception:
                pass
            
            return os_info
        
        except Exception as e:
            self.logger.error(f"Failed to get OS info: {e}")
            return {}
    
    def get_network_info(self) -> Dict[str, Any]:
        """Get network interface information"""
        try:
            network_info = {'interfaces': []}
            
            # Use ip command if available
            try:
                result = subprocess.run(
                    ['ip', 'addr', 'show'], 
                    capture_output=True, 
                    text=True, 
                    timeout=10
                )
                
                if result.returncode == 0:
                    # Parse ip addr output
                    current_interface = None
                    
                    for line in result.stdout.split('\n'):
                        line = line.strip()
                        
                        # Interface line
                        if re.match(r'^\d+:', line):
                            if current_interface:
                                network_info['interfaces'].append(current_interface)
                            
                            parts = line.split()
                            interface_name = parts[1].rstrip(':')
                            current_interface = {
                                'name': interface_name,
                                'state': 'UP' if 'UP' in line else 'DOWN',
                                'addresses': []
                            }
                        
                        # IP address line
                        elif line.startswith('inet') and current_interface:
                            parts = line.split()
                            if len(parts) >= 2:
                                current_interface['addresses'].append({
                                    'type': parts[0],  # inet or inet6
                                    'address': parts[1]
                                })
                    
                    # Add the last interface
                    if current_interface:
                        network_info['interfaces'].append(current_interface)
            
            except Exception as e:
                self.logger.debug(f"ip command failed: {e}")
            
            return network_info
        
        except Exception as e:
            self.logger.error(f"Failed to get network info: {e}")
            return {}
    
    def get_process_info(self, process_name: str) -> List[Dict[str, Any]]:
        """Get information about specific processes"""
        try:
            processes = []
            
            # Use ps command to find processes
            result = subprocess.run(
                ['ps', 'aux'], 
                capture_output=True, 
                text=True, 
                timeout=10
            )
            
            if result.returncode == 0:
                lines = result.stdout.strip().split('\n')[1:]  # Skip header
                
                for line in lines:
                    if process_name in line:
                        parts = line.split(None, 10)  # Split into max 11 parts
                        if len(parts) >= 11:
                            process = {
                                'user': parts[0],
                                'pid': int(parts[1]),
                                'cpu_percent': float(parts[2]),
                                'memory_percent': float(parts[3]),
                                'vsz': int(parts[4]),  # Virtual memory size
                                'rss': int(parts[5]),  # Resident set size
                                'tty': parts[6],
                                'stat': parts[7],
                                'start': parts[8],
                                'time': parts[9],
                                'command': parts[10]
                            }
                            processes.append(process)
            
            return processes
        
        except Exception as e:
            self.logger.error(f"Failed to get process info for {process_name}: {e}")
            return []
    
    # ------------------------------------------------------------------
    # Storage class detection (M5)
    # ------------------------------------------------------------------
    @staticmethod
    def _sysfs_for_path(path: str) -> Optional[str]:
        """Return the /sys/dev/block entry backing *path*, or None.

        Uses st_dev -> major:minor, which resolves through LVM, mdraid, bind
        mounts and partitions without any string parsing of device names.
        """
        try:
            st = os.stat(path)
            node = f'/sys/dev/block/{os.major(st.st_dev)}:{os.minor(st.st_dev)}'
            return node if os.path.exists(node) else None
        except (OSError, AttributeError):
            return None

    @classmethod
    def _rotational_for_sysfs(cls, node: str, _depth: int = 0) -> Optional[str]:
        """Read 'rotational' for a sysfs block node, following the stack down.

        A partition has no queue/ of its own (its parent disk does), and a
        device-mapper or md device delegates to the physical devices listed
        under slaves/. Both are followed here.
        """
        if node is None or _depth > 4:
            return None
        for candidate in (
            os.path.join(node, 'queue', 'rotational'),           # whole disk
            os.path.join(node, '..', 'queue', 'rotational'),     # partition
        ):
            try:
                with open(candidate) as fh:
                    value = fh.read().strip()
                if value in ('0', '1'):
                    return value
            except (OSError, IOError):
                continue
        # LVM / mdraid: descend into the underlying devices.
        slaves = os.path.join(node, 'slaves')
        try:
            for slave in sorted(os.listdir(slaves)):
                value = cls._rotational_for_sysfs(
                    os.path.join(slaves, slave), _depth + 1
                )
                if value is not None:
                    return value
        except (OSError, IOError):
            pass
        return None

    @staticmethod
    def _device_name_for_sysfs(node: str) -> str:
        """Best-effort device name for a sysfs block node (for nvme detection)."""
        try:
            return os.path.basename(os.path.realpath(node))
        except OSError:
            return ''

    def get_storage_type(self, datadir: str = None) -> str:
        """Detect the storage class backing MySQL's data directory.

        Returns 'nvme', 'ssd', 'hdd' or 'unknown'.

        M5: this used to scan EVERY device in /sys/block and return 'nvme' if
        any non-rotational NVMe existed anywhere on the box. On the very common
        "NVMe boot device + spinning or network-attached data volume" layout it
        therefore recommended innodb_io_capacity=4000, io_capacity_max=8000 and
        flush_neighbors=0 for a ROTATIONAL datadir — the opposite of correct,
        and enough to cause flush storms on a busy HDD. It now resolves the
        actual device behind @@datadir via st_dev, following partitions, LVM and
        mdraid, and only falls back to the whole-system scan when that fails.
        """
        if datadir:
            node = self._sysfs_for_path(datadir)
            rotational = self._rotational_for_sysfs(node)
            if rotational is not None:
                if rotational == '1':
                    self.logger.debug(
                        f"Storage for datadir {datadir}: rotational (hdd)"
                    )
                    return 'hdd'
                name = self._device_name_for_sysfs(node)
                kind = 'nvme' if 'nvme' in name.lower() else 'ssd'
                self.logger.debug(
                    f"Storage for datadir {datadir}: non-rotational ({kind}, {name})"
                )
                return kind
            self.logger.debug(
                f"Could not resolve the device behind datadir {datadir}; "
                f"falling back to a whole-system scan"
            )

        return self._get_storage_type_systemwide()

    def _get_storage_type_systemwide(self) -> str:
        """Fallback: classify from all block devices (pre-M5 behaviour).

        Deliberately CONSERVATIVE in a way the original was not: if any device
        is rotational, report 'hdd'. Without knowing which device holds the
        data, assuming the slowest one avoids recommending flash-only I/O
        settings for a spinning datadir.
        """
        try:
            block_root = '/sys/block'
            if not os.path.isdir(block_root):
                return 'unknown'
            have_nvme = False
            have_ssd = False
            have_hdd = False
            for dev in os.listdir(block_root):
                # Skip virtual / removable pseudo-devices
                if dev.startswith(('loop', 'ram', 'sr', 'fd', 'dm-', 'md')):
                    continue
                rot_path = os.path.join(block_root, dev, 'queue', 'rotational')
                try:
                    with open(rot_path) as fh:
                        rotational = fh.read().strip()
                except (OSError, IOError):
                    continue
                if rotational == '0':
                    if dev.startswith('nvme'):
                        have_nvme = True
                    else:
                        have_ssd = True
                elif rotational == '1':
                    have_hdd = True
            # Slowest-wins: a mixed box may well have the datadir on the HDD.
            if have_hdd:
                return 'hdd'
            if have_nvme:
                return 'nvme'
            if have_ssd:
                return 'ssd'
        except Exception as e:
            self.logger.debug(f"Storage type detection failed: {e}")
        return 'unknown'

    @staticmethod
    def get_free_space_mb(path: str) -> Optional[int]:
        """Free space in MB on the filesystem holding *path* (None if measurement fails).

        Used for the C1 guard: innodb_redo_log_capacity is PREALLOCATED on
        startup, so a value the disk cannot satisfy stops the server from
        starting — and `mysqld --validate-config` will happily approve it,
        because the value itself is legal.
        """
        if not path:
            return None
        try:
            import shutil as _shutil
            probe = path
            while probe and not os.path.exists(probe):
                parent = os.path.dirname(probe)
                if parent == probe:
                    return None
                probe = parent
            if not probe:
                return None
            return int(_shutil.disk_usage(probe).free // (1024 * 1024))
        except (OSError, ValueError, AttributeError):
            return None

    def get_system_metrics(self, datadir: str = None) -> Dict[str, Any]:
        """
        Convenience wrapper that returns a flat dict of all system metrics
        suitable for merging into the main metrics dict used by the analysis
        pipeline.  Keys are prefixed to avoid collisions with MySQL metrics.
        """
        try:
            memory  = self.get_memory_info()
            cpu     = self.get_cpu_info()
            disk    = self.get_disk_info(datadir)
            uptime  = self.get_uptime()
            os_info = self.get_os_info()

            return {
                # Memory
                'system_total_ram_mb':     memory.get('total_mb', 0),
                'system_total_ram_gb':     memory.get('total_gb', 0),
                'system_free_ram_mb':      memory.get('free_mb', 0),
                'system_available_ram_mb': memory.get('available_mb', 0),
                'system_used_ram_mb':      memory.get('used_mb', 0),
                'system_ram_usage_pct':    memory.get('usage_percentage', 0),
                # Swap
                'system_swap_total_mb':    memory.get('raw_data', {}).get('SwapTotal', 0) // 1024,
                'system_swap_free_mb':     memory.get('raw_data', {}).get('SwapFree', 0) // 1024,
                # CPU
                'system_cpu_cores':        cpu.get('cores', 1),
                'system_cpu_model':        cpu.get('model', 'Unknown'),
                'system_load_1min':        cpu.get('1min', 0.0),
                'system_load_5min':        cpu.get('5min', 0.0),
                'system_load_15min':       cpu.get('15min', 0.0),
                # Disk
                'system_disk_total_gb':    disk.get('total_space_gb', 0),
                'system_disk_used_gb':     disk.get('used_space_gb', 0),
                'system_disk_avail_gb':    disk.get('available_space_gb', 0),
                'system_disk_measured_at': disk.get('measured_at', ''),
                # Uptime
                'system_uptime_seconds':   uptime.get('uptime_seconds', 0),
                'system_uptime_days':      uptime.get('uptime_days', 0),
                # OS
                'system_os':               os_info.get('os', 'Unknown'),
                'system_kernel':           os_info.get('kernel', 'Unknown'),
                'system_hostname':         os_info.get('hostname', 'Unknown'),
                # Storage — resolved from MySQL's datadir when known (M5)
                'system_storage_type':     self.get_storage_type(datadir),
                'datadir':                 datadir or '',
                'datadir_free_mb':         (
                    self.get_free_space_mb(datadir) if datadir else None
                ),
            }
        except Exception as e:
            self.logger.error(f"get_system_metrics failed: {e}")
            return {}

    def check_system_resources(self) -> Dict[str, Any]:
        """Check overall system resource status"""
        try:
            memory = self.get_memory_info()
            cpu = self.get_cpu_info()
            disk = self.get_disk_info()
            
            # Calculate resource usage levels
            memory_usage = memory.get('usage_percentage', 0)
            load_1min = cpu.get('1min', 0)
            cpu_cores = cpu.get('cores', 1)
            
            # Determine resource status
            status = {
                'memory': {
                    'usage_percent': memory_usage,
                    'status': 'critical' if memory_usage > 90 else 'warning' if memory_usage > 80 else 'ok',
                    'available_mb': memory.get('available_mb', 0)
                },
                'cpu': {
                    'load_1min': load_1min,
                    'load_per_core': load_1min / cpu_cores if cpu_cores > 0 else 0,
                    'status': 'critical' if load_1min > cpu_cores * 2 else 'warning' if load_1min > cpu_cores else 'ok',
                    'cores': cpu_cores
                },
                'overall_status': 'ok'
            }
            
            # Determine overall status
            if status['memory']['status'] == 'critical' or status['cpu']['status'] == 'critical':
                status['overall_status'] = 'critical'
            elif status['memory']['status'] == 'warning' or status['cpu']['status'] == 'warning':
                status['overall_status'] = 'warning'
            
            return status
        
        except Exception as e:
            self.logger.error(f"Failed to check system resources: {e}")
            return {'overall_status': 'unknown'}

# Utility functions
def get_system_summary() -> Dict[str, Any]:
    """Get a summary of system information"""
    system_info = SystemInfo()
    
    summary = {
        'memory': system_info.get_memory_info(),
        'cpu': system_info.get_cpu_info(),
        'disk': system_info.get_disk_info(),
        'uptime': system_info.get_uptime(),
        'os': system_info.get_os_info(),
        'resources': system_info.check_system_resources()
    }
    
    return summary

def check_mysql_processes() -> List[Dict[str, Any]]:
    """Check for running MySQL processes"""
    system_info = SystemInfo()
    
    mysql_processes = []
    mysql_processes.extend(system_info.get_process_info('mysqld'))
    mysql_processes.extend(system_info.get_process_info('mariadbd'))
    
    return mysql_processes


"""
backend.system_monitor
Lightweight system resource monitor using Linux /proc filesystem.
Zero external dependency (no psutil required).
"""
from __future__ import annotations
import os
import time
from typing import Dict, Any
from backend.models import SystemStatus

class SystemMonitor:
    def __init__(self):
        self.last_cpu_time = 0.0
        self.last_cpu_total = 0.0
        self.last_cpu_idle = 0.0
        self._init_cpu()

    def _init_cpu(self):
        try:
            with open("/proc/stat", "r") as f:
                line = f.readline()
                fields = [float(x) for x in line.strip().split()[1:]]
                self.last_cpu_idle = fields[3] + (fields[4] if len(fields) > 4 else 0.0)
                self.last_cpu_total = sum(fields)
                self.last_cpu_time = time.time()
        except Exception:
            pass

    def get_status(self) -> SystemStatus:
        cpu_percent = 0.0
        mem_percent = 0.0
        mem_used_mb = 0.0
        mem_total_mb = 0.0
        load_1m = 0.0
        load_5m = 0.0
        load_15m = 0.0

        # 1. CPU Usage Calculation
        try:
            with open("/proc/stat", "r") as f:
                line = f.readline()
                fields = [float(x) for x in line.strip().split()[1:]]
                idle = fields[3] + (fields[4] if len(fields) > 4 else 0.0)
                total = sum(fields)

            delta_total = total - self.last_cpu_total
            delta_idle = idle - self.last_cpu_idle

            if delta_total > 0:
                cpu_percent = max(0.0, min(100.0, (1.0 - delta_idle / delta_total) * 100.0))

            self.last_cpu_total = total
            self.last_cpu_idle = idle
            self.last_cpu_time = time.time()
        except Exception:
            pass

        # 2. Memory Usage Calculation
        try:
            meminfo: Dict[str, int] = {}
            with open("/proc/meminfo", "r") as f:
                for line in f:
                    parts = line.split(":")
                    if len(parts) == 2:
                        meminfo[parts[0].strip()] = int(parts[1].strip().split()[0])
            total_kb = meminfo.get("MemTotal", 0)
            avail_kb = meminfo.get("MemAvailable", meminfo.get("MemFree", 0))
            used_kb = max(0, total_kb - avail_kb)

            if total_kb > 0:
                mem_percent = (used_kb / total_kb) * 100.0
                mem_used_mb = used_kb / 1024.0
                mem_total_mb = total_kb / 1024.0
        except Exception:
            pass

        # 3. Load Average
        try:
            with open("/proc/loadavg", "r") as f:
                parts = f.readline().strip().split()
                if len(parts) >= 3:
                    load_1m = float(parts[0])
                    load_5m = float(parts[1])
                    load_15m = float(parts[2])
        except Exception:
            pass

        return SystemStatus(
            cpu_percent=round(cpu_percent, 1),
            mem_percent=round(mem_percent, 1),
            mem_used_mb=round(mem_used_mb, 1),
            mem_total_mb=round(mem_total_mb, 1),
            load_avg=[round(load_1m, 2), round(load_5m, 2), round(load_15m, 2)],
        )

system_monitor = SystemMonitor()

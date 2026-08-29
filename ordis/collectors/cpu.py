"""CPU 采集器：负载 + 使用率（跨平台兼容）。"""

from __future__ import annotations
import psutil
from collectors.base import BaseCollector


class CpuCollector(BaseCollector):
    name = "cpu"

    def collect(self):
        # 整体使用率（非阻塞）
        pct = psutil.cpu_percent(interval=0.1)
        per_cpu = psutil.cpu_percent(interval=0.1, percpu=True)

        # 1/5/15 分钟负载（Unix 有 getloadavg，Windows 用 psutil 模拟）
        try:
            import os
            load_1, load_5, load_15 = os.getloadavg()
        except (AttributeError, OSError):
            # Windows: 用 cpu_percent 近似替代
            load_1 = round(pct / 100 * psutil.cpu_count(), 2)
            load_5 = load_1
            load_15 = load_1

        return {
            "load_1min": round(load_1, 2),
            "load_5min": round(load_5, 2),
            "load_15min": round(load_15, 2),
            "cpu_percent": pct,
            "per_cpu": per_cpu,
            "core_count": len(per_cpu),
        }


collector = CpuCollector()

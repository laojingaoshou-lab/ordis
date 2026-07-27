"""内存采集器：物理内存 + swap。"""

import psutil
from collectors.base import BaseCollector


class MemoryCollector(BaseCollector):
    name = "memory"

    def collect(self):
        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()

        return {
            "total_gb": round(mem.total / (1024**3), 2),
            "used_gb": round(mem.used / (1024**3), 2),
            "available_gb": round(mem.available / (1024**3), 2),
            "percent": mem.percent,
            "swap_total_gb": round(swap.total / (1024**3), 2),
            "swap_used_gb": round(swap.used / (1024**3), 2),
            "swap_percent": swap.percent,
        }


collector = MemoryCollector()

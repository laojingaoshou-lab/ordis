"""磁盘采集器：根分区使用率。"""

import psutil
from collectors.base import BaseCollector


class DiskCollector(BaseCollector):
    name = "disk"

    def collect(self):
        # 只关注根分区 /
        usage = psutil.disk_usage("/")

        return {
            "total_gb": round(usage.total / (1024**3), 2),
            "used_gb": round(usage.used / (1024**3), 2),
            "free_gb": round(usage.free / (1024**3), 2),
            "use_pct": usage.percent,
        }


collector = DiskCollector()

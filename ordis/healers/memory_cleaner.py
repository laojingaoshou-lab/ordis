"""内存清理修复器：清理 PM2 日志 + drop page cache。"""

import subprocess
from healers.base import BaseHealer


class MemoryCleaner(BaseHealer):
    name = "memory_cleaner"

    def heal(self, context):
        actions = []

        # 1. 清理 PM2 日志
        try:
            r = subprocess.run(
                ["pm2", "flush"],
                capture_output=True, text=True, timeout=10,
            )
            ok = r.returncode == 0
            actions.append({
                "action": "pm2 flush logs",
                "success": ok,
                "detail": r.stdout.strip() or r.stderr.strip(),
            })
        except FileNotFoundError:
            actions.append({
                "action": "pm2 flush logs",
                "success": False,
                "detail": "pm2 not found",
            })
        except Exception as e:
            actions.append({
                "action": "pm2 flush logs",
                "success": False,
                "detail": str(e),
            })

        # 2. 释放 page cache / dentries / inodes
        try:
            # 先看当前缓存
            with open("/proc/meminfo") as f:
                before = [l for l in f if "Cached" in l]

            # sync 落盘后 drop
            subprocess.run(["sync"], timeout=10)
            with open("/proc/sys/vm/drop_caches", "w") as f:
                f.write("3\n")

            actions.append({
                "action": "drop page cache + dentries + inodes",
                "success": True,
                "detail": f"before: {'; '.join(b.strip() for b in before)}",
            })
        except Exception as e:
            actions.append({
                "action": "drop caches",
                "success": False,
                "detail": str(e),
            })

        return {
            "success": True,   # 尽力而为，不算失败
            "actions": actions,
        }


healer = MemoryCleaner()

"""磁盘清理修复器：清理旧日志、临时文件、apt 缓存。"""

import subprocess
import shutil
from pathlib import Path
from healers.base import BaseHealer


class DiskCleaner(BaseHealer):
    name = "disk_cleaner"

    # 要清理的目标
    TARGETS = [
        "/tmp",
        "/var/tmp",
        "/var/cache/apt/archives",
        "/var/log/journal",
    ]

    def heal(self, context):
        actions = []

        for target in self.TARGETS:
            p = Path(target)
            if not p.exists():
                continue

            if target == "/var/cache/apt/archives":
                # apt 缓存
                try:
                    r = subprocess.run(
                        ["apt-get", "clean"],
                        capture_output=True, text=True, timeout=30,
                    )
                    actions.append({
                        "action": "apt-get clean",
                        "success": r.returncode == 0,
                        "detail": "cleaned apt package cache",
                    })
                except Exception as e:
                    actions.append({
                        "action": "apt-get clean",
                        "success": False,
                        "detail": str(e),
                    })

            elif target.endswith("journal"):
                # journal 日志：只保留最近 3 天
                try:
                    r = subprocess.run(
                        ["journalctl", "--vacuum-time=3d"],
                        capture_output=True, text=True, timeout=30,
                    )
                    actions.append({
                        "action": "journalctl --vacuum-time=3d",
                        "success": r.returncode == 0,
                        "detail": r.stdout.strip() or r.stderr.strip(),
                    })
                except Exception as e:
                    actions.append({
                        "action": "journalctl vacuum",
                        "success": False,
                        "detail": str(e),
                    })

            elif target in ("/tmp", "/var/tmp"):
                # 清理 7 天前的临时文件
                try:
                    count = 0
                    for f in p.rglob("*"):
                        if f.is_file():
                            try:
                                age = f.stat().st_mtime
                                import time
                                if time.time() - age > 7 * 86400:
                                    f.unlink()
                                    count += 1
                            except Exception:
                                pass
                    actions.append({
                        "action": f"clean old files in {target} (>7d)",
                        "success": True,
                        "detail": f"removed {count} files",
                    })
                except Exception as e:
                    actions.append({
                        "action": f"clean {target}",
                        "success": False,
                        "detail": str(e),
                    })

        return {
            "success": True,
            "actions": actions,
        }


healer = DiskCleaner()

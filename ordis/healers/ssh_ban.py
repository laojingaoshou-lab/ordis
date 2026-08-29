"""SSH 封禁修复器：将爆破 IP 加入 ufw deny。"""

from __future__ import annotations
import subprocess
from healers.base import BaseHealer


class SshBan(BaseHealer):
    name = "ssh_ban"

    def heal(self, context):
        offenders = context.get("value", {}).get("offenders", {})
        actions = []

        for ip, count in offenders.items():
            if count < 5:
                continue

            # 检查是否已经被封
            try:
                r = subprocess.run(
                    ["ufw", "status"],
                    capture_output=True, text=True, timeout=10,
                )
                already_banned = ip in r.stdout
            except Exception:
                already_banned = False

            if already_banned:
                actions.append({
                    "action": f"ufw deny from {ip}",
                    "success": True,
                    "detail": f"already banned ({count} failed attempts)",
                })
                continue

            try:
                r = subprocess.run(
                    ["ufw", "deny", "from", ip, "to", "any"],
                    capture_output=True, text=True, timeout=15,
                )
                ok = r.returncode == 0
                actions.append({
                    "action": f"ufw deny from {ip}",
                    "success": ok,
                    "detail": f"banned after {count} failed attempts. {r.stdout.strip() or r.stderr.strip()}",
                })
            except Exception as e:
                actions.append({
                    "action": f"ufw deny from {ip}",
                    "success": False,
                    "detail": str(e),
                })

        if not actions:
            actions.append({
                "action": "no IP to ban",
                "success": True,
                "detail": "no offenders",
            })

        return {
            "success": True,
            "actions": actions,
        }


healer = SshBan()

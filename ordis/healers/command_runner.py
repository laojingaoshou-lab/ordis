"""
命令运行器修复器：执行审批晋升产生的自定义修复命令。

只接受 rules.yaml 中 rule.params.command 的显式配置
（由 `ordis promote apply` 人工审批写入），不自动执行任何 AI 建议。

强制回检：必须配置 check_command 或 check_port，防止盲目执行。
"""

from __future__ import annotations

import subprocess
import time

from healers.base import BaseHealer


def _check_port(port: int, timeout: float = 2.0) -> bool:
    import socket
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect(("127.0.0.1", int(port)))
        s.close()
        return True
    except OSError:
        return False


class CommandRunner(BaseHealer):
    name = "command_runner"

    def heal(self, context):
        params = context.get("params") or {}
        command = (params.get("command") or "").strip()
        check_cmd = (params.get("check_command") or "").strip()
        check_port = params.get("check_port")

        if not command:
            return {"success": False,
                    "actions": [{"error": "未配置 params.command"}]}

        # 强制要求回检方式（check_command 或 check_port 至少一个）
        if not check_cmd and not check_port:
            return {"success": False,
                    "actions": [{"error": "未配置回检方式（check_command 或 check_port 必须至少一个）"}]}

        try:
            r = subprocess.run(command, shell=True, capture_output=True,
                               text=True, timeout=60)
            detail = (r.stdout or "").strip() or (r.stderr or "").strip()
            rc = r.returncode
        except subprocess.TimeoutExpired:
            return {"success": False,
                    "actions": [{"command": command,
                                 "error": "命令超时(60s)"}]}
        except Exception as e:
            return {"success": False,
                    "actions": [{"command": command, "error": str(e)}]}

        if rc != 0:
            return {"success": False,
                    "actions": [{"command": command, "rc": rc,
                                 "detail": detail[:300]}]}

        # 回检阶段
        check_ok = True
        check_detail = ""

        # check_command 回检
        if check_cmd:
            try:
                check_r = subprocess.run(check_cmd, shell=True,
                                        capture_output=True, text=True,
                                        timeout=75)
                if check_r.returncode != 0:
                    check_ok = False
                    check_detail = f"check_command 失败 rc={check_r.returncode}: {check_r.stderr[:200]}"
            except Exception as e:
                check_ok = False
                check_detail = f"check_command 异常: {e}"

        # check_port 回检
        if check_ok and check_port:
            time.sleep(2)
            if not _check_port(check_port):
                check_ok = False
                check_detail = (check_detail + "; " if check_detail else "") + \
                    f"端口 {check_port} 未恢复"

        if not check_ok:
            return {"success": False,
                    "actions": [{"command": command, "rc": rc,
                                 "detail": detail[:200],
                                 "check_failed": check_detail}]}

        return {
            "success": True,
            "actions": [{
                "command": command,
                "rc": rc,
                "detail": (detail or "(无输出)")[:300],
                "check_ok": True,
            }],
        }


healer = CommandRunner()

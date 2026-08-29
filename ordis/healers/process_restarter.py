"""进程重启修复器：PM2 + systemd + Docker，按纳管清单执行，含端口回检。

服务来源（优先级）：
1. rules.yaml 的 process.adopted —— `ordis discover --adopt` 写入的显式纳管
2. PORT_APP_MAP —— 历史预置映射（视为已纳管，保持老服务器行为不变）
未纳管的端口死掉时只报告不动作（needs_ai=True 走 AI 诊断）。
"""

from __future__ import annotations
import subprocess, socket, time
from healers.base import BaseHealer
from config import load_rules

# 历史预置映射（生产服务器已验证的服务），视为已纳管
PORT_APP_MAP = {
    80: "systemd:nginx",
    3000: "pm2:photography",
    3001: "pm2:haigui",
    3002: "pm2:claude-chat",
}

RESTART_TIMEOUT = {"systemd": 30, "docker": 30, "pm2": 15}


def check_port(port, timeout=2):
    s = socket.socket(); s.settimeout(timeout)
    try:
        s.connect(('127.0.0.1', port)); s.close()
        return True
    except:
        return False


def _load_adopted() -> dict[int, str]:
    """端口 → 'manager:target'。合并预置映射与 rules.yaml 纳管清单。"""
    port_map = {p: m for p, m in PORT_APP_MAP.items()}
    try:
        adopted = (load_rules().get("process", {}) or {}).get("adopted", []) or []
    except Exception:
        adopted = []
    for a in adopted:
        manager, target, ports = a.get("manager"), a.get("target"), a.get("ports", [])
        if not manager or not target or not ports:
            continue
        key = f"{manager}:{target}"
        for p in ports:
            port_map.setdefault(int(p), key)
    return port_map


def _discover_service_ports() -> dict[str, list[int]]:
    """实时发现：'manager:target' → [port]，用于纳管后无端口映射的兜底定位。"""
    try:
        from collectors.discovery import collector as disco
        return {f"{s['manager']}:{s['target']}": s["ports"]
                for s in disco.collect().get("services", [])}
    except Exception:
        return {}


class ProcessRestarter(BaseHealer):
    name = "process_restarter"

    def _restart(self, manager: str, target: str):
        """执行对应管理器的重启命令，返回 (命令描述, 输出)。抛异常由上层接。"""
        timeout = RESTART_TIMEOUT.get(manager, 30)
        if manager == "systemd":
            cmd = ["systemctl", "restart", target]
            action = f"systemctl restart {target}"
        elif manager == "docker":
            cmd = ["docker", "restart", target]
            action = f"docker restart {target}"
        else:
            cmd = ["pm2", "restart", target]
            action = f"pm2 restart {target}"
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = r.stdout.strip() or r.stderr.strip()
        return action, out, r.returncode == 0

    def _service_exists(self, service_key: str) -> bool:
        """预置映射服务在本机是否存在（systemd unit / pm2 / docker 容器）。"""
        manager, _, target = service_key.partition(":")
        try:
            if manager == "systemd":
                r = subprocess.run(["systemctl", "status", target],
                                   capture_output=True, timeout=8)
                # 0=running 3=stopped但存在 均算存在；4=unit 不存在
                return r.returncode in (0, 3)
            if manager == "docker":
                r = subprocess.run(["docker", "inspect", target],
                                   capture_output=True, timeout=8)
                return r.returncode == 0
            if manager == "pm2":
                r = subprocess.run(["pm2", "describe", target],
                                   capture_output=True, timeout=10)
                return r.returncode == 0
        except Exception:
            return False
        return False

    def heal(self, context):
        ports = context.get("value", {}).get("ports", {})
        results = []

        # 预置映射里的服务在本机可能根本不存在（如容器里没有 nginx/pm2），
        # 先用实时发现校验：服务真实存在才尝试修复
        discovered = _discover_service_ports()

        for port_str, alive in ports.items():
            port = int(port_str)
            if alive:
                continue

            service_key = _load_adopted().get(port)
            # 兜底：端口没配映射但服务已被纳管 → 用实时发现找它的 key
            if not service_key:
                for key, plist in discovered.items():
                    if port in plist and not key.startswith("manual:"):
                        service_key = key
                        break

            detail = ""
            success = False
            action = ""

            if not service_key:
                action = f"no managed service for port {port}"
                detail = ("unmanaged: run `ordis discover --adopt <name>` "
                          "to enable auto-heal")
            elif f"{service_key}" not in discovered and \
                    not self._service_exists(service_key):
                # 预置映射的服务本机不存在（如容器内无 systemd/pm2）→ 跳过不误报
                action = f"skip: {service_key} (port {port})"
                detail = "service not present on this host, skip"
                results.append({
                    "port": port,
                    "success": False,
                    "action": action,
                    "detail": detail,
                    "needs_ai": False,
                    "service": service_key,
                    "skipped": True,
                })
                continue
            else:
                manager, target = service_key.split(":", 1)
                try:
                    action, detail, restarted = self._restart(manager, target)
                    action += f" (port {port})"
                    if not restarted:
                        detail = detail or f"{manager} restart failed"
                    else:
                        time.sleep(2 if manager != "systemd" else 1)
                        alive_after = check_port(port)
                        success = alive_after
                        if not alive_after:
                            detail = f"{manager} restarted but port still DEAD"
                except FileNotFoundError:
                    detail = f"{manager} CLI not found"
                except Exception as e:
                    detail = str(e)

            results.append({
                "port": port,
                "success": success,
                "action": action,
                "detail": detail,
                "needs_ai": not success,
                "service": service_key or str(port),
            })

        attempted = [r for r in results if not r.get("skipped")]
        return {
            "success": (all(r["success"] for r in attempted)
                        if attempted else False),
            "applicable": bool(attempted),
            "skipped": bool(results) and not attempted,
            "actions": results,
        }

healer = ProcessRestarter()

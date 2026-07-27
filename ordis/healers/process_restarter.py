"""进程重启修复器：PM2 + systemd + 端口回检。"""

import subprocess, socket, time
from healers.base import BaseHealer

PORT_APP_MAP = {
    80: "systemd:nginx",
    3000: "photography",
    3001: "haigui",
    3002: "claude-chat",
}

def check_port(port, timeout=2):
    s = socket.socket(); s.settimeout(timeout)
    try:
        s.connect(('127.0.0.1', port)); s.close()
        return True
    except:
        return False


class ProcessRestarter(BaseHealer):
    name = "process_restarter"

    def heal(self, context):
        ports = context.get("value", {}).get("ports", {})
        results = []

        for port_str, alive in ports.items():
            port = int(port_str)
            if alive:
                continue
            app_name = PORT_APP_MAP.get(port)
            detail = ""
            success = False

            if not app_name:
                action = f"no app mapping for port {port}"
                detail = "no healer action"
            elif app_name.startswith("systemd:"):
                svc = app_name.split(":", 1)[1]
                try:
                    r = subprocess.run(["systemctl", "restart", svc], capture_output=True, text=True, timeout=30)
                    time.sleep(1)
                    alive_after = check_port(port)
                    success = alive_after
                    detail = r.stdout.strip() or r.stderr.strip() or ("restarted and port OK" if success else "restarted but port still DEAD")
                except Exception as e:
                    detail = str(e)
                action = f"systemctl restart {svc} (port {port})"
            else:
                try:
                    r = subprocess.run(["pm2", "restart", app_name], capture_output=True, text=True, timeout=15)
                    time.sleep(2)
                    alive_after = check_port(port)
                    success = alive_after
                    detail = r.stdout.strip() or r.stderr.strip()
                    if not alive_after:
                        detail = "PM2 restarted but port still not responding (code may be broken)"
                except FileNotFoundError:
                    detail = "pm2 not found"
                except Exception as e:
                    detail = str(e)
                action = f"pm2 restart {app_name} (port {port})"

            results.append({
                "port": port,
                "success": success,
                "action": action,
                "detail": detail,
                "needs_claude": not success,
                "service": app_name.replace("systemd:","") if app_name else str(port),
            })

        return {
            "success": all(r["success"] for r in results) if results else False,
            "actions": results,
        }

healer = ProcessRestarter()

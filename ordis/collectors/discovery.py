"""
服务自动发现采集器：扫描本机监听端口，归类到对应进程管理器。

归类优先级：pm2 > systemd(cgroup 反查) > docker(端口映射) > manual(仅观察)
发现结果不直接生效；通过 `ordis discover --adopt <name>` 纳管后才进入自愈。
"""

from __future__ import annotations  # 兼容 Python 3.7+（老系统 VM 实测需要）

import json
import re
import subprocess
from pathlib import Path
from collectors.base import BaseCollector
from config import load_rules


def _run(cmd: list[str], timeout: int = 10) -> str | None:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else None
    except Exception:
        return None


class DiscoveryCollector(BaseCollector):
    name = "discovery"

    def collect(self) -> dict:
        listeners = self._scan_listeners()
        pm2_pids = self._scan_pm2()
        docker_ports = self._scan_docker()

        services: dict[str, dict] = {}
        for port, pid, proc in listeners:
            svc = self._classify(port, pid, proc, pm2_pids, docker_ports)
            key = f"{svc['manager']}:{svc['target']}"
            if key in services:
                services[key]["ports"].append(port)
            else:
                services[key] = svc

        result = sorted(services.values(),
                        key=lambda s: (s["manager"], s["target"]))
        return {"services": result, "count": len(result)}

    # ── 扫描层 ─────────────────────────────────────────────────
    def _scan_listeners(self) -> list[tuple[int, int, str]]:
        """ss -tlpn → [(port, pid, 进程名)]。无权限看 pid 时 pid=0。"""
        out = _run(["ss", "-tlpn"])
        if not out:
            return []
        found = []
        for line in out.splitlines()[1:]:
            addr = line.split()
            if len(addr) < 4 or ":" not in addr[3]:
                continue
            try:
                port = int(addr[3].rsplit(":", 1)[1])
            except ValueError:
                continue
            m = re.search(r'users:\(\("([^"]+)",pid=(\d+)', line)
            pid, proc = (int(m.group(2)), m.group(1)) if m else (0, "unknown")
            found.append((port, pid, proc))
        return found

    def _scan_pm2(self) -> set[int]:
        """pm2 jlist → 受管进程 pid 集合。"""
        out = _run(["pm2", "jlist"])
        if not out:
            return set()
        try:
            apps = json.loads(out)
            return {int(a.get("pid") or 0) for a in apps} - {0}
        except Exception:
            return set()

    def _scan_docker(self) -> dict[int, str]:
        """docker ps → {宿主机端口: 容器名}。没装 docker 返回空。"""
        out = _run(["docker", "ps", "--format",
                    "{{.Names}}\t{{.Ports}}"])
        if not out:
            return {}
        mapping = {}
        for line in out.strip().splitlines():
            parts = line.split("\t")
            if len(parts) != 2:
                continue
            name, ports = parts
            # 0.0.0.0:8080->80/tcp, :::8080->80/tcp
            for m in re.finditer(r":(?:\d+)->(\d+)/tcp", ports):
                host_port = re.search(r":(\d+)->", m.group(0))
                if host_port:
                    mapping[int(host_port.group(1))] = name
        return mapping

    # ── 归类层 ─────────────────────────────────────────────────
    def _systemd_unit(self, pid: int) -> str | None:
        """/proc/<pid>/cgroup 反查 systemd unit 名。"""
        if not pid:
            return None
        try:
            text = Path(f"/proc/{pid}/cgroup").read_text()
        except OSError:
            return None
        for line in text.splitlines():
            unit = line.rsplit("/", 1)[-1]
            if unit.endswith(".service"):
                return unit[:-len(".service")]
        return None

    def _classify(self, port: int, pid: int, proc: str,
                  pm2_pids: set[int], docker_ports: dict[int, str]) -> dict:
        adopted = self._load_adopted_keys()
        if pid in pm2_pids:
            manager, target = "pm2", proc
        else:
            unit = self._systemd_unit(pid)
            if unit and unit not in ("user", "init"):
                manager, target = "systemd", unit
            elif port in docker_ports:
                manager, target = "docker", docker_ports[port]
            else:
                manager, target = "manual", proc

        return {
            "manager": manager,
            "target": target,
            "ports": [port],
            "pid": pid,
            "auto_heal": manager != "manual"
                         and f"{manager}:{target}" in adopted,
        }

    # ── 纳管状态 ────────────────────────────────────────────────
    @staticmethod
    def _load_adopted_keys() -> set[str]:
        proc_cfg = load_rules().get("process", {}) or {}
        adopted = proc_cfg.get("adopted", []) or []
        return {f"{a['manager']}:{a['target']}" for a in adopted}


collector = DiscoveryCollector()

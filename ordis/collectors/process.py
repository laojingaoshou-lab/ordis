"""进程/端口采集器：检查配置的进程是否存活（端口是否在监听）。"""

from __future__ import annotations
import socket
from collectors.base import BaseCollector
from config import load_rules


class ProcessCollector(BaseCollector):
    name = "process"

    def __init__(self):
        # 从规则中读取需要监控的端口
        self._watch_ports: list[int] = []

    def collect(self):
        return self._check_ports()

    def _load_watch_ports(self):
        """从 rules.yaml 的 process.watch_ports 读取端口列表。"""
        rules = load_rules()
        proc_rules = rules.get("process", {})
        self._watch_ports = proc_rules.get("watch_ports") or []

    def _check_port(self, port: int) -> bool:
        """检测本机某个端口是否有进程在监听。"""
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=2)
            sock.close()
            return True
        except (ConnectionRefusedError, OSError):
            return False

    def _check_ports(self):
        self._load_watch_ports()
        if not self._watch_ports:
            # 没配置就返回健康，不误报
            return {"alive": True, "ports": {}}

        status = {}
        for port in self._watch_ports:
            status[str(port)] = self._check_port(port)

        all_alive = all(status.values())
        return {
            "alive": all_alive,
            "ports": status,
        }


collector = ProcessCollector()

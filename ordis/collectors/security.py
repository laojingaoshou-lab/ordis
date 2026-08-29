"""安全采集器：增量检测 SSH 失败登录（用 journal cursor 去重，不重复计数）。"""

from __future__ import annotations
import json
import subprocess
from pathlib import Path
from collections import defaultdict
from collectors.base import BaseCollector

CURSOR_FILE = Path(__file__).parent.parent / "logs" / ".security_cursor"


class SecurityCollector(BaseCollector):
    name = "security"

    FAIL_PATTERNS = ["Failed password", "Connection closed by authenticating user"]

    def collect(self):
        return self._scan_journal_incremental()

    def _scan_journal_incremental(self):
        """
        用 journalctl cursor 做增量读取：只统计上次读到之后的新记录。
        首次运行没有 cursor 时，跳过历史，从此刻开始监听。
        """
        # 读上次的 cursor
        last_cursor = ""
        if CURSOR_FILE.exists():
            try:
                last_cursor = CURSOR_FILE.read_text().strip()
            except Exception:
                last_cursor = ""

        # 构建 journalctl 命令
        cmd = ["journalctl", "-u", "ssh", "--no-pager", "-q", "-o", "json"]
        if last_cursor:
            cmd += ["--after-cursor", last_cursor]
        else:
            # 首次运行：只看最近 2 分钟，不反查全部历史
            cmd += ["--since", "2 minutes ago"]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        except Exception:
            return {"failed_attempts": 0, "offenders": {}}

        lines = result.stdout.strip()
        if not lines:
            return {"failed_attempts": 0, "offenders": {}}

        # 解析 json lines，提取失败记录
        offenders: dict[str, int] = defaultdict(int)
        total = 0
        new_cursor = last_cursor

        for line in lines.splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            # 更新 cursor（最后一条的 cursor 会覆盖前面的）
            cursor = entry.get("__CURSOR", "")
            if cursor:
                new_cursor = cursor

            msg = entry.get("MESSAGE", "")
            matched = any(p in msg for p in self.FAIL_PATTERNS)
            if not matched:
                continue

            total += 1
            # 从消息中提取 IP
            parts = msg.split()
            for i, p in enumerate(parts):
                if p == "from" and i + 1 < len(parts):
                    ip = parts[i + 1]
                    offenders[ip] += 1
                    break

        # 持久化新的 cursor
        if new_cursor and new_cursor != last_cursor:
            try:
                CURSOR_FILE.parent.mkdir(parents=True, exist_ok=True)
                CURSOR_FILE.write_text(new_cursor)
            except Exception:
                pass

        return {
            "failed_attempts": total,
            "offenders": dict(offenders),
        }


collector = SecurityCollector()

"""磁盘清理自愈器：只清理明确白名单目录中的过期普通文件。"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from healers.base import BaseHealer

log = logging.getLogger(__name__)

_FORBIDDEN_DIRS = {"/", "/home", "/root", "/usr", "/etc", "/opt", "/var", "/tmp"}
_DEFAULT_DIRS = ["/var/log/ordis", "/tmp/ordis-cache"]


class DiskCleaner(BaseHealer):
    name = "disk_cleaner"

    def heal(self, context):
        params = context.get("params") or {}
        dirs = params.get("dirs", _DEFAULT_DIRS)
        exclude = params.get("exclude_patterns", [])

        if not isinstance(dirs, list) or not dirs:
            return {"success": False, "actions": [{"error": "dirs 必须是非空目录列表"}]}
        if not isinstance(exclude, list):
            return {"success": False, "actions": [{"error": "exclude_patterns 必须是列表"}]}

        try:
            days = int(params.get("days", 7))
        except (TypeError, ValueError):
            return {"success": False, "actions": [{"error": "days 必须是非负整数"}]}
        if days < 0:
            return {"success": False, "actions": [{"error": "days 必须是非负整数"}]}

        bases = []
        for item in dirs:
            if not isinstance(item, str) or not item.strip():
                return {"success": False, "actions": [{"error": "dirs 含无效目录"}]}
            raw = item.strip().replace("\\", "/").rstrip("/") or "/"
            if raw in _FORBIDDEN_DIRS:
                return {"success": False,
                        "actions": [{"error": f"危险目录 {item} 在黑名单中，拒绝清理"}]}
            base = Path(item).resolve()
            bases.append(base)

        cutoff = time.time() - days * 86400
        deleted = 0
        errors = []
        scanned = 0

        for base in bases:
            if not base.is_dir():
                continue
            for root, _, files in os.walk(base, followlinks=False):
                for filename in files:
                    path = Path(root) / filename
                    if path.is_symlink() or any(path.match(pattern) for pattern in exclude):
                        continue
                    try:
                        scanned += 1
                        if path.stat().st_mtime < cutoff:
                            path.unlink()
                            deleted += 1
                    except OSError as e:
                        errors.append(f"{path}: {e}")

        action = {"dirs": [str(base) for base in bases], "scanned": scanned,
                  "deleted": deleted}
        if errors:
            action["errors"] = errors[:10]
        return {"success": deleted > 0, "actions": [action]}


healer = DiskCleaner()

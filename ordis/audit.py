"""权限敏感操作审计。"""
from __future__ import annotations


def log_audit(action: str, details: dict | str | None = None):
    """将审计记录写入 SQLite。"""
    import db

    if not isinstance(details, dict):
        details = {"value": details} if details is not None else {}
    db.save_audit(action, details)

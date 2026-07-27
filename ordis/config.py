"""
配置管理：从 YAML 文件加载规则和通知配置。
"""

from pathlib import Path
from typing import Any

import yaml

CFG_DIR = Path(__file__).parent


def load_rules(path: str | None = None) -> dict[str, Any]:
    """
    加载 rules.yaml，返回字典。
    找不到文件时返回空配置。
    """
    file = Path(path) if path else CFG_DIR / "rules.yaml"
    if not file.exists():
        return {}
    with open(file, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save_rules(rules: dict, path: str | None = None) -> None:
    """持久化规则到 YAML 文件。"""
    file = Path(path) if path else CFG_DIR / "rules.yaml"
    with open(file, "w", encoding="utf-8") as f:
        yaml.safe_dump(rules, f, allow_unicode=True, default_flow_style=False)

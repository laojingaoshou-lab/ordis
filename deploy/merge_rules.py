"""Merge an existing Ordis rules file over newly installed defaults."""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml


def merge(defaults, existing):
    if isinstance(defaults, dict) and isinstance(existing, dict):
        result = dict(defaults)
        for key, value in existing.items():
            result[key] = merge(result.get(key), value)
        return result
    return existing


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("existing", type=Path)
    parser.add_argument("installed", type=Path)
    args = parser.parse_args()
    defaults = yaml.safe_load(args.installed.read_text(encoding="utf-8")) or {}
    existing = yaml.safe_load(args.existing.read_text(encoding="utf-8")) or {}
    merged = merge(defaults, existing)
    args.installed.write_text(
        yaml.safe_dump(merged, allow_unicode=True, sort_keys=False),
        encoding="utf-8")


if __name__ == "__main__":
    main()

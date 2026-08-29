"""
统一日志模块：同时输出到控制台和文件，支持按天轮转。
"""

from __future__ import annotations
import logging
import sys
from pathlib import Path
from logging.handlers import TimedRotatingFileHandler

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

_fmt = logging.Formatter(
    "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def get_logger(name: str, level: int = logging.DEBUG) -> logging.Logger:
    logger = logging.getLogger(name)

    if logger.handlers:
        return logger

    logger.setLevel(level)

    # 控制台
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.DEBUG)
    sh.setFormatter(_fmt)
    logger.addHandler(sh)

    # 文件（每天轮转，保留 30 天）
    fh = TimedRotatingFileHandler(
        LOG_DIR / "guardian.log", when="midnight", backupCount=30, encoding="utf-8"
    )
    fh.setLevel(logging.INFO)
    fh.setFormatter(_fmt)
    logger.addHandler(fh)

    return logger

"""
采集器基类：所有采集器继承此类，实现 collect() 方法。
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any


class BaseCollector(ABC):
    name: str = "base"

    @abstractmethod
    def collect(self) -> dict[str, Any]:
        """采集数据，返回字典。"""
        ...

    def __repr__(self):
        return f"<Collector:{self.name}>"

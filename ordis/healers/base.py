"""修复器基类。"""

from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any


class BaseHealer(ABC):
    name: str = "base"

    @abstractmethod
    def heal(self, context: dict[str, Any]) -> dict[str, Any]:
        """
        执行修复动作。
        context: {rule_name, collector_name, value, threshold, ...}
        返回: {success: bool, action: str, detail: str}
        """
        ...

    def __repr__(self):
        return f"<Healer:{self.name}>"

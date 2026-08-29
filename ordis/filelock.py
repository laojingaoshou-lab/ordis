"""
跨进程文件锁：防止 rules.yaml/events.json 等多进程并发写冲突。

使用 fcntl (Linux) 或 msvcrt (Windows) 实现文件锁。
原子写模式：tmp + replace() 保证单次写入完整性；锁保证多进程串行。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Callable

# 平台检测
if sys.platform == "win32":
    import msvcrt
    LOCK_IMPL = "msvcrt"
else:
    import fcntl
    LOCK_IMPL = "fcntl"


class FileLock:
    """
    跨进程文件锁上下文管理器。

    用法:
        with FileLock(path):
            # 独占操作
            data = path.read_text()
            path.write_text(new_data)
    """

    def __init__(self, path: Path | str, timeout: float = 5.0):
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.timeout = timeout
        self.fd = None

    def __enter__(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = open(self.lock_path, "w")
        deadline = time.time() + self.timeout

        while True:
            try:
                if LOCK_IMPL == "fcntl":
                    fcntl.flock(self.fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:  # msvcrt
                    msvcrt.locking(self.fd.fileno(), msvcrt.LK_NBLCK, 1)
                return self
            except (IOError, OSError):
                if time.time() >= deadline:
                    self.fd.close()
                    raise TimeoutError(f"获取文件锁超时: {self.lock_path}")
                time.sleep(0.05)

    def __exit__(self, *_):
        if self.fd:
            try:
                if LOCK_IMPL == "fcntl":
                    fcntl.flock(self.fd.fileno(), fcntl.LOCK_UN)
                else:
                    msvcrt.locking(self.fd.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception:
                pass
            finally:
                self.fd.close()


def locked_write_text(path: Path | str, content: str, encoding: str = "utf-8"):
    """
    原子写 + 文件锁：tmp.write → tmp.replace(target)，全程持锁。

    用于 rules.yaml/events.json/skills.json/drafts.json 等多进程共享文件。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with FileLock(path):
        tmp = path.with_suffix(".tmp")
        tmp.write_text(content, encoding=encoding)
        tmp.replace(path)


def locked_update(path: Path | str, update_fn: Callable[[str], str],
                  encoding: str = "utf-8", default: str = ""):
    """
    锁定读-修改-写：持锁读取 → 回调修改 → 原子写回。

    参数:
    - path: 文件路径
    - update_fn: (old_content) -> new_content
    - default: 文件不存在时的初始内容
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with FileLock(path):
        if path.exists():
            old = path.read_text(encoding=encoding)
        else:
            old = default
        new = update_fn(old)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(new, encoding=encoding)
        tmp.replace(path)


def exclusive(path: Path | str, timeout: float = 5.0):
    """
    别名：返回 FileLock 上下文管理器（兼容现有代码 `with exclusive(file):`）。
    """
    return FileLock(path, timeout=timeout)

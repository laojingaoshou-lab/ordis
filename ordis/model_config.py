"""
AI 供应商配置管理：多供应商存储 + 一键切换。

配置文件位于 ~/.ordis/model.json（脱离仓库目录，避免误提交）。
格式：
{
  "active": "provider",
  "providers": {
    "provider": {"base_url": "...", "model": "...", "api_key": "...",
                    "added_at": "2026-08-24"}
  }
}
"""

from __future__ import annotations
import getpass
import json
import os
import time
from pathlib import Path

try:
    from logger import get_logger
    log = get_logger("model_config")
except ImportError:
    import logging
    log = logging.getLogger("model_config")

try:
    from paths import data_home
    CONFIG_PATH = data_home() / "model.json"
except ImportError:
    CONFIG_PATH = Path.home() / ".ordis" / "model.json"

DEFAULT_BASE_URL = "https://api.openai.com/v1"


# ── 存取 ───────────────────────────────────────────────────────
def load(path: Path | None = None) -> dict:
    file = path or CONFIG_PATH
    if not file.exists():
        return {"active": None, "providers": {}}
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
        data.setdefault("providers", {})
        data.setdefault("active", None)
        return data
    except Exception as e:
        log.warning("模型配置读取失败 (%s): %s，按空配置处理", file, e)
        return {"active": None, "providers": {}}


def save(data: dict, path: Path | None = None):
    from filelock import locked_write_text
    file = path or CONFIG_PATH
    file.parent.mkdir(parents=True, exist_ok=True)
    locked_write_text(file, json.dumps(data, ensure_ascii=False, indent=2))
    try:  # POSIX 收紧权限；Windows 上基本是 no-op
        os.chmod(file, 0o600)
    except OSError:
        pass


def add_provider(name: str, base_url: str, model: str, api_key: str,
                 path: Path | None = None) -> None:
    """新增或覆盖供应商，并设为激活。"""
    data = load(path)
    data["providers"][name] = {
        "base_url": base_url.strip().rstrip("/"),
        "model": model,
        "api_key": api_key,
        "added_at": time.strftime("%Y-%m-%d"),
    }
    data["active"] = name
    save(data, path)


def set_active(name: str, path: Path | None = None) -> bool:
    data = load(path)
    if name not in data["providers"]:
        return False
    data["active"] = name
    save(data, path)
    return True


def remove_provider(name: str, path: Path | None = None) -> bool:
    data = load(path)
    if name not in data["providers"]:
        return False
    del data["providers"][name]
    if data["active"] == name:
        data["active"] = next(iter(data["providers"]), None)
    save(data, path)
    return True


def load_active(path: Path | None = None) -> dict | None:
    """返回激活供应商的完整配置；无激活时返回 None。"""
    data = load(path)
    name = data.get("active")
    if name and name in data["providers"]:
        cfg = dict(data["providers"][name])
        cfg["name"] = name
        return cfg
    return None


def mask_key(key: str) -> str:
    if not key:
        return "(空)"
    if len(key) <= 12:
        return "***"
    return f"{key[:7]}...{key[-4:]}"


# ── 连通性测试 ──────────────────────────────────────────────────
def test_provider(cfg: dict) -> tuple[str, float]:
    """对给定供应商配置做一次最小真实调用，返回 (响应文本, 耗时秒)。失败抛异常。"""
    from ai_diagnose import _raw_chat
    t0 = time.time()
    text = _raw_chat(
        "连通性测试：请只回复两个字：正常",
        base=cfg["base_url"],
        model=cfg["model"], api_key=cfg["api_key"],
    )
    return text, round(time.time() - t0, 1)


# ── 交互式向导（ordis model 入口）───────────────────────────────
def interactive_wizard(path: Path | None = None):
    data = load(path)
    provs = data["providers"]

    if provs:
        print("已配置的供应商:")
        names = list(provs)
        for i, n in enumerate(names, 1):
            mark = "  ← 当前激活" if n == data.get("active") else ""
            print(f"  [{i}] {n}: {provs[n]['model']}{mark}")
        choice = input("输入序号切换激活，直接回车=添加新供应商: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(provs):
            name = names[int(choice) - 1]
            set_active(name, path)
            print(f"已切换激活: {name} / {provs[name]['model']}")
            return

    print("── 添加新供应商 " + "─" * 30)
    name = ""
    while not name:
        name = input("供应商名称: ").strip()

    base = input(f"API Base URL [{DEFAULT_BASE_URL}]: ").strip() or DEFAULT_BASE_URL
    model = ""
    while not model:
        model = input("模型名称 (如 gpt-4o-mini): ").strip()
    key = ""
    while not key:
        key = getpass.getpass("API Key (输入不回显): ").strip()

    add_provider(name, base, model, key, path)
    print(f"→ 正在测试 {base} / {model} ...")
    try:
        text, elapsed = test_provider(load_active(path))
        print(f"[OK] 连通正常 ({elapsed}s)，响应: {text.strip()[:80]}")
        print(f"已保存并激活: {name} / {model}")
    except Exception as e:
        remove_provider(name, path)
        print(f"[FAIL] 测试失败，未保存（请检查 Key/网络）: {e}")
        raise SystemExit(1)

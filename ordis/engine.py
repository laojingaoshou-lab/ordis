"""规则引擎：加载规则 → 运行采集器 → 评估条件 → 触发修复 + 通知。"""

import importlib
import json
import time
from pathlib import Path
from datetime import datetime
from logger import get_logger
from config import load_rules
from notifier import dingtalk_send, wechat_send, email_send

log = get_logger("engine")

# 已触发的冷却记录: {rule_name: last_trigger_time}
_cooldowns: dict[str, float] = {}

# 事件持久化文件（CLI 和 daemon 共享数据）
EVENTS_FILE = Path(__file__).parent / "logs" / "events.json"

# 内存缓存（减少 IO）
_events_cache: list[dict] | None = None


def _load_collector(name: str):
    """动态导入采集器模块，返回 collector 实例。"""
    try:
        mod = importlib.import_module(f"collectors.{name}")
        return mod.collector
    except ModuleNotFoundError:
        log.error("采集器 '%s' 未找到", name)
        return None
    except AttributeError:
        log.error("采集器模块 '%s' 缺少 'collector' 实例", name)
        return None


def _load_healer(name: str):
    """动态导入修复器模块，返回 healer 实例。"""
    if not name:
        return None
    try:
        mod = importlib.import_module(f"healers.{name}")
        return mod.healer
    except ModuleNotFoundError:
        log.warning("修复器 '%s' 未找到", name)
        return None
    except AttributeError:
        log.warning("修复器模块 '%s' 缺少 'healer' 实例", name)
        return None


def _eval_condition(condition: str, value: dict, threshold) -> bool:
    """
    评估条件表达式。
    condition 是一个 Python 表达式字符串，如 "value['available_gb'] < threshold"
    """
    if condition is None:
        return False
    # 如果条件里没用到 threshold 变量，threshold 为 None 也允许
    if threshold is None and "threshold" in condition:
        # 条件用到 threshold 但没设阈值，跳过
        return False
    try:
        return bool(eval(condition, {"__builtins__": {}}, {"value": value, "threshold": threshold}))
    except Exception as e:
        log.error("条件评估失败 '%s': %s", condition, e)
        return False


def _in_cooldown(rule_name: str, cooldown_sec: int) -> bool:
    """检查规则是否在冷却期内。"""
    last = _cooldowns.get(rule_name)
    if last is None:
        return False
    return time.time() - last < cooldown_sec


def _set_cooldown(rule_name: str):
    _cooldowns[rule_name] = time.time()


def load_events() -> list[dict]:
    """从文件加载事件（CLI / dashboard 用）。"""
    if not EVENTS_FILE.exists():
        return []
    try:
        return json.loads(EVENTS_FILE.read_text())
    except Exception:
        return []


def _save_events(events: list[dict]):
    """持久化事件到文件。"""
    try:
        EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        EVENTS_FILE.write_text(json.dumps(events, ensure_ascii=False, indent=2))
    except Exception as e:
        log.warning("保存事件文件失败: %s", e)


def _record_event(rule_name: str, collector_name: str, value: dict,
                  triggered: bool, heal_result: dict | None, notify_ok: bool):
    """记录事件到文件。"""
    event = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "rule": rule_name,
        "collector": collector_name,
        "value": value,
        "triggered": triggered,
        "heal": heal_result,
        "notified": notify_ok,
    }
    events = load_events()
    events.insert(0, event)
    if len(events) > 200:
        events = events[:200]
    _save_events(events)


def run_once() -> list[dict]:
    """
    运行一轮所有规则：采集 → 评估 → 修复 → 通知。
    返回本轮触发的事件列表。
    """
    config = load_rules()
    rules = config.get("rules", [])
    global_cfg = config.get("global", {})
    notify_cfg = global_cfg.get("notification", {})
    dingtalk_url = notify_cfg.get("dingtalk_webhook", "")
    wechat_url = notify_cfg.get("wechat_webhook", "")
    email_cfg = notify_cfg.get("email", {})

    triggered = []

    for rule in rules:
        if not rule.get("enabled", True):
            continue

        name = rule["name"]
        collector_name = rule["collector"]
        condition = rule.get("condition")
        threshold = rule.get("threshold")
        healer_name = rule.get("healer")
        cooldown = rule.get("cooldown", 60)
        should_notify = rule.get("notify", False)

        # 冷却检查
        if _in_cooldown(name, cooldown):
            continue

        # 加载采集器
        collector = _load_collector(collector_name)
        if collector is None:
            continue

        # 采集
        try:
            value = collector.collect()
        except Exception as e:
            log.warning("采集器 '%s' 执行失败: %s", collector_name, e)
            continue

        # 评估
        if not _eval_condition(condition, value, threshold):
            continue

        # 触发！
        log.info("规则触发 | %s | value=%s", name, value)
        _set_cooldown(name)

        # 修复
        heal_result = None
        healer = _load_healer(healer_name)
        if healer:
            try:
                ctx = {
                    "rule_name": name,
                    "collector_name": collector_name,
                    "value": value,
                    "threshold": threshold,
                }
                heal_result = healer.heal(ctx)
                log.info("修复完成 | %s | %s", name, heal_result)
            except Exception as e:
                log.error("修复器 '%s' 执行失败: %s", healer_name, e)
                heal_result = {"success": False, "actions": [], "error": str(e)}

        # 通知
        notify_ok = False
        if should_notify:
            title = f"Guardian 告警: {name}"
            body = (
                f"**规则**: {name}\n"
                f"**采集数据**: {value}\n"
                f"**修复结果**: {heal_result}"
            )
            if dingtalk_url:
                notify_ok = dingtalk_send(dingtalk_url, title, body)
            if wechat_url:
                notify_ok = wechat_send(wechat_url, title, body) or notify_ok
            if email_cfg.get("enabled"):
                notify_ok = email_send(email_cfg, title, body) or notify_ok

        # 记录
        _record_event(name, collector_name, value, True, heal_result, notify_ok)
        triggered.append({
            "rule": name,
            "value": value,
            "heal": heal_result,
        })

    return triggered


def run_loop(interval: int = 30):
    """守护进程主循环，每隔 interval 秒运行一轮。"""
    log.info("Guardian 启动，检查间隔 %ds", interval)
    while True:
        try:
            run_once()
        except Exception as e:
            log.exception("主循环异常: %s", e)
        time.sleep(interval)

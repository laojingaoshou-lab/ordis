"""规则引擎：加载规则 → 运行采集器 → 评估条件 → 触发修复 + 通知。"""

from __future__ import annotations
import importlib
import time
from datetime import datetime
from logger import get_logger
from config import load_rules
from notifier import dingtalk_send, wechat_send
import ai_diagnose

log = get_logger("engine")

# 已触发的冷却记录: {rule_name: last_trigger_time}
_cooldowns: dict[str, float] = {}
_finding_cooldowns: dict[str, float] = {}

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
    """评估规则条件表达式。"""
    if condition is None:
        return False
    if threshold is None and "threshold" in condition:
        return False
    try:
        return bool(eval(condition, {"__builtins__": {}},
                        {"value": value, "threshold": threshold}))
    except Exception as e:
        log.error("条件评估失败 '%s': %s", condition, e)
        return False


def _in_cooldown(rule_name: str, cooldown_sec: int) -> bool:
    """检查规则是否在冷却期内。"""
    last = _cooldowns.get(rule_name)
    return last is not None and time.time() - last < cooldown_sec


def _set_cooldown(rule_name: str):
    _cooldowns[rule_name] = time.time()


def load_events() -> list[dict]:
    """从数据库加载规则和扩展检测事件（CLI / dashboard 用）。"""
    import db
    rows = db.get_recent_events(limit=200)
    events = []
    for row in rows:
        if row["type"] == "rule_trigger":
            events.append(row["data"])
        elif row["type"] == "health_finding":
            finding = row["data"]
            events.append({
                "time": finding.get("observed_at", ""),
                "rule": finding.get("summary", finding.get("reason", "health finding")),
                "collector": finding.get("source", "detection"),
                "value": finding,
                "triggered": True,
                "heal": None,
                "notified": False,
            })
    return events


def _record_event(rule_name: str, collector_name: str, value: dict,
                  triggered: bool, heal_result: dict | None, notify_ok: bool):
    """记录规则触发事件到数据库。"""
    import db
    event = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "rule": rule_name,
        "collector": collector_name,
        "value": value,
        "triggered": triggered,
        "heal": heal_result,
        "notified": notify_ok,
    }
    db.save_event("rule_trigger", value.get("hostname", ""), event)


def _needs_ai_diagnosis(heal_result: dict | None) -> bool:
    """Skipped/non-applicable repairs are not failures and need no AI call."""
    if heal_result is None:
        return True
    if heal_result.get("applicable") is False:
        return False
    return not heal_result.get("success", False)


def _finding_skill(config: dict, fingerprint: str) -> dict | None:
    return next((rule for rule in config.get("rules", [])
                 if rule.get("trigger") == "health_finding"
                 and rule.get("enabled", True)
                 and rule.get("fingerprint") == fingerprint), None)


def _run_finding_skill(rule: dict, finding: dict) -> dict:
    healer = _load_healer(rule.get("healer"))
    if not healer:
        return {"attempted": False, "success": False,
                "reason": "已审核 skill 的修复器不可用", "actions": []}
    try:
        result = healer.heal({
            "rule_name": rule.get("name"),
            "collector_name": finding.get("source"),
            "value": finding,
            "params": rule.get("params"),
        })
        result["attempted"] = True
        result["source"] = "skill"
        result["skill_id"] = rule.get("_skill_id")
        return result
    except Exception as exc:
        return {"attempted": True, "success": False, "source": "skill",
                "skill_id": rule.get("_skill_id"), "actions": [],
                "reason": str(exc).splitlines()[0][:240]}


def _finding_repair_config(config: dict, source: str) -> dict:
    detection = config.get("detection") or {}
    return ((detection.get(source) or {}).get("repair") or {})


def _handle_findings(reports: list[dict], cooldown_sec: int = 300,
                     config: dict | None = None) -> list[dict]:
    """Run approved/built-in repairs, then route failures to AI takeover."""
    import db
    import health_repair

    config = config or {}
    now = time.time()
    triggered = []
    k8s_clients = {}
    for report in reports:
        for finding in report.get("findings", []):
            fingerprint = finding["fingerprint"]
            last = _finding_cooldowns.get(fingerprint)
            if last is not None and now - last < cooldown_sec:
                continue
            _finding_cooldowns[fingerprint] = now
            db.save_event("health_finding", finding.get("name", ""), finding)
            attempts = []
            skill = _finding_skill(config, fingerprint)
            if skill:
                skill_result = _run_finding_skill(skill, finding)
                attempts.append(skill_result)
                repair_result = skill_result
            else:
                repair_result = None

            if not repair_result or not repair_result.get("success"):
                source = finding.get("source", "")
                repair_config = _finding_repair_config(config, source)
                client = None
                if source == "kubernetes" and repair_config.get("enabled"):
                    if source not in k8s_clients:
                        from k8s_client import KubernetesClient
                        source_config = (config.get("detection") or {}).get(source) or {}
                        k8s_clients[source] = KubernetesClient(
                            context=str(source_config.get("context") or ""),
                            kubeconfig=str(source_config.get("kubeconfig") or ""),
                            timeout=float(source_config.get("timeout", 15)))
                    client = k8s_clients[source]
                builtin_result = health_repair.attempt(
                    finding, repair_config, k8s_client=client)
                attempts.append(builtin_result)
                repair_result = builtin_result

            combined_result = {
                "attempted": any(item.get("attempted") for item in attempts),
                "success": bool(repair_result and repair_result.get("success")),
                "source": repair_result.get("source", "builtin")
                if repair_result else "builtin",
                "attempts": attempts,
            }
            if repair_result and repair_result.get("reason"):
                combined_result["reason"] = repair_result["reason"]
            db.save_event("health_repair", finding.get("name", ""), {
                "fingerprint": fingerprint,
                "result": combined_result,
            })
            if not combined_result["success"]:
                try:
                    ai_diagnose.request_diagnosis_async(
                        finding["summary"], finding["source"], finding,
                        combined_result, fingerprint_key=fingerprint)
                except Exception as exc:
                    log.warning("扩展故障 AI 诊断调度异常: %s", exc)
            triggered.append({
                "rule": finding["summary"],
                "value": finding,
                "heal": combined_result,
                "finding": True,
            })
    return triggered


def run_detection_checks(config: dict | None = None) -> list[dict]:
    """Run enabled read-only traditional and Kubernetes health detectors."""
    if config is None:
        config = load_rules()
    detection = config.get("detection") or {}
    reports = []

    traditional = detection.get("traditional") or {}
    if traditional.get("enabled", False):
        from traditional_checks import run_checks
        reports.append(run_checks(traditional))

    kubernetes = detection.get("kubernetes") or {}
    if kubernetes.get("enabled", False):
        from k8s_checks import run_checks
        reports.append(run_checks(kubernetes))
    return reports


def run_once(verbose: bool = True) -> list[dict]:
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

    triggered = []

    for rule in rules:
        if not rule.get("enabled", True):
            continue
        if rule.get("trigger") == "health_finding":
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
        if verbose:
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
                    "params": rule.get("params"),  # 晋升规则的修复命令等
                }
                heal_result = healer.heal(ctx)
                if verbose:
                    log.info("修复完成 | %s | %s", name, heal_result)
                from audit import log_audit
                log_audit("auto_heal", {
                    "rule": name,
                    "healer": healer_name,
                    "result": heal_result
                })
            except Exception as e:
                log.error("修复器 '%s' 执行失败: %s", healer_name, e)
                heal_result = {"success": False, "actions": [], "error": str(e)}

        # AI 诊断调度：仅在修复失败或无修复器时触发（异步，不阻塞主循环）
        heal_failed = _needs_ai_diagnosis(heal_result)
        if heal_failed:
            try:
                ai_diagnose.request_diagnosis_async(
                    name, collector_name, value, heal_result)
            except Exception as e:
                log.warning("AI诊断调度异常: %s", e)

        # 通知
        notify_ok = False
        if should_notify:
            title = f"Guardian 告警: {name}"
            body = (
                f"**规则**: {name}\n"
                f"**采集数据**: {value}\n"
                f"**修复结果**: {heal_result}"
            )
            # 修复失败时附带最近一次 AI 诊断摘要（可能尚未完成，属正常）
            if heal_failed:
                summary = ai_diagnose.get_latest_diagnosis_summary()
                if summary:
                    body += f"\n\n**AI 诊断**:\n{summary}"
            if dingtalk_url:
                notify_ok = dingtalk_send(dingtalk_url, title, body)
            if wechat_url:
                notify_ok = wechat_send(wechat_url, title, body) or notify_ok

        # 记录
        _record_event(name, collector_name, value, True, heal_result, notify_ok)
        triggered.append({
            "rule": name,
            "value": value,
            "heal": heal_result,
        })

    reports = run_detection_checks(config)
    finding_cooldown = int((config.get("detection") or {}).get("cooldown", 300))
    triggered.extend(_handle_findings(reports, finding_cooldown, config))
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

"""AI 故障接管模式：自动修复或邮件建议。"""

from __future__ import annotations

import json
import os
import shlex
from email.utils import parseaddr
from pathlib import Path

from logger import get_logger
from paths import data_home

log = get_logger("ai_mode")

MODES = ("auto", "email")
SECURITY_MODES = ("ssl", "starttls", "plain")
CONFIG_PATH = data_home() / "ai_mode.json"

DEFAULT_CONFIG = {
    "mode": "email",
    "email": {
        "to": "",
        "smtp_host": "",
        "smtp_port": 465,
        "smtp_user": "",
        "from_address": "",
        "password_env": "ORDIS_SMTP_PASSWORD",
        "security": "ssl",
    },
}


def _defaults() -> dict:
    return {
        "mode": DEFAULT_CONFIG["mode"],
        "email": dict(DEFAULT_CONFIG["email"]),
    }


def load(path: Path | None = None) -> dict:
    """加载模式配置；缺失或损坏时返回安全的 email 默认模式。"""
    file = path or CONFIG_PATH
    data = _defaults()
    if not file.exists():
        return data
    try:
        saved = json.loads(file.read_text(encoding="utf-8"))
        if saved.get("mode") in MODES:
            data["mode"] = saved["mode"]
        if isinstance(saved.get("email"), dict):
            data["email"].update(saved["email"])
    except Exception as exc:
        log.warning("AI 接管配置读取失败 (%s): %s", file, exc)
    return data


def save(config: dict, path: Path | None = None) -> None:
    """原子保存配置并收紧为仅当前用户可读写。"""
    from filelock import locked_write_text

    file = path or CONFIG_PATH
    locked_write_text(
        file,
        json.dumps(config, ensure_ascii=False, indent=2),
    )
    try:
        os.chmod(file, 0o600)
    except OSError:
        pass


def set_mode(mode: str, path: Path | None = None) -> dict:
    """切换接管模式并返回完整配置。"""
    if mode not in MODES:
        raise ValueError(f"无效 AI 接管模式: {mode}")
    config = load(path)
    config["mode"] = mode
    save(config, path)
    return config


def configure_email(path: Path | None = None, **updates) -> dict:
    """更新非敏感邮件配置；SMTP 密码只允许通过环境变量提供。"""
    config = load(path)
    email = config["email"]
    for key, value in updates.items():
        if value is not None:
            email[key] = value
    if not email.get("from_address") and email.get("smtp_user"):
        email["from_address"] = email["smtp_user"]
    issues = email_configuration_issues(config, require_password=False)
    if issues:
        raise ValueError("；".join(issues))
    save(config, path)
    return config


def _valid_address(value: str) -> bool:
    if not value or any(ch in value for ch in "\r\n"):
        return False
    _, address = parseaddr(value)
    return bool(address and "@" in address)


def email_configuration_issues(config: dict | None = None,
                               require_password: bool = True) -> list[str]:
    """返回邮件配置缺失项，不输出任何凭据值。"""
    email = (config or load()).get("email") or {}
    issues = []
    if not _valid_address(str(email.get("to") or "")):
        issues.append("管理员收件邮箱无效或未配置")
    if not email.get("smtp_host"):
        issues.append("SMTP 服务器未配置")
    try:
        port = int(email.get("smtp_port"))
        if not 1 <= port <= 65535:
            raise ValueError
    except (TypeError, ValueError):
        issues.append("SMTP 端口无效")
    if email.get("security") not in SECURITY_MODES:
        issues.append("SMTP 安全模式无效")
    if not _valid_address(str(email.get("from_address") or "")):
        issues.append("发件邮箱无效或未配置")
    user = str(email.get("smtp_user") or "")
    password_env = str(email.get("password_env") or "")
    if user and not password_env:
        issues.append("SMTP 用户已配置但密码环境变量名缺失")
    elif require_password and user and not os.environ.get(password_env):
        issues.append(f"环境变量 {password_env} 未设置")
    return issues


def _reject_shell_compound(command: str) -> str | None:
    """后台自动修复只接受单条命令，拒绝 shell 组合与重定向。"""
    if any(token in command for token in ("\n", "\r", ";", "&&", "||",
                                           "|", ">", "<", "`", "$(")):
        return "自动修复只允许单条命令，禁止 shell 组合、重定向或命令替换"
    return None


def _kubernetes_auto_command_supported(command: str,
                                       finding: dict | None) -> bool:
    if not finding or finding.get("source") != "kubernetes":
        return False
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    if len(argv) != 7 or argv[0].rsplit("/", 1)[-1] != "kubectl":
        return False
    if argv[1:3] != ["set", "image"] or argv[5] not in {"-n", "--namespace"}:
        return False
    resource = argv[3]
    if not resource.startswith("deployment/") or "=" not in argv[4]:
        return False
    deployment = resource.split("/", 1)[1]
    container, image = argv[4].split("=", 1)
    namespace = argv[6]
    if not all((deployment, container, image, namespace)):
        return False
    if (finding.get("kind") != "Deployment"
            or deployment != finding.get("name")
            or namespace != (finding.get("namespace") or "default")):
        return False
    from config import load_rules
    repair = (((load_rules().get("detection") or {}).get("kubernetes") or {})
              .get("repair") or {})
    allowed = set(str(item) for item in repair.get("allowed_namespaces") or [])
    return bool(repair.get("enabled") and repair.get("ai_enabled")
                and namespace in allowed)


def _traditional_auto_command_supported(command: str,
                                        finding: dict | None) -> bool:
    if not finding or finding.get("source") != "traditional":
        return True
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    if len(argv) < 3:
        return False
    tool = argv[0].rsplit("/", 1)[-1]
    reason = finding.get("reason")
    name = str(finding.get("name") or "")
    if reason == "systemd_unit_failed":
        return ((tool == "systemctl" and argv[1] in {"start", "restart", "reload"}
                 and argv[2] == name)
                or (tool == "service" and argv[1] == name
                    and argv[2] in {"start", "restart", "reload"}))
    if str(reason).startswith("container_"):
        return (tool == "docker" and argv[1] in {"start", "restart"}
                and argv[2] == name)
    return False


def _auto_command_supported(command: str, finding: dict | None = None) -> bool:
    """后台无人值守修复仅开放可确定回检的常见服务操作。"""
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    if len(argv) < 3:
        return False
    tool = argv[0].rsplit("/", 1)[-1]
    action = argv[1]
    supported = (
        (tool == "systemctl" and action in {"start", "restart", "reload"})
        or (tool == "service" and argv[2] in {"start", "restart", "reload"})
        or (tool == "docker" and action in {"start", "restart"})
        or (tool == "pm2" and action in {"start", "restart"})
        or _kubernetes_auto_command_supported(command, finding)
    )
    return supported and _traditional_auto_command_supported(command, finding)


def execute_auto_repair(diagnosis: dict, finding: dict | None = None) -> dict:
    """校验、执行并回检 AI 修复命令。"""
    import ai_levels
    import promotion
    from audit import log_audit
    from healers.command_runner import healer

    command = str(diagnosis.get("fix_command") or "").strip()
    if not command:
        return {"mode": "auto", "attempted": False, "success": False,
                "reason": "AI 未提供 fix_command"}
    compound_reason = _reject_shell_compound(command)
    if compound_reason:
        return {"mode": "auto", "attempted": False, "success": False,
                "command": command, "reason": compound_reason}
    if not promotion._is_repair_command(command):
        return {"mode": "auto", "attempted": False, "success": False,
                "command": command, "reason": "候选命令不是修复动作"}
    auto_safe, _ = ai_levels.check_allowed(command, "operate")
    if not auto_safe:
        return {"mode": "auto", "attempted": False, "success": False,
                "command": command,
                "reason": "超出后台自动修复安全边界，仅允许常规运维操作"}
    if not _auto_command_supported(command, finding):
        return {"mode": "auto", "attempted": False, "success": False,
                "command": command,
                "reason": "命令不在后台自动修复支持列表"}
    allowed, reason = ai_levels.check_allowed(command)
    if not allowed:
        return {"mode": "auto", "attempted": False, "success": False,
                "command": command, "reason": reason}
    check_port, check_command = promotion._infer_verification(
        diagnosis, command)
    if not check_port and not check_command:
        return {"mode": "auto", "attempted": False, "success": False,
                "command": command, "reason": "无法生成确定性的修复效果回检"}

    result = healer.heal({"params": {
        "command": command,
        "check_port": check_port,
        "check_command": check_command,
    }})
    outcome = {
        "mode": "auto",
        "attempted": True,
        "success": bool(result.get("success")),
        "command": command,
        "check_port": check_port,
        "check_command": check_command,
        "result": result,
    }
    log_audit("ai_auto_repair", outcome)
    return outcome


def send_email_advice(rule_name: str, collector_name: str, value: dict,
                      diagnosis: dict, config: dict | None = None) -> dict:
    """将 AI 诊断和修复建议发送给管理员，不执行候选命令。"""
    from audit import log_audit
    from notifier import email_send

    config = config or load()
    issues = email_configuration_issues(config, require_password=True)
    if issues:
        return {"mode": "email", "success": False, "sent": False,
                "reason": "；".join(issues)}

    host = str(value.get("hostname") or "unknown")
    evidence = diagnosis.get("evidence") or []
    if not isinstance(evidence, list):
        evidence = [str(evidence)]
    evidence_text = "\n".join(f"- {item}" for item in evidence) or "- 无"
    content = (
        f"节点: {host}\n"
        f"规则: {rule_name}\n"
        f"采集器: {collector_name}\n"
        f"严重级别: {diagnosis.get('severity', 'unknown')}\n\n"
        f"根因:\n{diagnosis.get('root_cause', '')}\n\n"
        f"证据:\n{evidence_text}\n\n"
        f"修复建议:\n{diagnosis.get('fix_direction', '')}\n\n"
        f"候选命令（未执行）:\n{diagnosis.get('fix_command') or '(无)'}"
    )
    title = f"[Ordis] {host} 故障修复建议: {rule_name}"
    sent = email_send(config["email"], title, content)
    outcome = {"mode": "email", "success": sent, "sent": sent,
               "recipient": config["email"]["to"]}
    if not sent:
        outcome["reason"] = "邮件发送失败"
    log_audit("ai_email_advice", outcome)
    return outcome


def process_diagnosis(fingerprint_key: str, rule_name: str,
                      collector_name: str, value: dict,
                      diagnosis: dict) -> dict:
    """根据当前模式接管一次成功的 AI 诊断。"""
    config = load()
    if config["mode"] == "email":
        return send_email_advice(rule_name, collector_name, value,
                                 diagnosis, config)

    outcome = execute_auto_repair(diagnosis, value)
    if outcome.get("success"):
        import promotion
        case = {"_key": fingerprint_key, "diagnosis": diagnosis}
        skill = promotion.auto_draft_from_ai(fingerprint_key, case)
        outcome["skill_id"] = skill.get("id") if skill else None
    return outcome

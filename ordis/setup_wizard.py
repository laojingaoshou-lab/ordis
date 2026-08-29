"""Interactive first-run setup and consolidated configuration reporting."""

from __future__ import annotations

import getpass
import os
import re
import time
from pathlib import Path
from typing import Callable

import ai_mode
import model_config


InputFn = Callable[[str], str]
SecretFn = Callable[[str], str]


def _prompt(input_fn: InputFn, label: str, default: str = "",
            required: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        value = input_fn(f"{label}{suffix}: ").strip()
        value = value or default
        if value or not required:
            return value
        print(f"{label}不能为空")


def _choose(input_fn: InputFn, label: str, choices: tuple[str, ...],
            default: str, descriptions: dict[str, str]) -> str:
    rendered = "  ".join(
        f"[{index}] {descriptions[value]}" for index, value in enumerate(choices, 1))
    aliases = {str(index): value for index, value in enumerate(choices, 1)}
    aliases.update({value: value for value in choices})
    while True:
        print(rendered)
        raw = input_fn(f"{label} [{default}]: ").strip().lower()
        value = aliases.get(raw or default)
        if value:
            return value
        print("输入无效，请输入序号或选项名称")


def _safe_error(exc: Exception) -> str:
    text = str(exc).splitlines()[0].strip()
    text = re.sub(r"(?i)bearer\s+[a-z0-9._-]+", "Bearer [已隐藏]", text)
    text = re.sub(r"(?i)(api[_-]?key|token|password)[=: ]+[^&\s]+",
                  r"\1=[已隐藏]", text)
    return text[:300] or exc.__class__.__name__


def configuration_summary(model_path: Path | None = None,
                          mode_path: Path | None = None) -> dict:
    model_data = model_config.load(model_path)
    active_name = model_data.get("active")
    provider = (model_data.get("providers") or {}).get(active_name or "", {})
    mode_data = ai_mode.load(mode_path)
    permission = str(model_data.get("ai_level") or "view")
    email_issues = ai_mode.email_configuration_issues(
        mode_data, require_password=True)
    mode_ready = mode_data["mode"] == "auto" or not email_issues
    return {
        "ok": bool(provider) and mode_ready,
        "model": {
            "configured": bool(provider),
            "provider": active_name or "",
            "base_url": provider.get("base_url", ""),
            "model": provider.get("model", ""),
            "api_key": model_config.mask_key(provider.get("api_key", "")),
        },
        "ai": {
            "mode": mode_data["mode"],
            "permission": permission,
            "daily_limit": max(0, _env_int("ORDIS_AI_DAILY_LIMIT", 0)),
        },
        "email": {
            "to": mode_data["email"].get("to", ""),
            "smtp_host": mode_data["email"].get("smtp_host", ""),
            "smtp_port": mode_data["email"].get("smtp_port"),
            "security": mode_data["email"].get("security", "ssl"),
            "password_env": mode_data["email"].get("password_env", ""),
            "ready": not email_issues,
            "issues": email_issues,
        },
    }


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def run_model_interactive(input_fn: InputFn = input,
                          secret_fn: SecretFn = getpass.getpass,
                          model_path: Path | None = None,
                          test_fn: Callable[[dict], tuple[str, float]] | None = None
                          ) -> dict:
    """Configure one model provider, testing it before changing disk state."""
    data = model_config.load(model_path)
    current_name = data.get("active") or ""
    current = (data.get("providers") or {}).get(current_name, {})
    print("=== 配置模型 API ===")

    provider_name = _prompt(
        input_fn, "供应商名称", current_name or "siliconflow", required=True)
    base_url = _prompt(
        input_fn, "API Base URL",
        current.get("base_url") or model_config.DEFAULT_BASE_URL,
        required=True)
    base_url = model_config.normalize_base_url(base_url)
    model_name = _prompt(
        input_fn, "模型名称", current.get("model", ""), required=True)
    existing_key = current.get("api_key", "") if provider_name == current_name else ""
    key_label = "API Key（回车保留当前值）: " if existing_key else "API Key（输入不回显）: "
    api_key = secret_fn(key_label).strip() or existing_key
    if not api_key:
        raise ValueError("API Key 不能为空")

    provider = {"base_url": base_url, "model": model_name, "api_key": api_key}
    print(f"正在测试模型：{provider_name} / {model_name} ...")
    try:
        response, elapsed = (test_fn or model_config.test_provider)(provider)
    except Exception as exc:
        raise RuntimeError(f"模型测试失败，未保存：{_safe_error(exc)}") from exc

    data.setdefault("providers", {})[provider_name] = {
        **provider, "added_at": time.strftime("%Y-%m-%d")}
    data["active"] = provider_name
    model_config.save(data, model_path)
    print(f"模型连接正常（{elapsed}s）：{response.strip()[:60]}")
    print(f"已保存并激活：{provider_name} / {model_name}")
    return configuration_summary(model_path=model_path)


def run_interactive(input_fn: InputFn = input,
                    secret_fn: SecretFn = getpass.getpass,
                    model_path: Path | None = None,
                    mode_path: Path | None = None,
                    test_fn: Callable[[dict], tuple[str, float]] | None = None
                    ) -> dict:
    """Collect, validate, test, and then persist a complete Ordis setup."""
    model_data = model_config.load(model_path)
    current_name = model_data.get("active") or ""
    current = (model_data.get("providers") or {}).get(current_name, {})
    current_mode = ai_mode.load(mode_path)
    current_permission = str(model_data.get("ai_level") or "view")

    print("=== Ordis 一键配置 ===")
    print("配置模型 API、AI 接管模式和全局权限；不会启动守护进程。\n")

    provider_name = _prompt(
        input_fn, "供应商名称", current_name or "siliconflow", required=True)
    base_url = _prompt(
        input_fn, "API Base URL",
        current.get("base_url") or model_config.DEFAULT_BASE_URL,
        required=True)
    base_url = model_config.normalize_base_url(base_url)
    model_name = _prompt(
        input_fn, "模型名称", current.get("model", ""), required=True)

    existing_key = current.get("api_key", "") if provider_name == current_name else ""
    key_label = "API Key（回车保留当前值）: " if existing_key else "API Key（输入不回显）: "
    api_key = secret_fn(key_label).strip() or existing_key
    if not api_key:
        raise ValueError("API Key 不能为空")

    mode = _choose(
        input_fn, "AI 接管模式", ("auto", "email"), current_mode["mode"],
        {"auto": "auto（受限自动修复）", "email": "email（仅发送建议）"})
    permission = _choose(
        input_fn, "全局权限", ("view", "operate", "root"), current_permission,
        {"view": "view（只读）", "operate": "operate（常规运维）",
         "root": "root（对话可执行全部命令）"})

    email_config = dict(current_mode["email"])
    if mode == "email":
        print("\n── 管理员邮件 ──")
        email_to = _prompt(
            input_fn, "管理员邮箱", email_config.get("to", ""), True)
        smtp_host = _prompt(
            input_fn, "SMTP 服务器", email_config.get("smtp_host", ""), True)
        smtp_port_text = _prompt(input_fn, "SMTP 端口", str(
            email_config.get("smtp_port", 465)), True)
        try:
            smtp_port = int(smtp_port_text)
        except ValueError as exc:
            raise ValueError("SMTP 端口必须是整数") from exc
        email_config.update({
            "to": email_to,
            "smtp_host": smtp_host,
            "smtp_port": smtp_port,
            "smtp_user": _prompt(
                input_fn, "SMTP 用户名", email_config.get("smtp_user", "")),
            "from_address": _prompt(
                input_fn, "发件邮箱", email_config.get("from_address", ""), True),
            "security": _choose(
                input_fn, "SMTP 安全模式", ai_mode.SECURITY_MODES,
                email_config.get("security", "ssl"),
                {"ssl": "ssl", "starttls": "starttls", "plain": "plain"}),
            "password_env": _prompt(
                input_fn, "SMTP 密码环境变量",
                email_config.get("password_env", "ORDIS_SMTP_PASSWORD"), True),
        })
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*",
                            email_config["password_env"]):
            raise ValueError("SMTP 密码环境变量名无效")
        candidate_mode = {"mode": mode, "email": email_config}
        issues = ai_mode.email_configuration_issues(
            candidate_mode, require_password=False)
        if issues:
            raise ValueError("邮件配置无效：" + "；".join(issues))

    provider = {
        "base_url": base_url,
        "model": model_name,
        "api_key": api_key,
    }
    print(f"\n正在测试模型：{provider_name} / {model_name} ...")
    tester = test_fn or model_config.test_provider
    try:
        response, elapsed = tester(provider)
    except Exception as exc:
        raise RuntimeError(f"模型测试失败，未保存：{_safe_error(exc)}") from exc

    # All validation and network checks have completed; persist only now.
    model_data.setdefault("providers", {})[provider_name] = {
        **provider,
        "added_at": time.strftime("%Y-%m-%d"),
    }
    model_data["active"] = provider_name
    model_data["ai_level"] = permission
    model_config.save(model_data, model_path)
    ai_mode.save({"mode": mode, "email": email_config}, mode_path)

    print(f"模型连接正常（{elapsed}s）：{response.strip()[:60]}")
    print("\n配置完成：")
    print(f"  模型：{provider_name} / {model_name}")
    print(f"  模式：{mode}")
    print(f"  权限：{permission}")
    if mode == "email" and email_config.get("smtp_user"):
        password_env = email_config["password_env"]
        state = "已设置" if os.environ.get(password_env) else "尚未设置"
        print(f"  SMTP 密码：环境变量 {password_env}（{state}）")
    if mode == "auto" and permission == "view":
        print("  注意：view 权限会拦截 AI 写操作，自动修复只会给出诊断。")
    print("  守护进程：未启动（需要时执行 `ordis run-install`）")
    return configuration_summary(model_path, mode_path)

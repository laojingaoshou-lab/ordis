"""通知模块：钉钉机器人 / 企业微信 webhook / SMTP 邮箱。"""

import json
import os
import requests
from email.message import EmailMessage
import smtplib
from logger import get_logger

log = get_logger("notifier")


def dingtalk_send(webhook: str, title: str, content: str) -> bool:
    """发送钉钉 markdown 消息，返回是否成功。"""
    if not webhook:
        return False

    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": title,
            "text": (
                f"## {title}\n\n"
                f"{content}\n\n"
                f"> Guardian @ {_hostname()}"
            ),
        },
    }
    try:
        r = requests.post(webhook, json=payload, timeout=10)
        ok = r.status_code == 200
        if not ok:
            log.warning("钉钉通知失败: %s %s", r.status_code, r.text[:200])
        return ok
    except Exception as e:
        log.warning("钉钉通知异常: %s", e)
        return False


def wechat_send(webhook: str, title: str, content: str) -> bool:
    """发送企业微信 markdown 消息。"""
    if not webhook:
        return False

    payload = {
        "msgtype": "markdown",
        "markdown": {
            "content": (
                f"## {title}\n"
                f"{content}\n"
                f"> Guardian @ {_hostname()}"
            ),
        },
    }
    try:
        r = requests.post(webhook, json=payload, timeout=10)
        ok = r.status_code == 200
        if not ok:
            log.warning("微信通知失败: %s %s", r.status_code, r.text[:200])
        return ok
    except Exception as e:
        log.warning("微信通知异常: %s", e)
        return False


def email_send(config: dict, title: str, content: str) -> bool:
    """发送邮件告警，返回是否成功。"""
    if not config or not config.get("enabled"):
        return False

    smtp_host = config.get("smtp_host")
    smtp_port = config.get("smtp_port", 465)
    use_ssl = config.get("use_ssl", True)
    username = config.get("username")
    from_addr = config.get("from")
    to_addrs = config.get("to", [])
    password_env = config.get("password_env", "ORDIS_SMTP_PASSWORD")
    password = os.getenv(password_env)

    if not all([smtp_host, username, from_addr, to_addrs, password]):
        log.warning("邮箱配置不完整，跳过发送")
        return False

    try:
        msg = EmailMessage()
        msg["Subject"] = title
        msg["From"] = from_addr
        msg["To"] = ", ".join(to_addrs)
        msg.set_content(content)

        if use_ssl:
            server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=10)
        else:
            server = smtplib.SMTP(smtp_host, smtp_port, timeout=10)
            server.starttls()

        server.login(username, password)
        server.send_message(msg)
        server.quit()
        return True
    except Exception as e:
        log.warning("邮件发送失败: %s", e)
        return False


def _hostname() -> str:
    import socket
    return socket.gethostname()

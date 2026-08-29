"""通知模块：钉钉、企业微信和 SMTP 邮件。"""

from __future__ import annotations
import requests
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
    """通过 SMTP 发送管理员邮件；密码仅从指定环境变量读取。"""
    import os
    import smtplib
    import ssl
    from email.message import EmailMessage

    host = str(config.get("smtp_host") or "")
    port = int(config.get("smtp_port") or 0)
    user = str(config.get("smtp_user") or "")
    sender = str(config.get("from_address") or user)
    recipient = str(config.get("to") or "")
    security = str(config.get("security") or "ssl")
    password_env = str(config.get("password_env") or "")
    password = os.environ.get(password_env, "") if password_env else ""
    if (not host or not port or not sender or not recipient or
            any(ch in sender + recipient + title for ch in "\r\n")):
        log.warning("邮件通知配置不完整或邮件头无效")
        return False
    if user and not password:
        log.warning("邮件通知缺少 SMTP 密码环境变量: %s", password_env)
        return False

    message = EmailMessage()
    message["Subject"] = title
    message["From"] = sender
    message["To"] = recipient
    message.set_content(content)

    try:
        context = ssl.create_default_context()
        if security == "ssl":
            client = smtplib.SMTP_SSL(host, port, timeout=15,
                                      context=context)
        else:
            client = smtplib.SMTP(host, port, timeout=15)
        with client:
            if security == "starttls":
                client.starttls(context=context)
            if user:
                client.login(user, password)
            client.send_message(message)
        return True
    except Exception as exc:
        log.warning("邮件通知失败: %s", exc)
        return False


def _hostname() -> str:
    import socket
    return socket.gethostname()

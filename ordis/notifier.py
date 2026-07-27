"""通知模块：钉钉机器人 / 企业微信 webhook。"""

import json
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


def _hostname() -> str:
    import socket
    return socket.gethostname()

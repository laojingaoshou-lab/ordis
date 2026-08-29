"""
AI 权限分级：统一约束 AI 在所有场景下能生成/执行什么级别的命令。

覆盖场景：
  - talk        AI 诊断建议与命令执行
  - suggest     命令补全候选
  - 自动修复    晋升技能（command_runner）的命令在 confirm 与执行时校验
                （view 级下 AI 生成的技能直接拒绝入库）

三级模型：
  L0 view    只读观察——AI 仅可查看状态/日志并给建议，候选全部为只读命令；
             自动修复仅允许内置只读类，AI 提议的写操作技能拒绝生成
  L1 operate 正常操作——允许服务重启、容器管理等运维写操作
  L2 root    root 权限——不限制命令类型（包管理、磁盘、用户、任意 shell）

talk 中级别内命令直接执行，越权命令经过 y/N 确认；root 级命令直接执行。
suggest 和技能晋升仍保留各自的选择、确认与效果回检流程。

配置：~/.ordis/model.json 中 "ai_level": "view|operate|root"（默认 view）
切换：ordis config permission [view|operate|root]
兼容：ordis view|operate|root、ordis level [view|operate|root]
"""

from __future__ import annotations

import re

LEVELS = ("view", "operate", "root")

# 各级别禁止 AI 提议的模式（正则，小写匹配）
_LEVEL_DENY: dict[str, tuple] = {
    # L0: 一切写操作都禁止，只允许观察类
    "view": (
        r"\b(rm|rmdir|mv|dd|mkfs|shutdown|reboot|halt|poweroff|init)\b",
        r"\b(kill|pkill|killall)\b",
        r"\bchmod|\bchown|\buseradd|\buserdel|\bpasswd\b",
        r"\b(systemctl|service)\s+(start|stop|restart|reload|disable|enable|mask)",
        r"\bdocker\s+(rm|rmi|stop|restart|kill|prune|exec)",
        r"\bpm2\s+(delete|stop|restart|kill)",
        r"\bkubectl\s+(delete|apply|edit|scale|rollout|cordon|drain|taint)",
        r"\b(apt|apt-get|yum|dnf|pip3?|npm|gem)\s+\w*(install|remove|upgrade|update)",
        r"\b(tee|truncate)\s", r">\s*/etc/", r"\biptables|-ufw\b",
        r"\bcurl\b[^|]*\|\s*(ba)?sh", r"\bwget\b",
    ),
    # L1: 在 L0 基础上额外禁止系统级危险动作（包管理/磁盘/用户/防火墙/关机）
    "operate": (
        r"\b(dd|mkfs|fdisk|parted|shutdown|reboot|halt|poweroff|init\s+[06])\b",
        r"\b(useradd|userdel|usermod|passwd|visudo)\b",
        r"\b(iptables|firewall-cmd|ufw)\b",
        r"\brm\s+-[rf]{1,2}\s+(/|~|\$HOME)?\s*$",
        r"\b(apt|apt-get|yum|dnf)\s+(install|remove|purge|upgrade|update)",
        r"\bcrontab\b", r"\bmodprobe\b", r"\bsysctl\s+-w\b",
    ),
    "root": (),
}


def current_level() -> str:
    """读取当前 AI 权限等级。默认 view。"""
    try:
        from model_config import load
        data = load()
        lv = (data.get("ai_level") or "view").lower()
        return lv if lv in LEVELS else "view"
    except Exception:
        return "view"


def set_level(level: str) -> bool:
    """写入权限等级到 ~/.ordis/model.json。"""
    level = (level or "").lower()
    if level not in LEVELS:
        return False
    from model_config import load, save
    data = load()
    data["ai_level"] = level
    save(data)
    return True


def check_allowed(cmd: str, level: str | None = None) -> tuple[bool, str]:
    """
    判断 cmd 是否允许出现在 AI 候选里。
    返回 (是否允许, 原因)。deny 规则命中即拒绝。
    """
    level = level or current_level()
    cmd_l = (cmd or "").strip().lower()
    if not cmd_l:
        return False, "空命令"
    for pat in _LEVEL_DENY.get(level, ()):
        if re.search(pat, cmd_l):
            return False, f"超出 {level} 级权限"
    return True, ""


def filter_candidates(candidates: list[dict],
                      level: str | None = None) -> list[dict]:
    """按当前等级过滤 AI 候选命令列表。"""
    level = level or current_level()
    out = []
    for c in candidates or []:
        ok, reason = check_allowed(c.get("cmd", ""), level)
        if ok:
            out.append(c)
        else:
            from logger import get_logger
            log = get_logger("ai_suggest")
            log.info("候选被 %s 级拦截: %s (%s)", level,
                     c.get("cmd", "")[:60], reason)
    return out


def level_prompt(level: str | None = None) -> str:
    """生成注入 system prompt 的权限说明文本；可传覆盖等级（talk 单次会话用）。"""
    lv = level or current_level()
    desc = {
        "view": "当前为 view 级：你只能给出查看/诊断类命令"
                "（df/free/ps/ss/journalctl/systemctl status 等），"
                "严禁提议任何修改类命令。",
        "operate": "当前为 operate 级：你可以给出常规运维操作"
                   "（服务启停、docker/pm2 管理、kubectl 常规操作等），"
                   "但严禁磁盘格式化、包管理安装删除、用户管理、防火墙修改、关机。",
        "root": "当前为 root 级：无命令类型限制，但仍需谨慎，"
                "破坏性命令必须附带明确警告。",
    }[lv]
    return f"[AI 权限等级: {lv}] {desc}"


def talk_level_prompt(level: str | None = None) -> str:
    """talk 专用说明：允许模型提议越权命令，由 CLI 决定是否确认。"""
    lv = level or current_level()
    desc = {
        "view": "只读命令可直接执行；写操作属于越权操作，将逐条交给用户确认。",
        "operate": "常规运维命令可直接执行；系统级危险操作属于越权操作，"
                   "将逐条交给用户确认。",
        "root": "所有非空命令可直接执行，不再额外确认。执行破坏性命令前仍应"
                "在回答或命令选择中说明风险。",
    }[lv]
    return f"[talk 全局权限: {lv}] {desc}"

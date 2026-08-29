"""
Shell 历史捕获：让 ordis talk 知道用户刚在本机终端执行过什么。

两层机制：
1. PROMPT_COMMAND 钩子（ordis shellhook 写入 .bashrc）：
   每条命令执行完用 `history 1` 取回刚敲的命令（含打错/失败的），
   连同退出码追加到 ~/.ordis/shell_history.log
2. `ordis watch`：用 script(1) 录制整个会话（命令+输出原文）到
   ~/.ordis/session.log —— talk 优先读这个，能看到完整上下文

安装（每台机器一次性）：
    eval "$(ordis shellhook)" >> ~/.bashrc
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

try:
    from paths import data_home
    HIST_LOG = data_home() / "shell_history.log"
    SESSION_LOG = data_home() / "session.log"
except ImportError:
    HIST_LOG = Path.home() / ".ordis" / "shell_history.log"
    SESSION_LOG = Path.home() / ".ordis" / "session.log"
MAX_ENTRIES = 15          # 最多回溯的命令条数
MAX_CHARS = 6000          # 命令历史注入上限字符
SESSION_TAIL_CHARS = 3500 # 会话录像注入上限字符

SKIP_PATTERNS = ("ordis talk", "ordis setup", "ordis config", "ordis model", "ordis agent",
                 "ordis server", "ordis join", "shellhook",
                 "__ordis_log", "python3 -c")


def install_hook() -> str:
    """返回写入 .bashrc 的钩子代码；确保日志文件存在。"""
    HIST_LOG.parent.mkdir(parents=True, exist_ok=True)
    HIST_LOG.touch(exist_ok=True)
    hist = str(HIST_LOG)
    # 不用 str.format：模板里全是 shell 的 {}，format 会误解析
    return f'''# >>> ordis shell history hook >>>
__ordis_log() {{
  local ec=$?
  local _h
  _h=$(HISTTIMEFORMAT= history 1 2>/dev/null)
  local cmd="${{_h#*[[:space:]][[:digit:]]+[[:space:]]}}"
  # history 输出可能保留前导空格，剥掉
  while [[ "$cmd" == " "* ]]; do cmd="${{cmd# }}"; done
  [ -z "$cmd" ] && return 0
  case "$cmd" in
    *ordis\\ *|*shellhook*|*__ordis_log*) return 0 ;;
  esac
  python3 -c '
import json, sys
print(json.dumps({{"ts": __import__("time").strftime("%F %T"),
                   "cwd": sys.argv[1], "cmd": sys.argv[2],
                   "ec": int(sys.argv[3])}}))
' "$PWD" "$cmd" "$ec" >> {hist} 2>/dev/null
}}
# --ordis 后缀触发 AI 命令补全: `top -tail 20 --ordis` 执行后，
# PROMPT_COMMAND 检测到后缀即自动调 ordis suggest 弹出 AI 候选。
# （bash DEBUG trap 改 BASH_COMMAND 无法阻止原命令执行，已实测放弃）
__ordis_maybe_suggest() {{
  local _h
  _h=$(HISTTIMEFORMAT= history 1 2>/dev/null)
  local cmd="${{_h#*[[:space:]][[:digit:]]+[[:space:]]}}"
  while [[ "$cmd" == " "* ]]; do cmd="${{cmd# }}"; done
  case "$cmd" in
    *"--ordis")
      local frag="${{cmd%--ordis}}"
      while [[ "$frag" == *" " ]]; do frag="${{frag% }}"; done
      [ -z "$frag" ] && return 0
      # 后台弹出补全菜单（不阻塞下一个提示符）
      ordis suggest -- "$frag"
      ;;
  esac
}}
PROMPT_COMMAND="__ordis_log; __ordis_maybe_suggest${{PROMPT_COMMAND:+;$PROMPT_COMMAND}}"
# <<< ordis shell history hook <<<
'''


def rotate(log: Path = HIST_LOG, max_bytes: int = 512 * 1024):
    """日志超过 512KB 时保留后半段，防止无限膨胀。"""
    try:
        if log.exists() and log.stat().st_size > max_bytes:
            text = log.read_text(encoding="utf-8", errors="replace")
            nl = text.find("\n", len(text) // 2)
            log.write_text(text[nl + 1:] if nl >= 0 else "", encoding="utf-8")
    except Exception:
        pass


def _should_skip(cmd: str) -> bool:
    low = cmd.strip().lower()
    return (not low
            or low.startswith("#")
            or any(p in low for p in SKIP_PATTERNS))


def read_recent(limit: int = MAX_ENTRIES,
                max_chars: int = MAX_CHARS) -> list[dict]:
    """
    从 shell_history.log 读最近的命令（时间正序返回）。
    文件不存在/损坏/垃圾行一律跳过，绝不抛异常。
    """
    if not HIST_LOG.exists():
        return []
    try:
        lines = HIST_LOG.read_text(encoding="utf-8",
                                   errors="replace").strip().splitlines()
    except Exception:
        return []
    entries: list[dict] = []
    budget = max_chars
    for line in reversed(lines):
        if len(entries) >= limit or budget <= 0:
            break
        try:
            e = json.loads(line)
            cmd = str(e.get("cmd", ""))[:500].strip()
        except Exception:
            continue
        if _should_skip(cmd):
            continue
        e["cmd"] = cmd
        entries.append(e)
        budget -= len(line) + 1
    entries.reverse()
    return entries


_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[A-Za-z]"      # CSI 序列
    r"|\x1b\][^\x07]*(?:\x07|\x1b\\\\)"  # OSC（窗口标题等）
    r"|\x1b[=>]"                   # 键盘模式
)


def read_session_tail(max_chars: int = SESSION_TAIL_CHARS) -> str | None:
    """
    读 script(1) 会话录像尾部（或 watch 未启用时 None）。
    清理 ANSI 转义与 \\r 回车噪声后返回纯文本。
    """
    if not SESSION_LOG.exists():
        return None
    try:
        raw = SESSION_LOG.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    text = _ANSI_RE.sub("", raw)
    # 合并被 \r 拆散的行
    text = re.sub(r"[ \t]*\n\r?\n*", "\n", text).strip()
    if not text:
        return None
    return text[-max_chars:]


def as_context(entries: list[dict] | None = None) -> str | None:
    """格式化命令历史为注入 system prompt 的文本块；无内容返回 None。"""
    if entries is None:
        entries = read_recent()
    if not entries:
        return None
    lines = []
    for e in entries:
        line = f"$ {e['cmd']}"
        ec = e.get("ec")
        if ec not in (0, "0", None):
            line += f"   # 退出码 {ec}"
        lines.append(line)
    return "\n".join(lines)

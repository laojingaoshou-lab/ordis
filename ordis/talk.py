"""
ordis talk — 自然语言服务器管理。

执行权限遵从 `ordis config permission` 的全局设置：
- view：只读命令直接执行，写操作逐条人工确认
- operate：常规运维命令直接执行，越权操作逐条人工确认
- root：所有非空命令直接执行

协议：模型每轮输出一个 JSON 动作 {"tool":"run","command":...} 或
{"tool":"answer","answer":...}，循环直到给出答案或步数耗尽。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import threading

try:
    from logger import get_logger
    log = get_logger("talk")
except ImportError:
    import logging
    log = logging.getLogger("talk")

MAX_STEPS = 8
CMD_TIMEOUT = 12
OUTPUT_LIMIT = 1500


class TextSpinner:
    """在交互终端等待模型响应时显示轻量文本状态动画。"""

    FRAMES = ("|", "/", "-", "\\")

    def __init__(self, label: str = "AI 正在生成回复", enabled: bool = True,
                 interval: float = 0.12, stream=None):
        self.label = label
        self.interval = interval
        self.stream = stream or sys.stdout
        self.status = ""
        self.enabled = enabled and bool(
            getattr(self.stream, "isatty", lambda: False)())
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self._stopped = False

    def __enter__(self):
        self.resume()
        return self

    def __exit__(self, *_):
        self.stop()

    def update(self, status: str):
        """更新当前状态；动画线程会在下一帧使用新状态。"""
        with self._lock:
            self.status = status

    def stop(self):
        """停止动画并清除当前行，允许在上下文结束前提前清理。"""
        if not self.enabled or self._stopped:
            return
        self._stopped = True
        self.pause()

    def pause(self):
        """临时暂停并清除动画，用于显示人工确认提示。"""
        if not self.enabled or not self._thread:
            return
        self._stop.set()
        self._thread.join(timeout=max(self.interval * 2, 0.2))
        self._thread = None
        self.stream.write("\r\033[2K")
        self.stream.flush()

    def resume(self):
        """在人工确认结束后恢复动画。"""
        if (not self.enabled or self._stopped or
                (self._thread and self._thread.is_alive())):
            return
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._animate, daemon=True)
        self._thread.start()

    def _animate(self):
        # 避免模型瞬时返回时闪过一帧无意义的状态文本。
        if self._stop.wait(self.interval):
            return
        index = 0
        while not self._stop.is_set():
            with self._lock:
                label = self.label
                status = self.status
            text = f"{label} {status}".rstrip()
            self.stream.write(
                f"\r{text} {self.FRAMES[index % len(self.FRAMES)]}")
            self.stream.flush()
            index += 1
            if self._stop.wait(self.interval):
                break


def format_error(exc: Exception, limit: int = 300) -> str:
    """把请求或模型异常压缩成适合终端展示的一行原因。"""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    reason = ""

    if status is not None:
        # API 错误通常是 {"error": {"message": "..."}}，也兼容常见网关格式。
        payload = None
        try:
            payload = response.json()
        except Exception:
            pass
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                reason = error.get("message") or error.get("detail") or ""
            elif error:
                reason = str(error)
            reason = reason or str(payload.get("message") or
                                  payload.get("detail") or "")
        if not reason:
            reason = str(getattr(response, "reason", "") or "")
        reason = f"HTTP {status}: {reason}" if reason else f"HTTP {status}"
    else:
        # 异常文本可能包含换行（例如底层网络错误），只保留第一行。
        reason = str(exc).splitlines()[0].strip() if str(exc).strip() else ""
        if not reason:
            reason = "请求失败（无详细原因）"

    # 防止异常文本中的控制字符破坏终端排版，并遮蔽常见凭据格式。
    reason = " ".join(reason.split())
    reason = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1***", reason)
    reason = re.sub(
        r"(?i)(api[_-]?key|token)(\s*[=:]\s*)[^\s,;]+",
        r"\1\2***",
        reason,
    )
    if len(reason) > limit:
        reason = reason[:limit - 1].rstrip() + "…"
    return reason

# ── 只读命令白名单 ──────────────────────────────────────────────
# 二进制名 → 允许的子命令集合；None 表示不限制参数
READONLY_ALLOW = {
    "df": None, "free": None, "ps": None, "ss": None, "top": {"-b", "-n"},
    "uptime": None, "lscpu": None, "lsblk": None, "uname": None,
    "hostname": None, "date": None, "who": None, "w": None,
    "cat": None, "ls": None, "head": None, "tail": None, "wc": None,
    "grep": None, "du": None, "stat": None, "find": None, "which": None,
    "vmstat": None, "iostat": None, "netstat": None, "ip": None,
    "ping": {"-c", "-w", "-W"},
    "dmesg": None, "journalctl": None,
    "systemctl": {"status", "show", "is-active", "is-enabled",
                  "list-units", "list-timers", "list-unit-files",
                  "--failed", "--no-pager"},
    "docker": {"ps", "images", "logs", "inspect", "stats", "top",
               "--no-stream"},
    "pm2": {"jlist", "ls", "list", "describe", "logs", "prettylist"},
    "kubectl": {"get", "describe", "logs", "top"},
}
# find 危险参数单独拦（可删文件）
_FIND_BLOCK = ("-delete", "-exec", "-execdir", "-ok", "-fprint")


def bin_of(cmd: str) -> str:
    """取命令的裸二进制名，兼容绝对路径。"""
    first = cmd.strip().split()[0] if cmd.strip() else ""
    return first.rsplit("/", 1)[-1]


def readonly_check(cmd: str) -> tuple[bool, str]:
    """返回 (是否允许, 拒绝原因)。白名单制：不在名单=拒绝。"""
    tokens = cmd.strip().split()
    if not tokens:
        return False, "空命令"
    # 管道/重定向/命令替换一律视为非只读（右侧命令不受白名单控制）
    if any(ch in cmd for ch in ("|", ">", "<", "`", "$(")):
        return False, "包含管道/重定向/命令替换"
    b = bin_of(cmd)
    allowed = READONLY_ALLOW.get(b)
    if allowed is None and b not in READONLY_ALLOW:
        return False, f"'{b}' 不在只读白名单"
    if isinstance(allowed, set):
        subs = [t for t in tokens[1:] if not t.startswith("-")]
        if subs and subs[0] not in allowed and not subs[0][:1].isdigit():
            return False, f"'{b} {subs[0]}' 不在允许的子命令中"
    if b == "find" and any(t in _FIND_BLOCK for t in tokens):
        return False, "find 含危险参数"
    return True, ""


def execution_policy(cmd: str, level: str) -> tuple[str, str, bool]:
    """返回 (execute|confirm|reject, 原因, 是否只读)。"""
    cmd = (cmd or "").strip()
    if not cmd:
        return "reject", "空命令", False

    readonly, readonly_reason = readonly_check(cmd)
    if readonly:
        return "execute", "", True
    if level == "root":
        return "execute", "", False
    if level == "view":
        return "confirm", "超出 view 级权限（只允许直接执行只读命令）", False

    import ai_levels
    allowed, level_reason = ai_levels.check_allowed(cmd, level)
    if allowed:
        return "execute", "", False
    return "confirm", level_reason or readonly_reason, False


def run_command(cmd: str, timeout: int = CMD_TIMEOUT) -> str:
    """执行 shell 命令并返回截断后的输出。任何异常都转为文本观察。"""
    try:
        # errors='replace'：Windows 中文终端常出 GBK 字节流，硬解 utf-8 会崩
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        out = (r.stdout or "")
        err = (r.stderr or "")
        text = (out + ("\n[stderr] " + err if err else "")).strip()
    except subprocess.TimeoutExpired:
        text = f"[超时 {timeout}s，已终止]"
    except Exception as e:
        text = f"[执行异常] {e}"
    if len(text) > OUTPUT_LIMIT:
        text = text[:OUTPUT_LIMIT] + f"\n…(截断，共 {len(text)} 字符)"
    return text or "(无输出)"


# ── Prompt ─────────────────────────────────────────────────────
SYSTEM_PROMPT = """你是 Ordis，一台 Linux 服务器上的 SRE 助手，通过工具与服务器交互。

每轮严格输出一个 JSON 对象（不要输出其他内容）：
取证: {"tool": "run", "command": "<一条 shell 命令>"}
回答: {"tool": "answer", "answer": "<面向用户的完整回答，中文>"}

规则：
1. 命令按当前全局 AI 权限执行；级别内命令直接执行，越权命令会交给用户确认。
   用户要求变更时应输出准确的 run 动作，不要只在 answer 里罗列命令。
2. 每个 run 动作只包含一条完整命令，不要通过嵌套 shell 绕过权限判断。
3. 先取证再下结论，通常 1~4 条命令足够。
4. 回答要直接给结论 + 关键证据数字，不要罗列原始输出。
5. JSON 中路径含空格时用双引号包裹整个路径，不要用反斜杠转义。"""


# ── 终端编码兼容 ────────────────────────────────────────────────
def _setup_stdio():
    """
    防 UnicodeDecodeError 崩溃：
    - stdin 用 surrogateescape 保住非 UTF-8 字节（Windows SSH 客户端常发 GBK）
    - stdout/stderr 用 replace 保证任何 AI 输出都能打印
    """
    import sys
    for stream, err in ((sys.stdin, "surrogateescape"),
                        (sys.stdout, "replace"),
                        (sys.stderr, "replace")):
        try:
            stream.reconfigure(errors=err)
        except Exception:
            pass


def _salvage(text: str | None) -> str | None:
    """
    还原被 surrogateescape 保住的原始字节：优先按 GBK/gb18030 再解码。
    纯 UTF-8 输入原样返回。
    """
    if not text:
        return text
    has_special = ("\ufffd" in text
                   or any(0xD800 <= ord(c) <= 0xDFFF for c in text))
    if not has_special:
        return text
    try:
        raw = text.encode("utf-8", "surrogateescape")
    except Exception:
        return text
    for enc in ("gbk", "gb18030"):
        try:
            fixed = raw.decode(enc)
            if "\ufffd" not in fixed:
                return fixed
        except (UnicodeDecodeError, ValueError):
            continue
    return text


def context_brief() -> str:
    """注入 Ordis 自身视角：shell 历史/会话录像 + 纳管服务 + 最近事件。"""
    parts = []
    # 用户刚在本机终端做过什么：优先会话录像（含输出），其次命令历史
    try:
        import shell_history
        shell_history.rotate()
        session = shell_history.read_session_tail()
        if session:
            parts.append("用户当前终端会话的最近记录(命令与输出，供理解"
                         "工作背景，可能含乱码噪声):\n" + session)
        else:
            hist = shell_history.as_context()
            if hist:
                parts.append("用户在本机终端最近执行的命令(供理解当前"
                             "工作背景):\n" + hist)
    except Exception:
        pass
    try:
        from collectors.discovery import collector as disco
        svcs = disco.collect().get("services", [])
        if svcs:
            lines = [f"- {s['manager']}:{s['target']} ports={s['ports']}"
                     for s in svcs[:10]]
            parts.append("本机服务(自动发现):\n" + "\n".join(lines))
    except Exception:
        pass
    try:
        from engine import load_events
        evs = load_events()[:5]
        if evs:
            lines = [f"- [{e['time']}] {e['rule']}" for e in evs]
            parts.append("最近告警事件:\n" + "\n".join(lines))
    except Exception:
        pass
    return "\n\n".join(parts)


# ── 会话 ───────────────────────────────────────────────────────
class TalkSession:
    def __init__(self, quiet: bool = False):
        import ai_diagnose
        import ai_levels
        self.ai = ai_diagnose
        self.level = ai_levels.current_level()
        base, model, key = ai_diagnose._resolve_api_cfg()
        self.model = model
        self.key = key
        self.base = base
        self.quiet = quiet
        brief = context_brief()
        sys_prompt = (SYSTEM_PROMPT + "\n" +
                      ai_levels.talk_level_prompt(self.level))
        if brief:
            sys_prompt += "\n\n当前服务器上下文:\n" + brief
        self.messages = [{"role": "system", "content": sys_prompt}]

    def _chat(self, status=None) -> dict:
        import requests
        if status is None:
            with TextSpinner(enabled=not self.quiet) as own_status:
                return self._chat(status=own_status)

        status.update("连接模型")
        resp = requests.post(
            f"{self.base}/chat/completions",
            headers={"Authorization": f"Bearer {self.key}",
                     "Content-Type": "application/json"},
            json={"model": self.model, "temperature": 0.2,
                  "max_tokens": 1200, "messages": self.messages},
            timeout=self.ai.TIMEOUT,
            stream=True)
        resp.raise_for_status()
        # stream=True 会在响应头到达后返回，此后读取正文属于模型生成阶段。
        status.update("分析中")
        text = resp.json()["choices"][0]["message"]["content"] or ""
        try:
            result = self.ai._extract_json(text)
            status.update("整理回复")
            return result
        except Exception:
            # 模型输出了非法 JSON：把原文喂回去让它重说，绝不让对话循环崩掉
            self.messages.append({"role": "assistant", "content": text[:500]})
            self.messages.append({"role": "user", "content":
                                  "[解析失败] 你的上一条输出不是合法 JSON。"
                                  "重新输出，严格按协议: "
                                  '{"tool":"run","command":"..."} 或 '
                                  '{"tool":"answer","answer":"..."}'})
            return {"tool": "_retry"}

    def step(self, user_text: str) -> bool:
        """处理一句用户输入，返回是否以 answer 结束。"""
        self.messages.append({"role": "user", "content": user_text})
        blocked_times: dict[str, int] = {}   # 同命令拦截计数（防模型死循环重试）
        with TextSpinner(enabled=not self.quiet) as status:
            for _ in range(MAX_STEPS):
                try:
                    action = self._chat(status=status)
                except Exception as exc:
                    status.stop()
                    print(f"AI 请求失败: {format_error(exc)}")
                    return False
                tool = action.get("tool")
                if tool == "_retry":
                    continue  # 解析失败已回喂模型，消耗一步重试
                if tool == "answer":
                    status.update("整理回复")
                    status.stop()
                    print(action.get("answer", "(空回答)"))
                    self.messages.append(
                        {"role": "assistant",
                         "content": json.dumps(action, ensure_ascii=False)})
                    return True
                if tool == "run":
                    cmd = (action.get("command") or "").strip()
                    policy, reason, readonly = execution_policy(cmd, self.level)
                    executed = policy == "execute"
                    if policy == "confirm":
                        status.pause()
                        executed = self._confirm(cmd, reason)
                        status.resume()
                    if executed:
                        status.update("执行命令")
                        print(f"$ {cmd}") if not self.quiet else None
                        from audit import log_audit
                        log_audit("ai_exec", {
                            "command": cmd,
                            "readonly": readonly,
                            "level": self.level,
                            "escalated": policy == "confirm",
                        })
                        obs = run_command(cmd)
                        blocked_times.pop(cmd, None)
                    else:
                        blocked_times[cmd] = blocked_times.get(cmd, 0) + 1
                        n = blocked_times[cmd]
                        prefix = "[未执行]" if policy == "confirm" else "[已拦截]"
                        obs = f"{prefix} {reason}"
                        if n >= 2:
                            obs += (f"\n[系统] 命令已被拒绝 {n} 次，禁止再尝试。"
                                    "立即输出 answer 总结已有信息。")
                        if n >= 3:
                            status.stop()
                            print(obs) if not self.quiet else None
                            print("(同一命令三次被拦截，熔断)")
                            return False
                        print(obs) if not self.quiet else None
                    self.messages.append({"role": "assistant", "content":
                                          json.dumps(action, ensure_ascii=False)})
                    self.messages.append({"role": "user",
                                          "content": f"[命令输出]\n{obs}"})
                    continue
                # 无法理解的输出
                self.messages.append({"role": "assistant", "content":
                                      json.dumps(action, ensure_ascii=False)[:500]})
                self.messages.append(
                    {"role": "user",
                     "content": "[格式错误] 请按协议输出 {\"tool\": ...} JSON"})
            status.stop()
        print(f"(已达最大取证步数 {MAX_STEPS}，停止)")
        return False

    @staticmethod
    def _confirm(cmd: str, reason: str) -> bool:
        """越权命令的人工确认闸门。"""
        if not sys.stdin.isatty():
            print(f"[拒绝] '{cmd}' 需要越权确认({reason})，"
                  "非交互终端无法确认")
            return False
        try:
            ans = input(f"[越权操作] {cmd}\n  原因: {reason}\n  执行? [y/N] ")
        except EOFError:
            return False
        return ans.strip().lower() in ("y", "yes")

    def repl(self):
        _setup_stdio()
        print(f"Ordis 对话模式 [权限: {self.level}]（Ctrl+C 或 exit 退出）")
        while True:
            try:
                line = input("你> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            except UnicodeDecodeError:
                # 兜底：极老终端仍可能抛出，跳过该行而不是崩掉
                print("(该行输入包含无法解码的字符，已跳过)")
                continue
            line = _salvage(line) or ""
            if not line:
                continue
            if line.lower() in ("exit", "quit", "q"):
                break
            self.step(line)


def main(question: str | None = None, quiet: bool = False):
    _setup_stdio()
    question = _salvage(question)
    try:
        s = TalkSession(quiet=quiet)
    except RuntimeError as e:
        # 未配置 AI 供应商等启动期错误：给一句话提示而非裸 traceback
        print(f"AI 未就绪: {e}")
        raise SystemExit(1)
    if question:
        s.step(question)
    else:
        s.repl()

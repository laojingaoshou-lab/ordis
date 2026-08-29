"""
orids ai 补全：`--ordis` 触发的 AI 命令补全。

用法（bash 快捷绑定）：
    top -tail 20 --ordis
    → AI 根据上下文（当前目录、最近命令、系统状态）推测意图，
      给出 3 个候选命令，键入数字选择后回车执行。

安全约束：
    候选命令执行前展示全文，回车确认才跑；默认只生成只读类命令，
    含写操作标记 [写操作] 提示，由用户自行判断。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

try:
    from logger import get_logger
    log = get_logger("ai_suggest")
except ImportError:
    import logging
    log = logging.getLogger("ai_suggest")

MAX_HISTORY = 10

def _gather_context(raw_input: str) -> dict:
    """收集推测所需的上下文：输入、shell 历史、cwd、主机名。"""
    ctx: dict = {"input": raw_input, "cwd": os.getcwd()}
    try:
        import socket
        ctx["host"] = socket.gethostname()
    except Exception:
        ctx["host"] = ""
    # 最近 shell 历史（PROMPT_COMMAND 钩子记录）
    try:
        from shell_history import read_recent
        hist = [e.get("cmd", "") for e in read_recent(limit=MAX_HISTORY)]
        if hist:
            ctx["recent_commands"] = hist
    except Exception:
        pass
    return ctx


SYSTEM_PROMPT = """你是 Linux 命令行 AI 补全助手。用户输入了一条不完整/可能有误的
命令片段（如 `top -tail 20`），请推测其真实意图，给出最可能的 2~3 条完整可执行的命令。

要求：
1. 只输出 JSON 数组，不要其他内容：
[{"cmd": "<完整命令>", "explain": "<一句话说明，中文>"}]
2. 命令必须可直接在 bash 执行；优先修正用户参数错误（top 没有 -tail，应为 -n 次数）。
3. 第一条给最可能的意图，其余给合理变体。
4. 若上下文里有最近命令/当前目录，优先推测与当前工作相关的意图。"""


def suggest(raw_input: str, retries: int = 2) -> list[dict]:
    """调用 LLM 返回候选命令列表 [{cmd, explain}]。失败返回空列表。"""
    import ai_diagnose

    ctx = _gather_context(raw_input)
    user_prompt = ("## 用户输入的命令片段\n```\n" + raw_input + "\n```\n"
                   "## 上下文\n```json\n"
                   + json.dumps(ctx, ensure_ascii=False, indent=2)
                   + "\n```")
    try:
        with_ai = ai_diagnose._resolve_api_cfg()
        base, model, key = with_ai
    except Exception as e:
        log.warning("AI 未配置: %s", e)
        return []
    import requests
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = requests.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"},
                json={"model": model, "temperature": 0.2, "max_tokens": 1500,
                      "messages": [{"role": "system",
                                    "content": SYSTEM_PROMPT},
                                   {"role": "user",
                                    "content": user_prompt}]},
                timeout=ai_diagnose.TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            if data["choices"][0].get("finish_reason") == "length":
                # 输出被截断：加大 token 重试
                raise ValueError("truncated-output")
            text = data["choices"][0]["message"]["content"] or ""
            try:
                result = ai_diagnose._extract_json(text)
                # 兼容模型直接返回数组
                if isinstance(result, list):
                    items = result
                else:
                    items = (result.get("candidates")
                             or result.get("commands") or [])
            except ValueError:
                # 补全协议本来就是数组，_extract_json 拒数组时手动解析
                cleaned = text.strip()
                for pre in ("```json", "```"):
                    if cleaned.startswith(pre):
                        cleaned = cleaned[len(pre):]
                        break
                cleaned = cleaned.strip().removesuffix("```").strip()
                items = json.loads(cleaned)
            out = []
            for it in items:
                if isinstance(it, dict) and it.get("cmd"):
                    out.append({"cmd": str(it["cmd"]).strip(),
                                "explain": str(it.get("explain", ""))[:120]})
            if out:
                # 权限分级过滤：L0 view 剔除一切写操作，L1 剔除系统级危险动作
                import ai_levels
                return ai_levels.filter_candidates(out)[:3]
            raise ValueError("empty candidates")
        except (KeyboardInterrupt, SystemExit):
            # LLM 等待期间 Ctrl+C：安静退出，不打 Traceback
            print("\n(已中断)")
            return []
        except Exception as e:
            last_err = e
            log.warning("AI 补全第%d次尝试失败: %s", attempt + 1, e)
            try:
                time.sleep(1)
            except KeyboardInterrupt:
                print("\n(已中断)")
                return []
    log.warning("AI 补全失败: %s", last_err)
    return []


WRITE_HINTS = re.compile(
    r"\b(rm|mv|mkfs|dd|shutdown|reboot|kill|pkill|killall|chmod|chown"
    r"|systemctl\s+(start|stop|restart|disable)|docker\s+(rm|rmi|stop|restart"
    r"|prune)|pm2\s+(delete|stop|kill)|apt|yum|dnf|pip|npm\s+install)\b")


def is_write(cmd: str) -> bool:
    return bool(WRITE_HINTS.search(cmd))


# ── 交互选择 ────────────────────────────────────────────────────
def choose(candidates: list[dict]) -> str | None:
    """打印候选项，返回用户选择的命令；放弃返回 None。"""
    print()
    for i, c in enumerate(candidates, 1):
        tag = "  [写操作]" if is_write(c["cmd"]) else ""
        print(f"  {i}. {c['cmd']}{tag}")
        print(f"     {c['explain']}")
    print()
    try:
        ans = input("选择执行 (1-3, 回车取消): ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if not ans or not ans.isdigit() or not (1 <= int(ans) <= len(candidates)):
        return None
    return candidates[int(ans) - 1]["cmd"]


def confirm_exec(cmd: str) -> bool:
    if not sys.stdin.isatty():
        print("[非交互终端，跳过执行]")
        return False
    try:
        ans = input(f"执行: {cmd}\n确认? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return ans in ("y", "yes")


def run_selected(cmd: str):
    """执行选中的命令（透传终端，交互式命令也能用）。"""
    print()
    rc = subprocess.call(cmd, shell=True)
    sys.exit(rc)

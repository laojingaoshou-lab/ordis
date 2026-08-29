"""
AI 诊断模块：规则修复失败时，调用 LLM 分析故障根因并给出修复方向。

调度策略（防 token 失控 + 不阻塞主循环）：
- 触发条件：仅当规则触发且修复失败 / 无修复器时
- 去重闸门：同指纹 30 分钟内只诊断一次
- 每日限额：默认不限；ORDIS_AI_DAILY_LIMIT > 0 时启用
- 异步执行：后台线程调用 LLM，主循环不等它
"""

from __future__ import annotations
import json
import os
import re
import threading
import time
from datetime import datetime, date
from pathlib import Path

import requests

try:
    from logger import get_logger
    log = get_logger("ai_diagnose")
except ImportError:  # 允许独立运行/测试
    import logging
    log = logging.getLogger("ai_diagnose")

# ── 配置（环境变量可覆盖）───────────────────────────────────────
API_BASE = os.environ.get("ORDIS_AI_BASE", "https://api.openai.com/v1")
API_KEY_ENV = "MODEFLARE_API_KEY"
MODEL = os.environ.get("ORDIS_AI_MODEL", "gpt-5.6-terra")
TIMEOUT = int(os.environ.get("ORDIS_AI_TIMEOUT", "120"))
DEDUP_WINDOW = 30 * 60        # 同指纹去重窗口（秒）
try:
    DAILY_LIMIT = max(0, int(os.environ.get("ORDIS_AI_DAILY_LIMIT", "0")))
except ValueError:
    DAILY_LIMIT = 0
    log.warning("ORDIS_AI_DAILY_LIMIT 不是有效整数，按不限次数处理")

_lock = threading.Lock()
# 进行中的诊断指纹（防同指纹并发竞态重复调度）
_inflight: set[str] = set()
# 并发 LLM 调用上限（防异指纹洪泛打满 API 配额）
_llm_semaphore = threading.Semaphore(2)

# ── Prompt ─────────────────────────────────────────────────────
SYSTEM_PROMPT = """你是一名资深 Linux/Kubernetes 运维专家（SRE）。监控系统采集到异常，
自动修复器处理失败。请根据提供的证据分析故障。

当提示中给出“允许的单条修复命令”时，该命令已由 Ordis 根据探测器 finding
和管理员允许列表收窄。必须将它原样写入 fix_command，不要改成查看日志等诊断命令，
也不要因为根因尚未完全确定而留空。命令仍会由 Ordis 二次校验并进行效果回检。

如果 source 是 kubernetes，修复命令必须准确包含证据里的资源类型、资源名和命名空间。
Deployment 镜像明显错误时，优先使用单条 `kubectl set image deployment/<name>
<container>=<correct-image> -n <namespace>`；不要使用管道、重定向、命令组合或交互命令。
若 evidence.annotations 包含 `ordis.ai/repair-image`，它是管理员提供的目标镜像，
必须把当前容器名和该镜像用于 fix_command，不要自行猜测其他镜像。

严格按以下 JSON 格式输出，不要输出任何其他内容：
{
  "fingerprint": {"type": "<故障类型英文标识，如 port_dead/disk_full/cpu_overload>",
                   "service": "<涉及服务名或端口，无则填 unknown>"},
  "severity": "<low|medium|high|critical>",
  "root_cause": "<最可能的根因，一句话，中文>",
  "evidence": ["<支持该判断的证据点1>", "<证据点2>"],
  "fix_direction": "<建议的修复方向，中文说明>",
  "fix_command": "<修复该故障的一条可直接执行的命令，如 systemctl restart nginx。"
                 "注意：必须是修复动作本身，不能是查看/诊断类命令"
                 "（ss/grep/journalctl/cat 等只读命令不算修复）；"
                 "若当前证据不足以确定修复命令，填空字符串>",
  "confidence": <0到1之间的数字>
}"""


# ── 存取 ────────────────────────────────────────────────────────
def load_cases() -> list[dict]:
    """兼容层：从数据库加载案例，返回旧格式供测试使用。"""
    import db
    cases = db.get_recent_cases(limit=200)
    result = []
    for c in cases:
        # 将数据库格式转换为旧格式
        diag_str = c.get("diagnosis")
        if diag_str:
            try:
                diagnosis = json.loads(diag_str)
            except Exception:
                diagnosis = None
        else:
            diagnosis = None

        created_at = c.get("created_at", "")
        # created_at 是时间戳数字，转换为字符串
        if isinstance(created_at, (int, float)):
            from datetime import datetime
            time_str = datetime.fromtimestamp(created_at).strftime("%Y-%m-%d %H:%M:%S")
            date_str = time_str[:10]
        else:
            time_str = str(created_at)
            date_str = time_str[:10] if time_str else ""

        result.append({
            "_key": c.get("fingerprint", ""),
            "_date": date_str,
            "time": time_str,
            "rule": c.get("message", "").split(" | ")[0] if " | " in c.get("message", "") else "",
            "collector": c.get("message", "").split(" | ")[1] if " | " in c.get("message", "") else "",
            "diagnosis": diagnosis,
            "error": c.get("error"),
        })
    return result


def _save_cases(cases: list[dict]):
    """兼容层：测试 mock 点保留。实际已不使用。"""
    pass



def _fingerprint_key(fp: dict) -> str:
    """指纹归一化 key，用于去重匹配。"""
    return f"{fp.get('type', 'unknown')}:{fp.get('service', 'unknown')}"


# ── 调度闸门 ────────────────────────────────────────────────────
def _recently_diagnosed(fp_key: str) -> bool:
    import db
    return db.case_exists(fp_key, within_seconds=DEDUP_WINDOW)


def _daily_count_exceeded() -> bool:
    if DAILY_LIMIT <= 0:
        return False
    import db
    from datetime import datetime
    today = date.today()
    # 今天 0 点的时间戳
    today_start = datetime.combine(today, datetime.min.time()).timestamp()

    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM cases WHERE created_at >= ?",
            (today_start,)
        ).fetchone()
        count = row["cnt"] if row else 0
    return count >= DAILY_LIMIT


# ── LLM 调用 ───────────────────────────────────────────────────
def _resolve_api_cfg(base: str | None = None, model: str | None = None,
                     api_key: str | None = None) -> tuple[str, str, str]:
    """
    解析 API 配置。优先级：
    显式参数 > ~/.ordis/model.json 激活供应商（ordis config model）> 环境变量兜底
    """
    cfg = None
    try:
        from model_config import load_active
        cfg = load_active()
    except Exception:
        pass
    base = base or (cfg or {}).get("base_url") or API_BASE
    model = model or (cfg or {}).get("model") or MODEL
    api_key = api_key or (cfg or {}).get("api_key") or os.environ.get(API_KEY_ENV, "")
    if not api_key:
        raise RuntimeError(
            f"AI 供应商未配置：运行 `ordis setup` 或 `ordis config model` 添加，"
            f"或设置环境变量 {API_KEY_ENV}")
    return base, model, api_key


def _build_user_prompt(rule_name: str, value: dict, heal_result: dict | None,
                       host_logs: str | None) -> str:
    parts = [
        f"## 触发规则\n{name_safe(rule_name)}",
        f"## 采集器数据\n```json\n{json.dumps(value, ensure_ascii=False, indent=2)}\n```",
    ]
    if heal_result:
        parts.append(f"## 自动修复结果（已失败）\n```json\n"
                     f"{json.dumps(heal_result, ensure_ascii=False, indent=2)}\n```")
    else:
        parts.append("## 自动修复结果\n该异常无对应修复器，未执行任何修复动作。")
    command_hint = _repair_command_hint(value)
    if command_hint:
        parts.append(
            "## 自动修复接管约束\n"
            f"允许的单条修复命令：`{command_hint}`\n"
            "请将该命令原样写入 fix_command；不要返回 journalctl、status、get、"
            "describe 等查看命令。")
    if host_logs:
        parts.append(f"## 相关日志尾部\n```\n{host_logs[-2000:]}\n```")
    return "\n\n".join(parts)


def _repair_command_hint(value: dict) -> str | None:
    """Derive the one command already permitted by a detector-scoped finding."""
    source = str(value.get("source") or "")
    reason = str(value.get("reason") or "")
    name = str(value.get("name") or "")
    if (source == "traditional" and reason == "systemd_unit_failed"
            and re.fullmatch(r"[A-Za-z0-9@_.:-]+\.service", name)):
        return f"systemctl restart {name}"

    if (source != "kubernetes" or value.get("kind") != "Deployment"
            or reason != "replicas_unavailable"):
        return None
    namespace = str(value.get("namespace") or "default")
    evidence = value.get("evidence") or {}
    annotations = evidence.get("annotations") or {}
    target_image = str(annotations.get("ordis.ai/repair-image") or "")
    images = evidence.get("images") or {}
    if len(images) != 1:
        return None
    container = str(next(iter(images)))
    dns_name = r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?"
    if (not re.fullmatch(dns_name, namespace)
            or not re.fullmatch(dns_name, name)
            or not re.fullmatch(dns_name, container)
            or not target_image or any(ch.isspace() for ch in target_image)
            or any(ch in target_image for ch in ";|&`$<>")):
        return None
    return (f"kubectl set image deployment/{name} "
            f"{container}={target_image} -n {namespace}")


def name_safe(name: str) -> str:
    return name


def _raw_chat(user_prompt: str, base: str | None = None, model: str | None = None,
              api_key: str | None = None) -> str:
    """最小化对话调用，返回原始文本（供连通性测试用，不校验 JSON）。"""
    base, model, api_key = _resolve_api_cfg(base, model, api_key)
    base = str(base).rstrip("/")
    resp = requests.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        json={"model": model, "max_tokens": 100,
              "messages": [{"role": "user", "content": user_prompt}]},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    try:
        payload = resp.json()
    except ValueError as exc:
        status = getattr(resp, "status_code", "?")
        body = str(getattr(resp, "text", "") or "").strip()
        body = re.sub(r"\s+", " ", body)[:180]
        raise RuntimeError(
            f"模型接口返回非 JSON（HTTP {status}）：{body or '空响应'}") from exc
    try:
        return payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("模型接口 JSON 缺少 choices[0].message.content") from exc


def _safe_serialize(obj):
    """把任意采集器输出转成可 JSON 序列化、体积可控的结构。"""
    if obj is None:
        return None
    try:
        json.dumps(obj)
        return obj
    except Exception:
        pass
    try:
        s = json.dumps(obj, default=str)
        return json.loads(s)
    except Exception:
        return {"unserializable": str(obj)[:200]}


def _extract_json(text: str) -> dict:
    """
    从 LLM 输出提取 JSON 对象。策略：
    1. 直接解析（模型最听话时）
    2. 剥掉 markdown 代码围栏后解析
    3. 括号平衡扫描：找到第一个完整的 {...}（比贪婪正则抗噪）
    """
    text = text.strip()
    if text.startswith("["):
        raise ValueError(f"顶层是数组而非对象，拒绝: {text[:120]}")
    for candidate in (text,
                      text.removeprefix("```json").removeprefix("```")
                          .removesuffix("```").strip()):
        try:
            result = json.loads(candidate)
            if isinstance(result, dict):
                return result
        except Exception:
            pass
    # 括号平衡扫描（字符串内的引号/转义感知）
    depth = 0
    start = None
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    return json.loads(text[start:i + 1])
                except Exception:
                    start = None  # 这一段不是合法 JSON，继续找下一个
    raise ValueError(f"无法从输出提取 JSON: {text[:200]}")


def _call_llm(user_prompt: str, base: str | None = None, model: str | None = None,
              api_key: str | None = None) -> dict:
    """调用 OpenAI 兼容端点，解析结构化输出。失败抛异常。"""
    base, model, api_key = _resolve_api_cfg(base, model, api_key)

    resp = requests.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
            "max_tokens": 1500,
        },
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    text = resp.json()["choices"][0]["message"]["content"]

    result = _extract_json(text)

    # 必要字段校验 + 类型校验（LLM 可能返回数字/空串等垃圾值）
    required = ["fingerprint", "root_cause", "fix_direction"]
    missing = [k for k in required if k not in result]
    if missing:
        raise ValueError(f"LLM 输出缺少字段: {missing}")
    if not isinstance(result["fingerprint"], dict):
        raise ValueError("fingerprint 应为对象")
    for k in ("root_cause", "fix_direction"):
        v = result[k]
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"字段 {k} 应为非空字符串，实际: {v!r}")
    # fix_command 可选：非字符串/空白一律归一化为 None（兼容不输出的模型）
    fc = result.get("fix_command")
    result["fix_command"] = fc.strip() if isinstance(fc, str) and fc.strip() else None
    try:
        conf = float(result.get("confidence", 0.5))
    except (TypeError, ValueError):
        conf = 0.5
    result["confidence"] = max(0.0, min(1.0, conf))
    return result


# ── 日志采集（增强诊断证据）────────────────────────────────────
def _collect_evidence(collector_name: str, value: dict) -> str | None:
    """根据故障类型抓取相关日志尾部，给 LLM 更多上下文。"""
    import subprocess

    def _tail(cmd: list[str], n: int = 30) -> str | None:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            return r.stdout if r.returncode == 0 else None
        except Exception:
            return None

    try:
        # 各类异常统一抓 journal 错误级日志作为证据；process 类额外带 pm2 状态
        evidence = []
        if (collector_name == "traditional"
                and value.get("reason") == "systemd_unit_failed"):
            unit = str(value.get("name") or "")
            if re.fullmatch(r"[A-Za-z0-9@_.:-]+\.service", unit):
                status = _tail(
                    ["systemctl", "status", unit, "--no-pager", "-n", "20"])
                journal = _tail(
                    ["journalctl", "-u", unit, "-n", "30", "--no-pager"])
                if status:
                    evidence.append(status)
                if journal:
                    evidence.append(journal)
        journ = _tail(["journalctl", "-p", "err", "-n", "20", "--no-pager"])
        if journ:
            evidence.append(journ)
        if collector_name == "process":
            pm2 = _tail(["pm2", "jlist"])
            if pm2:
                evidence.append(pm2[:1500])
        return "\n\n".join(evidence) if evidence else None
    except Exception:
        return None
# ── 主入口：异步调度 ───────────────────────────────────────────
def request_diagnosis_async(rule_name: str, collector_name: str, value: dict,
                            heal_result: dict | None,
                            fingerprint_key: str | None = None):
    """
    非阻塞入口：engine.py 在修复失败后调用此函数。
    通过全部闸门后在后台线程执行 LLM 诊断，结果写入 cases.json。
    """
    # 预估指纹（用规则名 + 采集器做粗粒度去重；LLM 会给出更精确的指纹）
    fp_key = fingerprint_key or f"{collector_name}:{rule_name}"

    with _lock:
        if _recently_diagnosed(fp_key):
            log.info("AI诊断跳过：指纹 %s 在 %d 分钟内已诊断", fp_key,
                     DEDUP_WINDOW // 60)
            return
        if DAILY_LIMIT > 0 and _daily_count_exceeded():
            log.warning("AI诊断跳过：已达每日限额 %d 次", DAILY_LIMIT)
            return
        if fp_key in _inflight:
            log.info("AI诊断跳过：指纹 %s 正在诊断中", fp_key)
            return
        _inflight.add(fp_key)

    logs = _collect_evidence(collector_name, value)

    def _release():
        with _lock:
            _inflight.discard(fp_key)

    t = threading.Thread(
        target=_diagnose_worker,
        args=(fp_key, rule_name, collector_name, value, heal_result, logs),
        kwargs={"on_done": _release},
        daemon=True,
    )
    t.start()


def wait_for_pending(timeout: float | None = None) -> bool:
    """Wait for async diagnoses; intended for deterministic one-shot checks."""
    deadline = time.monotonic() + (timeout if timeout is not None else TIMEOUT * 2 + 10)
    while time.monotonic() < deadline:
        with _lock:
            if not _inflight:
                return True
        time.sleep(0.2)
    return False


def _diagnose_worker(fp_key, rule_name, collector_name, value, heal_result,
                     logs, on_done=None):
    """后台线程：调 LLM、落盘。on_done 在任何退出路径上都会被调用（释放 inflight）。"""
    started = time.time()
    diagnosis = None
    error = None

    # prompt 构建可能因采集数据不可序列化而失败——降级为极简 prompt，不放弃诊断
    try:
        user_prompt = _build_user_prompt(rule_name, value, heal_result, logs)
    except Exception as e:
        log.warning("诊断 prompt 构建失败，降级为最小上下文: %s", e)
        user_prompt = (
            f"## 触发规则\n{rule_name} (采集器: {collector_name})\n\n"
            f"## 说明\n采集器数据异常无法展示: {e}")

    # 结构化校验失败重试一次；信号量限制全局并发
    with _llm_semaphore:
        for attempt in range(2):
            try:
                diagnosis = _call_llm(user_prompt)
                break
            except Exception as e:
                error = str(e)
                log.warning("AI诊断第%d次尝试失败: %s", attempt + 1, e)

    elapsed = round(time.time() - started, 1)

    try:
        import db
        # 保存到数据库（成功或失败都保存）
        db.save_case(
            fingerprint=fp_key,
            hostname=value.get("hostname", ""),
            message=f"{rule_name} | {collector_name}",
            diagnosis=json.dumps(diagnosis, ensure_ascii=False) if diagnosis else None,
            command=diagnosis.get("fix_direction", "") if diagnosis else None,
            fix_command=diagnosis.get("fix_command") if diagnosis else None,
            error=error  # 失败时保存错误信息
        )

        if diagnosis:
            # 记录诊断事件
            db.save_event("diagnosis", value.get("hostname", ""), {
                "_key": fp_key,
                "_date": date.today().isoformat(),
                "rule": rule_name,
                "collector": collector_name,
                "elapsed_sec": elapsed,
                "root_cause": diagnosis.get("root_cause"),
                "fix_direction": diagnosis.get("fix_direction"),
            })

            # AI 接管：auto 执行并回检，email 只发送建议。
            import ai_mode
            handoff = ai_mode.process_diagnosis(
                fp_key, rule_name, collector_name, value, diagnosis)
            db.save_event("ai_handoff", value.get("hostname", ""), {
                "fingerprint": fp_key,
                "rule": rule_name,
                "collector": collector_name,
                "mode": handoff.get("mode"),
                "success": handoff.get("success", False),
                "attempted": handoff.get("attempted", False),
                "sent": handoff.get("sent", False),
                "reason": handoff.get("reason"),
                "skill_id": handoff.get("skill_id"),
            })
            log.info("AI接管完成 | 模式=%s 成功=%s 原因=%s",
                     handoff.get("mode"), handoff.get("success"),
                     handoff.get("reason", ""))
    except Exception as e:
        log.error("AI诊断结果处理失败: %s", e)
    finally:
        if on_done:
            try:
                on_done()
            except Exception:
                pass

    if diagnosis:
        log.info("AI诊断完成 (%.1fs) | 根因: %s | 方向: %s",
                 elapsed, diagnosis.get("root_cause"),
                 diagnosis.get("fix_direction"))
    else:
        log.error("AI诊断彻底失败 (%.1fs): %s", elapsed, error)


def get_latest_diagnosis_summary() -> str | None:
    """给通知模块用：最近一条成功诊断的根因+方向摘要。"""
    import db
    cases = db.get_recent_cases(limit=10)
    for c in cases:
        diag_str = c.get("diagnosis")
        if diag_str:
            try:
                d = json.loads(diag_str)
                return (f"根因: {d.get('root_cause')}\n"
                        f"修复方向: {d.get('fix_direction')} "
                        f"(置信度 {d.get('confidence', '?')})")
            except Exception:
                pass
    return None

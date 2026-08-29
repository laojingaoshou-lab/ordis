"""
规则晋升状态机：让 AI 诊断结论变成可执行的自动修复规则。

流程：
  同一指纹的故障复发 ≥2 次（每次都有成功 AI 诊断）
      → 自动生成规则草案（pending，含根因与修复方向）
      → 人工 `ordis promote apply <id> --port N --command "..."` 审批
      → 写入 rules.yaml（command_runner 修复器 + 端口监视）
      → 同类故障此后秒级自动修复，不再依赖 AI

安全边界：写进 rules.yaml 的命令必须经人审批，AI 只提议不落盘。
"""

from __future__ import annotations

import json
import shlex
import time
import uuid

try:
    from logger import get_logger
    log = get_logger("promotion")
except ImportError:
    import logging
    log = logging.getLogger("promotion")

PROMOTE_THRESHOLD = 2     # 同指纹第 N 次成功诊断后生成草案


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _is_repair_command(command: str) -> bool:
    """拒绝把纯查看命令晋升为自动修复动作。"""
    try:
        argv = shlex.split(command or "")
    except ValueError:
        return False
    if not argv:
        return False
    tool = argv[0].rsplit("/", 1)[-1].lower()
    if tool in {"ss", "grep", "journalctl", "cat", "tail", "head", "ps", "top",
                "df", "du", "free", "find", "less", "more"}:
        return False
    if tool == "systemctl" and len(argv) > 1 and argv[1] in {
            "status", "show", "is-active", "is-enabled", "list-units"}:
        return False
    if tool in {"docker", "kubectl"} and len(argv) > 1 and argv[1] in {
            "ps", "inspect", "logs", "get", "describe"}:
        return False
    return True


def _infer_verification(diag: dict, command: str) -> tuple[int | None, str | None]:
    """从结构化指纹或受支持的命令生成确定性的效果回检。"""
    fp = diag.get("fingerprint") if isinstance(diag.get("fingerprint"), dict) else {}
    service = str(fp.get("service") or "").strip()
    check_port = None
    if service.isdigit() and 0 < int(service) <= 65535:
        check_port = int(service)

    try:
        argv = shlex.split(command)
    except ValueError:
        return None, None
    check_command = None
    if len(argv) >= 3 and argv[0] == "systemctl" and argv[1] in {
            "start", "restart", "reload"}:
        unit = shlex.quote(argv[2])
        check_command = f"systemctl is-active --quiet {unit}"
    elif (len(argv) == 7 and argv[0].rsplit("/", 1)[-1] == "kubectl"
          and argv[1:3] == ["set", "image"]
          and argv[3].startswith("deployment/")
          and argv[5] in {"-n", "--namespace"}):
        resource = shlex.quote(argv[3])
        namespace = shlex.quote(argv[6])
        check_command = (f"kubectl rollout status {resource} -n {namespace} "
                         "--timeout=60s")
    return check_port, check_command


def load() -> list[dict]:
    """加载草稿列表（兼容旧接口）。"""
    import db
    return db.load_drafts()


def _save(drafts: list[dict]):
    """批量保存草稿（兼容旧接口）。"""
    import db
    for d in drafts:
        db.save_draft(d)


def check_and_draft(fingerprint_key: str, case: dict) -> dict | None:
    """
    AI 诊断成功落盘后调用。
    同指纹成功诊断次数达阈值且无现存草案 → 生成 pending 草案并返回。
    """
    import db
    drafts = db.load_drafts()
    if any(d.get("fingerprint") == fingerprint_key
           and d.get("status") in ("pending", "applied") for d in drafts):
        return None                      # 已有待审/已生效草案，不重复生成

    # 统计指纹复发次数
    cases = db.get_recent_cases(limit=200)
    occurrences = sum(
        1 for c in cases
        if c.get("fingerprint") == fingerprint_key and c.get("diagnosis"))

    if occurrences < PROMOTE_THRESHOLD:
        return None

    diag_str = case.get("diagnosis")
    if isinstance(diag_str, str):
        try:
            diag = json.loads(diag_str)
        except Exception:
            diag = {}
    else:
        diag = diag_str or {}

    draft = {
        "id": _new_id("draft"),
        "fingerprint": fingerprint_key,
        "root_cause": diag.get("root_cause", ""),
        "fix_direction": diag.get("fix_direction", ""),
        "confidence": diag.get("confidence"),
        "occurrences": occurrences,
        "status": "pending",
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "skill": {},
    }
    db.save_draft(draft)
    log.info("生成规则草案 %s | %s 复发 %d 次", draft["id"],
             fingerprint_key, occurrences)
    return draft


def apply_draft(draft_id: str, command: str, name: str | None = None,
                port: int | None = None, cooldown: int = 300,
                check_command: str | None = None) -> dict:
    """
    预审批：校验草案与命令后生成待确认记录（不写 rules.yaml）。
    真正落盘需要 `ordis skills confirm <skill_id>` 人工二次确认。
    """
    from audit import log_audit

    drafts = load()
    draft = next((d for d in drafts if d.get("id") == draft_id), None)
    if not draft:
        raise ValueError(f"草案不存在: {draft_id}")
    if draft.get("status") == "applied":
        raise ValueError("该草案已应用过")
    if not command:
        raise ValueError("必须提供修复命令 (--command)")
    if not port and not (check_command or "").strip():
        raise ValueError("必须提供回检方式 (--port 或 --check-command)")

    skill = {
        "id": _new_id("skill"),
        "draft_id": draft_id,
        "fingerprint": draft["fingerprint"],
        "root_cause": draft.get("root_cause", ""),
        "name": name or f"自动修复-{draft['fingerprint']}",
        "command": command,
        "port": port,
        "check_command": (check_command or "").strip() or None,
        "cooldown": cooldown,
        # pending_confirm: 待人工确认；active: 已生效；disabled: 已停用
        "status": "pending_confirm",
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "confirmed_at": None,
    }
    import db
    db.save_skill(skill)

    draft["status"] = "awaiting_confirm"
    draft["skill_id"] = skill["id"]
    db.save_draft(draft)
    log_audit("skill.apply", {
        "skill_id": skill["id"], "command": command,
        "from_draft": draft_id, "check_port": port,
        "check_command": skill["check_command"],
    })
    log.info("技能待确认 %s | %s → %s", skill["id"],
             draft["fingerprint"], command)
    return skill


def auto_draft_from_ai(fingerprint_key: str, case: dict):
    """
    AI 自动修复执行并回检成功后调用：
    直接从诊断结论生成待审批技能，无需人工先跑 promote --apply。
    生成的技能仍是 pending_confirm——生效必须管理员 ordis skills confirm。
    受 AI 权限分级约束：view 级下写操作技能直接拒绝生成。
    """
    import db
    diag_str = case.get("diagnosis")
    if isinstance(diag_str, str):
        try:
            diag = json.loads(diag_str)
        except Exception:
            diag = {}
    else:
        diag = diag_str or {}

    draft_id = _new_id("draft")

    # 同指纹已有 auto/pending/awaiting_confirm 草案则不重复生成
    drafts = db.load_drafts()
    if any(d.get("fingerprint") == fingerprint_key
           and d.get("status") in ("auto", "pending", "awaiting_confirm")
           for d in drafts):
        return None

    command = (diag.get("fix_command")
               or extract_first_command(diag.get("fix_direction", "")))
    if not _is_repair_command(command):
        db.save_draft({
            "id": draft_id,
            "fingerprint": fingerprint_key,
            "status": "blocked_command",
            "block_reason": "候选命令是查看/诊断动作，不是修复动作",
            "skill": {
                "root_cause": diag.get("root_cause", ""),
                "fix_direction": diag.get("fix_direction", ""),
                "command": command,
            },
        })
        log.warning("拒绝生成技能：候选不是修复动作 | %s", command[:80])
        return None

    check_port, check_command = _infer_verification(diag, command)
    if not check_port and not check_command:
        db.save_draft({
            "id": draft_id,
            "fingerprint": fingerprint_key,
            "status": "blocked_verification",
            "block_reason": "无法生成效果回检，请人工 apply 时提供 --port 或 --check-command",
            "skill": {
                "root_cause": diag.get("root_cause", ""),
                "fix_direction": diag.get("fix_direction", ""),
                "command": command,
            },
        })
        log.warning("拒绝生成技能：缺少可验证的修复效果 | %s", command[:80])
        return None

    skill = {
        "id": _new_id("skill"),
        "fingerprint": fingerprint_key,
        "name": f"自动修复-{fingerprint_key}",
        "description": diag.get("root_cause", ""),
        "command": command,
        "port": check_port,
        "check_command": check_command,
        "cooldown": 300,
        "status": "pending_confirm",
        "source": "ai_auto",
        "trigger": ("health_finding"
                    if fingerprint_key.startswith(("host:", "k8s:")) else "rule"),
        "occurrences": 1,
    }

    # 权限分级：view 级下写操作技能直接拒绝生成（记录草案供查阅，不入技能库）
    ok, reason = check_skill_level(skill)
    if not ok:
        draft = {
            "id": draft_id,
            "fingerprint": fingerprint_key,
            "status": "blocked_level",
            "block_reason": reason,
            "skill": {
                "root_cause": diag.get("root_cause", ""),
                "fix_direction": diag.get("fix_direction", ""),
                "command": skill["command"],
                "block_reason": reason,
            },
        }
        db.save_draft(draft)
        log.warning("技能被权限分级拦截 (%s): %s | %s",
                    current_level_name(), skill["command"][:60], reason)
        return None

    # 技能查重：与已有技能功能类似 → 不另建，标记 duplicate 等人工审核并入
    dup = find_similar_skill(skill["command"], fingerprint_key)
    if dup:
        changed = _norm_cmd(dup.get("command", "")) != _norm_cmd(skill["command"])
        draft = {
            "id": draft_id,
            "fingerprint": fingerprint_key,
            "status": "duplicate",
            "existing_skill_id": dup["id"],
            "suggested_command": skill["command"],
            "command_changed": changed,
            "skill": {
                "root_cause": diag.get("root_cause", ""),
                "fix_direction": diag.get("fix_direction", ""),
            },
        }
        db.save_draft(draft)
        log.info("技能查重命中 %s：生成并入提案 %s（命令%s）",
                 dup["id"], draft_id,
                 "有变化待审核" if changed else "相同，仅记录复发")
        return None

    # 新技能：保存到数据库
    db.save_skill(skill)
    draft = {
        "id": draft_id,
        "fingerprint": fingerprint_key,
        "status": "auto",
        "skill": {
            "id": skill["id"],
            "root_cause": diag.get("root_cause", ""),
            "fix_direction": diag.get("fix_direction", ""),
            "confidence": diag.get("confidence"),
        },
    }
    db.save_draft(draft)
    log.info("AI 修复成功，自动生成待审批技能 %s | %s", skill["id"],
             fingerprint_key)
    return skill


def current_level_name() -> str:
    import ai_levels
    return ai_levels.current_level()


def check_skill_level(skill: dict) -> tuple[bool, str]:
    """
    权限分级校验：技能命令是否允许在当前 AI 等级下存在/生效。
    view 级只放行只读命令；operate 级禁止系统级危险动作；root 不限。
    """
    import ai_levels
    cmd = skill.get("command", "")
    ok, reason = ai_levels.check_allowed(cmd)
    if not ok:
        reason = (reason + f"（如需此技能请用 ordis config permission 提升 AI 权限，"
                  "或手动维护规则）")
    return ok, reason


def extract_first_command(text: str) -> str:
    """从 AI 的修复方向文本里提取第一条反引号命令作为候选修复命令。"""
    import re
    m = re.search(r"`([^`]+)`", text or "")
    if m:
        return m.group(1).strip()
    return (text or "").strip()[:200]


# ── 技能查重（生成时比对已有技能，类似则走并入审核）──────────────
def _norm_cmd(cmd: str) -> str:
    """命令归一化：压空白、小写、去尾部分隔符，用于相似比较。"""
    return " ".join((cmd or "").split()).lower().rstrip(";&|")


def find_similar_skill(command: str, fingerprint: str | None = None) -> dict | None:
    """
    查找与候选功能类似的已有技能；无则返回 None。
    相似判定：归一化命令相同，或同指纹已有非停用技能。
    """
    for s in load_skills():
        if s.get("status") == "disabled":
            continue
        if fingerprint and s.get("fingerprint") == fingerprint:
            return s
        if command and _norm_cmd(s.get("command", "")) == _norm_cmd(command):
            return s
    return None


# ── 技能库（审批生效后的修复流程集合）────────────────────────────

def load_skills() -> list[dict]:
    """加载技能列表（兼容旧接口）。"""
    import db
    return db.load_skills()


def _save_skills(skills: list[dict]):
    """批量保存技能（兼容旧接口）。"""
    import db
    for s in skills:
        db.save_skill(s)


def get_skill(skill_id: str) -> dict | None:
    """根据技能 ID 查询技能详情。"""
    import db
    return db.get_skill(skill_id)


def set_status(skill_id: str, status: str) -> bool:
    """更新技能状态。"""
    from audit import log_audit
    import db
    s = db.get_skill(skill_id)
    if not s:
        return False
    s = dict(s)
    old = s.get("status")
    s["status"] = status
    db.save_skill(s)
    log_audit(f"skill_{status}", {"skill_id": skill_id, "from": old, "to": status})
    return True


def confirm_skill(skill_id: str) -> dict:
    """
    人工确认：把 pending_confirm 的技能写入 rules.yaml 并激活。
    这是技能生效的唯一路径——AI/预审批都不能绕过这一步。

    批准前预演命令影响面（pkill/systemctl/docker 等），展示给用户。
    """
    from audit import log_audit
    from config import load_rules, save_rules
    import db

    s = db.get_skill(skill_id)
    if not s:
        raise ValueError(f"技能不存在: {skill_id}")
    s = dict(s)  # sqlite3.Row 转 dict，允许修改
    if s.get("status") != "pending_confirm":
        raise ValueError(f"技能状态为 {s['status']}，只有待确认状态可确认")

    if not s.get("port") and not (s.get("check_command") or "").strip():
        raise ValueError("技能缺少效果回检（check_port 或 check_command）")

    # 权限分级：confirm 时再次校验（管理员可能已调低等级）
    ok, reason = check_skill_level(s)
    if not ok:
        raise ValueError(f"权限分级拦截: {reason}")

    # 预演影响面
    preview = preview_skill_impact(s)
    if preview.get("affected"):
        print(f"\n⚠️  命令将影响以下进程：")
        for proc in preview["affected"]:
            parts = [f"PID {proc['pid']}: {proc['name']}"]
            if "unit" in proc:
                parts.append(f"(服务 {proc['unit']})")
            if "container" in proc:
                parts.append(f"(容器 {proc['container']})")
            print(f"  {''.join(parts)}")
    if preview.get("warning"):
        print(f"\n⚠️  {preview['warning']}")

    rules = load_rules()
    if s.get("port"):
        watch = (rules.setdefault("process", {})
                 .setdefault("watch_ports", []))
        if s["port"] not in watch:
            watch.append(s["port"])

    rule_name = f"skill-{s['name']}"
    # 幂等：同 skill 重复确认时先移除旧规则
    rules["rules"] = [r for r in (rules.get("rules") or [])
                      if r.get("_skill_id") != skill_id]
    rule = {
        "name": rule_name,
        "cooldown": s.get("cooldown", 300),
        "enabled": True,
        "notify": True,
        "healer": "command_runner",
        "params": {"command": s["command"],
                   "check_port": s.get("port"),
                   "check_command": s.get("check_command")},
        "_skill_id": skill_id,
    }
    if s.get("trigger") == "health_finding" or str(s.get("fingerprint", "")).startswith(
            ("host:", "k8s:")):
        rule.update({"trigger": "health_finding",
                     "fingerprint": s["fingerprint"]})
    else:
        rule.update({"collector": "process",
                     "condition": "not value['alive']",
                     "threshold": None})
    rules.setdefault("rules", []).append(rule)
    save_rules(rules)

    s["status"] = "active"
    s["confirmed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    db.save_skill(s)

    for draft in db.load_drafts():
        linked_id = draft.get("skill_id") or (draft.get("skill") or {}).get("id")
        if linked_id == skill_id and draft.get("status") in {"auto", "awaiting_confirm"}:
            draft["status"] = "applied"
            db.save_draft(draft)

    # 审计日志
    log_audit("confirm_skill", {
        "skill_id": skill_id,
        "skill_name": s["name"],
        "command": s["command"],
        "preview": preview,
    })

    return {"success": True, "rule_name": rule_name}


def discard_draft(draft_id: str) -> bool:
    """废弃草稿。"""
    from audit import log_audit
    import db
    d = db.get_draft(draft_id)
    if not d:
        return False
    d = dict(d)
    d["status"] = "discarded"
    db.save_draft(d)
    log_audit("draft.discard", draft_id)
    return True


def preview_skill_impact(skill: dict) -> dict:
    """
    预演技能命令的影响面：在 Linux 环境下试运行命令（dry-run 模式），
    返回 {"affected": [...进程列表...], "warning": "..."} 供人审批时参考。

    当前实现针对常见危险模式（pkill/killall/systemctl stop|restart）；
    非 Linux 或命令不在覆盖范围内返回 affected=None（无预演能力）。
    """
    import platform
    import subprocess

    cmd = (skill.get("command") or "").strip()
    if not cmd or platform.system() != "Linux":
        return {"affected": None, "warning": None}
    try:
        argv = shlex.split(cmd)
    except ValueError as e:
        return {"affected": None, "warning": f"命令解析失败: {e}"}

    # pkill / killall [-f] <pattern>
    if argv and argv[0] in {"pkill", "killall"}:
        flags = [arg for arg in argv[1:-1] if arg.startswith("-")]
        pattern = argv[-1] if len(argv) >= 2 else ""
        if not pattern:
            return {"affected": None, "warning": "缺少进程匹配模式"}
        pgrep_args = ["pgrep", "-l"]
        if "-f" in flags or argv[0] == "killall":
            pgrep_args.append("-f")
        pgrep_args.append(pattern)
        try:
            out = subprocess.check_output(
                pgrep_args, stderr=subprocess.DEVNULL, timeout=3,
                encoding="utf-8").strip()
            lines = [line for line in out.splitlines() if line]
            procs = [line.split(None, 1) for line in lines]
            return {
                "affected": [{"pid": p[0], "name": p[1] if len(p) > 1 else "?"}
                             for p in procs],
                "warning": None,
            }
        except subprocess.CalledProcessError:
            return {"affected": [], "warning": "模式未匹配到任何进程"}
        except Exception as e:
            return {"affected": None, "warning": f"预演失败: {e}"}

    # systemctl stop|restart <unit>
    if len(argv) >= 3 and argv[0] == "systemctl" and argv[1] in {
            "stop", "restart", "kill"}:
        unit = argv[2]
        try:
            # 查服务主进程 PID
            out = subprocess.check_output(
                ["systemctl", "show", unit, "--property=MainPID",
                 "--property=Id"],
                stderr=subprocess.DEVNULL, timeout=2,
                encoding="utf-8").strip()
            props = dict(ln.split("=", 1) for ln in out.split("\n") if "=" in ln)
            pid = props.get("MainPID", "0")
            name = props.get("Id", unit)
            if pid == "0":
                return {"affected": [],
                        "warning": f"服务 {unit} 当前无运行进程"}
            return {"affected": [{"pid": pid, "name": name, "unit": unit}],
                    "warning": None}
        except subprocess.CalledProcessError:
            return {"affected": None,
                    "warning": f"服务 {unit} 不存在或无权限查询"}
        except Exception as e:
            return {"affected": None, "warning": f"预演失败: {e}"}

    # docker stop/restart/kill <container>
    if len(argv) >= 3 and argv[0] == "docker" and argv[1] in {
            "stop", "restart", "kill", "rm"}:
        ctr = argv[2]
        try:
            out = subprocess.check_output(
                ["docker", "inspect", ctr, "--format={{.State.Pid}} {{.Name}}"],
                stderr=subprocess.DEVNULL, timeout=2,
                encoding="utf-8").strip()
            pid, name = out.split(None, 1)
            if pid == "0":
                return {"affected": [],
                        "warning": f"容器 {ctr} 当前未运行"}
            return {"affected": [{"pid": pid, "name": name.lstrip("/"),
                                  "container": ctr}],
                    "warning": None}
        except subprocess.CalledProcessError:
            return {"affected": None,
                    "warning": f"容器 {ctr} 不存在或 docker 不可用"}
        except Exception as e:
            return {"affected": None, "warning": f"预演失败: {e}"}

    # 其他命令暂无预演能力
    return {"affected": None, "warning": None}


def merge_duplicate(draft_id: str) -> dict:
    """
    人工审核通过：把 duplicate 草案并入已有技能。
    - 命令有变化 → 更新目标技能命令；已生效技能同步 rules.yaml 规则
    - 命令相同 → 仅记录复发，不改动
    这是并入的唯一生效路径——AI 只生成提案，不自行合并。
    """
    from audit import log_audit
    from config import load_rules, save_rules
    import db

    d = db.get_draft(draft_id)
    if not d:
        raise ValueError(f"草案不存在: {draft_id}")
    d = dict(d)
    if d.get("status") != "duplicate":
        raise ValueError(f"草案状态为 {d.get('status')}，只有 duplicate 可并入")

    sid = d.get("existing_skill_id")
    s = db.get_skill(sid)
    if not s:
        raise ValueError(f"目标技能已不存在: {sid}")
    s = dict(s)

    new_cmd = (d.get("suggested_command") or s.get("command", "")).strip()
    changed = _norm_cmd(new_cmd) != _norm_cmd(s.get("command", ""))
    s["command"] = new_cmd
    s["last_merged"] = time.strftime("%Y-%m-%d %H:%M:%S")
    s["merge_count"] = int(s.get("merge_count") or 0) + 1

    # 已生效技能：rules.yaml 里对应规则的修复命令一并更新
    if s.get("status") == "active":
        rules = load_rules()
        for r in (rules.get("rules") or []):
            if r.get("_skill_id") == sid:
                r.setdefault("params", {})["command"] = new_cmd
        save_rules(rules)

    db.save_skill(s)
    d["status"] = "applied"
    d["merged_into"] = sid
    db.save_draft(d)
    log_audit("merge_skill", {
        "skill_id": sid,
        "draft_id": draft_id,
        "command": new_cmd,
        "command_changed": changed
    })
    log.info("草案 %s 已并入技能 %s（命令%s）", draft_id, sid,
             f"更新为: {new_cmd[:60]}" if changed else "不变，仅记录复发")
    return s

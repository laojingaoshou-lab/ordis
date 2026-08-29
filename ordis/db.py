"""
SQLite 持久化层：替代分散的 JSON 文件，统一事务、索引、并发。

表结构:
- cases: 诊断案例（原 cases.json）
- drafts: 待审核技能草稿（原 drafts.json）
- skills: 已确认技能（原 skills.json）
- rules: 自愈规则（原 rules.yaml 的 rule 数组）
- events: 事件历史（原 events.json）
- audit: 操作审计（新增）

迁移策略：
1. 保留 JSON 读取兼容（首次运行自动导入）
2. 新写操作全走 SQLite
3. 保留 rules.yaml 作为人工编辑入口（load 时合并）
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

from paths import data_home

log = logging.getLogger("ordis.db")
DB_PATH = data_home() / "ordis.db"


def _dict_factory(cursor, row):
    """sqlite3.Row 转字典。"""
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}


def get_conn() -> sqlite3.Connection:
    """获取数据库连接（自动初始化表）。"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
    conn.row_factory = _dict_factory
    _init_schema(conn)
    return conn


def _create_cases_table(conn: sqlite3.Connection):
    """创建可保留同类故障复发记录的案例表。"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fingerprint TEXT NOT NULL,
            created_at REAL NOT NULL,
            hostname TEXT,
            message TEXT,
            diagnosis TEXT,
            command TEXT,
            fix_command TEXT,
            error TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cases_created ON cases(created_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cases_fingerprint ON cases(fingerprint)")


def _ensure_column(conn: sqlite3.Connection, table: str, column: str,
                   definition: str):
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _migrate_cases_schema(conn: sqlite3.Connection):
    """将旧版以 fingerprint 为主键的 cases 表迁移为保留历史记录的结构。"""
    columns = conn.execute("PRAGMA table_info(cases)").fetchall()
    if not columns or not any(c["name"] == "fingerprint" and c["pk"] for c in columns):
        return

    names = {c["name"] for c in columns}
    legacy = "cases_legacy"
    conn.execute(f"DROP TABLE IF EXISTS {legacy}")
    conn.execute("ALTER TABLE cases RENAME TO cases_legacy")
    conn.execute("DROP INDEX IF EXISTS idx_cases_created")
    conn.execute("DROP INDEX IF EXISTS idx_cases_fingerprint")
    _create_cases_table(conn)

    copy_columns = ["fingerprint", "created_at", "hostname", "message", "diagnosis",
                    "command", "fix_command", "error"]
    available = [name if name in names else "NULL" for name in copy_columns]
    conn.execute(
        f"INSERT INTO cases ({', '.join(copy_columns)}) "
        f"SELECT {', '.join(available)} FROM {legacy}"
    )
    conn.execute(f"DROP TABLE {legacy}")


def _init_schema(conn: sqlite3.Connection):
    """初始化表结构（幂等，并迁移旧版 cases 主键）。"""
    _create_cases_table(conn)
    _migrate_cases_schema(conn)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS drafts (
        id TEXT PRIMARY KEY,
        created_at REAL NOT NULL,
        status TEXT NOT NULL,  -- pending/auto/awaiting_confirm/applied/discarded/blocked_level/duplicate
        skill TEXT NOT NULL,   -- JSON
        existing_skill_id TEXT,
        suggested_command TEXT,
        command_changed INTEGER,
        merged_into TEXT,
        data TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts(status);
    CREATE INDEX IF NOT EXISTS idx_drafts_created ON drafts(created_at DESC);

    CREATE TABLE IF NOT EXISTS skills (
        id TEXT PRIMARY KEY,
        created_at REAL NOT NULL,
        status TEXT NOT NULL,  -- pending_confirm/active/disabled
        name TEXT NOT NULL,
        description TEXT,
        command TEXT,
        fingerprint TEXT,
        source TEXT,          -- ai_auto/manual (AI自动生成或人工创建)
        occurrences INTEGER DEFAULT 1,
        last_triggered REAL,
        last_merged REAL,
        merge_count INTEGER DEFAULT 0,
        data TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX IF NOT EXISTS idx_skills_status ON skills(status);
    CREATE INDEX IF NOT EXISTS idx_skills_fingerprint ON skills(fingerprint);
    CREATE INDEX IF NOT EXISTS idx_skills_last_triggered ON skills(last_triggered DESC);

    CREATE TABLE IF NOT EXISTS rules (
        id TEXT PRIMARY KEY,
        created_at REAL NOT NULL,
        name TEXT NOT NULL,
        condition TEXT NOT NULL,  -- JSON
        action TEXT NOT NULL,     -- JSON
        params TEXT,              -- JSON (nullable)
        skill_id TEXT             -- 关联技能 ID（自动生成规则时填充）
    );
    CREATE INDEX IF NOT EXISTS idx_rules_skill ON rules(skill_id);

    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL NOT NULL,
        type TEXT NOT NULL,       -- anomaly/heal/diagnosis/skill_applied
        hostname TEXT,
        data TEXT NOT NULL        -- JSON
    );
    CREATE INDEX IF NOT EXISTS idx_events_ts ON events(timestamp DESC);
    CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);

    CREATE TABLE IF NOT EXISTS audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL NOT NULL,
        type TEXT NOT NULL,       -- auto_heal/ai_exec
        data TEXT NOT NULL        -- JSON
    );
    CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(timestamp DESC);
    CREATE INDEX IF NOT EXISTS idx_audit_type ON audit(type);
    CREATE TABLE IF NOT EXISTS migrations (
        source TEXT PRIMARY KEY,
        checksum TEXT NOT NULL,
        migrated_at REAL NOT NULL
    );
    """)
    _ensure_column(conn, "drafts", "data", "TEXT NOT NULL DEFAULT '{}'")
    _ensure_column(conn, "skills", "source", "TEXT")
    _ensure_column(conn, "skills", "data", "TEXT NOT NULL DEFAULT '{}'")
    _ensure_column(conn, "cases", "error", "TEXT")
    conn.commit()


# ============ Cases ============

def save_case(fingerprint: str, hostname: str, message: str,
              diagnosis: str | None, command: str | None,
              fix_command: str | None = None, error: str | None = None):
    """保存诊断案例并保留同一指纹的复发历史，自动清理至最近 200 条。"""
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO cases
            (fingerprint, created_at, hostname, message, diagnosis, command, fix_command, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (fingerprint, time.time(), hostname, message, diagnosis, command,
              fix_command, error))
        conn.execute("""
            DELETE FROM cases WHERE id NOT IN (
                SELECT id FROM cases ORDER BY created_at DESC, id DESC LIMIT 200
            )
        """)
        conn.commit()


def get_recent_cases(limit: int = 100) -> list[dict]:
    """获取最近诊断案例。"""
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM cases ORDER BY created_at DESC, id DESC LIMIT ?",
            (limit,)
        ).fetchall()


def case_exists(fingerprint: str, within_seconds: int = 1800) -> bool:
    """检查案例是否存在（30分钟内去重）。"""
    cutoff = time.time() - within_seconds
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM cases WHERE fingerprint=? AND created_at>?",
            (fingerprint, cutoff)
        ).fetchone()
        return row is not None


# ============ Drafts ============

def save_draft(draft: dict):
    """保存草稿（覆盖更新）。"""
    with get_conn() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO drafts
            (id, created_at, status, skill, existing_skill_id, suggested_command,
             command_changed, merged_into, data)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            draft["id"],
            draft.get("created_at", time.time()),
            draft["status"],
            json.dumps(draft.get("skill", {}), ensure_ascii=False),
            draft.get("existing_skill_id"),
            draft.get("suggested_command"),
            draft.get("command_changed"),
            draft.get("merged_into"),
            json.dumps(draft, ensure_ascii=False, default=str)
        ))
        conn.commit()


def load_drafts() -> list[dict]:
    """加载所有草稿。"""
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM drafts ORDER BY created_at DESC").fetchall()
    return [_expand_draft(r) for r in rows]


def _expand_draft(row: dict) -> dict:
    """数据库行转业务对象，并恢复完整草稿字段。"""
    stored = json.loads(row.get("data") or "{}")
    d = stored if isinstance(stored, dict) else {}
    d.update({k: v for k, v in row.items() if k != "data" and v is not None})
    d["skill"] = json.loads(row["skill"]) if row.get("skill") else d.get("skill", {})
    return d


def get_draft(draft_id: str) -> dict | None:
    """按 ID 获取草稿。"""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
    return _expand_draft(row) if row else None


def update_draft_status(draft_id: str, status: str, merged_into: str | None = None):
    """更新草稿状态。"""
    with get_conn() as conn:
        if merged_into:
            conn.execute(
                "UPDATE drafts SET status=?, merged_into=? WHERE id=?",
                (status, merged_into, draft_id)
            )
        else:
            conn.execute(
                "UPDATE drafts SET status=? WHERE id=?",
                (status, draft_id)
            )
        conn.commit()


# ============ Skills ============

def save_skill(skill: dict):
    """保存技能（覆盖更新）。"""
    with get_conn() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO skills
            (id, created_at, status, name, description, command, fingerprint,
             source, occurrences, last_triggered, last_merged, merge_count, data)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            skill["id"],
            skill.get("created_at", time.time()),
            skill["status"],
            skill["name"],
            skill.get("description"),
            skill.get("command"),
            skill.get("fingerprint"),
            skill.get("source"),
            skill.get("occurrences", 1),
            skill.get("last_triggered"),
            skill.get("last_merged"),
            skill.get("merge_count", 0),
            json.dumps(skill, ensure_ascii=False, default=str)
        ))
        conn.commit()


def _expand_skill(row: dict | None) -> dict | None:
    if not row:
        return None
    stored = json.loads(row.get("data") or "{}")
    skill = stored if isinstance(stored, dict) else {}
    skill.update({k: v for k, v in row.items() if k != "data" and v is not None})
    return skill


def load_skills() -> list[dict]:
    """加载所有技能并恢复完整业务字段。"""
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM skills ORDER BY created_at DESC").fetchall()
    return [_expand_skill(row) for row in rows]


def get_skill(skill_id: str) -> dict | None:
    """按 ID 获取技能。"""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM skills WHERE id=?", (skill_id,)).fetchone()
    return _expand_skill(row)


def find_skill_by_fingerprint(fingerprint: str) -> dict | None:
    """按指纹查找 active 技能。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM skills WHERE fingerprint=? AND status='active' LIMIT 1",
            (fingerprint,)
        ).fetchone()
    return _expand_skill(row)


def find_skill_by_command(command: str) -> dict | None:
    """按归一化命令查找非 disabled 技能。"""
    norm = " ".join(command.split()).lower().rstrip(";&|")
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM skills WHERE status != 'disabled'"
        ).fetchall()
    for row in rows:
        r = _expand_skill(row)
        if " ".join((r.get("command") or "").split()).lower().rstrip(";&|") == norm:
            return r
    return None


def delete_skill(skill_id: str):
    """删除技能。"""
    with get_conn() as conn:
        conn.execute("DELETE FROM skills WHERE id=?", (skill_id,))
        conn.commit()


# ============ Rules ============

def save_rule(rule: dict):
    """保存规则（覆盖更新）。"""
    with get_conn() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO rules
            (id, created_at, name, condition, action, params, skill_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            rule["id"],
            rule.get("created_at", time.time()),
            rule["name"],
            json.dumps(rule["condition"], ensure_ascii=False),
            json.dumps(rule["action"], ensure_ascii=False),
            json.dumps(rule.get("params", ), ensure_ascii=False) if rule.get("params") else None,
            rule.get("_skill_id")
        ))
        conn.commit()


def load_rules() -> list[dict]:
    """加载所有规则。"""
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM rules").fetchall()
    return [_expand_rule(r) for r in rows]


def _expand_rule(row: dict) -> dict:
    """数据库行转业务对象。"""
    r = dict(row)
    r["condition"] = json.loads(r["condition"])
    r["action"] = json.loads(r["action"])
    if r.get("params"):
        r["params"] = json.loads(r["params"])
    r["_skill_id"] = r.pop("skill_id", None)
    return r


def delete_rule(rule_id: str):
    """删除规则。"""
    with get_conn() as conn:
        conn.execute("DELETE FROM rules WHERE id=?", (rule_id,))
        conn.commit()


# ============ Events ============

def save_event(event_type: str, hostname: str, data: dict):
    """保存事件。"""
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO events (timestamp, type, hostname, data)
            VALUES (?, ?, ?, ?)
        """, (time.time(), event_type, hostname, json.dumps(data, ensure_ascii=False, default=str)))
        conn.commit()


def get_recent_events(limit: int = 100, event_type: str | None = None) -> list[dict]:
    """获取最近事件，可按类型过滤。"""
    with get_conn() as conn:
        if event_type:
            rows = conn.execute(
                "SELECT * FROM events WHERE type=? ORDER BY timestamp DESC LIMIT ?",
                (event_type, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM events ORDER BY timestamp DESC LIMIT ?",
                (limit,)).fetchall()
    return [_expand_event(r) for r in rows]


def _expand_event(row: dict) -> dict:
    """数据库行转业务对象。"""
    e = dict(row)
    e["data"] = json.loads(e["data"])
    return e


# ============ Audit ============

def save_audit(audit_type: str, data: dict):
    """保存审计记录。"""
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO audit (timestamp, type, data)
            VALUES (?, ?, ?)
        """, (time.time(), audit_type, json.dumps(data, ensure_ascii=False, default=str)))
        conn.commit()


def get_recent_audits(limit: int = 100) -> list[dict]:
    """获取最近审计记录。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM audit ORDER BY timestamp DESC LIMIT ?",
            (limit,)
        ).fetchall()
    return [_expand_audit(r) for r in rows]


def _expand_audit(row: dict) -> dict:
    """数据库行转业务对象。"""
    a = dict(row)
    a["data"] = json.loads(a["data"])
    return a


# ============ 迁移 ============

def _migration_checksum(source: Path) -> str:
    return hashlib.sha256(source.read_bytes()).hexdigest()


def _migration_done(conn: sqlite3.Connection, source: Path,
                    checksum: str) -> bool:
    row = conn.execute(
        "SELECT checksum FROM migrations WHERE source=?", (str(source),)
    ).fetchone()
    return bool(row and row["checksum"] == checksum)


def _mark_migration(conn: sqlite3.Connection, source: Path,
                    checksum: str):
    conn.execute(
        "INSERT OR REPLACE INTO migrations (source, checksum, migrated_at) "
        "VALUES (?, ?, ?)", (str(source), checksum, time.time()))


def _legacy_id(prefix: str, source: Path, index: int, item: dict) -> str:
    raw = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(f"{source}:{index}:{raw}".encode()).hexdigest()[:16]
    return f"{prefix}_{digest}"


def _migrate_json_file(conn: sqlite3.Connection, source: Path,
                       importer, report: dict):
    if not source.exists():
        return
    checksum = _migration_checksum(source)
    if _migration_done(conn, source, checksum):
        return
    try:
        items = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(items, list):
            raise ValueError(f"{source.name} 顶层必须是数组")
        importer(conn, source, items)
        _mark_migration(conn, source, checksum)
        conn.commit()
        source.rename(source.with_suffix(".json.migrated"))
        report["migrated"] += 1
    except Exception:
        conn.rollback()
        report["failed"] += 1
        log.exception("迁移旧 JSON 失败: %s", source)


def _import_cases(conn: sqlite3.Connection, source: Path, cases: list):
    for c in cases:
        message = c.get("message") or " | ".join(
            x for x in (c.get("rule"), c.get("collector")) if x)
        diagnosis = (json.dumps(c["diagnosis"], ensure_ascii=False)
                     if isinstance(c.get("diagnosis"), dict)
                     else c.get("diagnosis"))
        conn.execute(
            "INSERT INTO cases (fingerprint, created_at, hostname, message, "
            "diagnosis, command, fix_command, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (c.get("fingerprint") or c.get("_key", ""),
             c.get("created_at") or time.time(), c.get("hostname", ""),
             message, diagnosis, c.get("command") or c.get("fix_direction"),
             c.get("fix_command"), c.get("error")))
    conn.execute("DELETE FROM cases WHERE id NOT IN (SELECT id FROM cases "
                 "ORDER BY created_at DESC, id DESC LIMIT 200)")


def _import_drafts(conn: sqlite3.Connection, source: Path, items: list):
    for index, item in enumerate(items):
        item = dict(item)
        item.setdefault("id", _legacy_id("draft", source, index, item))
        item.setdefault("status", "pending")
        conn.execute(
            "INSERT OR REPLACE INTO drafts (id, created_at, status, skill, "
            "existing_skill_id, suggested_command, command_changed, merged_into, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item["id"], item.get("created_at", time.time()), item["status"],
             json.dumps(item.get("skill", {}), ensure_ascii=False, default=str),
             item.get("existing_skill_id"), item.get("suggested_command"),
             item.get("command_changed"), item.get("merged_into"),
             json.dumps(item, ensure_ascii=False, default=str)))


def _import_skills(conn: sqlite3.Connection, source: Path, items: list):
    for index, item in enumerate(items):
        item = dict(item)
        item.setdefault("id", _legacy_id("skill", source, index, item))
        item.setdefault("status", "active")
        item.setdefault("name", item.get("description") or item["id"])
        conn.execute(
            "INSERT OR REPLACE INTO skills (id, created_at, status, name, "
            "description, command, fingerprint, source, occurrences, "
            "last_triggered, last_merged, merge_count, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item["id"], item.get("created_at", time.time()), item["status"],
             item["name"], item.get("description"), item.get("command"),
             item.get("fingerprint"), item.get("source"),
             item.get("occurrences", 1), item.get("last_triggered"),
             item.get("last_merged"), item.get("merge_count", 0),
             json.dumps(item, ensure_ascii=False, default=str)))


def _import_events(conn: sqlite3.Connection, source: Path, events: list):
    for event in events:
        conn.execute(
            "INSERT INTO events (timestamp, type, hostname, data) VALUES (?, ?, ?, ?)",
            (event.get("timestamp") or event.get("time") or time.time(),
             event.get("type", "legacy"), event.get("hostname", ""),
             json.dumps(event.get("data", event), ensure_ascii=False, default=str)))


def migrate_from_json(logs_dir: Path | None = None) -> dict:
    """按文件事务导入旧 JSON；成功后改名，失败保留原文件并记录日志。"""
    logs_dir = logs_dir or (Path(__file__).parent / "logs")
    report = {"migrated": 0, "failed": 0}
    with get_conn() as conn:
        _migrate_json_file(conn, logs_dir / "cases.json", _import_cases, report)
        _migrate_json_file(conn, logs_dir / "drafts.json", _import_drafts, report)
        _migrate_json_file(conn, logs_dir / "skills.json", _import_skills, report)
        _migrate_json_file(conn, logs_dir / "events.json", _import_events, report)
    return report


def initialize(migrate: bool = True) -> dict:
    """初始化数据库，并在首次启动时导入旧 JSON 文件。"""
    with get_conn():
        pass
    return migrate_from_json() if migrate else {"migrated": 0, "failed": 0}

"""
集群模式：一主多从的多主机监控（类 k8s 拓扑）。

角色：
- server (`ordis server`) : 聚合各节点上报，提供全局视图 + 指令下发队列
- agent  (`ordis agent`)  : 周期采集本机状态上报，领取并执行重启指令

设计约束（延续 v3）：
- 纯 HTTP + 共享 token，零新增依赖（fastapi 已在 requirements）
- 自愈规则引擎仍在每台节点本地运行（server 挂了不影响单机自愈）
- server 单文件存储 logs/nodes.json，实验室规模不做数据库
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import threading
import time
import uuid
from pathlib import Path

import requests

try:
    from logger import get_logger
    log = get_logger("cluster")
except ImportError:
    import logging
    log = logging.getLogger("cluster")

try:
    from paths import data_home
    CONFIG_PATH = data_home() / "cluster.json"
except ImportError:
    CONFIG_PATH = Path.home() / ".ordis" / "cluster.json"
DATA_FILE = Path(__file__).parent / "logs" / "nodes.json"
DEFAULT_PORT = 9800
ORDER_ACK_TIMEOUT = 60
ORDER_MAX_ATTEMPTS = 3
_AGENT_PATHS = {"/ingest", "/order_result"}


# ── 配置 ────────────────────────────────────────────────────────
def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(cfg: dict):
    from filelock import exclusive
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with exclusive(CONFIG_PATH):
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(CONFIG_PATH)
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass


# ── 采集（agent 侧）─────────────────────────────────────────────
def collect_local_snapshot() -> dict:
    """本机全量快照。单个采集器挂掉不影响整体上报。"""
    snap = {}
    for name in ("cpu", "memory", "disk", "process"):
        try:
            mod = __import__(f"collectors.{name}", fromlist=["collector"])
            snap[name] = mod.collector.collect()
        except Exception as e:
            snap[name] = {"error": str(e)}
    try:
        from collectors.discovery import collector as disco
        svcs = disco.collect().get("services", [])
        snap["services"] = [f"{s['manager']}:{s['target']}" for s in svcs]
    except Exception:
        snap["services"] = []
    return snap


# ── 存储与指令队列（server 侧）──────────────────────────────────
class ClusterStore:
    def __init__(self):
        self._lock = threading.Lock()
        self.nodes: dict[str, dict] = {}
        self.orders: list[dict] = []
        self._load()

    def _load(self):
        try:
            data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            self.nodes = data.get("nodes", {})
            self.orders = data.get("orders", [])
        except Exception:
            pass

    def _persist(self):
        DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = DATA_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(
            {"nodes": self.nodes, "orders": self.orders[-100:]},
            ensure_ascii=False), encoding="utf-8")
        tmp.replace(DATA_FILE)

    def _refresh_orders(self, node: str, now: float):
        for order in self.orders:
            if order.get("node") != node or order.get("status") != "sent":
                continue
            sent_at = float(order.get("sent_at") or order.get("ts") or 0)
            if now - sent_at < ORDER_ACK_TIMEOUT:
                continue
            if int(order.get("attempts") or 0) >= ORDER_MAX_ATTEMPTS:
                order["status"] = "timeout"
                order["result"] = "agent 未在重试上限内回传执行结果"
            else:
                order["status"] = "pending"

    def ingest(self, node: str, snapshot: dict) -> list[dict]:
        with self._lock:
            now = time.time()
            self.nodes[node] = {"last_seen": now, "snapshot": snapshot}
            self._refresh_orders(node, now)
            mine = [o for o in self.orders
                    if o["node"] == node and o["status"] == "pending"]
            for order in mine:
                order["status"] = "sent"
                order["sent_at"] = now
                order["attempts"] = int(order.get("attempts") or 0) + 1
            self._persist()
            return [{"id": o["id"], "service": o["service"]} for o in mine]

    def add_order(self, node: str, service_key: str) -> str:
        oid = f"ord_{uuid.uuid4().hex[:16]}"
        with self._lock:
            self.orders.append({"id": oid, "node": node,
                                "service": service_key, "status": "pending",
                                "attempts": 0, "ts": time.time()})
            self._persist()
        return oid

    def order_result(self, node: str, order_id: str, ok: bool, detail: str) -> bool:
        with self._lock:
            updated = False
            for order in self.orders:
                if (order["id"] == order_id and order.get("node") == node
                        and order.get("status") == "sent"):
                    order["status"] = "done" if ok else "failed"
                    order["result"] = detail[:300]
                    order["completed_at"] = time.time()
                    updated = True
                    break
            self._persist()
            return updated

    def overview(self) -> list[dict]:
        with self._lock:
            now = time.time()
            rows = []
            for name, info in sorted(self.nodes.items()):
                s = info.get("snapshot", {})
                rows.append({
                    "node": name,
                    "cpu_pct": (s.get("cpu") or {}).get("cpu_percent"),
                    "mem_pct": (s.get("memory") or {}).get("percent"),
                    "disk_pct": (s.get("disk") or {}).get("use_pct"),
                    "services": len(s.get("services", []) or []),
                    "ports_down": sum(
                        1 for v in ((s.get("process") or {})
                                    .get("ports", {}) or {}).values()
                        if v is False),
                    "age_sec": int(now - info.get("last_seen", 0)),
                })
            return rows


STORE: ClusterStore | None = None


def get_store() -> ClusterStore:
    global STORE
    if STORE is None:
        STORE = ClusterStore()
    return STORE


# ── Server HTTP 层 ──────────────────────────────────────────────
def resolve_token(explicit: str | None = None) -> tuple[str, bool]:
    """
    server 端 token 解析：显式参数 > 配置文件；两者皆无则自动生成并持久化，
    返回 (token, 是否新生成)。保证 0.0.0.0 监听的服务默认就有共享 token。
    """
    import secrets
    cfg = load_config()
    tok = explicit or cfg.get("token")
    if tok:
        return tok, False
    tok = secrets.token_urlsafe(24)
    cfg["token"] = tok
    save_config(cfg)
    return tok, True


def enroll_node(node: str) -> str:
    """生成或轮换节点独立凭证。"""
    node = (node or "").strip()[:64]
    if not node:
        raise ValueError("节点名不能为空")
    cfg = load_config()
    node_tokens = cfg.setdefault("node_tokens", {})
    token = secrets.token_urlsafe(24)
    node_tokens[node] = token
    save_config(cfg)
    return token


def _token_allowed(path: str, node: str | None, provided: str | None,
                   admin_token: str | None, node_tokens: dict[str, str]) -> bool:
    """管理接口只接受管理 token；Agent 接口优先强制节点独立凭证。"""
    if path in _AGENT_PATHS and node_tokens:
        expected = node_tokens.get(node or "")
    else:
        expected = admin_token
    return bool(expected and provided and secrets.compare_digest(expected, provided))


def make_app(token: str | None = None, node_tokens: dict[str, str] | None = None):
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    app = FastAPI(title="Ordis Cluster Server")
    configured_node_tokens = dict(
        node_tokens if node_tokens is not None
        else (load_config().get("node_tokens") or {}))

    @app.middleware("http")
    async def _auth(request: Request, call_next):
        node = request.headers.get("X-Ordis-Node")
        if request.url.path in _AGENT_PATHS:
            try:
                body_node = str((await request.json()).get("node") or "")[:64]
            except Exception:
                body_node = ""
            if node and node != body_node:
                return JSONResponse({"error": "node identity mismatch"}, status_code=401)
            node = body_node
        if not _token_allowed(
                request.url.path, node,
                request.headers.get("X-Ordis-Token"),
                token, configured_node_tokens):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)

    @app.post("/ingest")
    async def ingest(body: dict):
        # 注意: 不能用 `request: Request` 注解——future annotations 使其无法被
        # FastAPI 解析(局部导入的类名)，会被误当成 query 参数返回 422
        node = str(body.get("node") or "unknown")[:64]
        return {"orders": get_store().ingest(node, body.get("snapshot", {}))}

    @app.post("/order")
    async def order(body: dict):
        oid = get_store().add_order(str(body.get("node"))[:64],
                                    str(body.get("service"))[:120])
        return {"id": oid}

    @app.post("/order_result")
    async def order_result(body: dict):
        updated = get_store().order_result(
            str(body.get("node") or "")[:64], str(body.get("id") or ""),
            bool(body.get("ok")), str(body.get("detail", "")))
        if not updated:
            return JSONResponse({"error": "order not found or not sent"}, status_code=404)
        return {"ok": True}

    @app.get("/nodes")
    async def nodes():
        return {"nodes": get_store().overview(),
                "orders": get_store().orders[-20:]}

    @app.get("/")
    async def health():
        return {"service": "ordis-cluster", "nodes": len(get_store().nodes)}

    return app


# ── Agent 循环 ──────────────────────────────────────────────────
def execute_order(service: str) -> tuple[bool, str]:
    """在节点上执行一条重启指令。复用修复器的统一重启入口。"""
    manager, _, target = service.partition(":")
    if not target:
        return False, f"服务标识格式错误: {service}（应为 manager:target）"
    if manager == "manual":
        return False, "manual 类服务无进程管理器，拒绝自动重启"
    try:
        from healers.process_restarter import ProcessRestarter
        action, out, ok = ProcessRestarter()._restart(manager, target)
        return ok, f"{action}: {(out or '')[:150]}"
    except FileNotFoundError:
        return False, f"'{manager}' CLI 不存在于本机"
    except Exception as e:
        return False, str(e)[:200]


def agent_loop(server: str, token: str | None = None, interval: int = 30,
               verify: bool | str = True):
    node = socket.gethostname()
    url = server.rstrip("/")
    headers = {"X-Ordis-Node": node}
    if token:
        headers["X-Ordis-Token"] = token
    completed: dict[str, tuple[bool, str]] = {}
    log.info("Agent 启动 | node=%s -> %s (每 %ds)", node, url, interval)
    while True:
        try:
            payload = {"node": node, "snapshot": collect_local_snapshot()}
            r = requests.post(f"{url}/ingest", json=payload,
                              headers=headers, timeout=8, verify=verify)
            r.raise_for_status()
            for order in (r.json() or {}).get("orders", []):
                order_id = str(order.get("id") or "")
                if order_id in completed:
                    ok, detail = completed[order_id]
                else:
                    ok, detail = execute_order(order.get("service", ""))
                    completed[order_id] = (ok, detail)
                    if len(completed) > 100:
                        completed.pop(next(iter(completed)))
                result = requests.post(
                    f"{url}/order_result",
                    json={"node": node, "id": order_id,
                          "ok": ok, "detail": detail},
                    headers=headers, timeout=8, verify=verify)
                result.raise_for_status()
                log.info("指令 %s [%s] %s", order_id,
                         "OK" if ok else "FAIL", detail)
        except Exception as e:
            log.warning("上报失败（下一轮重试）: %s", e)
        time.sleep(interval)

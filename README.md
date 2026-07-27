<p align="center">
  <img src="screenshots/dashboard.png" alt="Ordis Dashboard" width="100%">
</p>

# Ordis — 轻量运维自动化 · 服务器自愈守护进程

<p align="center">
  <strong>⚡ 50MB 内存 · 5 个采集器 · 4 个修复器 · AI 诊断</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/状态-🚧_Demo_开发中-orange" alt="status">
  <img src="https://img.shields.io/badge/运维自动化-auto--heal-brightgreen" alt="auto-heal">
  <img src="https://img.shields.io/badge/RAM-50MB-lightgrey" alt="50MB">
  <img src="https://img.shields.io/badge/AI-Claude%20Code-blue" alt="Claude">
  <img src="https://img.shields.io/badge/process-PM2-red" alt="PM2">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="MIT">
</p>

<p align="center">
  <a href="#english">English</a> ·
  <a href="#中文">中文</a>
</p>

---

## English

### ⚠️ Status: Demo / Work in Progress

This is a **functional demo**, not a production-hardened tool. It runs on my personal server (Alibaba Cloud ECS, 1.6GB RAM) and handles real incidents. But there are sharp edges — error handling is minimal, there's no distributed mode, and configuration is file-based. **Use at your own risk. Contributions welcome.**

### What Problem Does It Solve?

Traditional monitoring tools (Prometheus, Zabbix, Netdata) are great at **telling you something broke**. Ordis tries to **fix it**.

- Server process crashes at 3 AM? Ordis restarts it.
- Memory creeping up? Ordis flushes caches.
- SSH brute-force attack? Ordis bans the IP.
- Weird failure you need AI to diagnose? Ordis calls Claude Code.

### Architecture Deep Dive

```
┌─────────────────────────────────────────┐
│              Ordis Daemon                │
│          (30-second loop)               │
├─────────────────────────────────────────┤
│                                         │
│  ┌──────────┐    ┌──────────┐           │
│  │ Collectors│───▶│  Engine  │           │
│  └──────────┘    └────┬─────┘           │
│       │               │                 │
│       │          ┌────▼─────┐           │
│       │          │  Healers  │          │
│       │          └────┬─────┘           │
│       │               │                 │
│  ┌────▼───────────────▼─────┐           │
│  │     Event Logger          │          │
│  └───────────────────────────┘          │
│                                         │
│  ┌───────────────────────────┐          │
│  │   Web Dashboard (FastAPI)  │          │
│  │   /api/status  /api/events │          │
│  │   /api/rules   /api/chat   │          │
│  └───────────────────────────┘          │
└─────────────────────────────────────────┘
```

#### Collectors (6)

| Collector | Source | Output |
|-----------|--------|--------|
| `cpu` | `psutil.cpu_percent()` | load_1min, load_5min, load_15min, cpu_percent |
| `memory` | `psutil.virtual_memory()` | available_gb, used_gb, total_gb, swap_used_gb, percent |
| `disk` | `shutil.disk_usage("/")` | use_pct, used_gb, free_gb |
| `process` | TCP socket connect | per-port alive/dead map |
| `security` | `journalctl` with cursor | failed SSH attempts, offending IPs, bans |

Each collector extends `BaseCollector` — just implement `collect()` and return a dict.

#### Rule Engine

```yaml
rules:
  - name: "进程端口不通"
    collector: process
    condition: "not all(value['ports'].values())"
    healer: process_restarter
    cooldown: 60
    enabled: true

  - name: "内存不足告警"
    collector: memory
    condition: "value['available_gb'] < threshold"
    threshold: 0.2
    healer: memory_cleaner
    cooldown: 300
```

Uses Python's `eval()` on collector output. Cooldown prevents thrashing.

#### Healers (4, with port re-check)

| Healer | Action | Verifies? |
|--------|--------|-----------|
| `process_restarter` | PM2 restart / systemctl restart | ✅ TCP port re-check; marks `needs_claude` on failure |
| `memory_cleaner` | `sync; echo 3 > /proc/sys/vm/drop_caches` | ❌ |
| `disk_cleaner` | `apt clean`, temp file cleanup | ❌ |
| `ssh_ban` | `ufw deny from <IP>` | ❌ |

When a healer fails even after re-checking, the dashboard shows an **"AI诊断"** button that pre-fills the chat with diagnostic context.

#### AI Copilot (Claude Code)

```
Dashboard Chat ──▶ /api/chat ──▶ claude-chat (Express) ──▶ claude --permission-mode bypassPermissions
                                                                  │
                                                             runs as 'ordish' user
                                                             (non-root for YOLO mode)
```

The AI can execute commands, read logs, and suggest repairs. It's **not a black box** — the `✨ 思考链` (thinking chain) toggle shows Claude's reasoning.

#### Dashboard (VoltAgent Dark Theme)

- **Live metrics**: CPU load, memory usage, disk, SSH attacks — 30s refresh
- **Expandable drawers**: Click any metric card for detailed breakdown
- **Service status**: Green/red dots for each monitored process
- **Event timeline**: Last 5 auto-heal events with success/failure + AI button
- **Login gate**: 3 failed attempts → 30 min cooldown
- **Chat persistence**: Messages survive page refresh (localStorage)

### Quick Start

```bash
git clone https://github.com/laojingaoshou-lab/ordis.git
cd ordis
pip install -r requirements.txt

# Edit rules for your server
vim rules.yaml

# Test one scan cycle
python3 ordisd.py once

# Run as daemon
python3 ordisd.py run

# Start dashboard
uvicorn dashboard:app --host 0.0.0.0 --port 9999
```

---

## 中文

### ⚠️ 当前状态：Demo / 开发中

这是一个**能跑起来的 Demo**，不是生产级工具。它跑在我的阿里云 ECS（1.6GB 内存）上，能处理真实故障。但错误处理很粗糙，没有分布式模式，配置靠文件。**自用随意，生产慎用。欢迎 PR。**

### 它解决什么问题？

传统监控工具（Prometheus、Zabbix、Netdata）擅长**告诉你什么坏了**。Ordis 尝试**直接修好它**。

- 凌晨三点进程崩了？Ordis 自动重启。
- 内存越占越多？Ordis 清缓存。
- SSH 被暴力破解？Ordis 封 IP。
- 遇到诡异故障？点一下"AI诊断"，Claude Code 来分析。

### 架构详解

#### 采集器 ×6

每个采集器继承 `BaseCollector`，实现 `collect()`，返回字典。新增采集器不需要改引擎代码。

#### 规则引擎

YAML 定义规则 + Python `eval()` 执行条件 + 冷却时间防抖。

#### 修复器 ×4（含端口回检）

`process_restarter` 在重启进程后会再次检查端口——如果端口还是不通，标记为失败并弹出 AI 诊断入口。

#### AI 副驾（Claude Code）

仪表盘聊天框 → `/api/chat` → Node.js 转发 → `claude --permission-mode bypassPermissions`。以非 root 用户 `ordish` 运行。能看到思考链。

### 为什么做这个？

我是集成电路专业的在校生，正在自学运维方向。这是我自己练手的项目——通过造轮子来理解监控、自愈和 AI 集成。如果你觉得好用，那是意外收获。

### 生产数据（我的个人服务器）

- 阿里云 ECS · 1.6GB RAM · 双核 · Ubuntu 24.04
- 已连续运行 17 天
- 自动封禁 12 次 SSH 暴力破解
- 3 次服务自动重启（OOM 恢复）

### License

MIT — do whatever you want, just don't sue me.

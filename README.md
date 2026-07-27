# Ordis — Self-Healing Server Daemon

<p align="center">
  <strong>⚡ 50MB · 5 Collectors · 4 Healers · AI Copilot</strong><br>
  <sub>A server that heals itself. AI is optional, but recommended.</sub>
</p>

---

## What is Ordis?

Ordis is a **lightweight self-healing daemon** that monitors your server and **automatically fixes problems** before you even notice. Think of it as an autopilot for your Linux server.

- **30-second scan cycle** — collects CPU, memory, disk, process, and security data
- **Auto-repair** — when something breaks, Ordis fixes it. No human needed.
- **50MB RAM footprint** — runs on 1GB VPS without breaking a sweat
- **Claude Code integration** — chat with Ordis in natural language for diagnosis

## Architecture

```
Ordis Daemon (30s loop)
├── Collectors (6)
│   ├── CPU    — load, usage, per-core stats
│   ├── Memory — available, swap, usage %
│   ├── Disk   — usage, free space
│   ├── Process — port health (TCP check)
│   └── Security — SSH brute-force detection (journald cursor)
├── Rule Engine (YAML-driven)
│   └── eval() expressions with cooldown
├── Healers (4)
│   ├── Process Restarter — PM2 + systemd dual support
│   ├── Memory Cleaner   — flush caches + drop_caches
│   ├── Disk Cleaner      — apt clean + log rotation
│   └── SSH Ban           — ufw auto-block attackers
└── Web Dashboard (FastAPI)
    ├── Real-time metrics
    ├── Event timeline
    ├── Rule management
    └── Claude Code Chat
```

## Quick Start

```bash
# Install
pip install -r requirements.txt

# Configure
cp rules.yaml.example rules.yaml
vim rules.yaml  # Set your watch_ports

# Run once (dry-run)
python3 ordisd.py once

# Run as daemon
python3 ordisd.py run

# Install systemd service
cp ordisd.service /etc/systemd/system/
systemctl enable --now ordisd
```

## AI Chat

Ordis integrates with **Claude Code** for AI-powered diagnosis:

```bash
# The dashboard chat forwards to claude-code
# Set up claude-chat service separately:
cd /var/www/claude-chat
pm2 start server.js --name claude-chat
```

When a healer fails, click **"AI诊断"** → diagnostic question auto-fills the chat → Claude Code analyzes logs and suggests repairs.

## Rules

```yaml
rules:
  - name: "内存不足告警"
    collector: memory
    condition: "value['available_gb'] < threshold"
    threshold: 0.2
    healer: memory_cleaner
    cooldown: 300
    enabled: true
```

Add new collectors and healers without touching the engine — just create a Python file and add a rule.

## Compared to...

| Feature | Prometheus | Zabbix | Netdata | Ordis |
|---------|-----------|--------|---------|-------|
| Auto-repair | ❌ | ❌ | ❌ | ✅ |
| AI diagnosis | ❌ | ❌ | ❌ | ✅ |
| RAM usage | 200MB+ | 500MB+ | 100MB+ | **50MB** |
| Deploy time | 30min+ | 60min+ | 5min | **30s** |
| YAML rules | ✅ | ❌ | ❌ | ✅ |
| SSH defense | ❌ | ❌ | ❌ | ✅ |

## Production Stats

- Running on Alibaba Cloud ECS (1.6GB RAM, 2-core)
- 17+ days uptime
- 12 SSH brute-force attacks auto-blocked
- 3 service auto-restarts (OOM recovery)

## License

MIT

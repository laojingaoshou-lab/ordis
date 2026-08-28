

# Ordis v1.0

<p align="center">
  <strong>面向 Linux 的轻量运维检测、自愈与 AI 故障诊断工具</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/版本-v1.0-blue" alt="版本">
  <img src="https://img.shields.io/badge/平台-Linux-lightgrey" alt="平台">
  <img src="https://img.shields.io/badge/Kubernetes-兼容-326ce5" alt="Kubernetes">
  <img src="https://img.shields.io/badge/许可证-MIT-green" alt="许可证">
</p>

Ordis 是一个运行在 Linux 主机上的轻量运维 Demo。它持续采集主机、传统业务和 Kubernetes 的健康状态，在发现故障后执行受控的自动修复；内置修复无法解决时，将现场证据交给 AI 进行诊断，并根据配置选择自动修复或向管理员发送修复建议。

> 当前版本定位为可运行的开源 Demo，不等同于生产级监控平台。请先在测试环境验证规则、权限和修复命令，再接入重要业务。

## 功能概览

- **自动巡检**：`ordis run` 按配置周期循环检测，`ordis once` 手动执行一轮。
- **Linux 主机检测**：CPU、内存、磁盘、inode、僵尸进程、端口、systemd、Docker 和 SSH 安全事件。
- **传统业务探针**：HTTP 状态码和响应内容、TCP 端口、DNS 解析、TLS 证书有效期。
- **Kubernetes 基础检测**：Node 状态与压力、Pod Pending、CrashLoopBackOff、OOM、拉取镜像失败、频繁重启、工作负载副本不足、Job 失败、PVC 异常和 Service 无可用 Endpoint。
- **受控自愈**：先尝试已审核技能和内置确定性修复；仅对明确授权的服务或命名空间执行操作。
- **AI 接管**：探测到的 Finding、证据和内置修复结果会作为诊断上下文交给模型。
- **两种 AI 模式**：`auto` 执行通过权限检查和回检的修复；`email` 只发送建议，不执行 AI 命令。
- **技能沉淀**：AI 修复成功且回检通过后生成待审核技能；查重、合并、确认后才会进入生效规则。
- **权限分级**：`view`、`operate`、`root` 统一约束 talk、AI 建议、自动修复和技能审核。
- **SQLite 持久化**：案例、事件、草稿、技能和审计记录存储在 `~/.ordis/ordis.db`，支持 `ORDIS_HOME`。
- **集群模式**：server 聚合节点状态，agent 上报快照并领取订单；节点 token、管理 token 和订单重试均有隔离。

## 工作流程

```
自动巡检 / 手动 once
        ↓
采集器收集主机、业务或 Kubernetes 状态
        ↓
规则引擎生成稳定 Finding 并执行冷却
        ↓
已审核技能 → 内置确定性修复 → 效果回检
        ↓
失败或没有可用修复
        ↓
AI 诊断（Finding + 证据 + 修复结果）
        ├─ auto：权限检查 → 执行候选命令 → 回检 → 待审核技能草稿
        └─ email：整理修复建议 → SMTP 邮件通知管理员
```

`ordis check` 和 `ordis k8s check` 是只读检测命令；`ordis run` 和 `ordis once` 才会进入规则触发与修复闭环。探测器自动修复默认关闭，必须在规则中显式开启并配置白名单。

## 安装

Ordis 面向 Linux，建议使用 Python 3.11 及以上版本。

```bash
git clone https://github.com/laojingaoshou-lab/ordis.git
cd ordis
python3 -m pip install .
ordis --help
```

不安装到系统 PATH 时，也可以直接运行源码入口：

```bash
python3 ordisc --help
```

## 首次配置

交互式向导可以一次配置模型 API、AI 接管模式和全局权限：

```bash
ordis setup
ordis config
ordis config test
```

配置文件默认位于 `~/.ordis/`：

- `model.json`：模型供应商、Base URL、模型名和 API key，权限为 600。
- `ai_mode.json`：接管模式和邮件参数；SMTP 密码只从环境变量读取。
- `ordis.db`：SQLite 运行数据。
- 设置 `ORDIS_HOME=/data/ordis` 可将运行数据迁移到持久化目录。

模型配置支持 OpenAI 兼容接口。API key 建议通过交互式配置或部署环境注入，禁止提交到 Git。

## CLI 用法

```bash
# 查看帮助和本机状态
ordis --help
ordis status
ordis config

# 自动巡检
ordis run
ordis once
ordis once --wait-ai

# 只读检测
ordis check
ordis k8s doctor
ordis k8s check
ordis --json k8s check

# 事件、案例和技能
ordis events
ordis cases -v
ordis skills -v
ordis skills confirm <draft-or-skill-id>
ordis skills merge <draft-id>
ordis skills disable <skill-id>

# AI 接管配置
ordis config mode auto
ordis config mode email
ordis config permission view
ordis config permission operate
ordis config permission root

# 自然语言运维
ordis talk
ordis talk "检查内存和 nginx 状态"
```

进入 `talk` 后可以多轮交流，使用 `exit` 或 `Ctrl+C` 退出。会话中的命令执行遵从全局权限：低权限或越权命令会请求确认，`root` 模式下不再逐条确认。请谨慎使用 `root`。

## Linux 与传统业务检测

在 `ordis/rules.yaml` 中配置检测范围。下面的配置只展示常用字段：

```yaml
detection:
  cooldown: 300
  traditional:
    enabled: true
    systemd: true
    systemd_units: []
    systemd_ignore_units: [known-benign.service]
    docker: true
    docker_containers: [api, worker]
    zombie_threshold: 5
    inode_threshold: 90
    endpoints:
      - name: checkout
        type: http
        url: "https://127.0.0.1:8443/health"
        expected_status: 200
        body_contains: ok
      - name: database
        type: tcp
        host: 127.0.0.1
        port: 3306
      - name: public-dns
        type: dns
        host: example.com
      - name: public-tls
        type: tls
        host: example.com
        minimum_days: 14
    repair:
      enabled: false
      allowed_systemd_units: [nginx.service]
      allowed_docker_containers: [api]
```

传统业务修复默认关闭。开启后也只允许白名单中的 systemd 服务和 Docker 容器执行受控的启动、重启或 reload 操作。

## Kubernetes 兼容

Ordis 不依赖 Prometheus，可直接使用当前 `kubectl` context 或 Pod ServiceAccount 读取资源快照：

```bash
ordis k8s doctor
ordis k8s check
```

启用 `ordis run` 或 `ordis once` 中的 Kubernetes 检测：

```yaml
detection:
  kubernetes:
    enabled: true
    context: ""
    namespaces: []
    node_ignore_names: []
    reason_allowlist: []
    pod_pending_seconds: 300
    pod_restart_threshold: 5
    repair:
      enabled: false
      ai_enabled: false
      allowed_namespaces: []
      timeout: 45
```

- 空的 `namespaces` 表示检测所有命名空间。
- 空的 `allowed_namespaces` 不授予任何 Kubernetes 写权限。
- 集群内运行时，默认 RBAC 只授予 `get/list` 读取权限。
- Kubernetes AI 修复需要额外显式开启，并且只接受白名单命名空间中、与故障资源精确匹配的受控操作。
- 建议先使用 `ordis k8s check` 和 `ordis once` 验证，不会修改你的 Kubernetes 实验资源。

## AI 诊断与技能审核

AI 不会直接绕过 Ordis 的权限和回检约束。一次 AI 接管包含：

1. 保存故障指纹、采集证据和内置修复结果。
2. 调用模型生成根因、修复方向和候选修复命令。
3. 根据 `auto` 或 `email` 模式执行后续动作。
4. 自动修复必须通过效果回检；只读诊断命令不能成为修复技能。
5. AI 生成的技能先进入待审核状态。
6. 管理员确认或合并后，技能才会写入生效规则。

技能会按规范化命令和故障指纹查重。重复草稿不会直接覆盖旧技能，需人工执行 `ordis skills merge`。

## 邮件建议模式

邮件模式只发送 AI 修复建议，不执行 AI 命令。SMTP 密码不写入配置文件，使用环境变量注入：

```bash
export ORDIS_SMTP_PASSWORD='由部署环境注入'
ordis config mode email \
  --to admin@example.com \
  --smtp-host smtp.example.com \
  --smtp-port 465 \
  --smtp-user ordis@example.com \
  --from ordis@example.com \
  --password-env ORDIS_SMTP_PASSWORD \
  --smtp-security ssl

ordis config test
ordis ai-mode test-email
```

生产环境应通过 systemd `EnvironmentFile`、容器 Secret 或其他凭据管理方式注入密码。

## 集群模式

集群由一个 server 和多个 agent 组成：

```
server（聚合节点） ← agent（各 Linux 节点）
       状态上报、节点查询、受控修复订单
```

```bash
# server
ordis server --host 0.0.0.0 --port 9800 --token <admin-token>

# 节点加入并运行 agent
ordis join https://<server>:9800 --token <node-token>
ordis agent --server https://<server>:9800 --token <node-token>

# 管理端查看节点
ordis nodes --server https://<server>:9800 --token <admin-token>
```

非 loopback 部署建议使用 HTTPS。管理 token 只能访问管理接口，节点 token 绑定节点身份，只能访问上报和订单回执接口。订单超时会有限次重试，agent 按订单 ID 做幂等处理。

## 运行方式

开发和测试：

```bash
ordis once
ordis once --wait-ai
```

长期运行建议使用 systemd，并为 `ORDIS_HOME`、模型配置和 SMTP 凭据准备持久化与安全注入方式。Web 面板可按项目部署方式启动：

```bash
uvicorn dashboard:app --host 0.0.0.0 --port 9999
```

## 安全边界

- 仅在明确授权的测试主机和 Kubernetes 集群中开启修复。
- 自动修复只处理白名单范围；AI 输出不等于授权。
- 不要把 API key、SMTP 授权码、集群 token、kubeconfig、密码或生产地址提交到仓库。
- `root` 权限只应在隔离实验环境使用。
- 启用远程 server 时配置 token；非本机网络建议启用 TLS。
- 对未知故障优先选择 `email` 模式，让管理员审核建议后再操作。

## 开发与测试

```bash
python3 -m pytest -q
python3 ordis/test_ai_diagnose.py
python3 ordis/test_adversarial.py
python3 ordis/test_auto_skill.py
python3 ordis/test_levels.py
python3 ordis/test_k8s_checks.py
```

测试中的模型调用应 mock，避免把真实 API key 和外部服务带入测试。详细架构、部署、测试报告和接手说明见：

- [集成说明](docs/INTEGRATION.md)
- [测试报告](docs/TEST_REPORT_20260827.md)

## 当前定位

v1.0 是一个完整可运行的 Linux 运维自动化 Demo，重点展示“检测 → 确定性修复 → AI 接管 → 技能沉淀”的闭环。它仍需要更多生产化工作，包括更完整的权限隔离、配置管理、可观测性、升级策略和大规模集群验证。欢迎提交 Issue 和 Pull Request，共同完善项目。

## 许可证

本项目采用 MIT 许可证。

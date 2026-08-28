# Ordis 集成说明（公开版）

本文档介绍 Ordis v1.0 的公开架构、数据流、配置方式和运行边界，不包含内部测试地址、凭据或生产环境信息。

## 1. 系统定位

Ordis 是面向 Linux 的轻量运维自动化 Demo，目标是在低配置主机上完成主机状态、传统业务和 Kubernetes 基础状态检测，并通过白名单和效果回检实现受控自愈。

Ordis 不依赖 Prometheus。`ordis check` 与 `ordis k8s check` 是只读检测；`ordis run` 与 `ordis once` 才会进入规则触发、修复和 AI 接管流程。

## 2. 核心数据流

```text
采集器 / 业务探针 / kubectl
              ↓
        Finding 与稳定指纹
              ↓
       YAML 规则引擎与冷却
              ↓
 已审核技能 → 内置修复 → 效果回检
              ↓（失败或不支持）
 AI 诊断：故障证据 + 修复结果
       ├─ auto：受控执行 → 回检 → 待审核技能
       └─ email：生成建议 → SMTP 通知管理员
```

## 3. 模块说明

- `collectors/`：CPU、内存、磁盘、端口和安全事件等基础采集器。
- `traditional_checks.py`：systemd、Docker、inode、僵尸进程及 HTTP/TCP/DNS/TLS 业务探针。
- `k8s_checks.py`：通过 Kubernetes API 快照检查 Node、Pod、工作负载、Job、PVC 和 Service。
- `engine.py`：统一编排采集、规则匹配、冷却、修复、回检和事件记录。
- `health_repair.py`：传统业务与 Kubernetes 的受控修复入口。
- `ai_diagnose.py`：异步模型调用、故障指纹去重和诊断案例记录。
- `ai_mode.py`：自动修复与邮件建议两种 AI 接管模式。
- `promotion.py`：技能查重、审核、合并、规则同步和效果约束。
- `db.py`：SQLite 持久化、审计记录及旧 JSON 数据迁移。
- `cluster.py`：server/agent 状态上报、节点认证和订单状态管理。

## 4. 检测与修复边界

检测范围可以在 `ordis/rules.yaml` 中配置。传统业务和 Kubernetes 修复默认关闭，启用后仍必须指定允许的 systemd 服务、Docker 容器或 Kubernetes 命名空间。

Kubernetes 集群内运行时默认只需要 `get/list` 读取权限。空的 `allowed_namespaces` 不授予 Kubernetes 写权限。AI 输出不会绕过 Ordis 的权限、白名单和回检约束。

## 5. AI 与技能闭环

AI 接收到 Finding、采集证据和前置修复结果，生成根因、修复方向和候选命令。`auto` 模式只执行通过权限检查的候选命令，并要求回检成功；`email` 模式只发送建议，不执行 AI 命令。

AI 修复成功后只生成待审核技能草稿。系统会按故障指纹和规范化命令查重，管理员确认或合并后才会写入生效规则。纯诊断命令不能成为修复技能。

## 6. 数据与安全

- 默认运行数据位于 `~/.ordis/ordis.db`，可使用 `ORDIS_HOME` 指向持久化目录。
- 模型配置位于 `model.json`，权限应为 600；API key 不进入代码、文档或 Git。
- SMTP 密码只从部署环境变量或 Secret 注入，不写入仓库。
- 集群管理 token 与节点 token 分离；非本机部署建议启用 HTTPS。
- 生产环境应先完成权限隔离、审计、备份、升级和故障演练，再启用自动修复。

## 7. 常用命令

```bash
ordis setup
ordis config test
ordis check
ordis k8s doctor
ordis k8s check
ordis once
ordis run
ordis events
ordis cases
ordis skills
```

详细安装和示例请参阅仓库根目录 README。

# Changelog

All notable changes to Ordis will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-08-23

### Added
- **SMTP 邮箱告警功能**：新增邮件通知渠道，与钉钉/企业微信并行
  - 支持 SMTP/SSL 发送（163/QQ/Gmail 等）
  - 密码通过环境变量 `ORDIS_SMTP_PASSWORD` 注入
  - 失败时记录 warning 不影响守护进程运行
- `notifier.py`：新增 `email_send()` 函数
- `engine.py`：集成邮箱告警到通知流程
- `rules.yaml`：添加邮箱配置示例（163 邮箱）
- README 更新：通知方式对比表格、邮箱配置说明（中英文）

### Changed
- 顶部徽章更新为"3 种通知"

## [0.1.0] - 2026-08-16

### Added
- 初始版本发布
- 5 个采集器：CPU、内存、磁盘、进程端口、SSH 安全
- 4 个修复器：进程重启、内存清理、磁盘清理、SSH 封禁
- 规则引擎：YAML 配置 + Python `eval()` 条件
- 修复器端口回检：重启后验证端口，失败时触发 AI 诊断
- FastAPI Web 面板（VoltAgent 暗色主题）
- AI 副驾：Claude Code 集成，YOLO 模式无需审批
- 通知渠道：钉钉机器人、企业微信 webhook
- 登录保护：3 次失败锁定 30 分钟
- 聊天持久化：localStorage 保存消息
- systemd 服务配置：`ordisd.service`

[0.2.0]: https://github.com/laojingaoshou-lab/ordis/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/laojingaoshou-lab/ordis/releases/tag/v0.1.0

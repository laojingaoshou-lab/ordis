# Contributing to Ordis

感谢你愿意改进 Ordis。欢迎提交 bug 报告、文档改进、测试和新探测器/修复器。

## 提交 Issue

请尽量包含：

- Linux 发行版、Python 版本和 Ordis 版本；
- 复现步骤、期望行为和实际日志；
- 脱敏后的 `rules.yaml` 片段。

不要在 Issue 或 Pull Request 中提交 API key、SMTP 密码、集群 token、kubeconfig 或生产环境地址。

## 开发流程

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e .
python3 -m compileall -q ordis
```

修复行为时，请同时补充测试，并说明是否会触发系统服务、Docker 或 Kubernetes 的写操作。所有自动修复都应保持默认关闭、白名单约束和效果回检。

## Pull Request

PR 描述请说明动机、行为变化、测试命令和安全影响。小而聚焦的 PR 更容易审查；涉及破坏性配置变化时，请同步更新 README 和变更记录。

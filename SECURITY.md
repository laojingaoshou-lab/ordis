# Security Policy

## 报告安全问题

请不要在公开 Issue 中发布可利用的安全漏洞、凭据或生产日志。优先通过 GitHub Security Advisories 私下报告；如果无法使用，请先联系仓库维护者并提供最少必要信息。

## 使用边界

Ordis 当前是开源 Demo，不是经过生产认证的安全产品。请只在你拥有或明确获准管理的测试主机和集群上启用自动修复。API key、SMTP 密码、kubeconfig 和节点 token 必须通过环境变量或 Secret 注入，不能提交到仓库。

"""Opt-in, deterministic first-pass repairs for detector findings."""

from __future__ import annotations

import shlex
import subprocess
import time
from typing import Any


def _result(action: str, *, attempted: bool = True, success: bool = False,
            reason: str = "", command: str = "", **extra: Any) -> dict[str, Any]:
    action_result = {
        "action": action,
        "command": command,
        "success": success,
        "reason": reason,
    }
    result = {"attempted": attempted, "success": success,
              "actions": [action_result]}
    if reason:
        result["reason"] = reason
    result.update(extra)
    return result


def _run(argv: list[str], timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True,
                          timeout=timeout, check=False)


def _brief(result: subprocess.CompletedProcess) -> str:
    text = (result.stderr or "").strip() or (result.stdout or "").strip()
    return text.splitlines()[0][:300] if text else f"exit {result.returncode}"


def _repair_systemd(finding: dict[str, Any], config: dict[str, Any]) -> dict | None:
    if finding.get("reason") != "systemd_unit_failed":
        return None
    unit = str(finding.get("name") or "").strip()
    if not unit.endswith(".service") or "/" in unit or any(ch.isspace() for ch in unit):
        return _result("systemd restart", reason="systemd 单元名称无效")
    allowed = set(str(item) for item in config.get("allowed_systemd_units") or [])
    if unit not in allowed:
        return _result("systemd restart", attempted=False,
                       reason=f"systemd 单元未加入自动修复白名单: {unit}")
    command = ["systemctl", "restart", unit]
    check = ["systemctl", "is-active", "--quiet", unit]
    try:
        repaired = _run(command)
        if repaired.returncode != 0:
            return _result("systemd restart", command=shlex.join(command),
                           reason=_brief(repaired), rc=repaired.returncode)
        verified = _run(check, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _result("systemd restart", command=shlex.join(command),
                       reason=str(exc).splitlines()[0][:240])
    if verified.returncode:
        return _result("systemd restart", command=shlex.join(command),
                       reason="systemd is-active 回检失败", rc=verified.returncode)
    return _result("systemd restart", command=shlex.join(command), success=True,
                   check_command=shlex.join(check))


def _repair_docker(finding: dict[str, Any], config: dict[str, Any]) -> dict | None:
    if finding.get("reason") not in {
            "container_unhealthy", "container_restarting",
            "container_dead", "container_exited"}:
        return None
    name = str(finding.get("name") or "").strip()
    if not name or name.startswith("-") or any(ch.isspace() for ch in name):
        return _result("docker restart", reason="Docker 容器名称无效")
    allowed = set(str(item) for item in config.get("allowed_docker_containers") or [])
    if name not in allowed:
        return _result("docker restart", attempted=False,
                       reason=f"Docker 容器未加入自动修复白名单: {name}")
    command = ["docker", "restart", name]
    check = ["docker", "inspect", "-f", "{{.State.Running}}", name]
    try:
        repaired = _run(command)
        if repaired.returncode != 0:
            return _result("docker restart", command=shlex.join(command),
                           reason=_brief(repaired), rc=repaired.returncode)
        time.sleep(1)
        verified = _run(check, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _result("docker restart", command=shlex.join(command),
                       reason=str(exc).splitlines()[0][:240])
    if verified.returncode or verified.stdout.strip().lower() != "true":
        return _result("docker restart", command=shlex.join(command),
                       reason="Docker 容器运行状态回检失败", rc=verified.returncode)
    return _result("docker restart", command=shlex.join(command), success=True,
                   check_command=shlex.join(check))


def _repair_kubernetes(finding: dict[str, Any], config: dict[str, Any],
                       client: Any) -> dict | None:
    kind = finding.get("kind")
    reason = finding.get("reason")
    supported_pod = kind == "Pod" and reason in {
        "pod_crashloopbackoff", "pod_oomkilled", "pod_restarts_high"}
    supported_deployment = kind == "Deployment" and reason == "replicas_unavailable"
    if not supported_pod and not supported_deployment:
        return None
    namespace = str(finding.get("namespace") or "default")
    name = str(finding.get("name") or "")
    allowed = set(str(item) for item in config.get("allowed_namespaces") or [])
    if namespace not in allowed:
        return _result("kubernetes pod recreate", attempted=False,
                       reason=f"命名空间未加入自动修复白名单: {namespace}")
    timeout = float(config.get("timeout", 45))
    if supported_deployment:
        command = (f"kubectl rollout restart deployment/{shlex.quote(name)} "
                   f"-n {shlex.quote(namespace)}")
        action = "kubernetes deployment restart"
    else:
        command = f"kubectl delete pod {shlex.quote(name)} -n {shlex.quote(namespace)}"
        action = "kubernetes pod recreate"
    try:
        if supported_deployment:
            success, detail = client.restart_deployment(
                namespace, name, timeout=timeout)
        else:
            success, detail = client.recreate_managed_pod(
                namespace, name, timeout=timeout)
    except Exception as exc:
        return _result(action, command=command,
                       reason=str(exc).splitlines()[0][:240])
    return _result(action, command=command,
                   success=success, reason=detail)


def attempt(finding: dict[str, Any], config: dict[str, Any] | None = None,
            k8s_client: Any = None) -> dict[str, Any]:
    """Try one explicitly enabled repair and return its verification result."""
    config = config or {}
    if not config.get("enabled", False):
        return _result("health repair", attempted=False,
                       reason="检测自动修复未启用")
    if finding.get("source") == "traditional":
        repair = _repair_systemd(finding, config)
        if repair is None:
            repair = _repair_docker(finding, config)
        return repair or _result("health repair", attempted=False,
                                 reason="该故障没有安全的内置修复动作")
    if finding.get("source") == "kubernetes" and k8s_client is not None:
        repair = _repair_kubernetes(finding, config, k8s_client)
        return repair or _result("health repair", attempted=False,
                                 reason="该 Kubernetes 故障没有安全的内置修复动作")
    return _result("health repair", attempted=False,
                   reason="缺少对应的检测修复器")

"""Kubernetes inspection client with one opt-in managed-Pod repair action."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import requests

RESOURCE_PATHS = (
    "/api/v1/nodes",
    "/api/v1/pods",
    "/apis/apps/v1/deployments",
    "/apis/apps/v1/statefulsets",
    "/apis/apps/v1/daemonsets",
    "/apis/batch/v1/jobs",
    "/apis/batch/v1/cronjobs",
    "/api/v1/persistentvolumeclaims",
    "/api/v1/services",
    "/api/v1/endpoints",
)

KUBECTL_RESOURCES = (
    "nodes,pods,deployments,statefulsets,daemonsets,jobs,cronjobs,"
    "persistentvolumeclaims,services,endpoints"
)


class KubernetesUnavailable(RuntimeError):
    """Raised when no usable read-only cluster transport is available."""


class KubernetesClient:
    def __init__(self, *, context: str = "", kubeconfig: str = "",
                 timeout: float = 15, environ: dict[str, str] | None = None):
        self.context = context
        self.kubeconfig = kubeconfig
        self.timeout = timeout
        self.environ = environ if environ is not None else os.environ
        host = self.environ.get("KUBERNETES_SERVICE_HOST", "")
        port = self.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443")
        self.in_cluster_url = f"https://{host}:{port}" if host else ""

    @property
    def mode(self) -> str:
        if self.in_cluster_url:
            return "in-cluster"
        if shutil.which("kubectl"):
            return "kubectl"
        return "unavailable"

    def doctor(self) -> dict[str, Any]:
        result = {
            "ok": False,
            "mode": self.mode,
            "context": self.context or "current",
            "kubeconfig": bool(self.kubeconfig or self.environ.get("KUBECONFIG")),
            "cluster": "",
            "error": "",
        }
        try:
            snapshot = self.snapshot(resources=("nodes",))
            nodes = snapshot.get("Node", [])
            result["ok"] = True
            result["nodes"] = len(nodes)
            result["cluster"] = self.cluster_name()
        except KubernetesUnavailable as exc:
            result["error"] = str(exc).splitlines()[0][:300]
        return result

    def cluster_name(self) -> str:
        if self.context:
            return self.context
        if self.mode == "kubectl":
            command = self._kubectl_base() + ["config", "current-context"]
            result = subprocess.run(command, capture_output=True, text=True,
                                    timeout=self.timeout, check=False)
            if result.returncode == 0:
                return result.stdout.strip() or "current"
        return self.environ.get("ORDIS_K8S_CLUSTER", "in-cluster")

    def snapshot(self, resources: tuple[str, ...] | None = None) -> dict[str, list[dict]]:
        if self.mode == "in-cluster":
            return self._in_cluster_snapshot(resources)
        if self.mode == "kubectl":
            return self._kubectl_snapshot(resources)
        raise KubernetesUnavailable(
            "Kubernetes is not configured: run inside a Pod or install kubectl with a valid context")

    def recreate_managed_pod(self, namespace: str, name: str,
                             timeout: float = 45) -> tuple[bool, str]:
        """Delete one controller-owned Pod and wait for its replacement."""
        if self.mode != "kubectl":
            return False, "Pod 自动重建目前仅支持 kubectl 模式"
        if not _safe_name(namespace) or not _safe_name(name):
            return False, "Pod 或命名空间名称无效"

        get_old = self._kubectl_base() + [
            "get", "pod", name, "--namespace", namespace,
            "--request-timeout", f"{int(self.timeout)}s", "-o", "json"]
        old_result = self._run_kubectl(get_old)
        if old_result.returncode:
            return False, _brief_result(old_result)
        try:
            old_pod = json.loads(old_result.stdout)
        except json.JSONDecodeError:
            return False, "kubectl 返回了无效 Pod JSON"
        owners = (old_pod.get("metadata") or {}).get("ownerReferences") or []
        controller = next((item for item in owners if item.get("controller")), None)
        if not controller or not controller.get("uid"):
            return False, "拒绝删除没有控制器的独立 Pod"
        owner_uid = str(controller["uid"])

        delete = self._kubectl_base() + [
            "delete", "pod", name, "--namespace", namespace,
            "--wait=false", "--request-timeout", f"{int(self.timeout)}s"]
        deleted = self._run_kubectl(delete)
        if deleted.returncode:
            return False, _brief_result(deleted)

        deadline = time.monotonic() + max(1, timeout)
        while time.monotonic() < deadline:
            list_command = self._kubectl_base() + [
                "get", "pods", "--namespace", namespace,
                "--request-timeout", f"{int(self.timeout)}s", "-o", "json"]
            listed = self._run_kubectl(list_command)
            if listed.returncode == 0:
                try:
                    pods = json.loads(listed.stdout).get("items", [])
                except json.JSONDecodeError:
                    pods = []
                for pod in pods:
                    metadata = pod.get("metadata") or {}
                    if metadata.get("name") == name:
                        continue
                    pod_owners = metadata.get("ownerReferences") or []
                    if not any(str(item.get("uid")) == owner_uid
                               for item in pod_owners):
                        continue
                    status = pod.get("status") or {}
                    conditions = {item.get("type"): item
                                  for item in status.get("conditions") or []}
                    if (status.get("phase") == "Running"
                            and conditions.get("Ready", {}).get("status") == "True"):
                        return True, "替代 Pod 已 Ready"
            time.sleep(2)
        return False, "等待替代 Pod Ready 超时"

    def restart_deployment(self, namespace: str, name: str,
                           timeout: float = 45) -> tuple[bool, str]:
        """Roll out one deployment and verify that the rollout completes."""
        if self.mode != "kubectl":
            return False, "Deployment 自动重启目前仅支持 kubectl 模式"
        if not _safe_name(namespace) or not _safe_name(name):
            return False, "Deployment 或命名空间名称无效"
        resource = f"deployment/{name}"
        restart = self._kubectl_base() + [
            "rollout", "restart", resource, "--namespace", namespace,
            "--request-timeout", f"{int(self.timeout)}s"]
        restarted = self._run_kubectl(restart)
        if restarted.returncode:
            return False, _brief_result(restarted)
        status = self._kubectl_base() + [
            "rollout", "status", resource, "--namespace", namespace,
            f"--timeout={max(1, int(timeout))}s",
            "--request-timeout", f"{max(1, int(timeout))}s"]
        verified = self._run_kubectl(
            status, timeout=max(timeout + 5, self.timeout + 5))
        if verified.returncode:
            return False, _brief_result(verified)
        return True, "Deployment rollout 已完成"

    def _run_kubectl(self, command: list[str],
                     timeout: float | None = None) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(command, capture_output=True, text=True,
                                  timeout=timeout or self.timeout + 5,
                                  check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise KubernetesUnavailable(str(exc)) from exc

    def _kubectl_base(self) -> list[str]:
        command = ["kubectl"]
        if self.kubeconfig:
            command += ["--kubeconfig", self.kubeconfig]
        if self.context:
            command += ["--context", self.context]
        return command

    def _kubectl_snapshot(self, resources: tuple[str, ...] | None) -> dict[str, list[dict]]:
        requested = ",".join(resources) if resources else KUBECTL_RESOURCES
        command = self._kubectl_base() + [
            "get", requested, "--all-namespaces", "--ignore-not-found=true",
            "--request-timeout", f"{int(self.timeout)}s", "-o", "json",
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True,
                                    timeout=self.timeout + 5, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise KubernetesUnavailable(str(exc)) from exc
        if result.returncode:
            error = (result.stderr or result.stdout).strip()
            raise KubernetesUnavailable(error or f"kubectl exited {result.returncode}")
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise KubernetesUnavailable("kubectl returned invalid JSON") from exc
        return _group_items(data.get("items", []))

    def _in_cluster_snapshot(self, resources: tuple[str, ...] | None) -> dict[str, list[dict]]:
        token_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
        ca_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
        try:
            token = token_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise KubernetesUnavailable("service account token is unavailable") from exc
        requested = set(resources or ())
        items = []
        headers = {"Authorization": f"Bearer {token}"}
        verify: bool | str = str(ca_path) if ca_path.exists() else True
        for path in RESOURCE_PATHS:
            resource_name = path.rsplit("/", 1)[-1]
            if requested and resource_name not in requested:
                continue
            try:
                response = requests.get(self.in_cluster_url + path, headers=headers,
                                        timeout=self.timeout, verify=verify)
                response.raise_for_status()
                items.extend(response.json().get("items", []))
            except (requests.RequestException, ValueError) as exc:
                # Never include headers or token in the error.
                raise KubernetesUnavailable(
                    f"Kubernetes API {path} failed: {str(exc).splitlines()[0][:240]}") from exc
        return _group_items(items)


def _group_items(items: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for item in items:
        kind = str(item.get("kind", "Unknown"))
        grouped.setdefault(kind, []).append(item)
    return grouped


def _safe_name(value: str) -> bool:
    return bool(value and not value.startswith("-")
                and all(char.isalnum() or char in ".-_" for char in value))


def _brief_result(result: subprocess.CompletedProcess) -> str:
    text = (result.stderr or "").strip() or (result.stdout or "").strip()
    return text.splitlines()[0][:240] if text else f"kubectl exit {result.returncode}"

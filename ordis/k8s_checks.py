"""Kubernetes resource-state detectors that do not require Prometheus."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from findings import make_finding, report
from k8s_client import KubernetesClient, KubernetesUnavailable

BAD_WAITING_REASONS = {
    "CrashLoopBackOff": "critical",
    "ImagePullBackOff": "high",
    "ErrImagePull": "high",
    "CreateContainerConfigError": "high",
    "CreateContainerError": "high",
    "RunContainerError": "high",
}


def _meta(item: dict) -> tuple[str, str]:
    metadata = item.get("metadata") or {}
    return metadata.get("namespace", "default"), metadata.get("name", "unknown")


def _age_seconds(timestamp: str | None) -> float:
    if not timestamp:
        return 0
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        return max(0, (datetime.now(timezone.utc) - parsed).total_seconds())
    except ValueError:
        return 0


def _finding(cluster: str, item: dict, reason: str, summary: str,
             severity: str, evidence: dict[str, Any]) -> dict[str, Any]:
    namespace, name = _meta(item)
    kind = item.get("kind", "Unknown")
    return make_finding(
        source="kubernetes", cluster=cluster, namespace=namespace,
        kind=kind, name=name, reason=reason, summary=summary,
        severity=severity, evidence=evidence,
        fingerprint=f"k8s:{cluster}:{namespace}:{kind}:{name}:{reason}")


def _detect_nodes(cluster: str, items: list[dict],
                  config: dict[str, Any]) -> list[dict]:
    findings = []
    ignored = set(str(item) for item in config.get("node_ignore_names") or [])
    for item in items:
        if _meta(item)[1] in ignored:
            continue
        conditions = {c.get("type"): c for c in (item.get("status", {}).get("conditions") or [])}
        ready = conditions.get("Ready", {})
        if ready.get("status") != "True":
            name = _meta(item)[1]
            findings.append(_finding(
                cluster, item, "node_not_ready", f"node {name} is not Ready",
                "critical", {"condition": ready}))
        for condition_name in ("MemoryPressure", "DiskPressure", "PIDPressure", "NetworkUnavailable"):
            condition = conditions.get(condition_name, {})
            if condition.get("status") == "True":
                name = _meta(item)[1]
                findings.append(_finding(
                    cluster, item, f"node_{condition_name.lower()}",
                    f"node {name} reports {condition_name}", "high",
                    {"condition": condition}))
    return findings


def _detect_pods(cluster: str, items: list[dict], config: dict[str, Any]) -> list[dict]:
    findings = []
    pending_seconds = int(config.get("pod_pending_seconds", 300))
    restart_threshold = int(config.get("pod_restart_threshold", 5))
    for item in items:
        namespace, name = _meta(item)
        status = item.get("status") or {}
        phase = status.get("phase", "Unknown")
        age = _age_seconds(item.get("metadata", {}).get("creationTimestamp"))
        if phase == "Pending" and age >= pending_seconds:
            findings.append(_finding(
                cluster, item, "pod_pending", f"pod {namespace}/{name} has been Pending for {int(age)}s",
                "high", {"phase": phase, "age_seconds": int(age),
                         "threshold": pending_seconds}))

        statuses = (status.get("initContainerStatuses") or []) + (status.get("containerStatuses") or [])
        for container in statuses:
            cname = container.get("name", "unknown")
            waiting = (container.get("state") or {}).get("waiting") or {}
            reason = waiting.get("reason")
            if reason in BAD_WAITING_REASONS:
                findings.append(_finding(
                    cluster, item, f"pod_{reason.lower()}",
                    f"pod {namespace}/{name} container {cname}: {reason}",
                    BAD_WAITING_REASONS[reason],
                    {"container": cname, "image": container.get("image", ""),
                     "state": waiting,
                     "owners": item.get("metadata", {}).get("ownerReferences") or []}))
            terminated = (container.get("lastState") or {}).get("terminated") or {}
            if terminated.get("reason") == "OOMKilled":
                findings.append(_finding(
                    cluster, item, "pod_oomkilled",
                    f"pod {namespace}/{name} container {cname} was OOMKilled",
                    "critical", {"container": cname, "terminated": terminated}))
            restarts = int(container.get("restartCount") or 0)
            if restarts >= restart_threshold:
                findings.append(_finding(
                    cluster, item, "pod_restarts_high",
                    f"pod {namespace}/{name} container {cname} restarted {restarts} times",
                    "medium", {"container": cname, "restart_count": restarts,
                               "threshold": restart_threshold}))
    return findings


def _detect_workloads(cluster: str, snapshot: dict[str, list[dict]]) -> list[dict]:
    findings = []
    for kind in ("Deployment", "StatefulSet", "DaemonSet"):
        for item in snapshot.get(kind, []):
            spec, status = item.get("spec") or {}, item.get("status") or {}
            if kind == "DaemonSet":
                desired = int(status.get("desiredNumberScheduled") or 0)
                available = int(status.get("numberAvailable") or 0)
            else:
                desired = int(spec.get("replicas") if spec.get("replicas") is not None else 1)
                available = int(status.get("availableReplicas") or 0)
            if available < desired:
                namespace, name = _meta(item)
                findings.append(_finding(
                    cluster, item, "replicas_unavailable",
                    f"{kind} {namespace}/{name} has {available}/{desired} available replicas",
                    "high", {"desired": desired, "available": available,
                             "ready": status.get("readyReplicas", 0),
                             "updated": status.get("updatedReplicas", 0),
                             "images": {
                                 str(container.get("name") or "unknown"):
                                 str(container.get("image") or "")
                                 for container in (((spec.get("template") or {})
                                                    .get("spec") or {})
                                                   .get("containers") or [])
                             },
                             "conditions": status.get("conditions") or [],
                             "annotations": (item.get("metadata", {})
                                             .get("annotations") or {})}))
    return findings


def _detect_jobs(cluster: str, items: list[dict]) -> list[dict]:
    findings = []
    for item in items:
        namespace, name = _meta(item)
        status = item.get("status") or {}
        failed_condition = next((c for c in status.get("conditions") or []
                                 if c.get("type") == "Failed" and c.get("status") == "True"), None)
        if failed_condition or int(status.get("failed") or 0) > 0:
            findings.append(_finding(
                cluster, item, "job_failed", f"Job {namespace}/{name} failed",
                "high", {"failed": status.get("failed", 0),
                         "condition": failed_condition or {}}))
    return findings


def _detect_pvcs(cluster: str, items: list[dict], config: dict[str, Any]) -> list[dict]:
    findings = []
    pending_seconds = int(config.get("pvc_pending_seconds", 300))
    for item in items:
        namespace, name = _meta(item)
        phase = (item.get("status") or {}).get("phase", "Unknown")
        age = _age_seconds(item.get("metadata", {}).get("creationTimestamp"))
        if phase == "Lost" or (phase == "Pending" and age >= pending_seconds):
            severity = "critical" if phase == "Lost" else "high"
            findings.append(_finding(
                cluster, item, f"pvc_{phase.lower()}",
                f"PVC {namespace}/{name} is {phase}", severity,
                {"phase": phase, "age_seconds": int(age)}))
    return findings


def _detect_services(cluster: str, services: list[dict], endpoints: list[dict]) -> list[dict]:
    findings = []
    endpoint_map = {_meta(item): item for item in endpoints}
    for item in services:
        spec = item.get("spec") or {}
        if spec.get("type") == "ExternalName" or not spec.get("selector"):
            continue
        namespace, name = _meta(item)
        endpoint = endpoint_map.get((namespace, name), {})
        subsets = endpoint.get("subsets") or []
        ready = sum(len(subset.get("addresses") or []) for subset in subsets)
        if ready == 0:
            not_ready = sum(len(subset.get("notReadyAddresses") or []) for subset in subsets)
            findings.append(_finding(
                cluster, item, "service_no_ready_endpoints",
                f"Service {namespace}/{name} has no ready endpoints", "high",
                {"ready_addresses": ready, "not_ready_addresses": not_ready,
                 "ports": spec.get("ports") or []}))
    return findings


def detect(snapshot: dict[str, list[dict]], cluster: str,
           config: dict[str, Any] | None = None) -> list[dict]:
    config = config or {}
    namespaces = set(str(item) for item in config.get("namespaces") or [])
    if namespaces:
        snapshot = {
            kind: items if kind == "Node" else [
                item for item in items if _meta(item)[0] in namespaces]
            for kind, items in snapshot.items()
        }
    findings = []
    findings.extend(_detect_nodes(cluster, snapshot.get("Node", []), config))
    findings.extend(_detect_pods(cluster, snapshot.get("Pod", []), config))
    findings.extend(_detect_workloads(cluster, snapshot))
    findings.extend(_detect_jobs(cluster, snapshot.get("Job", [])))
    findings.extend(_detect_pvcs(cluster, snapshot.get("PersistentVolumeClaim", []), config))
    findings.extend(_detect_services(cluster, snapshot.get("Service", []),
                                      snapshot.get("Endpoints", [])))
    # One object can expose several independent failures. Fingerprints keep them stable.
    allowed_reasons = set(str(item) for item in config.get("reason_allowlist") or [])
    if allowed_reasons:
        findings = [item for item in findings
                    if item.get("reason") in allowed_reasons]
    unique = {item["fingerprint"]: item for item in findings}
    return list(unique.values())


def run_checks(config: dict[str, Any] | None = None,
               client: KubernetesClient | None = None) -> dict[str, Any]:
    config = config or {}
    client = client or KubernetesClient(
        context=str(config.get("context") or ""),
        kubeconfig=str(config.get("kubeconfig") or ""),
        timeout=float(config.get("timeout", 15)))
    checks = []
    try:
        snapshot = client.snapshot()
        cluster = str(config.get("cluster_name") or client.cluster_name())
    except KubernetesUnavailable as exc:
        detail = str(exc).splitlines()[0][:300]
        checks.append({"name": "kubernetes_api", "status": "error",
                       "detail": detail})
        cluster = str(config.get("cluster_name") or config.get("context") or "unknown")
        finding = make_finding(
            source="kubernetes", cluster=cluster, kind="Cluster", name=cluster,
            reason="kubernetes_api_unavailable", severity="critical",
            summary=f"Kubernetes API for cluster {cluster} is unavailable",
            evidence={"error": detail},
            fingerprint=f"k8s:{cluster}:api:unavailable")
        return report("kubernetes", checks, [finding], cluster=cluster)
    counts = {kind: len(items) for kind, items in sorted(snapshot.items())}
    checks.append({"name": "kubernetes_api", "status": "ok",
                   "resources": counts})
    findings = detect(snapshot, cluster, config)
    return report("kubernetes", checks, findings, cluster=cluster)

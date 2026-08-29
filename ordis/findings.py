"""Normalized health findings shared by host and Kubernetes detectors."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

SEVERITIES = ("info", "low", "medium", "high", "critical")


def make_finding(*, source: str, reason: str, summary: str,
                 severity: str = "medium", cluster: str = "",
                 namespace: str = "", kind: str = "", name: str = "",
                 evidence: dict[str, Any] | None = None,
                 fingerprint: str | None = None) -> dict[str, Any]:
    """Build a stable, JSON-safe finding."""
    if severity not in SEVERITIES:
        raise ValueError(f"invalid severity: {severity}")
    identity = {
        "source": source,
        "cluster": cluster,
        "namespace": namespace,
        "kind": kind,
        "name": name,
        "reason": reason,
    }
    if not fingerprint:
        raw = json.dumps(identity, sort_keys=True, ensure_ascii=True)
        suffix = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
        fingerprint = f"{source}:{reason}:{suffix}"
    return {
        "fingerprint": fingerprint,
        "source": source,
        "cluster": cluster,
        "namespace": namespace,
        "kind": kind,
        "name": name,
        "reason": reason,
        "severity": severity,
        "summary": summary,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "evidence": evidence or {},
    }


def report(source: str, checks: list[dict[str, Any]],
           findings: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    """Return the stable CLI/report envelope used by all detectors."""
    payload = {
        "ok": not findings and not any(c.get("status") == "error"
                                        for c in checks),
        "source": source,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "findings": len(findings),
            "critical": sum(f["severity"] == "critical" for f in findings),
            "high": sum(f["severity"] == "high" for f in findings),
            "errors": sum(c.get("status") == "error" for c in checks),
        },
        "checks": checks,
        "findings": findings,
    }
    payload.update(extra)
    return payload

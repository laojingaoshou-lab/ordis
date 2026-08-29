"""Read-only checks for common Linux host and business service failures."""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import ssl
import subprocess
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import psutil
import requests
from findings import make_finding, report

DEFAULTS = {
    "systemd": True,
    "systemd_units": [],
    "systemd_ignore_units": [],
    "docker": True,
    "zombie_threshold": 5,
    "inode_threshold": 90,
    "inode_paths": ["/"],
    "endpoints": [],
}


def _run(command: list[str], timeout: float = 8) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True,
                          timeout=timeout, check=False)


def _brief_error(exc: Exception) -> str:
    text = str(exc).splitlines()[0].strip()
    text = re.sub(r"https?://[^\s]+", lambda match: _safe_url(match.group(0)), text)
    text = re.sub(r"(?i)(token|api[_-]?key|password)=([^&\s]+)",
                  r"\1=[redacted]", text)
    return text[:240] or exc.__class__.__name__


def _safe_url(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return parsed._replace(netloc=host, query="", fragment="").geturl()


def _systemd_check(config: dict[str, Any],
                   findings: list[dict[str, Any]]) -> dict[str, Any]:
    if not shutil.which("systemctl"):
        return {"name": "systemd", "status": "skipped",
                "detail": "systemctl not installed"}
    try:
        result = _run(["systemctl", "--failed", "--type=service",
                       "--no-legend", "--no-pager"])
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"name": "systemd", "status": "error",
                "detail": _brief_error(exc)}
    # Containers commonly have systemctl installed without systemd as PID 1.
    combined = (result.stdout + "\n" + result.stderr).strip()
    if result.returncode and "not been booted with systemd" in combined:
        return {"name": "systemd", "status": "skipped",
                "detail": "systemd is not PID 1"}
    if result.returncode:
        return {"name": "systemd", "status": "error",
                "detail": combined[:240] or f"exit {result.returncode}"}

    units = []
    ignored = 0
    monitored = set(config.get("systemd_units") or [])
    ignored_units = set(config.get("systemd_ignore_units") or [])
    for raw in result.stdout.splitlines():
        line = raw.strip().lstrip("●").strip()
        if not line or "0 loaded units listed" in line:
            continue
        unit = line.split()[0]
        if not unit.endswith(".service"):
            continue
        if unit in ignored_units or (monitored and unit not in monitored):
            ignored += 1
            continue
        units.append(unit)
        findings.append(make_finding(
            source="traditional", kind="SystemdService", name=unit,
            reason="systemd_unit_failed", severity="high",
            summary=f"systemd 服务 {unit} 处于 failed 状态",
            evidence={"unit": unit, "status_line": raw.strip()},
            fingerprint=f"host:systemd:{unit}:failed"))
    return {"name": "systemd", "status": "ok", "failed_units": len(units),
            "ignored_units": ignored}


def _docker_check(config: dict[str, Any],
                  findings: list[dict[str, Any]]) -> dict[str, Any]:
    if not shutil.which("docker"):
        return {"name": "docker", "status": "skipped",
                "detail": "docker not installed"}
    try:
        result = _run(["docker", "ps", "-a", "--format", "{{json .}}"])
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"name": "docker", "status": "error",
                "detail": _brief_error(exc)}
    combined = (result.stderr or result.stdout).strip()
    if result.returncode:
        # Permission denied and an unavailable daemon are visibility problems.
        return {"name": "docker", "status": "error",
                "detail": combined[:240] or f"exit {result.returncode}"}

    monitored = set(config.get("docker_containers") or [])
    count = 0
    for raw in result.stdout.splitlines():
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            continue
        count += 1
        name = item.get("Names") or item.get("ID") or "unknown"
        status = item.get("Status", "")
        state = item.get("State", "").lower()
        lowered = status.lower()
        reason = ""
        severity = "high"
        if "unhealthy" in lowered:
            reason = "container_unhealthy"
        elif state == "restarting" or lowered.startswith("restarting"):
            reason = "container_restarting"
        elif state == "dead":
            reason = "container_dead"
            severity = "critical"
        elif name in monitored and state == "exited":
            reason = "container_exited"
        if reason:
            findings.append(make_finding(
                source="traditional", kind="Container", name=name,
                reason=reason, severity=severity,
                summary=f"Docker 容器 {name} 状态异常：{status or state}",
                evidence={"state": state, "status": status,
                          "image": item.get("Image", "")},
                fingerprint=f"host:docker:{name}:{reason}"))
    return {"name": "docker", "status": "ok", "containers": count}


def _process_check(config: dict[str, Any],
                   findings: list[dict[str, Any]]) -> dict[str, Any]:
    zombies = []
    try:
        for proc in psutil.process_iter(["pid", "name", "status"]):
            if proc.info.get("status") == psutil.STATUS_ZOMBIE:
                zombies.append({"pid": proc.info.get("pid"),
                                "name": proc.info.get("name")})
    except (psutil.Error, OSError) as exc:
        return {"name": "process", "status": "error",
                "detail": _brief_error(exc)}
    threshold = max(0, int(config.get("zombie_threshold", 5)))
    if len(zombies) > threshold:
        findings.append(make_finding(
            source="traditional", kind="Host", name=socket.gethostname(),
            reason="zombie_processes", severity="medium",
            summary=f"主机存在 {len(zombies)} 个僵尸进程",
            evidence={"count": len(zombies), "threshold": threshold,
                      "sample": zombies[:20]},
            fingerprint="host:process:zombies"))
    return {"name": "process", "status": "ok", "zombies": len(zombies)}


def _inode_check(config: dict[str, Any],
                 findings: list[dict[str, Any]]) -> dict[str, Any]:
    threshold = float(config.get("inode_threshold", 90))
    paths = config.get("inode_paths", ["/"])
    checked = 0
    errors = []
    for raw_path in paths:
        path = str(raw_path)
        try:
            stats = os.statvfs(path)
            total = stats.f_files
            free = stats.f_ffree
            if total <= 0:
                continue
            checked += 1
            used_pct = round((total - free) / total * 100, 2)
            if used_pct >= threshold:
                findings.append(make_finding(
                    source="traditional", kind="Filesystem", name=path,
                    reason="inode_usage_high", severity="high",
                    summary=f"文件系统 {path} 的 inode 使用率为 {used_pct}%",
                    evidence={"path": path, "used_pct": used_pct,
                              "threshold": threshold, "free": free,
                              "total": total},
                    fingerprint=f"host:filesystem:{path}:inode_high"))
        except (OSError, ValueError) as exc:
            errors.append(f"{path}: {_brief_error(exc)}")
    status = "error" if errors and not checked else "ok"
    result = {"name": "inode", "status": status, "paths": checked}
    if errors:
        result["detail"] = "; ".join(errors)[:240]
    return result


def _endpoint_finding(item: dict[str, Any], reason: str, detail: str,
                      evidence: dict[str, Any]) -> dict[str, Any]:
    raw_url = str(item.get("url") or "")
    name = item.get("name") or (_safe_url(raw_url) if raw_url else None) \
        or item.get("host") or "endpoint"
    return make_finding(
        source="traditional", kind="Endpoint", name=str(name),
        reason=reason, severity=item.get("severity", "high"),
        summary=f"业务探针 {name} 检测失败：{detail}", evidence=evidence,
        fingerprint=f"host:endpoint:{name}:{reason}")


def _check_http(item: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
    url = str(item["url"])
    timeout = float(item.get("timeout", 5))
    response = requests.get(url, timeout=timeout,
                            allow_redirects=item.get("follow_redirects", True))
    expected = item.get("expected_status")
    if expected is None:
        accepted = 200 <= response.status_code < 400
    elif isinstance(expected, list):
        accepted = response.status_code in [int(v) for v in expected]
    else:
        accepted = response.status_code == int(expected)
    contains = item.get("body_contains")
    if accepted and contains is not None:
        accepted = str(contains) in response.text
    evidence = {"url": _safe_url(url), "status_code": response.status_code,
                "elapsed_ms": round(response.elapsed.total_seconds() * 1000, 1)}
    if not accepted:
        detail = f"unexpected HTTP {response.status_code}"
        if contains is not None and response.status_code < 400:
            detail = "required response text is missing"
        return False, detail, evidence
    return True, "ok", evidence


def _check_tcp(item: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
    host, port = str(item["host"]), int(item["port"])
    started = datetime.now(timezone.utc)
    with socket.create_connection((host, port), timeout=float(item.get("timeout", 3))):
        pass
    elapsed = (datetime.now(timezone.utc) - started).total_seconds() * 1000
    return True, "ok", {"host": host, "port": port,
                         "elapsed_ms": round(elapsed, 1)}


def _check_dns(item: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
    host = str(item["host"])
    addresses = sorted({entry[4][0] for entry in socket.getaddrinfo(host, None)})
    return True, "ok", {"host": host, "addresses": addresses[:10]}


def _check_tls(item: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
    host = str(item["host"])
    port = int(item.get("port", 443))
    context = ssl.create_default_context()
    with socket.create_connection(
            (host, port), timeout=float(item.get("timeout", 5))) as raw, \
            context.wrap_socket(raw, server_hostname=host) as secure:
        cert = secure.getpeercert()
    expires = datetime.strptime(
        cert["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(
            tzinfo=timezone.utc)
    days = (expires - datetime.now(timezone.utc)).total_seconds() / 86400
    evidence = {"host": host, "port": port,
                "expires_at": expires.isoformat(), "days_left": round(days, 1)}
    minimum = float(item.get("minimum_days", 14))
    if days < minimum:
        return False, f"certificate expires in {days:.1f} days", evidence
    return True, "ok", evidence


def _endpoint_checks(config: dict[str, Any],
                     findings: list[dict[str, Any]]) -> dict[str, Any]:
    endpoints = config.get("endpoints") or []
    passed = 0
    errors = 0
    checkers = {"http": _check_http, "tcp": _check_tcp,
                "dns": _check_dns, "tls": _check_tls}
    for item in endpoints:
        kind = str(item.get("type", "http")).lower()
        checker = checkers.get(kind)
        if checker is None:
            errors += 1
            continue
        try:
            ok, detail, evidence = checker(item)
        except (KeyError, ValueError, OSError, requests.RequestException,
                ssl.SSLError) as exc:
            ok, detail = False, _brief_error(exc)
            if item.get("url"):
                detail = detail.replace(str(item["url"]), _safe_url(str(item["url"])))
            evidence = {key: item.get(key) for key in ("url", "host", "port")
                        if item.get(key) is not None}
            if evidence.get("url"):
                evidence["url"] = _safe_url(str(evidence["url"]))
        if ok:
            passed += 1
        else:
            reason = "tls_certificate_expiring" if kind == "tls" and "expires" in detail else f"{kind}_endpoint_failed"
            findings.append(_endpoint_finding(item, reason, detail, evidence))
    return {"name": "endpoints", "status": "ok", "configured": len(endpoints),
            "passed": passed, "failed": len(endpoints) - passed - errors,
            "invalid": errors}


def run_checks(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run configured traditional checks and return a stable report."""
    merged = dict(DEFAULTS)
    merged.update(config or {})
    findings: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    if merged.get("systemd", True):
        checks.append(_systemd_check(merged, findings))
    if merged.get("docker", True):
        checks.append(_docker_check(merged, findings))
    checks.append(_process_check(merged, findings))
    checks.append(_inode_check(merged, findings))
    checks.append(_endpoint_checks(merged, findings))
    return report("traditional", checks, findings)

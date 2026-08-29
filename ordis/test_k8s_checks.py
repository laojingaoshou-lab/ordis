"""Kubernetes snapshot detector and transport tests."""

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import k8s_checks
from k8s_client import KubernetesClient


def resource(kind, name, namespace="default", spec=None, status=None,
             age_seconds=600):
    return {
        "apiVersion": "v1",
        "kind": kind,
        "metadata": {
            "name": name,
            "namespace": namespace,
            "creationTimestamp": (
                datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
            ).isoformat(),
        },
        "spec": spec or {},
        "status": status or {},
    }


class TestKubernetesDetectors(unittest.TestCase):
    def test_detects_six_resource_failure_families(self):
        node = resource("Node", "node-a", status={"conditions": [
            {"type": "Ready", "status": "False", "reason": "KubeletDown"},
            {"type": "DiskPressure", "status": "True"},
        ]})
        pod = resource("Pod", "api-1", "prod", status={
            "phase": "Pending",
            "containerStatuses": [{
                "name": "api", "restartCount": 8,
                "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137}},
            }],
        })
        deployment = resource(
            "Deployment", "api", "prod", spec={"replicas": 3},
            status={"availableReplicas": 1, "readyReplicas": 1})
        job = resource("Job", "backup", "prod", status={"failed": 1})
        pvc = resource("PersistentVolumeClaim", "data", "prod",
                       status={"phase": "Lost"})
        service = resource("Service", "api", "prod",
                           spec={"selector": {"app": "api"}, "ports": [{"port": 80}]})

        findings = k8s_checks.detect({
            "Node": [node], "Pod": [pod], "Deployment": [deployment],
            "Job": [job], "PersistentVolumeClaim": [pvc],
            "Service": [service], "Endpoints": [],
        }, "demo", {"pod_restart_threshold": 5})

        reasons = {finding["reason"] for finding in findings}
        self.assertTrue({
            "node_not_ready", "node_diskpressure", "pod_pending",
            "pod_crashloopbackoff", "pod_oomkilled", "pod_restarts_high",
            "replicas_unavailable", "job_failed", "pvc_lost",
            "service_no_ready_endpoints",
        }.issubset(reasons))
        self.assertEqual(len({f["fingerprint"] for f in findings}), len(findings))

    def test_workload_finding_includes_operator_repair_hint(self):
        deployment = resource(
            "Deployment", "api", "ordis-e2e", spec={
                "replicas": 1,
                "template": {"spec": {"containers": [
                    {"name": "web", "image": "nginx:broken"}]}}},
            status={"availableReplicas": 0})
        deployment["metadata"]["annotations"] = {
            "ordis.ai/repair-image": "nginx:stable"}
        finding = k8s_checks.detect(
            {"Deployment": [deployment]}, "demo")[0]
        self.assertEqual(finding["evidence"]["images"]["web"], "nginx:broken")
        self.assertEqual(
            finding["evidence"]["annotations"]["ordis.ai/repair-image"],
            "nginx:stable")

    def test_healthy_snapshot_has_no_findings(self):
        node = resource("Node", "node-a", status={
            "conditions": [{"type": "Ready", "status": "True"}]})
        deployment = resource("Deployment", "api", "prod",
                              spec={"replicas": 2},
                              status={"availableReplicas": 2})
        service = resource("Service", "api", "prod",
                           spec={"selector": {"app": "api"}})
        endpoints = resource("Endpoints", "api", "prod")
        endpoints["subsets"] = [{"addresses": [{"ip": "10.0.0.2"}]}]
        findings = k8s_checks.detect({
            "Node": [node], "Deployment": [deployment],
            "Service": [service], "Endpoints": [endpoints],
        }, "demo")
        self.assertEqual(findings, [])

    def test_optional_scope_filters_nodes_namespaces_and_reasons(self):
        ignored_node = resource("Node", "node1", status={
            "conditions": [{"type": "Ready", "status": "False"}]})
        other_pod = resource("Pod", "bad", "production", status={
            "phase": "Pending"})
        deployment = resource(
            "Deployment", "api", "ordis-e2e", spec={"replicas": 1},
            status={"availableReplicas": 0})
        findings = k8s_checks.detect({
            "Node": [ignored_node], "Pod": [other_pod],
            "Deployment": [deployment],
        }, "demo", {
            "node_ignore_names": ["node1"],
            "namespaces": ["ordis-e2e"],
            "reason_allowlist": ["replicas_unavailable"],
        })
        self.assertEqual([item["reason"] for item in findings],
                         ["replicas_unavailable"])

    def test_unavailable_api_is_itself_a_cluster_finding(self):
        client = mock.Mock()
        client.snapshot.side_effect = k8s_checks.KubernetesUnavailable("connection refused")
        result = k8s_checks.run_checks({"cluster_name": "demo"}, client=client)
        self.assertEqual(result["checks"][0]["status"], "error")
        self.assertEqual(result["findings"][0]["reason"],
                         "kubernetes_api_unavailable")


class TestKubectlTransport(unittest.TestCase):
    @mock.patch("k8s_client.shutil.which", return_value="/usr/bin/kubectl")
    @mock.patch("k8s_client.subprocess.run")
    def test_snapshot_is_one_read_only_kubectl_call(self, run, _which):
        run.return_value = mock.Mock(
            returncode=0, stdout=json.dumps({"items": [
                {"kind": "Node", "metadata": {"name": "node-a"}},
                {"kind": "Pod", "metadata": {"name": "api"}},
            ]}), stderr="")
        client = KubernetesClient(context="demo", timeout=7, environ={})
        snapshot = client.snapshot()
        self.assertEqual(len(snapshot["Node"]), 1)
        command = run.call_args.args[0]
        self.assertIn("get", command)
        self.assertIn("--all-namespaces", command)
        self.assertNotIn("delete", command)
        self.assertNotIn("apply", command)
        self.assertEqual(command[0:3], ["kubectl", "--context", "demo"])

    @mock.patch("k8s_client.shutil.which", return_value="/usr/bin/kubectl")
    @mock.patch("k8s_client.subprocess.run")
    def test_deployment_restart_is_followed_by_rollout_status(self, run, _which):
        run.side_effect = [
            mock.Mock(returncode=0, stdout="restarted", stderr=""),
            mock.Mock(returncode=1, stdout="", stderr="timed out"),
        ]
        client = KubernetesClient(context="demo", timeout=7, environ={})
        ok, reason = client.restart_deployment("ordis-e2e", "api", timeout=10)
        self.assertFalse(ok)
        self.assertIn("timed out", reason)
        restart = run.call_args_list[0].args[0]
        verify = run.call_args_list[1].args[0]
        self.assertEqual(restart[0:3], ["kubectl", "--context", "demo"])
        self.assertIn("restart", restart)
        self.assertIn("status", verify)
        self.assertIn("--timeout=10s", verify)


if __name__ == "__main__":
    unittest.main(verbosity=2)

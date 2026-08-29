"""Detector repair orchestration and write-boundary tests."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import health_repair


class TestTraditionalHealthRepair(unittest.TestCase):
    def test_systemd_restart_requires_verification(self):
        finding = {"source": "traditional", "reason": "systemd_unit_failed",
                   "name": "ordis-e2e.service"}
        responses = [
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
        ]
        with mock.patch.object(health_repair, "_run", side_effect=responses) as run:
            result = health_repair.attempt(finding, {
                "enabled": True,
                "allowed_systemd_units": ["ordis-e2e.service"],
            })
        self.assertTrue(result["success"])
        self.assertEqual(run.call_args_list[1].args[0][0:2],
                         ["systemctl", "is-active"])

    def test_systemd_repair_respects_allowlist(self):
        finding = {"source": "traditional", "reason": "systemd_unit_failed",
                   "name": "production.service"}
        with mock.patch.object(health_repair, "_run") as run:
            result = health_repair.attempt(finding, {
                "enabled": True,
                "allowed_systemd_units": ["ordis-e2e.service"],
            })
        self.assertFalse(result["attempted"])
        run.assert_not_called()


class TestKubernetesHealthRepair(unittest.TestCase):
    def test_recreates_only_allowed_namespace_pod(self):
        finding = {"source": "kubernetes", "kind": "Pod",
                   "reason": "pod_crashloopbackoff", "namespace": "ordis-e2e",
                   "name": "api-123"}
        client = mock.Mock()
        client.recreate_managed_pod.return_value = (True, "替代 Pod 已 Ready")
        result = health_repair.attempt(
            finding, {"enabled": True, "allowed_namespaces": ["ordis-e2e"]},
            k8s_client=client)
        self.assertTrue(result["success"])
        client.recreate_managed_pod.assert_called_once()

    def test_rejects_namespace_outside_allowlist(self):
        finding = {"source": "kubernetes", "kind": "Pod",
                   "reason": "pod_crashloopbackoff", "namespace": "production",
                   "name": "api-123"}
        client = mock.Mock()
        result = health_repair.attempt(
            finding, {"enabled": True, "allowed_namespaces": ["ordis-e2e"]},
            k8s_client=client)
        self.assertFalse(result["attempted"])
        client.recreate_managed_pod.assert_not_called()

    def test_restarts_deployment_and_requires_rollout_verification(self):
        finding = {"source": "kubernetes", "kind": "Deployment",
                   "reason": "replicas_unavailable", "namespace": "ordis-e2e",
                   "name": "api"}
        client = mock.Mock()
        client.restart_deployment.return_value = (False, "rollout 超时")
        result = health_repair.attempt(
            finding, {"enabled": True, "allowed_namespaces": ["ordis-e2e"],
                      "timeout": 10}, k8s_client=client)
        self.assertTrue(result["attempted"])
        self.assertFalse(result["success"])
        self.assertIn("rollout", result["reason"])
        client.restart_deployment.assert_called_once_with(
            "ordis-e2e", "api", timeout=10.0)


class TestFindingSkillPersistence(unittest.TestCase):
    def test_confirmed_finding_skill_writes_fingerprint_rule(self):
        import ai_levels
        import config
        import db
        import model_config
        import promotion

        root = Path(tempfile.mkdtemp())
        rules_path = root / "rules.yaml"
        rules_path.write_text("rules: []\nprocess: {watch_ports: []}\n",
                              encoding="utf-8")
        with mock.patch.object(db, "DB_PATH", root / "ordis.db"), \
                mock.patch.object(config, "CFG_DIR", root), \
                mock.patch.object(model_config, "CONFIG_PATH",
                                  root / "model.json"), \
                mock.patch.object(promotion, "preview_skill_impact",
                                  return_value={"affected": [], "warning": None}):
            db.initialize()
            ai_levels.set_level("operate")
            db.save_skill({
                "id": "skill_finding", "status": "pending_confirm",
                "name": "repair-demo", "description": "demo",
                "command": "systemctl restart demo.service",
                "fingerprint": "host:systemd:demo.service:failed",
                "source": "ai_auto", "trigger": "health_finding",
                "check_command": "systemctl is-active --quiet demo.service",
                "port": None,
            })
            promotion.confirm_skill("skill_finding")
            rule = config.load_rules()["rules"][0]
        self.assertEqual(rule["trigger"], "health_finding")
        self.assertEqual(rule["fingerprint"],
                         "host:systemd:demo.service:failed")
        self.assertNotIn("collector", rule)


if __name__ == "__main__":
    unittest.main(verbosity=2)

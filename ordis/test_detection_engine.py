"""Detection integration, event persistence, and cooldown tests."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import engine


class TestFindingIntegration(unittest.TestCase):
    def setUp(self):
        engine._finding_cooldowns.clear()

    @mock.patch("ai_diagnose.request_diagnosis_async")
    @mock.patch("db.save_event")
    def test_finding_is_persisted_and_ai_is_deduplicated(self, save_event, request):
        finding = {
            "fingerprint": "k8s:demo:prod:Pod:api:pod_oomkilled",
            "source": "kubernetes", "reason": "pod_oomkilled",
            "summary": "pod prod/api was OOMKilled", "name": "api",
        }
        report = {"source": "kubernetes", "findings": [finding]}

        first = engine._handle_findings([report], cooldown_sec=300)
        second = engine._handle_findings([report], cooldown_sec=300)

        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        save_event.assert_any_call("health_finding", "api", finding)
        self.assertEqual(save_event.call_count, 2)
        request.assert_called_once()
        self.assertEqual(request.call_args.kwargs["fingerprint_key"],
                         finding["fingerprint"])
        self.assertEqual(request.call_args.args[2], finding)

    @mock.patch("health_repair.attempt")
    @mock.patch("ai_diagnose.request_diagnosis_async")
    @mock.patch("db.save_event")
    def test_successful_builtin_repair_skips_ai(self, _save, request, attempt):
        attempt.return_value = {"attempted": True, "success": True,
                                "actions": []}
        finding = {
            "fingerprint": "host:systemd:demo.service:failed",
            "source": "traditional", "reason": "systemd_unit_failed",
            "summary": "demo failed", "name": "demo.service",
        }
        events = engine._handle_findings(
            [{"findings": [finding]}], config={"detection": {
                "traditional": {"repair": {"enabled": True}}}})
        self.assertTrue(events[0]["heal"]["success"])
        request.assert_not_called()

    @mock.patch("health_repair.attempt")
    @mock.patch("ai_diagnose.request_diagnosis_async")
    @mock.patch("db.save_event")
    def test_failed_builtin_repair_is_sent_to_ai(self, _save, request, attempt):
        attempt.return_value = {"attempted": True, "success": False,
                                "reason": "restart failed", "actions": []}
        finding = {
            "fingerprint": "host:systemd:demo.service:failed",
            "source": "traditional", "reason": "systemd_unit_failed",
            "summary": "demo failed", "name": "demo.service",
        }
        engine._handle_findings(
            [{"findings": [finding]}], config={"detection": {
                "traditional": {"repair": {"enabled": True}}}})
        heal_result = request.call_args.args[3]
        self.assertTrue(heal_result["attempted"])
        self.assertIn("restart failed", str(heal_result))

    @mock.patch("engine._run_finding_skill")
    @mock.patch("health_repair.attempt")
    @mock.patch("ai_diagnose.request_diagnosis_async")
    @mock.patch("db.save_event")
    def test_approved_finding_skill_runs_before_builtin(
            self, _save, request, attempt, run_skill):
        run_skill.return_value = {"attempted": True, "success": True,
                                  "source": "skill", "actions": []}
        finding = {"fingerprint": "k8s:demo:e2e:Deployment:api:replicas_unavailable",
                   "source": "kubernetes", "reason": "replicas_unavailable",
                   "summary": "deployment failed", "name": "api"}
        config = {"rules": [{"name": "learned", "enabled": True,
                             "trigger": "health_finding",
                             "fingerprint": finding["fingerprint"],
                             "healer": "command_runner"}]}
        events = engine._handle_findings([{"findings": [finding]}], config=config)
        self.assertEqual(events[0]["heal"]["source"], "skill")
        attempt.assert_not_called()
        request.assert_not_called()

    @mock.patch("traditional_checks.run_checks", return_value={"source": "traditional"})
    @mock.patch("k8s_checks.run_checks", return_value={"source": "kubernetes"})
    def test_only_enabled_detector_families_run(self, k8s_run, traditional_run):
        reports = engine.run_detection_checks({"detection": {
            "traditional": {"enabled": False},
            "kubernetes": {"enabled": True, "context": "demo"},
        }})
        self.assertEqual(reports, [{"source": "kubernetes"}])
        traditional_run.assert_not_called()
        k8s_run.assert_called_once_with({"enabled": True, "context": "demo"})

    @mock.patch("db.get_recent_events")
    def test_health_findings_are_visible_in_events(self, recent):
        recent.return_value = [{
            "type": "health_finding",
            "data": {"observed_at": "2026-08-27T08:00:00+00:00",
                     "summary": "pod is OOMKilled", "source": "kubernetes",
                     "reason": "pod_oomkilled"},
        }]
        events = engine.load_events()
        self.assertEqual(events[0]["rule"], "pod is OOMKilled")
        self.assertEqual(events[0]["collector"], "kubernetes")
        self.assertIsNone(events[0]["heal"])

    def test_non_applicable_heal_does_not_call_ai(self):
        self.assertFalse(engine._needs_ai_diagnosis({
            "success": False, "applicable": False, "skipped": True}))
        self.assertTrue(engine._needs_ai_diagnosis({
            "success": False, "applicable": True}))
        self.assertTrue(engine._needs_ai_diagnosis(None))


if __name__ == "__main__":
    unittest.main(verbosity=2)

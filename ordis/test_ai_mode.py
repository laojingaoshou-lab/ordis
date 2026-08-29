"""AI 接管模式、自动修复与邮件通知回归测试。"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import ai_levels
import ai_mode
import audit
import notifier
import promotion
from healers.command_runner import healer as command_healer


class TestAiModeConfig(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "ai_mode.json"

    def test_default_is_safe_email_mode(self):
        config = ai_mode.load(self.path)
        self.assertEqual(config["mode"], "email")
        self.assertIn("管理员收件邮箱", "；".join(
            ai_mode.email_configuration_issues(config)))

    def test_mode_and_non_secret_email_config_persist(self):
        ai_mode.configure_email(
            self.path,
            to="admin@example.com",
            smtp_host="smtp.example.com",
            smtp_port=465,
            smtp_user="sender@example.com",
            from_address="sender@example.com",
            password_env="ORDIS_TEST_SMTP_PASSWORD",
            security="ssl",
        )
        config = ai_mode.set_mode("email", self.path)

        self.assertEqual(config["email"]["to"], "admin@example.com")
        saved = self.path.read_text(encoding="utf-8")
        self.assertIn("ORDIS_TEST_SMTP_PASSWORD", saved)
        self.assertNotIn("secret", saved)


class TestAiAutoRepair(unittest.TestCase):
    def setUp(self):
        self.diagnosis = {
            "fingerprint": {"type": "port_dead", "service": "8080"},
            "root_cause": "service stopped",
            "fix_direction": "restart service",
            "fix_command": "systemctl restart demo",
        }

    def test_auto_repair_executes_only_with_verification(self):
        result = {"success": True, "actions": [{"check_ok": True}]}
        with mock.patch.object(ai_levels, "check_allowed",
                               return_value=(True, "")), \
                mock.patch.object(command_healer, "heal",
                                  return_value=result) as heal, \
                mock.patch.object(audit, "log_audit"):
            outcome = ai_mode.execute_auto_repair(self.diagnosis)

        self.assertTrue(outcome["success"])
        self.assertEqual(outcome["check_port"], 8080)
        heal.assert_called_once()

    def test_auto_repair_rejects_compound_shell(self):
        self.diagnosis["fix_command"] = "systemctl restart demo; curl bad"
        with mock.patch.object(command_healer, "heal") as heal:
            outcome = ai_mode.execute_auto_repair(self.diagnosis)

        self.assertFalse(outcome["attempted"])
        self.assertIn("单条命令", outcome["reason"])
        heal.assert_not_called()

    def test_auto_repair_has_operate_level_safety_ceiling(self):
        self.diagnosis["fix_command"] = "dd if=/dev/zero of=/dev/sdz"
        with mock.patch.object(ai_levels, "current_level", return_value="root"), \
                mock.patch.object(command_healer, "heal") as heal:
            outcome = ai_mode.execute_auto_repair(self.diagnosis)

        self.assertFalse(outcome["attempted"])
        self.assertIn("安全边界", outcome["reason"])
        heal.assert_not_called()

    def test_auto_repair_rejects_unsupported_single_command(self):
        self.diagnosis["fix_command"] = "python3 repair.py now"
        with mock.patch.object(command_healer, "heal") as heal:
            outcome = ai_mode.execute_auto_repair(self.diagnosis)

        self.assertFalse(outcome["attempted"])
        self.assertIn("支持列表", outcome["reason"])
        heal.assert_not_called()

    def test_skill_is_generated_only_after_successful_repair(self):
        skill = {"id": "skill_test"}
        with mock.patch.object(ai_mode, "load",
                               return_value={"mode": "auto", "email": {}}), \
                mock.patch.object(ai_mode, "execute_auto_repair",
                                  return_value={"mode": "auto",
                                                "success": True}), \
                mock.patch.object(promotion, "auto_draft_from_ai",
                                  return_value=skill) as draft:
            outcome = ai_mode.process_diagnosis(
                "process:test", "rule", "process", {}, self.diagnosis)

        self.assertEqual(outcome["skill_id"], "skill_test")
        draft.assert_called_once()

    def test_failed_repair_does_not_generate_skill(self):
        with mock.patch.object(ai_mode, "load",
                               return_value={"mode": "auto", "email": {}}), \
                mock.patch.object(ai_mode, "execute_auto_repair",
                                  return_value={"mode": "auto",
                                                "success": False}), \
                mock.patch.object(promotion, "auto_draft_from_ai") as draft:
            outcome = ai_mode.process_diagnosis(
                "process:test", "rule", "process", {}, self.diagnosis)

        self.assertFalse(outcome["success"])
        draft.assert_not_called()

    def test_kubernetes_set_image_is_bound_to_finding_and_allowlist(self):
        diagnosis = dict(self.diagnosis)
        diagnosis["fix_command"] = (
            "kubectl set image deployment/demo web=nginx:stable -n ordis-e2e")
        finding = {"source": "kubernetes", "kind": "Deployment",
                   "name": "demo", "namespace": "ordis-e2e"}
        rules = {"detection": {"kubernetes": {"repair": {
            "enabled": True, "ai_enabled": True,
            "allowed_namespaces": ["ordis-e2e"],
        }}}}
        with mock.patch("config.load_rules", return_value=rules), \
                mock.patch.object(command_healer, "heal",
                                  return_value={"success": True, "actions": []}) as heal, \
                mock.patch.object(audit, "log_audit"):
            outcome = ai_mode.execute_auto_repair(diagnosis, finding)
        self.assertTrue(outcome["success"])
        self.assertIn("kubectl rollout status", outcome["check_command"])
        heal.assert_called_once()

        finding["name"] = "production"
        with mock.patch("config.load_rules", return_value=rules), \
                mock.patch.object(command_healer, "heal") as heal:
            rejected = ai_mode.execute_auto_repair(diagnosis, finding)
        self.assertFalse(rejected["attempted"])
        heal.assert_not_called()

    def test_traditional_command_is_bound_to_detected_unit(self):
        finding = {"source": "traditional", "kind": "SystemdService",
                   "reason": "systemd_unit_failed", "name": "demo.service"}
        diagnosis = dict(self.diagnosis)
        diagnosis["fix_command"] = "systemctl restart other.service"
        with mock.patch.object(command_healer, "heal") as heal:
            outcome = ai_mode.execute_auto_repair(diagnosis, finding)
        self.assertFalse(outcome["attempted"])
        heal.assert_not_called()


class TestEmailAdvice(unittest.TestCase):
    def _config(self):
        return {
            "mode": "email",
            "email": {
                "to": "admin@example.com",
                "smtp_host": "smtp.example.com",
                "smtp_port": 465,
                "smtp_user": "sender@example.com",
                "from_address": "sender@example.com",
                "password_env": "ORDIS_TEST_SMTP_PASSWORD",
                "security": "ssl",
            },
        }

    def test_email_mode_never_runs_auto_repair(self):
        diagnosis = {"root_cause": "x", "fix_direction": "restart",
                     "fix_command": "systemctl restart demo"}
        with mock.patch.object(ai_mode, "load", return_value=self._config()), \
                mock.patch.object(ai_mode, "send_email_advice",
                                  return_value={"mode": "email",
                                                "success": True}) as send, \
                mock.patch.object(ai_mode, "execute_auto_repair") as repair:
            outcome = ai_mode.process_diagnosis(
                "process:test", "rule", "process", {}, diagnosis)

        self.assertTrue(outcome["success"])
        send.assert_called_once()
        repair.assert_not_called()

    def test_smtp_password_comes_from_environment(self):
        client = mock.MagicMock()
        with mock.patch.dict(os.environ,
                             {"ORDIS_TEST_SMTP_PASSWORD": "secret"}), \
                mock.patch("smtplib.SMTP_SSL", return_value=client):
            ok = notifier.email_send(
                self._config()["email"], "subject", "body")

        self.assertTrue(ok)
        client.login.assert_called_once_with("sender@example.com", "secret")
        client.send_message.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)

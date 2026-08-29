"""One-shot setup wizard tests."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import ai_mode
import model_config
import setup_wizard


class ScriptedInput:
    def __init__(self, values):
        self.values = iter(values)

    def __call__(self, _prompt):
        return next(self.values)


class TestSetupWizard(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.model_path = self.root / "model.json"
        self.mode_path = self.root / "ai_mode.json"

    def test_auto_setup_tests_before_persisting_all_settings(self):
        answers = ScriptedInput([
            "siliconflow", "https://api.example.com/v1", "demo/model",
            "1", "2",
        ])
        result = setup_wizard.run_interactive(
            input_fn=answers, secret_fn=lambda _: "secret-key",
            model_path=self.model_path, mode_path=self.mode_path,
            test_fn=lambda _: ("正常", 0.2))

        active = model_config.load_active(self.model_path)
        self.assertEqual(active["name"], "siliconflow")
        self.assertEqual(active["api_key"], "secret-key")
        self.assertEqual(model_config.load(self.model_path)["ai_level"], "operate")
        self.assertEqual(ai_mode.load(self.mode_path)["mode"], "auto")
        self.assertTrue(result["ok"])
        self.assertNotEqual(result["model"]["api_key"], "secret-key")

    def test_email_setup_persists_only_password_environment_name(self):
        answers = ScriptedInput([
            "provider", "https://api.example.com/v1", "demo/model",
            "2", "1", "admin@example.com", "smtp.example.com", "465",
            "sender@example.com", "sender@example.com", "1",
            "ORDIS_TEST_SMTP_PASSWORD",
        ])
        setup_wizard.run_interactive(
            input_fn=answers, secret_fn=lambda _: "api-secret",
            model_path=self.model_path, mode_path=self.mode_path,
            test_fn=lambda _: ("正常", 0.1))

        saved = self.mode_path.read_text(encoding="utf-8")
        self.assertIn("ORDIS_TEST_SMTP_PASSWORD", saved)
        self.assertNotIn("api-secret", saved)

    def test_failed_model_test_does_not_overwrite_existing_config(self):
        model_config.add_provider(
            "working", "https://working.example/v1", "working/model",
            "old-key", self.model_path)
        before = self.model_path.read_text(encoding="utf-8")
        answers = ScriptedInput([
            "broken", "https://broken.example/v1", "broken/model", "1", "1",
        ])
        with self.assertRaisesRegex(RuntimeError, "未保存"):
            setup_wizard.run_interactive(
                input_fn=answers, secret_fn=lambda _: "new-key",
                model_path=self.model_path, mode_path=self.mode_path,
                test_fn=lambda _: (_ for _ in ()).throw(RuntimeError("timeout")))
        self.assertEqual(self.model_path.read_text(encoding="utf-8"), before)
        self.assertFalse(self.mode_path.exists())

    def test_model_only_setup_does_not_overwrite_on_failed_test(self):
        model_config.add_provider(
            "working", "https://working.example/v1", "working/model",
            "old-key", self.model_path)
        before = self.model_path.read_text(encoding="utf-8")
        answers = ScriptedInput([
            "working", "https://broken.example/v1", "broken/model",
        ])
        with self.assertRaisesRegex(RuntimeError, "未保存"):
            setup_wizard.run_model_interactive(
                input_fn=answers, secret_fn=lambda _: "new-key",
                model_path=self.model_path,
                test_fn=lambda _: (_ for _ in ()).throw(RuntimeError("timeout")))
        self.assertEqual(self.model_path.read_text(encoding="utf-8"), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)

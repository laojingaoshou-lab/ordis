"""P0 安全边界与 SQLite 完整字段持久化回归测试。"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import audit
import db
import promotion
from healers.command_runner import CommandRunner
from healers.disk_cleaner import DiskCleaner


class IsolatedDatabase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.original_db = db.DB_PATH
        db.DB_PATH = self.tmp / "test.db"

    def tearDown(self):
        db.DB_PATH = self.original_db


class TestSQLitePayload(IsolatedDatabase):
    def test_skill_preserves_verification_fields(self):
        skill = {
            "id": "skill_payload", "status": "pending_confirm", "name": "test",
            "command": "systemctl restart app", "fingerprint": "process:test",
            "port": 3971, "check_command": "systemctl is-active app",
            "cooldown": 45, "confirmed_at": None,
        }
        db.save_skill(skill)
        loaded = db.get_skill(skill["id"])
        self.assertEqual(loaded["port"], 3971)
        self.assertEqual(loaded["check_command"], "systemctl is-active app")
        self.assertEqual(loaded["cooldown"], 45)

    def test_draft_preserves_business_fields(self):
        draft = {
            "id": "draft_payload", "status": "pending", "fingerprint": "cpu:test",
            "root_cause": "load", "occurrences": 3, "skill": {"confidence": 0.9},
        }
        db.save_draft(draft)
        loaded = db.get_draft(draft["id"])
        self.assertEqual(loaded["fingerprint"], "cpu:test")
        self.assertEqual(loaded["occurrences"], 3)
        self.assertEqual(loaded["root_cause"], "load")

    def test_audit_uses_database(self):
        audit.log_audit("test_action", {"command": "true"})
        rows = db.get_recent_audits()
        self.assertEqual(rows[0]["type"], "test_action")
        self.assertEqual(rows[0]["data"]["command"], "true")


class TestJSONMigration(IsolatedDatabase):
    def test_migrates_each_file_transactionally_and_marks_backup(self):
        logs = self.tmp / "logs"
        logs.mkdir()
        (logs / "cases.json").write_text(json.dumps([{
            "_key": "legacy:case", "rule": "r", "collector": "c",
            "diagnosis": {"root_cause": "x"},
        }]), encoding="utf-8")
        (logs / "drafts.json").write_text(json.dumps([{
            "status": "pending", "skill": {"name": "legacy"},
        }]), encoding="utf-8")
        report = db.migrate_from_json(logs)
        self.assertEqual(report, {"migrated": 2, "failed": 0})
        self.assertFalse((logs / "cases.json").exists())
        self.assertTrue((logs / "cases.json.migrated").exists())
        self.assertEqual(db.get_recent_cases(1)[0]["fingerprint"], "legacy:case")
        self.assertEqual(len(db.load_drafts()), 1)

    def test_bad_file_is_retained_and_logged_as_failed(self):
        logs = self.tmp / "logs"
        logs.mkdir()
        source = logs / "events.json"
        source.write_text("{bad", encoding="utf-8")
        report = db.migrate_from_json(logs)
        self.assertEqual(report["failed"], 1)
        self.assertTrue(source.exists())


class TestDiskCleaner(unittest.TestCase):
    def test_rejects_broad_tmp(self):
        result = DiskCleaner().heal({"params": {"dirs": ["/tmp"], "days": 1}})
        self.assertFalse(result["success"])
        self.assertIn("危险目录", result["actions"][0]["error"])

    def test_deletes_only_old_regular_files_in_whitelist(self):
        root = Path(tempfile.mkdtemp())
        old = root / "old.log"
        fresh = root / "fresh.log"
        excluded = root / "keep.pid"
        for path in (old, fresh, excluded):
            path.write_text("x", encoding="utf-8")
        old_time = time.time() - 10 * 86400
        os.utime(old, (old_time, old_time))
        os.utime(excluded, (old_time, old_time))

        result = DiskCleaner().heal({"params": {
            "dirs": [str(root)], "days": 7, "exclude_patterns": ["*.pid"]}})
        self.assertTrue(result["success"])
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue(excluded.exists())


class TestCommandVerification(unittest.TestCase):
    def test_refuses_command_without_check(self):
        result = CommandRunner().heal({"params": {"command": "true"}})
        self.assertFalse(result["success"])
        self.assertIn("未配置回检方式", result["actions"][0]["error"])

    @mock.patch("healers.command_runner.subprocess.run")
    def test_runs_effect_check_after_command(self, run):
        run.side_effect = [
            mock.Mock(returncode=0, stdout="restarted", stderr=""),
            mock.Mock(returncode=0, stdout="active", stderr=""),
        ]
        result = CommandRunner().heal({"params": {
            "command": "systemctl restart app",
            "check_command": "systemctl is-active app",
        }})
        self.assertTrue(result["success"])
        self.assertEqual(run.call_count, 2)


class TestImpactPreview(unittest.TestCase):
    @mock.patch("platform.system", return_value="Linux")
    @mock.patch("subprocess.check_output", return_value="123 safe-process\n")
    def test_pkill_preview_never_uses_shell(self, check_output, _):
        result = promotion.preview_skill_impact(
            {"command": "pkill -f 'safe; touch /tmp/should-not-run'"})
        self.assertEqual(result["affected"][0]["pid"], "123")
        args, kwargs = check_output.call_args
        self.assertIsInstance(args[0], list)
        self.assertNotIn("shell", kwargs)
        self.assertEqual(args[0][-1], "safe; touch /tmp/should-not-run")


if __name__ == "__main__":
    unittest.main(verbosity=2)

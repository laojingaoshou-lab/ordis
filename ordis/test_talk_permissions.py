"""talk 全局权限与越权确认回归测试。"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import ai_diagnose
import ai_levels
import audit
import talk


class TestTalkPermissions(unittest.TestCase):
    def test_execution_policy_follows_global_level(self):
        self.assertEqual(talk.execution_policy("df -h", "view")[0], "execute")
        self.assertEqual(
            talk.execution_policy("docker pull nginx", "view")[0], "confirm"
        )
        self.assertEqual(
            talk.execution_policy("systemctl restart nginx", "operate")[0],
            "execute",
        )
        self.assertEqual(
            talk.execution_policy("apt install nginx", "operate")[0],
            "confirm",
        )
        self.assertEqual(
            talk.execution_policy("dd if=/dev/zero of=/dev/sdz", "root")[0],
            "execute",
        )
        self.assertEqual(talk.execution_policy("", "root")[0], "reject")

    def test_session_reads_global_level(self):
        with mock.patch.object(ai_levels, "current_level", return_value="root"), \
                mock.patch.object(ai_diagnose, "_resolve_api_cfg",
                                  return_value=("https://example.invalid/v1",
                                                "model", "key")), \
                mock.patch.object(talk, "context_brief", return_value=""):
            session = talk.TalkSession(quiet=True)

        self.assertEqual(session.level, "root")
        self.assertIn("[talk 全局权限: root]", session.messages[0]["content"])

    def _session_with_actions(self, level):
        session = talk.TalkSession.__new__(talk.TalkSession)
        session.level = level
        session.quiet = True
        session.messages = []
        session._chat = mock.Mock(side_effect=[
            {"tool": "run", "command": "docker pull nginx"},
            {"tool": "answer", "answer": "done"},
        ])
        return session

    def test_root_executes_without_confirmation(self):
        session = self._session_with_actions("root")
        with mock.patch.object(session, "_confirm") as confirm, \
                mock.patch.object(talk, "run_command", return_value="ok") as run, \
                mock.patch.object(audit, "log_audit"), \
                mock.patch("builtins.print"):
            result = session.step("pull nginx")

        self.assertTrue(result)
        confirm.assert_not_called()
        run.assert_called_once_with("docker pull nginx")

    def test_view_confirms_over_level_command(self):
        session = self._session_with_actions("view")
        with mock.patch.object(session, "_confirm", return_value=True) as confirm, \
                mock.patch.object(talk, "run_command", return_value="ok") as run, \
                mock.patch.object(audit, "log_audit"), \
                mock.patch("builtins.print"):
            result = session.step("pull nginx")

        self.assertTrue(result)
        confirm.assert_called_once()
        run.assert_called_once_with("docker pull nginx")


if __name__ == "__main__":
    unittest.main(verbosity=2)

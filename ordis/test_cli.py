"""CLI entry point and argument compatibility tests."""

import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import ordisd


class TestCliParser(unittest.TestCase):
    def setUp(self):
        self.parser = ordisd.build_parser()

    def test_talk_without_question_routes_to_repl(self):
        args = self.parser.parse_args(["talk"])
        with mock.patch("talk.main") as talk_main:
            args.func(args)
        talk_main.assert_called_once_with(question=None)

    def test_talk_uses_global_level_without_legacy_flags(self):
        help_text = self.parser._subparsers._group_actions[0] \
            .choices["talk"].format_help()
        self.assertNotIn("--exec", help_text)
        self.assertNotIn("--level", help_text)

        with mock.patch("sys.stderr"):
            with self.assertRaises(SystemExit):
                self.parser.parse_args(["talk", "--exec"])
            with self.assertRaises(SystemExit):
                self.parser.parse_args(["talk", "--level", "root"])

    def test_cases_v_is_verbose_alias(self):
        args = self.parser.parse_args(["cases", "-v"])
        self.assertTrue(args.full)

    def test_run_install_is_registered(self):
        args = self.parser.parse_args(["run-install"])
        self.assertIs(args.func, ordisd.cmd_run_install)

    def test_root_is_global_level_shortcut(self):
        args = self.parser.parse_args(["root"])
        self.assertIs(args.func, ordisd.cmd_level)
        self.assertEqual(args.set, "root")

    def test_ai_mode_commands_are_registered(self):
        status = self.parser.parse_args(["ai-mode"])
        auto = self.parser.parse_args(["ai-mode", "auto"])
        email = self.parser.parse_args([
            "ai-mode", "email", "--to", "admin@example.com",
            "--smtp-host", "smtp.example.com",
        ])
        self.assertIs(status.func, ordisd.cmd_ai_mode)
        self.assertEqual(status.action, "status")
        self.assertEqual(auto.action, "auto")
        self.assertEqual(email.to, "admin@example.com")

    def test_consolidated_config_commands_are_registered(self):
        setup = self.parser.parse_args(["setup"])
        model = self.parser.parse_args(["config", "model", "test"])
        mode = self.parser.parse_args(["config", "mode", "auto"])
        permission = self.parser.parse_args([
            "config", "permission", "operate"])
        test = self.parser.parse_args(["config", "test", "email"])
        self.assertIs(setup.func, ordisd.cmd_setup)
        self.assertIs(model.func, ordisd.cmd_config)
        self.assertEqual(model.model_action, "test")
        self.assertEqual(mode.mode, "auto")
        self.assertEqual(permission.permission, "operate")
        self.assertEqual(test.target, "email")

    def test_legacy_config_commands_are_hidden_but_compatible(self):
        help_text = self.parser.format_help()
        for command in ("model", "ai-mode", "level", "view", "operate", "root"):
            self.assertNotIn(f"    {command} ", help_text)
        self.assertIs(self.parser.parse_args(["model", "status"]).func,
                      ordisd.cmd_model)
        self.assertIs(self.parser.parse_args(["root"]).func,
                      ordisd.cmd_level)

    def test_check_and_k8s_commands_are_registered(self):
        check = self.parser.parse_args(["check", "--json"])
        k8s = self.parser.parse_args(["--json", "k8s", "doctor"])
        self.assertIs(check.func, ordisd.cmd_check)
        self.assertTrue(check.output_json)
        self.assertIs(k8s.func, ordisd.cmd_k8s)
        self.assertEqual(k8s.action, "doctor")
        self.assertTrue(k8s.output_json)

    def test_once_renders_concise_chinese_summary(self):
        events = [{
            "rule": "进程端口不通",
            "value": {"ports": {"80": False}},
            "heal": {"success": False, "applicable": False, "skipped": True,
                     "actions": [{"port": 80, "skipped": True}]},
        }, {
            "rule": "systemd unit failed",
            "finding": True,
            "value": {"severity": "high",
                      "summary": "systemd 服务 app.service 处于 failed 状态"},
            "heal": None,
        }]
        args = self.parser.parse_args(["once"])
        stdout = io.StringIO()
        with mock.patch("engine.run_once", return_value=events) as run_once, \
                contextlib.redirect_stdout(stdout):
            args.func(args)
        output = stdout.getvalue()
        run_once.assert_called_once_with(verbose=False)
        self.assertIn("巡检完成：1 个故障，1 项跳过", output)
        self.assertIn("[跳过] 端口 80：对应服务未部署在本机", output)
        self.assertIn("[高] systemd 服务 app.service 处于 failed 状态", output)
        self.assertNotIn("{'ports'", output)

    def test_once_can_wait_for_ai_handoff(self):
        args = self.parser.parse_args(["once", "--wait-ai"])
        events = [{"rule": "demo", "value": {}, "heal": None}]
        with mock.patch("engine.run_once", return_value=events), \
                mock.patch("ai_diagnose.wait_for_pending",
                           return_value=True) as wait:
            self.assertEqual(args.func(args), 0)
        wait.assert_called_once_with()

    def test_skills_supports_subcommand_form(self):
        args = self.parser.parse_args(["skills", "confirm", "skill_demo"])
        self.assertEqual(args.action, "confirm")
        self.assertEqual(args.target, "skill_demo")

    def test_skills_subcommand_requires_target(self):
        args = self.parser.parse_args(["skills", "confirm"])
        with contextlib.redirect_stdout(io.StringIO()) as stdout, \
                self.assertRaisesRegex(SystemExit, "2"):
            args.func(args)
        self.assertIn("缺少 ID", stdout.getvalue())

    def test_human_check_report_uses_chinese_status(self):
        result = {
            "source": "traditional",
            "checks": [{"name": "systemd", "status": "ok"}],
            "findings": [{"severity": "high", "kind": "SystemdService",
                          "name": "app.service", "summary": "服务异常"}],
        }
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            ordisd._print_check_report(result)
        output = stdout.getvalue()
        self.assertIn("[正常] systemd", output)
        self.assertIn("[高] [SystemdService/app.service] 服务异常", output)
        self.assertNotIn("[HIGH]", output)

    def test_parser_errors_are_concise_chinese(self):
        cases = (
            (["chek"], "未知命令 'chek'"),
            (["talk", "--exec"], "无法识别的参数：--exec"),
            (["model", "--name"], "参数 --name 需要一个值"),
            (["k8s", "repair"], "参数 action 的值 'repair' 无效"),
        )
        for argv, expected in cases:
            with self.subTest(argv=argv), io.StringIO() as stderr, \
                    contextlib.redirect_stderr(stderr):
                with self.assertRaisesRegex(SystemExit, "2"):
                    self.parser.parse_args(argv)
                output = stderr.getvalue()
                self.assertIn(expected, output)
                self.assertNotIn("usage:", output)
                self.assertNotIn("invalid choice", output)
                self.assertNotIn("unrecognized arguments", output)


if __name__ == "__main__":
    unittest.main(verbosity=2)

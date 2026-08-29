"""
AI 诊断单元测试：不依赖真实 LLM，验证调度闸门和落盘逻辑。

运行：python -m pytest test_ai_diagnose.py -v  （或直接 python test_ai_diagnose.py）
"""

import json
import os
import sys
import time
import tempfile
import threading
from pathlib import Path
from unittest import mock

# 测试环境：隔离数据库到临时目录，注入假 key
_tmpdir = tempfile.mkdtemp()
os.environ.setdefault("MODEFLARE_API_KEY", "test-key-fake")

sys.path.insert(0, str(Path(__file__).parent))

# 在 import 前劫持数据库路径
import db
db.DB_PATH = Path(_tmpdir) / "test.db"

import ai_diagnose
import ai_mode

# AI worker 的草稿写入统一走临时 SQLite，不污染真实技能库
import promotion

import unittest


class TestFingerprint(unittest.TestCase):
    def test_fp_key_format(self):
        self.assertEqual(
            ai_diagnose._fingerprint_key({"type": "port_dead", "service": "nginx"}),
            "port_dead:nginx",
        )


class TestGates(unittest.TestCase):
    def setUp(self):
        # 清空数据库
        import db
        conn = db.get_conn()
        conn.execute("DELETE FROM cases")
        conn.commit()

    def test_empty_cases_not_recently_diagnosed(self):
        self.assertFalse(ai_diagnose._recently_diagnosed("cpu:高负载"))

    def test_recent_case_blocks(self):
        import db
        # 插入最近的案例
        db.save_case("cpu:高负载", "node1", "test rule", "{}", "test cmd")
        self.assertTrue(ai_diagnose._recently_diagnosed("cpu:高负载"))

    def test_old_case_does_not_block(self):
        import db
        # 插入旧案例（手动设置旧时间戳）
        old = time.time() - ai_diagnose.DEDUP_WINDOW - 100
        conn = db.get_conn()
        conn.execute(
            "INSERT INTO cases (fingerprint, hostname, message, diagnosis, command, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("cpu:高负载", "node1", "test", "{}", "cmd", old)
        )
        conn.commit()
        self.assertFalse(ai_diagnose._recently_diagnosed("cpu:高负载"))

    def test_worker_output_feeds_dedup_gate(self):
        """回归：worker 真实落盘的结构必须能被去重闸门识别。"""
        fake = {"fingerprint": {"type": "port_dead", "service": "x"},
                "root_cause": "r", "fix_direction": "f"}
        with mock.patch.object(ai_diagnose, "_call_llm", return_value=fake):
            ai_diagnose._diagnose_worker(
                "process:regress", "回归规则", "process", {}, None, None)
        self.assertTrue(ai_diagnose._recently_diagnosed("process:regress"))

    def test_daily_limit_is_disabled_by_default(self):
        self.assertEqual(ai_diagnose.DAILY_LIMIT, 0)
        self.assertFalse(ai_diagnose._daily_count_exceeded())

    def test_daily_limit_can_be_enabled(self):
        import db
        with mock.patch.object(ai_diagnose, "DAILY_LIMIT", 2):
            for i in range(2):
                db.save_case(f"test_{i}", "node1", "test", "{}", "cmd")
            self.assertTrue(ai_diagnose._daily_count_exceeded())


class TestRepairPrompt(unittest.TestCase):
    def test_systemd_finding_requires_exact_scoped_command(self):
        finding = {
            "source": "traditional",
            "reason": "systemd_unit_failed",
            "name": "ordis-e2e.service",
        }
        prompt = ai_diagnose._build_user_prompt(
            "failed unit", finding, {"success": False}, None)
        self.assertIn(
            "允许的单条修复命令：`systemctl restart ordis-e2e.service`",
            prompt)

    def test_systemd_hint_rejects_untrusted_unit_name(self):
        finding = {
            "source": "traditional",
            "reason": "systemd_unit_failed",
            "name": "demo.service; reboot",
        }
        self.assertIsNone(ai_diagnose._repair_command_hint(finding))

    def test_kubernetes_finding_uses_admin_repair_image(self):
        finding = {
            "source": "kubernetes",
            "kind": "Deployment",
            "reason": "replicas_unavailable",
            "namespace": "ordis-e2e",
            "name": "image-repair",
            "evidence": {
                "images": {"web": "busybox:ordis-missing"},
                "annotations": {"ordis.ai/repair-image": "busybox:latest"},
            },
        }
        self.assertEqual(
            ai_diagnose._repair_command_hint(finding),
            "kubectl set image deployment/image-repair "
            "web=busybox:latest -n ordis-e2e")


class TestLLMParsing(unittest.TestCase):
    def test_json_in_codeblock(self):
        text = '```json\n{"fingerprint": {"type": "port_dead"}, "root_cause": "x", "fix_direction": "y"}\n```'
        with mock.patch.object(ai_diagnose.requests, "post") as mp:
            mp.return_value.status_code = 200
            mp.return_value.raise_for_status = lambda: None
            mp.return_value.json = lambda: {
                "choices": [{"message": {"content": text}}]}
            result = ai_diagnose._call_llm("prompt")
            self.assertEqual(result["root_cause"], "x")

    def test_missing_field_raises(self):
        text = '{"fingerprint": {}}'
        with mock.patch.object(ai_diagnose.requests, "post") as mp:
            mp.return_value.status_code = 200
            mp.return_value.raise_for_status = lambda: None
            mp.return_value.json = lambda: {
                "choices": [{"message": {"content": text}}]}
            with self.assertRaises(ValueError):
                ai_diagnose._call_llm("prompt")

    def test_no_json_raises(self):
        with mock.patch.object(ai_diagnose.requests, "post") as mp:
            mp.return_value.status_code = 200
            mp.return_value.raise_for_status = lambda: None
            mp.return_value.json = lambda: {
                "choices": [{"message": {"content": "抱歉我无法判断"}}]}
            with self.assertRaises(ValueError):
                ai_diagnose._call_llm("prompt")

    @staticmethod
    def _mock_llm(text):
        patcher = mock.patch.object(ai_diagnose.requests, "post")
        mp = patcher.start()
        mp.return_value.status_code = 200
        mp.return_value.raise_for_status = lambda: None
        mp.return_value.json = lambda: {
            "choices": [{"message": {"content": text}}]}
        return patcher

    def test_fix_command_kept(self):
        text = ('{"fingerprint": {"type": "x"}, "root_cause": "r", '
                '"fix_direction": "d", "fix_command": "systemctl restart nginx"}')
        p = self._mock_llm(text)
        try:
            result = ai_diagnose._call_llm("prompt")
        finally:
            mock.patch.stopall()
        self.assertEqual(result["fix_command"], "systemctl restart nginx")

    def test_fix_command_garbage_becomes_none(self):
        text = ('{"fingerprint": {"type": "x"}, "root_cause": "r", '
                '"fix_direction": "d", "fix_command": 123}')
        p = self._mock_llm(text)
        try:
            result = ai_diagnose._call_llm("prompt")
        finally:
            mock.patch.stopall()
        self.assertIsNone(result["fix_command"])

    def test_fix_command_missing_is_none(self):
        """旧模型不输出 fix_command 时归一化为 None，不报错（向后兼容）。"""
        text = '{"fingerprint": {"type": "x"}, "root_cause": "r", "fix_direction": "d"}'
        p = self._mock_llm(text)
        try:
            result = ai_diagnose._call_llm("prompt")
        finally:
            mock.patch.stopall()
        self.assertIsNone(result["fix_command"])


class TestAsyncWorker(unittest.TestCase):
    def setUp(self):
        import db
        conn = db.get_conn()
        conn.execute("DELETE FROM cases")
        conn.commit()

    def test_worker_writes_case_on_success(self):
        fake = {"fingerprint": {"type": "port_dead", "service": "web"},
                "severity": "high", "root_cause": "进程崩溃",
                "evidence": ["a"], "fix_direction": "重启并检查日志",
                "confidence": 0.9}
        with mock.patch.object(ai_diagnose, "_call_llm", return_value=fake):
            ai_diagnose._diagnose_worker(
                "process:test", "测试规则", "process", {}, None, None)
            # 等待锁释放后读
            cases = ai_diagnose.load_cases()
            self.assertEqual(len(cases), 1)
            self.assertEqual(cases[0]["diagnosis"]["root_cause"], "进程崩溃")
            self.assertIsNone(cases[0]["error"])

    def test_worker_delegates_successful_diagnosis_to_ai_mode(self):
        fake = {"fingerprint": {"type": "port_dead", "service": "web"},
                "root_cause": "stopped", "fix_direction": "restart",
                "fix_command": "systemctl restart web"}
        handoff = {"mode": "email", "success": True, "sent": True}
        with mock.patch.object(ai_diagnose, "_call_llm", return_value=fake), \
                mock.patch.object(ai_mode, "process_diagnosis",
                                  return_value=handoff) as process:
            ai_diagnose._diagnose_worker(
                "process:handoff", "handoff rule", "process",
                {"hostname": "node"}, None, None)

        process.assert_called_once_with(
            "process:handoff", "handoff rule", "process",
            {"hostname": "node"}, fake)

    def test_worker_records_error_on_fail(self):
        with mock.patch.object(ai_diagnose, "_call_llm",
                               side_effect=RuntimeError("api down")):
            ai_diagnose._diagnose_worker(
                "process:test2", "规则B", "process", {}, None, None)
            cases = ai_diagnose.load_cases()
            self.assertEqual(len(cases), 1)
            self.assertIsNone(cases[0]["diagnosis"])
            self.assertIn("api down", cases[0]["error"])

    def test_async_entry_respects_dedup_gate(self):
        """同指纹 30 分钟内第二次调用不应产生新诊断线程。"""
        import db
        # 插入最近的案例
        db.save_case("process:dup规则", "node1", "dup规则 | process", "{}", "cmd")

        before = len(ai_diagnose.load_cases())
        threads_before = threading.active_count()
        ai_diagnose.request_diagnosis_async("dup规则", "process", {}, None)
        time.sleep(0.3)  # 给可能的误启动留时间窗

        self.assertEqual(len(ai_diagnose.load_cases()), before)
        self.assertEqual(threading.active_count(), threads_before)


if __name__ == "__main__":
    unittest.main(verbosity=2)

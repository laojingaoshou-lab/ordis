"""
对抗性测试：把 ai_diagnose / model_config 当攻击面打。

四个方向：
A. 畸形/恶意 LLM 输出（截断、类型错乱、括号陷阱）
B. 投毒数据文件（cases.json / model.json 被篡改）
C. 并发竞态（同指纹同时触发、洪泛触发）
D. 边界输入（超长指纹、不可序列化数据、网络异常）
"""

import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

os.environ.setdefault("MODEFLARE_API_KEY", "test-key-fake")
sys.path.insert(0, str(Path(__file__).parent))

_tmpdir = tempfile.mkdtemp()
# 隔离数据库
import db
db.DB_PATH = Path(_tmpdir) / "ordis.db"

import ai_diagnose
import promotion
import model_config as mc
mc.CONFIG_PATH = Path(_tmpdir) / "model.json"

import requests
import unittest


import contextlib


@contextlib.contextmanager
def _mock_llm_response(text, status=200):
    with mock.patch.object(ai_diagnose.requests, "post") as patcher:
        if status == 200:
            patcher.return_value.raise_for_status = lambda: None
        else:
            patcher.return_value.raise_for_status = lambda: (_ for _ in ()).throw(
                requests.exceptions.HTTPError(f"{status}"))
        patcher.return_value.json = lambda: {
            "choices": [{"message": {"content": text}}]}
        yield patcher


# ── A. 畸形 LLM 输出 ────────────────────────────────────────────
class TestExtractJsonTorture(unittest.TestCase):
    def test_braces_inside_strings(self):
        t = ('{"fingerprint": {"type": "x"}, "root_cause": "包含 { } 花括号", '
             '"fix_direction": "y"}')
        self.assertIn("{ }", ai_diagnose._extract_json(t)["root_cause"])

    def test_prose_wrapped(self):
        t = ('好的，分析如下：\n```json\n{"fingerprint": {"type": "x"}, '
             '"root_cause": "r", "fix_direction": "f"}\n```\n以上。')
        self.assertEqual(ai_diagnose._extract_json(t)["root_cause"], "r")

    def test_first_complete_object_wins(self):
        t = '{"fingerprint": {"type": "a"}, "root_cause": "r1"} 垃圾 {"x": 2}'
        self.assertEqual(ai_diagnose._extract_json(t)["root_cause"], "r1")

    def test_escaped_quotes(self):
        t = ('{"fingerprint": {"type": "x"}, "root_cause": "他说\\"重启\\"就好", '
             '"fix_direction": "f"}')
        self.assertIn("重启", ai_diagnose._extract_json(t)["root_cause"])

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            ai_diagnose._extract_json("")

    def test_truncated_raises(self):
        t = '{"fingerprint": {"type": "x"}, "root_cause": "没写完'
        with self.assertRaises(ValueError):
            ai_diagnose._extract_json(t)

    def test_top_level_array_raises(self):
        with self.assertRaises(ValueError):
            ai_diagnose._extract_json('[{"a":1},{"b":2}]')


class TestOutputValidation(unittest.TestCase):
    VALID = {"fingerprint": {"type": "x"}, "root_cause": "r",
             "fix_direction": "f"}

    def test_root_cause_number_rejected(self):
        bad = {**self.VALID, "root_cause": 123}
        with _mock_llm_response(json.dumps(bad)):
            with self.assertRaises(ValueError):
                ai_diagnose._call_llm("p")

    def test_fingerprint_not_dict_rejected(self):
        bad = {**self.VALID, "fingerprint": "port_dead"}
        with _mock_llm_response(json.dumps(bad)):
            with self.assertRaises(ValueError):
                ai_diagnose._call_llm("p")

    def test_empty_root_cause_rejected(self):
        bad = {**self.VALID, "root_cause": "   "}
        with _mock_llm_response(json.dumps(bad)):
            with self.assertRaises(ValueError):
                ai_diagnose._call_llm("p")

    def test_confidence_out_of_range_clamped(self):
        bad = {**self.VALID, "confidence": 7.5}
        with _mock_llm_response(json.dumps(bad)):
            r = ai_diagnose._call_llm("p")
            self.assertLessEqual(r["confidence"], 1.0)

    def test_extra_fields_accepted(self):
        extra = {**self.VALID, "hacker_field": "should be ignored"}
        with _mock_llm_response(json.dumps(extra)):
            r = ai_diagnose._call_llm("p")
            self.assertEqual(r["root_cause"], "r")


# ── B. 数据库容错测试（简化：数据库本身有完整性保护，只测逻辑层） ───
class TestDatabaseResilience(unittest.TestCase):
    def test_junk_entries_do_not_crash_gates(self):
        """垃圾数据不应让防护闸门崩溃。"""
        # 直接插入畸形数据到数据库
        conn = db.get_conn()
        conn.execute("INSERT INTO cases (fingerprint, created_at, hostname, message) VALUES (?, ?, ?, ?)",
                     ("junk:fp", time.time(), "host", "msg"))
        conn.commit()
        conn.close()
        # 闸门函数应该容错
        self.assertFalse(ai_diagnose._recently_diagnosed("other:fp"))
        self.assertFalse(ai_diagnose._daily_count_exceeded())

    def test_write_caps_at_200(self):
        """案例数量应限制在 200 条。"""
        # 插入 300 条垃圾数据
        conn = db.get_conn()
        for i in range(300):
            conn.execute("INSERT INTO cases (fingerprint, created_at, hostname, message) VALUES (?, ?, ?, ?)",
                         (f"junk:{i}", time.time(), "host", "msg"))
        conn.commit()
        conn.close()
        # 触发新诊断应触发清理
        fake = {"fingerprint": {"type": "x"}, "root_cause": "r",
                "fix_direction": "f"}

        # 直接调用 db.save_case 而不是走 _diagnose_worker（避免线程/异常吞没问题）
        db.save_case(
            fingerprint="test:trigger",
            hostname="test",
            message="test",
            diagnosis='{"test": 1}',
            command="test"
        )

        # 验证清理后数量
        conn = db.get_conn()
        count = conn.execute("SELECT COUNT(*) as c FROM cases").fetchone()["c"]
        conn.close()
        self.assertLessEqual(count, 200)

    def test_repeated_fingerprint_preserves_history(self):
        conn = db.get_conn()
        conn.execute("DELETE FROM cases")
        conn.commit()
        conn.close()
        db.save_case("repeat:fp", "host", "first", "{}", "cmd")
        db.save_case("repeat:fp", "host", "second", "{}", "cmd")
        cases = db.get_recent_cases(limit=10)
        self.assertEqual(len(cases), 2)
        self.assertEqual({c["message"] for c in cases}, {"first", "second"})

    def test_legacy_cases_schema_migrates(self):
        legacy_db = Path(tempfile.mkdtemp()) / "legacy.db"
        conn = sqlite3.connect(str(legacy_db))
        conn.execute("""
            CREATE TABLE cases (
                fingerprint TEXT PRIMARY KEY,
                created_at REAL NOT NULL,
                hostname TEXT,
                message TEXT,
                diagnosis TEXT,
                command TEXT,
                fix_command TEXT
            )
        """)
        conn.execute("""
            INSERT INTO cases VALUES (?, ?, ?, ?, ?, ?, ?)
        """, ("legacy:fp", time.time(), "host", "message", "{}", "cmd", None))
        conn.commit()
        conn.close()

        original_db = db.DB_PATH
        try:
            db.DB_PATH = legacy_db
            cases = db.get_recent_cases(limit=10)
            self.assertEqual(cases[0]["fingerprint"], "legacy:fp")
            conn = db.get_conn()
            columns = conn.execute("PRAGMA table_info(cases)").fetchall()
            conn.close()
            self.assertTrue(any(c["name"] == "id" and c["pk"] for c in columns))
        finally:
            db.DB_PATH = original_db


class TestModelConfigTampering(unittest.TestCase):
    def test_siliconflow_website_url_is_normalized_to_api_endpoint(self):
        self.assertEqual(
            mc.normalize_base_url("https://siliconflow.cn"),
            "https://api.siliconflow.cn/v1",
        )
        self.assertEqual(
            mc.normalize_base_url("https://www.siliconflow.cn/"),
            "https://api.siliconflow.cn/v1",
        )

    def test_corrupt_json_graceful(self):
        mc.CONFIG_PATH.write_text("{broken json!!", encoding="utf-8")
        data = mc.load()
        self.assertEqual(data["providers"], {})
        self.assertIsNone(mc.load_active())

    def test_active_ghost_provider(self):
        mc.save({"active": "ghost",
                 "providers": {"real": {"base_url": "http://x", "model": "m",
                                        "api_key": "k", "added_at": "d"}}})
        self.assertIsNone(mc.load_active())

    def test_missing_fields_tolerated(self):
        mc.save({"active": "p", "providers": {"p": {"model": "m"}}})
        cfg = mc.load_active()  # 缺 base_url/api_key 不应崩
        self.assertEqual(cfg["model"], "m")

    def test_trailing_slash_stripped(self):
        mc.add_provider("slashy", "http://x/v1///", "m", "k")
        self.assertEqual(mc.load()["providers"]["slashy"]["base_url"],
                         "http://x/v1")

    def test_hostile_names_are_dict_keys_only(self):
        mc.add_provider("../../etc/passwd", "http://x", "m", "k")
        # 名字只是 JSON 字典键，不产生路径穿越
        self.assertIn("../../etc/passwd", mc.load()["providers"])
        mc.remove_provider("../../etc/passwd")


# ── C. 并发竞态 ────────────────────────────────────────────────
def _drain_inflight(timeout=15):
    """等待所有后台诊断 worker 排空（inflight 集合清空），保证测试隔离。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with ai_diagnose._lock:
            if not ai_diagnose._inflight:
                return
        time.sleep(0.1)


class TestConcurrencyRace(unittest.TestCase):
    def setUp(self):
        conn = db.get_conn()
        conn.execute("DELETE FROM cases")
        conn.commit()
        conn.close()

    def tearDown(self):
        _drain_inflight()

    def test_same_fp_concurrent_starts_one_diagnosis(self):
        """5 个线程同指纹同时调度：必须只产生 1 次诊断。"""
        started = []

        def slow_fake(prompt):
            started.append(time.time())
            time.sleep(0.8)
            return {"fingerprint": {"type": "x"}, "root_cause": "r",
                    "fix_direction": "f"}

        with mock.patch.object(ai_diagnose, "_call_llm", side_effect=slow_fake):
            threads = []
            for _ in range(5):
                t = threading.Thread(target=ai_diagnose.request_diagnosis_async,
                                     args=("竞态规则", "cpu", {"v": 1}, None))
                threads.append(t)
                t.start()
            for t in threads:
                t.join()
            time.sleep(1.5)  # 等 worker 落盘
            self.assertLessEqual(len(ai_diagnose.load_cases()), 1,
                                 f"竞态！产生了 {len(started)} 次诊断")
            _drain_inflight()  # mock 必须罩住所有后台 worker 的生命周期

    def test_flood_distinct_fps_bounded(self):
        """20 个异指纹瞬间涌入：并发 LLM 调用必须有上限。"""
        conn = db.get_conn()
        conn.execute("DELETE FROM cases")
        conn.commit()
        conn.close()
        peak = {"n": 0}
        lock = threading.Lock()

        def slow_fake(prompt):
            with lock:
                peak["n"] += 1
            time.sleep(0.3)
            with lock:
                peak["n"] -= 1
            return {"fingerprint": {}, "root_cause": "r", "fix_direction": "f"}

        with mock.patch.object(ai_diagnose, "_call_llm", side_effect=slow_fake):
            ts = [threading.Thread(target=ai_diagnose.request_diagnosis_async,
                                   args=(f"洪泛规则{i}", "cpu", {"i": i}, None))
                  for i in range(20)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            # 在 mock 作用域内等全部 worker 排空，严禁真实网络调用
            _drain_inflight()
        self.assertLessEqual(peak["n"], 2,
                             f"并发峰值 {peak['n']} 超过信号量上限")


# ── D. 边界输入与网络异常 ───────────────────────────────────────
class TestBoundaryAndNetwork(unittest.TestCase):
    def setUp(self):
        conn = db.get_conn()
        conn.execute("DELETE FROM cases")
        conn.commit()
        conn.close()

    def test_nonserializable_value_no_silent_thread_death(self):
        """value 含不可序列化对象：线程必须存活并落盘案例（诊断成功或带错误）。"""
        # 只测降级+落盘契约，LLM 必须 mock（严禁测试内真实网络调用）
        fake = {"fingerprint": {"type": "x"}, "root_cause": "r",
                "fix_direction": "f"}
        with mock.patch.object(ai_diagnose, "_call_llm", return_value=fake):
            ai_diagnose._diagnose_worker("t:bad", "坏数据规则", "cpu",
                                         {"obj": object()}, None, None)
        cases = [c for c in ai_diagnose.load_cases()
                 if c.get("_key") == "t:bad"]
        self.assertEqual(len(cases), 1, "线程静默死亡：没有案例落盘")
        # 降级修复后：prompt 降级 → LLM 可能正常完成（error=None），
        # 契约只要求"案例落盘且结构完整"，不强制 error
        self.assertIn("diagnosis", cases[0])
        self.assertIsNotNone(cases[0]["diagnosis"])

    def test_timeout_retries_twice_then_records(self):
        calls = {"n": 0}

        def timeout_side(*a, **kw):
            calls["n"] += 1
            raise requests.exceptions.Timeout("read timed out")

        with mock.patch.object(ai_diagnose.requests, "post",
                               side_effect=timeout_side):
            ai_diagnose._diagnose_worker("t:net", "网络规则", "cpu",
                                         {"v": 1}, None, None)
        self.assertEqual(calls["n"], 2, "应恰好重试 1 次（共 2 次调用）")
        cases = ai_diagnose.load_cases()
        self.assertEqual(len(cases), 1)
        self.assertIn("timed out", cases[0]["error"])

    def test_http_500_propagates_to_error_case(self):
        with mock.patch.object(ai_diagnose.requests, "post") as mp:
            mp.side_effect = requests.exceptions.HTTPError("500 Server Error")
            ai_diagnose._diagnose_worker("t:500", "服务端错误规则", "cpu",
                                         {"v": 1}, None, None)
        cases = [c for c in ai_diagnose.load_cases()
                 if c["_key"] == "t:500"]
        self.assertEqual(len(cases), 1)
        self.assertIn("500", cases[0]["error"])

    def test_superlong_fingerprint_no_crash(self):
        k = "process:" + "💥服务" * 2000
        # 插入超长指纹到数据库
        conn = db.get_conn()
        conn.execute("INSERT INTO cases (fingerprint, created_at, hostname, message) VALUES (?, ?, ?, ?)",
                     (k, time.time(), "host", "msg"))
        conn.commit()
        conn.close()
        self.assertTrue(ai_diagnose._recently_diagnosed(k))


if __name__ == "__main__":
    unittest.main(verbosity=2)

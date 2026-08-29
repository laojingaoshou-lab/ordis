"""集群 P0：订单重试、身份绑定与节点凭证。"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent))

import cluster


class TestOrderRetry(unittest.TestCase):
    def setUp(self):
        self.data_file = Path(tempfile.mkdtemp()) / "nodes.json"
        self.patch = mock.patch.object(cluster, "DATA_FILE", self.data_file)
        self.patch.start()
        self.store = cluster.ClusterStore()

    def tearDown(self):
        self.patch.stop()

    def test_sent_order_retries_then_times_out(self):
        with mock.patch.object(cluster.time, "time", return_value=100.0):
            oid = self.store.add_order("node1", "systemd:app")
            self.assertEqual(self.store.ingest("node1", {}),
                             [{"id": oid, "service": "systemd:app"}])
        order = self.store.orders[0]
        self.assertEqual(order["attempts"], 1)

        for now, expected_attempts in ((161.0, 2), (222.0, 3)):
            with mock.patch.object(cluster.time, "time", return_value=now):
                resent = self.store.ingest("node1", {})
            self.assertEqual(len(resent), 1)
            self.assertEqual(order["attempts"], expected_attempts)

        with mock.patch.object(cluster.time, "time", return_value=283.0):
            self.assertEqual(self.store.ingest("node1", {}), [])
        self.assertEqual(order["status"], "timeout")

    def test_result_must_match_node_and_sent_state(self):
        oid = self.store.add_order("node1", "systemd:app")
        self.store.ingest("node1", {})
        self.assertFalse(self.store.order_result("node2", oid, True, "spoofed"))
        self.assertEqual(self.store.orders[0]["status"], "sent")
        self.assertTrue(self.store.order_result("node1", oid, True, "ok"))
        self.assertEqual(self.store.orders[0]["status"], "done")
        self.assertFalse(self.store.order_result("node1", oid, True, "duplicate"))


class TestOrderExecutionResult(unittest.TestCase):
    @mock.patch("healers.process_restarter.ProcessRestarter._restart")
    def test_restart_failure_is_reported_as_failed(self, restart):
        restart.return_value = (
            "systemctl restart missing", "Unit missing.service not found", False)
        ok, detail = cluster.execute_order("systemd:missing")
        self.assertFalse(ok)
        self.assertIn("not found", detail)


class TestClusterHttpAuth(unittest.TestCase):
    def setUp(self):
        self.data_file = Path(tempfile.mkdtemp()) / "nodes.json"
        self.data_patch = mock.patch.object(cluster, "DATA_FILE", self.data_file)
        self.data_patch.start()
        self.old_store = cluster.STORE
        cluster.STORE = cluster.ClusterStore()
        self.client = TestClient(cluster.make_app(
            "admin-secret", {"node1": "node-secret"}))

    def tearDown(self):
        cluster.STORE = self.old_store
        self.data_patch.stop()

    def test_agent_identity_is_bound_to_body_node(self):
        ok = self.client.post(
            "/ingest", json={"node": "node1", "snapshot": {}},
            headers={"X-Ordis-Node": "node1", "X-Ordis-Token": "node-secret"})
        self.assertEqual(ok.status_code, 200)
        spoof = self.client.post(
            "/ingest", json={"node": "node2", "snapshot": {}},
            headers={"X-Ordis-Node": "node1", "X-Ordis-Token": "node-secret"})
        self.assertEqual(spoof.status_code, 401)

    def test_node_token_cannot_access_admin_api(self):
        denied = self.client.get(
            "/nodes", headers={"X-Ordis-Token": "node-secret"})
        allowed = self.client.get(
            "/nodes", headers={"X-Ordis-Token": "admin-secret"})
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(allowed.status_code, 200)


class TestClusterIdentity(unittest.TestCase):
    def test_node_token_is_bound_to_agent_path_and_node(self):
        tokens = {"node1": "node-secret"}
        self.assertTrue(cluster._token_allowed(
            "/ingest", "node1", "node-secret", "admin-secret", tokens))
        self.assertFalse(cluster._token_allowed(
            "/ingest", "node2", "node-secret", "admin-secret", tokens))
        self.assertFalse(cluster._token_allowed(
            "/nodes", "node1", "node-secret", "admin-secret", tokens))
        self.assertTrue(cluster._token_allowed(
            "/nodes", None, "admin-secret", "admin-secret", tokens))

    def test_shared_token_remains_legacy_fallback_without_enrolled_nodes(self):
        self.assertTrue(cluster._token_allowed(
            "/ingest", "legacy", "shared", "shared", {}))

    def test_enroll_node_persists_independent_token(self):
        config_path = Path(tempfile.mkdtemp()) / "cluster.json"
        with mock.patch.object(cluster, "CONFIG_PATH", config_path):
            token1 = cluster.enroll_node("node1")
            token2 = cluster.enroll_node("node2")
            cfg = cluster.load_config()
        self.assertNotEqual(token1, token2)
        self.assertEqual(cfg["node_tokens"]["node1"], token1)
        self.assertEqual(cfg["node_tokens"]["node2"], token2)


if __name__ == "__main__":
    unittest.main(verbosity=2)

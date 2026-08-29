"""Traditional Linux and endpoint detector tests."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import traditional_checks


class TestTraditionalChecks(unittest.TestCase):
    @mock.patch("traditional_checks.shutil.which")
    @mock.patch("traditional_checks._run")
    def test_systemd_and_docker_failures_are_findings(self, run, which):
        which.side_effect = lambda name: f"/usr/bin/{name}"
        run.side_effect = [
            mock.Mock(returncode=0, stdout="● nginx.service loaded failed failed nginx\n", stderr=""),
            mock.Mock(returncode=0, stdout=(
                '{"Names":"api","State":"running","Status":"Up 2 minutes (unhealthy)","Image":"api:v1"}\n'
                '{"Names":"worker","State":"exited","Status":"Exited (1) 2 minutes ago","Image":"worker:v1"}\n'), stderr=""),
        ]
        with mock.patch("traditional_checks.psutil.process_iter", return_value=[]), \
                mock.patch("traditional_checks.os.statvfs", create=True) as statvfs:
            statvfs.return_value = mock.Mock(f_files=1000, f_ffree=900)
            result = traditional_checks.run_checks({"docker_containers": ["worker"]})
        reasons = {item["reason"] for item in result["findings"]}
        self.assertEqual(reasons, {
            "systemd_unit_failed", "container_unhealthy", "container_exited"})

    @mock.patch("traditional_checks.shutil.which", return_value="/usr/bin/systemctl")
    @mock.patch("traditional_checks._run")
    def test_systemd_scope_can_ignore_known_failed_units(self, run, _which):
        run.return_value = mock.Mock(
            returncode=0,
            stdout="api.service loaded failed failed API\nlegacy.service loaded failed failed Legacy\n",
            stderr="")
        findings = []
        check = traditional_checks._systemd_check({
            "systemd_units": ["api.service"],
            "systemd_ignore_units": ["api.service"],
        }, findings)
        self.assertEqual(findings, [])
        self.assertEqual(check["ignored_units"], 2)

    @mock.patch("traditional_checks.requests.get")
    def test_http_endpoint_checks_status_and_body(self, get):
        get.return_value = mock.Mock(
            status_code=200, text="maintenance", elapsed=mock.Mock(
                total_seconds=lambda: 0.05))
        result = traditional_checks.run_checks({
            "systemd": False, "docker": False, "inode_paths": [],
            "endpoints": [{"name": "checkout", "type": "http",
                           "url": "https://example.invalid/health",
                           "body_contains": "ok"}],
        })
        self.assertEqual(result["findings"][0]["reason"], "http_endpoint_failed")
        self.assertNotIn("maintenance", str(result["findings"]))

    @mock.patch("traditional_checks.requests.get")
    def test_http_endpoint_redacts_credentials_and_query(self, get):
        get.side_effect = traditional_checks.requests.ConnectionError(
            "failed https://user:secret@example.invalid/health?token=secret")
        result = traditional_checks.run_checks({
            "systemd": False, "docker": False, "inode_paths": [],
            "endpoints": [{"type": "http",
                           "url": "https://user:secret@example.invalid/health?token=secret"}],
        })
        rendered = str(result)
        self.assertNotIn("user:secret", rendered)
        self.assertNotIn("token=secret", rendered)
        self.assertIn("https://example.invalid/health", rendered)


if __name__ == "__main__":
    unittest.main(verbosity=2)

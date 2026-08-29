"""Process healer applicability and skip behavior tests."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

from healers.process_restarter import ProcessRestarter


class TestProcessRestarter(unittest.TestCase):
    @mock.patch("healers.process_restarter._discover_service_ports",
                return_value={})
    @mock.patch("healers.process_restarter._load_adopted",
                return_value={80: "systemd:nginx"})
    @mock.patch.object(ProcessRestarter, "_service_exists", return_value=False)
    def test_all_missing_services_are_skipped_not_repaired(
            self, _exists, _adopted, _discovered):
        result = ProcessRestarter().heal({
            "value": {"ports": {"80": False}}})
        self.assertFalse(result["success"])
        self.assertFalse(result["applicable"])
        self.assertTrue(result["skipped"])
        self.assertTrue(result["actions"][0]["skipped"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

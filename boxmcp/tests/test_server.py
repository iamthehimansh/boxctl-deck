from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from boxmcp import server


class SSHTests(unittest.TestCase):
    @patch("boxmcp.server.subprocess.run")
    def test_exec_uses_batch_ssh_and_quotes_payload(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, b"ok\n", b"")
        result = server.box_exec("printf '%s' 'hello world'")

        self.assertTrue(result["ok"])
        args = run.call_args.args[0]
        self.assertEqual(
            args[:6], ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "box"]
        )
        self.assertIn("bash -lc", args[-1])
        self.assertEqual(result["stdout"], "ok\n")

    @patch("boxmcp.server._ssh")
    def test_service_rejects_shell_metacharacters(self, ssh):
        result = server.box_service("safe; reboot", "status")
        self.assertFalse(result["ok"])
        ssh.assert_not_called()

    @patch("boxmcp.server._ssh")
    def test_service_status_accepts_systemctl_code_three(self, ssh):
        ssh.return_value = {"ok": False, "exit_code": 3, "stdout": "inactive\n"}
        result = server.box_service("example", "status")
        self.assertTrue(result["ok"])

    @patch("boxmcp.server._ssh")
    def test_write_enforces_size_limit(self, ssh):
        with patch.object(server, "MAX_WRITE_BYTES", 3):
            result = server.box_write("/tmp/x", "four")
        self.assertFalse(result["ok"])
        ssh.assert_not_called()


if __name__ == "__main__":
    unittest.main()

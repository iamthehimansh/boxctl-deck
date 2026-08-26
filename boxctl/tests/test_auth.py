from __future__ import annotations

import importlib.util
import pathlib
import types
import unittest
from unittest.mock import patch


MODULE_PATH = pathlib.Path(__file__).parents[1] / "boxctl.py"
SPEC = importlib.util.spec_from_file_location("boxctl_under_test", MODULE_PATH)
boxctl = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(boxctl)


class PasskeyPolicyTests(unittest.TestCase):
    def test_authorized_key_has_openssh_expiry(self):
        line = boxctl.passkey_authorization("ssh-ed25519 AAAATEST comment", 0)
        self.assertEqual(
            line,
            'expiry-time="19700101000000Z" ssh-ed25519 AAAATEST boxctl-passkey',
        )

    def test_session_key_has_openssh_expiry(self):
        line = boxctl.session_authorization("ssh-ed25519 AAAATEST boxctl-123", 0)
        self.assertEqual(
            line,
            'expiry-time="19700101000000Z" ssh-ed25519 AAAATEST boxctl-123',
        )

    @patch.object(boxctl, "passkey_remaining_days", return_value=-1)
    @patch.object(boxctl, "passkey_ready", return_value=True)
    @patch.object(boxctl, "meta", return_value={"passkey": True})
    def test_touch_id_mode_does_not_fall_into_hidden_totp_prompt(self, *_mocks):
        args = types.SimpleNamespace(
            totp=False, touch_id=True, remote=False, stdin_json=False
        )
        self.assertEqual(boxctl.cmd_connect(args), 1)


if __name__ == "__main__":
    unittest.main()

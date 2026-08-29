from __future__ import annotations

import importlib.util
import pathlib
import io
import tempfile
import types
import unittest
from unittest.mock import patch


MODULE_PATH = pathlib.Path(__file__).parents[1] / "boxctl.py"
SPEC = importlib.util.spec_from_file_location("boxctl_under_test", MODULE_PATH)
boxctl = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(boxctl)


class PasskeyPolicyTests(unittest.TestCase):
    @patch.object(boxctl, "lan_reachable", side_effect=lambda host: host == "192.168.1.204")
    @patch.object(boxctl.socket, "getaddrinfo", return_value=[
        (boxctl.socket.AF_INET, boxctl.socket.SOCK_STREAM, 6, "", ("192.168.1.204", 22))
    ])
    @patch.object(boxctl, "meta", return_value={"lan_host": "box.local"})
    def test_lan_route_prefers_reachable_ipv4(self, _meta, _resolve, _reachable):
        self.assertEqual(boxctl.preferred_lan_host(), "192.168.1.204")

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

    @patch.object(boxctl, "ssh_works", return_value=(True, ""))
    @patch.object(boxctl, "mint_session_key", return_value="installed")
    @patch.object(boxctl, "meta", return_value={})
    def test_password_only_json_login_does_not_require_totp(self, _meta, mint, _ssh):
        args = types.SimpleNamespace(
            totp=True, touch_id=False, remote=True, stdin_json=True
        )
        with patch("sys.stdin", io.StringIO('{"password":"secret"}')):
            self.assertEqual(boxctl.cmd_connect(args), 0)
        mint.assert_called_once_with("secret", "", force_remote=True)


class GUIAppTests(unittest.TestCase):
    @patch.object(boxctl, "_save_gui_registry")
    @patch.object(boxctl, "_gui_registry", return_value={"key": {"client_pid": 44}})
    @patch.object(boxctl, "_find_gui_session", return_value=("key", {
        "session_id": "session-1", "app": "Editor", "display": 222,
        "client_pid": 44, "attached": True}))
    @patch.object(boxctl.os, "kill")
    def test_detach_only_stops_local_client(self, kill, _find, _registry, save):
        self.assertEqual(boxctl.gui_detach("session-1"), 0)
        kill.assert_called_once_with(44, boxctl.signal.SIGTERM)
        self.assertEqual(save.call_args.args[0]["key"]["client_pid"], 0)

    def test_xpra_uses_linear_mac_trackpad_scrolling(self):
        env = boxctl.xpra_client_env()
        self.assertEqual(env["XPRA_SMOOTH_SCROLL_NORM"], "100")
        self.assertEqual(env["XPRA_MOUSE_SCROLL_SQRT_SCALE"], "0")

    def test_embedded_boxserver_is_valid_python(self):
        compile(boxctl.BOXSERVER, "boxserver", "exec")

    def test_desktop_id_rejects_shell_metacharacters(self):
        code, message = boxctl.desktop_command("app.desktop;touch-pwned")
        self.assertEqual(code, 2)
        self.assertEqual(message, "invalid application id")

    @patch.object(boxctl, "gui_launch", return_value=0)
    def test_custom_gui_command_stays_one_command(self, launch):
        args = types.SimpleNamespace(
            action="launch", desktop=None, microphone=False,
            command=["python", "viewer.py", "--demo"]
        )
        self.assertEqual(boxctl.cmd_gui(args), 0)
        launch.assert_called_once_with("python viewer.py --demo", "python viewer.py --demo", False)

    @patch.object(boxctl, "remote_gui_cleanup", return_value={"free": 0, "total": 1})
    def test_full_runtime_directory_blocks_launch(self, _cleanup):
        ready, reason = boxctl.gui_runtime_preflight()
        self.assertFalse(ready)
        self.assertIn("runtime directory is full", reason)

    def test_only_recorded_xpra_audio_holder_is_orphan_candidate(self):
        command = "/usr/bin/xpra --windows=no _audio_meter - - Xpra-Speaker.monitor 250"
        self.assertTrue(boxctl.is_recorded_orphan_helper(42, [42], command))
        self.assertFalse(boxctl.is_recorded_orphan_helper(43, [42], command))
        self.assertFalse(boxctl.is_recorded_orphan_helper(42, [42], "RelayDesk audio helper"))

    @patch.object(boxctl.os, "kill")
    @patch.object(boxctl, "remote_gui_cleanup", return_value={"removed": []})
    @patch.object(boxctl, "_save_gui_registry")
    @patch.object(boxctl, "_pid_command", return_value="Xpra seamless ssh://box/222")
    @patch.object(boxctl, "_gui_registry", return_value={"a": {"client_pid": 9, "display": 222}})
    def test_stale_cleanup_preserves_active_owned_session(self, _reg, _cmd, _save, _remote, kill):
        self.assertEqual(boxctl.gui_cleanup(False), (0, 0))
        kill.assert_not_called()

    @patch.object(boxctl, "_remote_start_app")
    @patch.object(boxctl, "_pid_command", return_value="Xpra seamless ssh://box/222")
    @patch.object(boxctl, "_gui_registry", return_value={})
    @patch.object(boxctl, "gui_runtime_preflight", return_value=(True, ""))
    @patch.object(boxctl, "run", return_value=types.SimpleNamespace(returncode=0, stdout="", stderr=""))
    @patch.object(boxctl, "ssh_works", return_value=(True, ""))
    @patch.object(boxctl, "xpra_binary", return_value="/Xpra")
    def test_duplicate_app_launch_does_not_start_second_server(
            self, _xpra, _ssh, _run, _preflight, registry, _pid, start):
        key = boxctl._app_key("Cana", "cocreate-desktop")
        registry.return_value = {key: {"client_pid": 9, "display": 222}}
        self.assertEqual(boxctl.gui_launch("cocreate-desktop", "Cana"), 0)
        start.assert_not_called()

    @patch.object(boxctl, "_save_gui_registry")
    @patch.object(boxctl, "_stop_remote_record")
    @patch.object(boxctl, "_remote_start_app", return_value={
        "session_id": "s", "display": 222, "server_pid": 10})
    @patch.object(boxctl, "_gui_registry", return_value={})
    @patch.object(boxctl, "gui_runtime_preflight", return_value=(True, ""))
    @patch.object(boxctl, "run", return_value=types.SimpleNamespace(returncode=0, stdout="", stderr=""))
    @patch.object(boxctl, "ssh_works", return_value=(True, ""))
    @patch.object(boxctl, "xpra_binary", return_value="/Xpra")
    def test_early_xpra_failure_returns_error(self, *_mocks):
        class FailedProcess:
            pid = 99
            def poll(self): return 1
            def terminate(self): pass
            def wait(self, timeout=None): return 1
            def kill(self): pass
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(boxctl, "CFG_DIR", pathlib.Path(folder)), \
             patch.object(boxctl, "GUI_REGISTRY", pathlib.Path(folder) / "sessions.json"), \
             patch.object(boxctl.subprocess, "Popen", return_value=FailedProcess()):
            self.assertEqual(boxctl.gui_launch("cocreate-desktop", "Cana"), 1)

    def test_log_rotation_is_bounded_and_keeps_tail(self):
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / "client.log"
            path.write_bytes(b"a" * 50 + b"TAIL")
            boxctl.rotate_log(path, limit=16, backups=2)
            self.assertEqual(path.stat().st_size, 0)
            rotated = path.with_name("client.log.1").read_bytes()
            self.assertLessEqual(len(rotated), 16)
            self.assertTrue(rotated.endswith(b"TAIL"))


if __name__ == "__main__":
    unittest.main()

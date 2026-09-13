import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


loader = importlib.machinery.SourceFileLoader("browser_launcher", str(Path(__file__).with_name("doubletake-browser")))
spec = importlib.util.spec_from_loader(loader.name, loader)
launcher = importlib.util.module_from_spec(spec)
loader.exec_module(launcher)


class BrowserTests(unittest.TestCase):
    def config(self):
        args = launcher.arguments(["--url", "https://example.invalid/dashboard", "--target", "192.0.2.1"])
        config = vars(args).copy()
        config["executables"] = {"browser": "/usr/bin/chromium", "sender": "/usr/bin/doubletake"}
        return config

    def test_invalid_url_does_not_leak_credentials(self):
        for url in ["https://user:private-value@example.invalid", "file:///etc/passwd", "https://bad host", "https://example.invalid:bad"]:
            output = io.StringIO()
            with contextlib.redirect_stderr(output), self.assertRaises(SystemExit):
                launcher.arguments(["--url", url, "--target", "192.0.2.1"])
            self.assertNotIn("private-value", output.getvalue())

    def test_no_automatic_receiver_or_repair(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            launcher.arguments(["--url", "https://example.invalid"])
        config = self.config()
        command = launcher.sender_command(config, 1234)
        self.assertEqual(command[command.index("-x11-window-id") + 1], "1234")
        self.assertIn("-no-audio", command)
        self.assertNotIn("-pair", command)
        self.assertNotIn("-debug", command)

    def test_browser_keeps_sandbox_and_url_out_of_command(self):
        config = self.config()
        config["url"] += "?secret=private-value"
        command = launcher.browser_command(config, Path("/tmp/private/launch.html"))
        self.assertNotIn("--no-sandbox", command)
        self.assertFalse(any("remote-debugging-port" in arg for arg in command))
        self.assertIn("--remote-debugging-pipe", command)
        self.assertNotIn("private-value", " ".join(command))
        self.assertIn("--ozone-platform=x11", command)

    def test_foreign_display_is_not_inherited(self):
        result = launcher.isolated_environment({"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0", "XAUTHORITY": "personal-cookie", "DBUS_SESSION_BUS_ADDRESS": "personal-bus", "PATH": "/bin"}, "/tmp/private")
        self.assertNotIn("DISPLAY", result)
        self.assertNotIn("WAYLAND_DISPLAY", result)
        self.assertNotIn("XAUTHORITY", result)
        self.assertEqual(result["XDG_RUNTIME_DIR"], "/tmp/private")

    def test_setup_never_requires_a_tv_and_rejects_pair(self):
        args = launcher.arguments(["--url", "https://example.invalid", "--setup"])
        self.assertIsNone(args.target)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            launcher.arguments(["--url", "https://example.invalid", "--setup", "--pair"])

    def test_status_is_private_and_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            launcher.write_status(directory, phase="stopped")
            path = Path(directory) / "status.json"
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text()), {"phase": "stopped"})
            self.assertFalse(path.with_suffix(".tmp").exists())

    def test_stop_terminates_owned_child_group(self):
        process = subprocess.Popen(["sleep", "60"], start_new_session=True)
        launcher.stop(process)
        self.assertIsNotNone(process.poll())

    def test_private_pipe_requests_graceful_browser_close(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "browser"
            saved = Path(directory) / "saved-profile"
            fixture.write_text("#!/usr/bin/env python3\nimport json, os\nfrom pathlib import Path\n"
                               "message = os.read(3, 4096).split(b'\\0')[0]\n"
                               "assert json.loads(message)['method'] == 'Browser.close'\n"
                               "Path(os.environ['TEST_SAVED_PROFILE']).write_text('saved')\n"
                               "os.write(4, b'{}\\0')\n")
            fixture.chmod(0o700)
            config = self.config()
            config["executables"]["browser"] = str(fixture)
            child, command_fd, response_fd = launcher.start_browser(config, Path(directory) / "launch.html", dict(os.environ, TEST_SAVED_PROFILE=str(saved)))
            try:
                launcher.close_browser(child, command_fd)
                self.assertEqual(child.returncode, 0)
                self.assertEqual(saved.read_text(), "saved")
            finally:
                launcher.stop(child)
                os.close(command_fd)
                os.close(response_fd)


if __name__ == "__main__":
    unittest.main()

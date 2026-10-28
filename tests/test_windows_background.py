"""Regression checks for persistent background startup and custom runtime paths."""

import base64
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from windows_background import launch_background


class BackgroundLaunchTests(unittest.TestCase):
    def test_launch_uses_windows_provider_and_preserves_environment_and_unicode_paths(self):
        command = [r"D:\cache folder\python.exe", r"D:\private & project\控制.py", "supervise"]
        runtime = Path(r"D:\custom runtime")
        environment = {"DAFEIYU_RUNTIME_ROOT": str(runtime), "CUSTOM_OPTION": "preserve"}
        with patch("windows_background.subprocess.run", return_value=Mock(stdout="1234\n")) as run:
            self.assertEqual(launch_background(command, runtime, environment), 1234)
        args, options = run.call_args
        script = base64.b64decode(args[0][-1]).decode("utf-16le")
        payload = re.search(r"FromBase64String\('([A-Za-z0-9+/=]+)'\)", script).group(1)
        decoded = json.loads(base64.b64decode(payload).decode("utf-8"))
        self.assertEqual(decoded["command_line"], subprocess.list2cmdline(command))
        self.assertEqual(decoded["cwd"], str(runtime))
        self.assertIs(options["env"], environment)
        self.assertIn("Invoke-CimMethod -ClassName Win32_Process", script)
        self.assertIn("EnvironmentVariables = [string[]]$variables", script)
        self.assertNotIn("shell", options)

    def test_provider_failure_is_not_reported_as_success(self):
        with patch("windows_background.subprocess.run", side_effect=subprocess.CalledProcessError(1, "powershell")), self.assertRaises(subprocess.CalledProcessError):
            launch_background(["python.exe", "control.py", "supervise"], Path.cwd(), {})

    def test_invalid_process_identifier_is_rejected(self):
        for output in ("", "0", "not a process identifier"):
            with self.subTest(output=output), patch("windows_background.subprocess.run", return_value=Mock(stdout=output)), self.assertRaises(RuntimeError):
                launch_background(["python.exe", "control.py", "supervise"], Path.cwd(), {})


if __name__ == "__main__":
    unittest.main()

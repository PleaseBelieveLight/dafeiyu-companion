"""Exercise real Windows entry points without starting services or running installers."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "nt", "Windows CMD integration")
class CmdEntrypointTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "中文 project with spaces"
        self.root.mkdir()
        # Copy the actual entries so broken relative paths fail in the Windows shell.
        for path in [*ROOT.glob("*.cmd"), *ROOT.glob("scripts/**/*.cmd"), ROOT / "tools/run.cmd"]:
            destination = self.root / path.relative_to(ROOT)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
        (self.root / "tools/launch.py").write_text(
            "import json, os, sys\n"
            "print(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd(), "
            "'runtime': os.environ.get('DAFEIYU_RUNTIME_ROOT')}))\n"
            "raise SystemExit(int(os.environ.get('TEST_EXIT_CODE', '0')))\n",
            encoding="utf-8",
        )
        self.env = dict(os.environ, DAFEIYU_NO_PAUSE="1", PYTHONUTF8="1")
        self.env["DAFEIYU_RUNTIME_ROOT"] = str(Path(self.temporary.name) / "独立 runtime")

    def run_entry(self, relative, arguments=()):
        return subprocess.run(
            [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", "call",
             str(self.root / relative), *arguments],
            cwd=self.temporary.name, env=self.env, capture_output=True,
            encoding="utf-8", errors="replace", timeout=30,
        )

    def test_root_and_nested_entries_forward_arguments_and_use_project_directory(self):
        for relative, action in {
            "启动全部.cmd": "start",
            "scripts/安装配置/安装.cmd": "install",
            "scripts/维护/部署更新.cmd": "deploy",
            "scripts/扩展功能/启动虚拟手机.cmd": "phone-start",
        }.items():
            with self.subTest(entry=relative):
                result = self.run_entry(relative, ["--label", "中文 value with spaces"])
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                data = json.loads(result.stdout)
                self.assertEqual(data["argv"], [action, "--label", "中文 value with spaces"])
                self.assertEqual(Path(data["cwd"]), self.root)
                self.assertEqual(data["runtime"], self.env["DAFEIYU_RUNTIME_ROOT"])

    def test_entry_returns_task_failure_instead_of_pause_status(self):
        self.env["TEST_EXIT_CODE"] = "7"
        result = self.run_entry("scripts/维护/验证安装.cmd")
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()

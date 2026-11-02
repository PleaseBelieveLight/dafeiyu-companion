"""Exercise tagged release packaging against isolated Git repositories."""

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import build_release


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.root = self.folder / "project"
        self.root.mkdir()
        self.git("init", "--quiet")
        self.git("config", "core.autocrlf", "false")
        self.files = {
            ".gitignore": b"/AGENTS.md\n/config/settings.json\n/run/\n/dist/\n",
            "README.md": "# 大肥鱼\n".encode(),
            "config/settings.example.json": b"{}\n",
            "content/knowledge/资料.md": "运行输入\n".encode(),
            "docs/说明.md": "安装说明\n".encode(),
            "tools/example.py": b"print('source')\n",
            "启动全部.cmd": b"@echo off\r\nexit /b 0\r\n",
        }
        for name, data in self.files.items():
            self.write(name, data)
        self.commit("feat: 初始版本")
        self.git("tag", "v0.1.0")

    def git(self, *args):
        return subprocess.run(
            ["git", "-c", "core.hooksPath=", "-c", "commit.gpgsign=false", *args],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()

    def write(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def commit(self, message):
        self.git("add", "--all")
        self.git(
            "-c",
            "user.name=Release test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--quiet",
            "-m",
            message,
        )

    def build(self, tag="v0.1.0", output=None):
        return build_release.build_release(tag, output or self.folder / "output", self.root)

    def test_archive_preserves_sources_and_excludes_untracked_and_ignored_files(self):
        self.write("AGENTS.md", b"local instructions")
        self.write("config/settings.json", b'{"private": "local value"}')
        self.write("run/private.txt", b"local runtime")
        self.write("tools/local.py", b"untracked helper")
        archive = self.build()
        with zipfile.ZipFile(archive) as bundle:
            self.assertIsNone(bundle.testzip())
            self.assertEqual(
                set(bundle.namelist()), {"dafeiyu-companion/" + name for name in self.files}
            )
            for name, data in self.files.items():
                self.assertEqual(bundle.read("dafeiyu-companion/" + name), data)
        digest, name = (archive.parent / "SHA256SUMS.txt").read_text().split()
        self.assertEqual(name, archive.name)
        self.assertEqual(digest, hashlib.sha256(archive.read_bytes()).hexdigest())
        notes = (archive.parent / "RELEASE_NOTES.md").read_text(encoding="utf-8")
        self.assertIn(self.git("rev-parse", "HEAD"), notes)
        self.assertIn("feat: 初始版本", notes)

    def test_archive_is_reproducible_across_output_directories(self):
        first = self.build(output=self.folder / "first")
        second = self.build(output=self.folder / "second")
        self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_notes_include_only_changes_after_previous_reachable_version(self):
        self.write("tools/example.py", b"print('fixed')\n")
        self.commit("fix: 更新安装入口")
        self.git("tag", "v0.1.1")
        archive = self.build("v0.1.1")
        notes = (archive.parent / "RELEASE_NOTES.md").read_text(encoding="utf-8")
        self.assertIn("自 v0.1.0", notes)
        self.assertIn("fix: 更新安装入口", notes)
        self.assertNotIn("feat: 初始版本", notes)

    def test_invalid_tags_are_rejected_before_output_is_created(self):
        for tag in ("1.0.0", "v01.0.0", "v1.2", "v1.2.3-rc.1", "v1.2.3/../../x"):
            with self.subTest(tag=tag), self.assertRaisesRegex(ValueError, "Release tag"):
                self.build(tag)
        self.assertFalse((self.folder / "output").exists())

    def test_tag_must_point_to_head(self):
        self.write("tools/example.py", b"print('newer')\n")
        self.commit("fix: 后续修改")
        with self.assertRaisesRegex(ValueError, "HEAD"):
            self.build()

    def test_modified_staged_and_missing_sources_are_rejected(self):
        path = self.root / "tools/example.py"
        path.write_bytes(b"modified\n")
        with self.assertRaisesRegex(ValueError, "tracked changes"):
            self.build()
        self.git("add", "tools/example.py")
        with self.assertRaisesRegex(ValueError, "tracked changes"):
            self.build()
        self.git("restore", "--staged", "tools/example.py")
        path.unlink()
        with self.assertRaisesRegex(ValueError, "tracked changes"):
            self.build()

    def test_tracked_runtime_files_and_nonpublic_files_block_packaging(self):
        for name in ("tools/private.db", "unexpected.txt"):
            with self.subTest(name=name):
                self.write(name, b"private fixture")
                self.git("add", name)
                self.git(
                    "-c",
                    "user.name=Release test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "--quiet",
                    "-m",
                    "test: private file",
                )
                self.git("tag", "--force", "v0.1.0")
                with self.assertRaisesRegex(ValueError, "distribution check"):
                    self.build()
                self.git("rm", name)
                self.commit("test: remove fixture")

    def test_nonempty_private_template_field_blocks_packaging(self):
        self.write("config/settings.example.json", json.dumps({"owner_qq": "fixture"}).encode())
        self.commit("test: invalid template")
        self.git("tag", "--force", "v0.1.0")
        with self.assertRaisesRegex(ValueError, "private example field"):
            self.build()

    def test_public_source_output_and_existing_artifacts_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "outside public source"):
            self.build(output=self.root / "docs/release")
        archive = self.build()
        original = archive.read_bytes()
        with self.assertRaises(FileExistsError):
            self.build()
        self.assertEqual(archive.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()

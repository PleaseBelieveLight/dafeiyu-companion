"""Validate source packaging, archive containment and release defaults."""

import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import check_release
import install


class DistributionTests(unittest.TestCase):
    def test_public_source_has_no_live_credentials_or_runtime_data(self):
        self.assertEqual(check_release.inspect(ROOT), [])

    def test_archive_tree_ignores_commit_root_name(self):
        with tempfile.TemporaryDirectory() as folder:
            archives = []
            for prefix in ["repo-main", "repo-123"]:
                path = Path(folder) / (prefix + ".zip")
                with zipfile.ZipFile(path, "w") as z:
                    z.writestr(prefix + "/file.txt", "same content")
                archives.append(path)
            self.assertEqual(
                install.tree_digest(archives[0], True), install.tree_digest(archives[1], True)
            )

    def test_extraction_rejects_traversal_drive_paths_and_symlinks(self):
        with tempfile.TemporaryDirectory() as folder:
            for name in [
                "../escape.txt",
                "C:/escape.txt",
                "root/../../escape.txt",
                "root/evil:stream",
                "\\absolute.txt",
            ]:
                with self.subTest(name=name):
                    archive = Path(folder) / "bad.zip"
                    with zipfile.ZipFile(archive, "w") as z:
                        z.writestr(name, "blocked")
                    with self.assertRaises(ValueError):
                        install.unpack(archive, Path(folder) / "out")
            archive = Path(folder) / "symlink.zip"
            info = zipfile.ZipInfo("link")
            info.external_attr = 0o120777 << 16
            with zipfile.ZipFile(archive, "w") as z:
                z.writestr(info, "../escape")
            with self.assertRaises(ValueError):
                install.unpack(archive, Path(folder) / "out")

    def test_source_dependencies_use_fixed_commits_and_tree_hashes(self):
        lock = json.loads((ROOT / "dependencies.lock.json").read_text(encoding="utf-8"))
        sources = [item for item in lock["artifacts"] if item["kind"] == "source"]
        self.assertEqual(len(sources), 10)
        for item in sources:
            self.assertRegex(item["commit"], r"^[0-9a-f]{40}$")
            self.assertRegex(item["tree_sha256"], r"^[0-9a-f]{64}$")
            self.assertIn(item["commit"], item["url"])

    def test_template_has_no_machine_specific_paths(self):
        settings = install.read_json(ROOT / "config/settings.example.json")
        for field in check_release.SECRET_FIELDS:
            self.assertFalse(settings.get(field))
        self.assertEqual(settings["emulator_serial"], "emulator-5556")


if __name__ == "__main__":
    unittest.main()

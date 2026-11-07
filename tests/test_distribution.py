"""Validate source packaging, archive containment and release defaults."""

import json
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import check_release
import install
import workspace


class DistributionTests(unittest.TestCase):
    def test_public_source_has_no_live_credentials_or_runtime_data(self):
        self.assertEqual(check_release.inspect(ROOT), [])

    def test_documentation_and_runtime_inputs_are_included_in_source_export(self):
        exported = set(check_release.source_files(ROOT))
        for folder in ("docs", "content"):
            with self.subTest(folder=folder):
                documents = set((ROOT / folder).rglob("*.md"))
                self.assertTrue(documents)
                self.assertTrue(documents.issubset(exported))
        self.assertIn(ROOT / "AGENTS.md", exported)

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

    def test_installer_stages_the_local_runtime_without_touching_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "project"
            source.mkdir()
            (source / "dependencies.lock.json").write_text('{"artifacts": []}', encoding="utf-8")
            (source / "config").mkdir()
            (source / "config/settings.example.json").write_text("{}", encoding="utf-8")
            args = types.SimpleNamespace(root=source / ".local", stage_only=True)
            with patch.object(workspace, "SOURCE_ROOT", source), \
                patch.object(install, "SOURCE", source), \
                patch.object(install, "os", types.SimpleNamespace(name="nt")), \
                patch.object(install.sys, "version_info", (3, 12)), \
                patch.object(install, "find_program", return_value=None), \
                patch.object(install, "apply_overlays") as overlays:
                install.install(args)
            overlays.assert_called_once_with(source / ".local")
            self.assertEqual(json.loads((source / ".local/config/settings.json").read_text()), {})
            self.assertFalse((source / "run").exists())

    def test_installer_requires_migration_when_a_legacy_runtime_exists(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "project"
            settings = Path(folder) / "Dafeiyu/config/settings.json"
            settings.parent.mkdir(parents=True)
            settings.write_text("{}", encoding="utf-8")
            args = types.SimpleNamespace(root=source / ".local")
            for existing_empty_directory in (False, True):
                if existing_empty_directory:
                    args.root.mkdir(parents=True)
                with self.subTest(existing_empty_directory=existing_empty_directory), \
                    patch.object(workspace, "SOURCE_ROOT", source), \
                    patch.object(install, "SOURCE", source), \
                    patch.object(install, "os", types.SimpleNamespace(name="nt")), \
                    patch.object(install.sys, "version_info", (3, 12)), \
                    self.assertRaisesRegex(RuntimeError, "Migrate"):
                    install.install(args)
                self.assertFalse((source / ".local/config/settings.json").exists())


if __name__ == "__main__":
    unittest.main()

"""Guard separation of tracked project files and private runtime data."""

import json
import os
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import deploy
import launch
import workspace


class WorkspaceTests(unittest.TestCase):
    def test_default_runtime_is_a_sibling_and_does_not_depend_on_working_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "private-project"
            with patch.object(workspace, "SOURCE_ROOT", source), patch.dict(os.environ, {}, clear=True):
                self.assertEqual(workspace.runtime_root(), (Path(folder) / "Dafeiyu").resolve())

    def test_custom_runtime_is_resolved_relative_to_the_project(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "project"
            with patch.object(workspace, "SOURCE_ROOT", source), patch.dict(
                os.environ, {"DAFEIYU_RUNTIME_ROOT": "../custom-runtime"}, clear=True
            ):
                self.assertEqual(workspace.runtime_root(), (Path(folder) / "custom-runtime").resolve())

    def test_runtime_cannot_contain_or_be_inside_source(self):
        source = workspace.SOURCE_ROOT
        for target in (source, source / "cache", source.parent):
            with self.subTest(target=target), patch.dict(
                os.environ, {"DAFEIYU_RUNTIME_ROOT": str(target)}
            ), self.assertRaises(ValueError):
                workspace.runtime_root()

    def test_launch_reads_code_from_project_and_interpreter_from_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder)
            python = runtime / "runtime/astrbot/Scripts/python.exe"
            python.parent.mkdir(parents=True)
            python.touch()
            with patch.object(launch, "RUNTIME_ROOT", runtime):
                self.assertEqual(launch.command("start", []), [
                    str(python), str(workspace.TOOLS / "control.py"), "start",
                ])
                self.assertEqual(launch.command("sync-content", ["extra"]), [
                    str(python), str(workspace.TOOLS / "initialize_api.py"), "extra",
                ])

    def test_missing_runtime_does_not_silently_install_or_start_a_second_instance(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(
            launch, "RUNTIME_ROOT", Path(folder)
        ), self.assertRaises(RuntimeError):
            launch.command("start", [])

    def test_mcp_migration_preserves_other_servers_and_options(self):
        config = {"mcpServers": {
            "existing": {"url": "http://127.0.0.1:8010/Browser", "active": True},
            "dafeiyu-admin": {"active": False, "env": {"CUSTOM_OPTION": "keep"}},
        }}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "mcp_server.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            deploy.update_admin_config(path)
            updated = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(updated["mcpServers"]["existing"], config["mcpServers"]["existing"])
        entry = updated["mcpServers"]["dafeiyu-admin"]
        self.assertFalse(entry["active"])
        self.assertEqual(entry["env"]["CUSTOM_OPTION"], "keep")
        self.assertEqual(entry["env"]["DAFEIYU_RUNTIME_ROOT"], str(workspace.RUNTIME_ROOT))
        self.assertEqual(entry["args"], [str(workspace.TOOLS / "admin_mcp.py")])

    def test_deployment_retires_old_source_without_stopping_qq_or_memory_server(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "runtime"
            root.mkdir()
            # Exercise an alias on every platform, even without Windows 8.3 names.
            root = root / ".." / "runtime"
            for name in ("config/settings.json", "astrbot/main.py", "source/dafeiyu-astrbot-main/old.py"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}", encoding="utf-8")
            state = {name: {"name": name} for name in (
                "napcat", "ollama", "astrbot", "asr", "phone", "browser", "initialize-api",
            )}
            control = types.SimpleNamespace(
                RUN=root / "run", ASTROPY=root / "python.exe", load=Mock(return_value=state),
                detach_supervisor=Mock(), stop_info=Mock(), store=Mock(), start=Mock(),
            )
            with patch.object(deploy, "ROOT", root), patch.dict(sys.modules, {"control": control}), \
                patch.object(deploy, "backup_code", return_value=root / "backup.zip"), \
                patch.object(deploy, "preserve_local_programs"), \
                patch("install.apply_overlays"), patch.object(deploy, "update_admin_config"), \
                patch.object(deploy, "refresh_legacy_tool_redirects"), \
                patch.object(deploy, "remove_legacy_cmd_redirects"), \
                patch.object(deploy.subprocess, "run"), \
                patch.object(deploy, "wait_ready"):
                deploy.deploy(prune_legacy_source=True)
            self.assertFalse((root / "source/dafeiyu-astrbot-main").exists())
            stopped = [call.args[0]["name"] for call in control.stop_info.call_args_list]
            self.assertNotIn("napcat", stopped)
            self.assertNotIn("ollama", stopped)
            self.assertEqual(set(state), {"napcat", "ollama"})

    def test_failed_deployment_restores_the_legacy_source_and_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "runtime"
            root.mkdir()
            root = root / ".." / "runtime"
            for name, text in {
                "config/settings.json": '{"existing_option": "preserve"}',
                "astrbot/main.py": "# existing runtime",
                "source/dafeiyu-astrbot-main/old.py": "# legacy source",
            }.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            control = types.SimpleNamespace(
                RUN=root / "run", ASTROPY=root / "python.exe", load=Mock(return_value={}),
                detach_supervisor=Mock(), stop_info=Mock(), store=Mock(),
            )
            with patch.object(deploy, "ROOT", root), patch.dict(sys.modules, {"control": control}), \
                patch.object(deploy, "preserve_local_programs"), \
                patch("install.apply_overlays", side_effect=RuntimeError("patch mismatch")), \
                patch.object(deploy.subprocess, "run"), self.assertRaisesRegex(RuntimeError, "patch mismatch"):
                deploy.deploy(prune_legacy_source=True)
            self.assertEqual(
                (root / "source/dafeiyu-astrbot-main/old.py").read_text(encoding="utf-8"),
                "# legacy source",
            )
            self.assertEqual(json.loads((root / "config/settings.json").read_text()),
                             {"existing_option": "preserve"})

    def test_deployment_rejects_linked_or_redirected_source_before_stopping_services(self):
        for link_type in ("symlink", "redirected_parent"):
            with self.subTest(link_type=link_type), tempfile.TemporaryDirectory() as folder:
                root = Path(folder) / "runtime"
                for name in ("config/settings.json", "astrbot/main.py",
                             "source/dafeiyu-astrbot-main/old.py"):
                    path = root / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("original", encoding="utf-8")
                legacy = root / "source/dafeiyu-astrbot-main"
                outside = Path(folder) / "outside"
                outside.mkdir()
                original_resolve, original_is_symlink = Path.resolve, Path.is_symlink

                def resolve(path, *args, **kwargs):
                    if path == legacy and link_type == "redirected_parent":
                        return original_resolve(outside)
                    return original_resolve(path, *args, **kwargs)

                def is_symlink(path):
                    if path == legacy and link_type == "symlink":
                        return True
                    return original_is_symlink(path)

                control = types.SimpleNamespace(detach_supervisor=Mock())
                with patch.object(deploy, "ROOT", root), \
                    patch.dict(sys.modules, {"control": control}), \
                    patch.object(deploy, "backup_code", return_value=root / "backup.zip"), \
                    patch.object(Path, "resolve", resolve), \
                    patch.object(Path, "is_symlink", is_symlink), \
                    patch("install.apply_overlays") as apply, \
                    self.assertRaisesRegex(RuntimeError, "linked or unexpected"):
                    deploy.deploy(prune_legacy_source=True)
                control.detach_supervisor.assert_not_called()
                apply.assert_not_called()
                self.assertEqual((legacy / "old.py").read_text(encoding="utf-8"), "original")
                self.assertEqual(list(outside.iterdir()), [])

    def test_recovery_rejects_archive_escape_with_a_noncanonical_runtime_root(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "runtime"
            root.mkdir()
            root = root / ".." / "runtime"
            for name in ("config/settings.json", "astrbot/main.py"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}", encoding="utf-8")
            outside = Path(folder) / "outside.txt"
            outside.write_text("preserve", encoding="utf-8")
            backup = root / "backup.zip"
            with zipfile.ZipFile(backup, "w") as archive:
                archive.writestr("../outside.txt", "overwrite")
            control = types.SimpleNamespace(
                RUN=root / "run", load=Mock(return_value={}),
                detach_supervisor=Mock(), stop_info=Mock(), store=Mock(),
            )
            with patch.object(deploy, "ROOT", root), \
                patch.dict(sys.modules, {"control": control}), \
                patch.object(deploy, "backup_code", return_value=backup), \
                patch.object(deploy, "preserve_local_programs"), \
                patch("install.apply_overlays", side_effect=RuntimeError("patch mismatch")), \
                self.assertRaisesRegex(RuntimeError, "Unexpected recovery archive path"):
                deploy.deploy()
            self.assertEqual(outside.read_text(encoding="utf-8"), "preserve")

    def test_runtime_cleanup_removes_only_unchanged_generated_cmd_entries(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "runtime"
            root.mkdir()
            source = Path(folder) / "project"
            generated = root / "启动全部.cmd"
            text = (
                '@echo off\r\nchcp 65001 >nul\r\n'
                f'set "DAFEIYU_RUNTIME_ROOT={root}"\r\n'
                f'call "{source / generated.name}" %*\r\n'
            )
            generated.write_bytes(text.encode("utf-8"))
            custom = root / "自定义.cmd"
            custom.write_bytes(b"@echo off\r\necho custom\r\n")
            edited = root / "停止全部.cmd"
            edited.write_bytes(text.replace(generated.name, edited.name).encode("utf-8")
                               + b"rem local change\r\n")
            unrelated = root / "settings.json"
            unrelated.write_text("{}", encoding="utf-8")
            custom_folder = root / "目录.cmd"
            custom_folder.mkdir()
            with patch.object(deploy, "ROOT", root), patch.object(deploy, "SOURCE_ROOT", source):
                self.assertEqual(deploy.remove_legacy_cmd_redirects(), [generated])
                self.assertEqual(deploy.remove_legacy_cmd_redirects(), [])
            self.assertFalse(generated.exists())
            self.assertTrue(custom.exists())
            self.assertTrue(edited.exists())
            self.assertTrue(unrelated.exists())
            self.assertTrue(custom_folder.is_dir())

    def test_refreshing_legacy_python_tools_does_not_recreate_cmd_entries(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "runtime"
            source = Path(folder) / "project"
            (root / "tools").mkdir(parents=True)
            (source / "tools").mkdir(parents=True)
            (source / "启动全部.cmd").touch()
            (source / "tools/control.py").touch()
            legacy = root / "tools/control.py"
            legacy.write_text("# old wrapper", encoding="utf-8")
            with patch.object(deploy, "ROOT", root), \
                patch.object(deploy, "TOOLS", source / "tools"):
                deploy.refresh_legacy_tool_redirects()
            self.assertEqual(list(root.glob("*.cmd")), [])
            self.assertIn("runpy.run_path", legacy.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

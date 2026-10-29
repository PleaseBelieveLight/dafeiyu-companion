"""Exercise durable state, read-only inspection and isolated plugin installation."""

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins"))
sys.path.insert(0, str(ROOT / "tools"))
from astrbot_plugin_life_continuity.state import (
    APPLICATION_ID,
    DATABASE_NAME,
    PLUGIN_NAME,
    SCHEMA_VERSION,
    ConflictError,
    LifeStore,
    StateError,
    read_snapshot,
)
import install

SCOPE = "test:private:owner"
OTHER_SCOPE = "test:private:other"


class LifeStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / "state"
        self.store = LifeStore(self.folder)
        self.addCleanup(self.store.close)

    def goal(self, scope=SCOPE, source_id="goal-source"):
        return self.store.create_goal(scope, source_id, "研究建造机制", "留下了一个疑问",
                                      "找到问题的有来源解释", "读取主人提供的资料")

    def episode(self, goal, source_id="read-source", **kwargs):
        return self.store.record_episode(goal["scope"], goal["id"], source_id, kind="read",
                                         evidence={"source": "owner-provided", "text": "资料片段"},
                                         **kwargs)

    def test_goal_episode_note_and_checkpoint_survive_reopen(self):
        goal = self.goal()
        episode = self.episode(goal, interpretation={"thought": "需要进一步核对"})
        self.store.save_note(SCOPE, goal["id"], 0, "初步笔记", [episode["id"]])
        self.store.save_note(SCOPE, goal["id"], 1, "补充笔记", [episode["id"]])
        self.store.save_checkpoint(SCOPE, goal["id"], 1, next_step="核对另一份资料",
                                   checkpoint={"question": "承重是否有限制", "position": None})
        self.store.close()
        store = LifeStore(self.folder)
        self.addCleanup(store.close)
        snapshot = read_snapshot(self.folder, scope=SCOPE)
        self.assertEqual(snapshot["schema_version"], SCHEMA_VERSION)
        self.assertEqual(snapshot["goals"][0]["checkpoint"]["position"], None)
        self.assertEqual(snapshot["goals"][0]["version"], 2)
        self.assertEqual(snapshot["episodes"][0]["interpretation"]["thought"], "需要进一步核对")
        self.assertEqual({row["version"] for row in snapshot["notes"]}, {1, 2})
        self.assertEqual(snapshot["counts"]["notes"], 2)
        self.assertIsNone(store.schema_backup)

    def test_goal_source_is_idempotent_even_after_progress(self):
        goal = self.goal()
        self.store.save_checkpoint(SCOPE, goal["id"], 1, next_step="新的问题", checkpoint={})
        repeated = self.goal()
        self.assertEqual(repeated["id"], goal["id"])
        self.assertEqual(repeated["next_step"], "新的问题")
        with self.assertRaises(ConflictError):
            self.store.create_goal(SCOPE, "goal-source", "另一个标题", "理由", "条件", "下一步")

    def test_episode_source_is_idempotent_and_changed_payload_conflicts(self):
        goal = self.goal()
        first = self.episode(goal)
        self.assertEqual(self.episode(goal)["id"], first["id"])
        with self.assertRaises(ConflictError):
            self.episode(goal, interpretation={"thought": "另一个说法"})
        self.assertEqual(read_snapshot(self.folder)["counts"]["episodes"], 1)

    def test_two_writers_cannot_overwrite_a_checkpoint_or_note_version(self):
        goal = self.goal()
        second = LifeStore(self.folder)
        self.addCleanup(second.close)
        self.store.save_checkpoint(SCOPE, goal["id"], 1, next_step="新进展", checkpoint={})
        with self.assertRaises(ConflictError):
            second.save_checkpoint(SCOPE, goal["id"], 1, next_step="旧状态", checkpoint={})
        episode = self.episode(goal)
        self.store.save_note(SCOPE, goal["id"], 0, "第一版", [episode["id"]])
        with self.assertRaises(ConflictError):
            second.save_note(SCOPE, goal["id"], 0, "冲突版本", [episode["id"]])
        self.assertEqual(second.get_goal(SCOPE, goal["id"])["next_step"], "新进展")

    def test_scopes_and_goal_references_are_isolated(self):
        own, other = self.goal(), self.goal(OTHER_SCOPE)
        source = self.episode(own)
        self.episode(other)
        self.assertIsNone(self.store.get_goal(OTHER_SCOPE, own["id"]))
        with self.assertRaises(StateError):
            self.store.record_episode(OTHER_SCOPE, own["id"], "cross", kind="read", evidence={})
        with self.assertRaises(StateError):
            self.store.save_note(OTHER_SCOPE, other["id"], 0, "跨会话笔记", [source["id"]])
        own_snapshot = read_snapshot(self.folder, scope=SCOPE)
        self.assertEqual({row["scope"] for row in own_snapshot["goals"]}, {SCOPE})
        self.assertEqual(own_snapshot["counts"]["episodes"], 1)
        self.assertEqual(read_snapshot(self.folder)["episodes"], [])

    def test_revisions_preserve_history_and_invalidate_old_note_sources(self):
        goal = self.goal()
        first = self.episode(goal)
        second = self.episode(goal, "correction", supersedes_id=first["id"])
        snapshot = read_snapshot(self.folder, scope=SCOPE)
        self.assertEqual(snapshot["counts"]["episodes"], 2)
        self.assertEqual([row["id"] for row in snapshot["episodes"]], [second["id"]])
        with self.assertRaises(StateError):
            self.store.save_note(SCOPE, goal["id"], 0, "旧来源", [first["id"]])
        self.store.save_note(SCOPE, goal["id"], 0, "修订后", [second["id"]])
        with self.assertRaises(sqlite3.IntegrityError):
            self.episode(goal, "fork", supersedes_id=first["id"])
        self.assertEqual(read_snapshot(self.folder)["counts"]["episodes"], 2)

    def test_run_reopen_and_readonly_queries_never_recover_running_state(self):
        goal = self.goal()
        run = self.store.start_run(SCOPE, goal["id"], 1, "trigger")
        self.assertEqual(self.store.start_run(SCOPE, goal["id"], 1, "trigger")["id"], run["id"])
        self.assertEqual(read_snapshot(self.folder, scope=SCOPE)["runs"][0]["status"], "running")
        self.store.close()
        store = LifeStore(self.folder)
        self.addCleanup(store.close)
        self.assertEqual(store.get_goal(SCOPE, goal["id"])["status"], "running")
        self.assertEqual(read_snapshot(self.folder, scope=SCOPE)["runs"][0]["status"], "running")

    def test_second_run_failure_rolls_back_goal_transition(self):
        first, second = self.goal(), self.goal(source_id="second")
        self.store.start_run(SCOPE, first["id"], 1, "first-trigger")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.start_run(SCOPE, second["id"], 1, "second-trigger")
        self.assertEqual(self.store.get_goal(SCOPE, second["id"])["status"], "ready")
        self.assertEqual(read_snapshot(self.folder)["counts"]["runs"], 1)

    def test_run_result_and_checkpoint_are_settled_together(self):
        goal = self.goal()
        run = self.store.start_run(SCOPE, goal["id"], 1, "trigger")
        self.episode(goal, run_id=run["id"])
        updated = self.store.finish_run(SCOPE, run["id"], status="completed", summary="完成一步",
                                        next_step="下一步", checkpoint={"source_id": "read-source"})
        self.assertEqual(updated["status"], "paused")
        with self.assertRaises(ConflictError):
            self.store.finish_run(SCOPE, run["id"], status="completed", summary="重复",
                                  next_step="覆盖", checkpoint={})
        self.assertEqual(self.store.get_goal(SCOPE, goal["id"])["next_step"], "下一步")

    def test_checkpoint_write_failure_rolls_back_the_run_result(self):
        goal = self.goal()
        run = self.store.start_run(SCOPE, goal["id"], 1, "trigger")
        self.store.db.execute("""CREATE TRIGGER reject_checkpoint BEFORE UPDATE ON goals
            BEGIN SELECT RAISE(ABORT, 'simulated checkpoint failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.finish_run(SCOPE, run["id"], status="completed", summary="不得单独落盘",
                                  next_step="未保存", checkpoint={"partial": True})
        snapshot = read_snapshot(self.folder, scope=SCOPE)
        self.assertEqual(snapshot["runs"][0]["status"], "running")
        self.assertIsNone(snapshot["runs"][0]["ended_at"])
        self.assertEqual(snapshot["runs"][0]["summary"], "")
        self.assertEqual(snapshot["goals"][0]["checkpoint"], {})

    def test_invalid_data_is_rejected_before_writing(self):
        goal = self.goal()
        for checkpoint in ({"value": float("nan")}, {"data": "大" * 30000}, []):
            with self.subTest(checkpoint_type=type(checkpoint)), self.assertRaises(ValueError):
                self.store.save_checkpoint(
                    SCOPE, goal["id"], 1, next_step="继续", checkpoint=checkpoint
                )
        with self.assertRaises(ValueError):
            self.episode(goal, occurred_at=-1)
        with self.assertRaises(ValueError):
            self.store.save_note(SCOPE, goal["id"], False, "笔记", [])
        self.assertEqual(self.store.get_goal(SCOPE, goal["id"])["version"], 1)

    def test_manual_pause_requires_explicit_ready_before_a_new_run(self):
        goal = self.goal()
        goal = self.store.save_checkpoint(SCOPE, goal["id"], 1, next_step="", checkpoint={
            "pause_reason": "主人暂停", "resume_policy": "manual",
        })
        with self.assertRaises(StateError):
            self.store.start_run(SCOPE, goal["id"], goal["version"], "trigger")

    def test_missing_readonly_state_does_not_create_a_directory(self):
        missing = Path(self.temp.name) / "missing" / "nested"
        self.assertFalse(read_snapshot(missing)["database_exists"])
        self.assertFalse(missing.parent.exists())

    def test_readonly_reopen_does_not_change_closed_database_bytes(self):
        self.goal()
        self.store.close()
        before = {path.name: path.read_bytes() for path in self.folder.iterdir()}
        read_snapshot(self.folder, scope=SCOPE)
        # SQLite may create WAL/shm sidecars for a read-only WAL connection;
        # the main ledger and all its logical state must remain unchanged.
        self.assertEqual((self.folder / DATABASE_NAME).read_bytes(), before[DATABASE_NAME])


class LifeMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.path = self.folder / DATABASE_NAME

    def test_v0_read_is_empty_without_migration_and_upgrade_is_backed_up_once(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("PRAGMA user_version=0")
        before = self.path.read_bytes()
        self.assertFalse(read_snapshot(self.folder)["initialized"])
        self.assertEqual(self.path.read_bytes(), before)
        store = LifeStore(self.folder)
        self.addCleanup(store.close)
        backup = store.schema_backup
        # Online backup is logically equivalent; SQLite updates destination
        # header counters, so byte identity is not the backup contract.
        with closing(sqlite3.connect(backup)) as db:
            self.assertEqual(db.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT name FROM sqlite_master").fetchall(), [])
        store.close()
        store = LifeStore(self.folder)
        self.addCleanup(store.close)
        self.assertIsNone(store.schema_backup)
        self.assertEqual(len(list((self.folder / "schema_backups").glob("*.sqlite3"))), 1)

    def test_online_backup_includes_committed_wal_header(self):
        with closing(sqlite3.connect(self.path, isolation_level=None)) as existing:
            existing.execute("PRAGMA journal_mode=WAL")
            existing.execute(f"PRAGMA application_id={APPLICATION_ID}")
            self.assertTrue(Path(str(self.path) + "-wal").exists())
            store = LifeStore(self.folder)
            self.addCleanup(store.close)
            with closing(sqlite3.connect(store.schema_backup)) as backup:
                self.assertEqual(
                    backup.execute("PRAGMA application_id").fetchone()[0], APPLICATION_ID
                )
                self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 0)

    def test_failed_migration_rolls_back_ddl_and_version_and_can_be_retried(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("PRAGMA user_version=0")

        def fail(store):
            store.db.execute("CREATE TABLE partial_write (id TEXT)")
            raise RuntimeError("injected migration failure")

        with patch.object(LifeStore, "_create_schema", fail), self.assertRaises(RuntimeError):
            LifeStore(self.folder)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT name FROM sqlite_master").fetchall(), [])
        store = LifeStore(self.folder)
        self.addCleanup(store.close)
        self.assertTrue(read_snapshot(self.folder)["initialized"])

    def test_failed_backup_prevents_any_schema_change(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("PRAGMA user_version=0")
        with patch.object(LifeStore, "_backup_schema", side_effect=OSError("disk full")), \
                self.assertRaises(OSError):
            LifeStore(self.folder)
        self.assertFalse(read_snapshot(self.folder)["initialized"])

    def test_future_schema_unrelated_database_and_corruption_fail_closed(self):
        for case in ("future", "unrelated", "corrupt"):
            with self.subTest(case=case):
                folder = self.folder / case
                folder.mkdir()
                path = folder / DATABASE_NAME
                if case == "corrupt":
                    path.write_bytes(b"not a sqlite database")
                else:
                    with closing(sqlite3.connect(path)) as db:
                        db.execute("PRAGMA user_version=999" if case == "future" else
                                   "CREATE TABLE other_application (id TEXT)")
                before = path.read_bytes()
                for operation in (lambda: LifeStore(folder), lambda: read_snapshot(folder)):
                    with self.assertRaises((StateError, sqlite3.DatabaseError)):
                        operation()
                self.assertEqual(path.read_bytes(), before)
                self.assertFalse((folder / "schema_backups").exists())

    def test_declared_version_does_not_repair_missing_tables(self):
        store = LifeStore(self.folder)
        store.db.execute("DROP TABLE notes")
        store.close()
        with self.assertRaisesRegex(StateError, "notes"):
            LifeStore(self.folder)
        with self.assertRaises(StateError):
            read_snapshot(self.folder)


class LifePluginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / "state"
        api = types.ModuleType("astrbot.api")
        api.logger = Mock()
        star = types.ModuleType("astrbot.api.star")

        class Star:
            def __init__(self, context):
                self.context = context

        star.Context, star.Star = object, Star
        star.StarTools = types.SimpleNamespace(get_data_dir=Mock(return_value=self.folder))
        star.register = lambda *args: lambda cls: cls
        event_api = types.ModuleType("astrbot.api.event")
        event_api.AstrMessageEvent = object
        decorator = lambda **kwargs: lambda function: function
        event_api.filter = types.SimpleNamespace(
            on_llm_request=decorator, llm_tool=decorator,
            event_message_type=lambda *args, **kwargs: lambda function: function,
            EventMessageType=types.SimpleNamespace(PRIVATE_MESSAGE=1),
        )
        spec = importlib.util.spec_from_file_location(
            "astrbot_plugin_life_continuity._test_main",
            ROOT / "plugins" / PLUGIN_NAME / "main.py",
        )
        self.module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"astrbot": types.ModuleType("astrbot"),
                                     "astrbot.api": api, "astrbot.api.star": star,
                                     "astrbot.api.event": event_api}):
            spec.loader.exec_module(self.module)
        self.data_dir = star.StarTools.get_data_dir

    async def test_disabled_or_invalid_switch_has_no_storage_or_background_side_effects(self):
        for config in ({}, {"enabled": False}, {"enabled": "false"}, {"enabled": 1}):
            plugin = self.module.LifeContinuityPlugin(object(), config)
            await plugin.initialize()
            self.assertIsNone(plugin.store)
            await plugin.terminate()
        self.data_dir.assert_not_called()
        self.assertFalse(self.folder.exists())

    async def test_enabled_lifecycle_only_opens_storage_and_preserves_config(self):
        config = {"enabled": True, "future_option": {"keep": True}}
        plugin = self.module.LifeContinuityPlugin(object(), config)
        await plugin.initialize()
        store = plugin.store
        await plugin.initialize()
        self.assertIs(store, plugin.store)
        self.assertEqual(config, {"enabled": True, "future_option": {"keep": True}})
        self.assertEqual(read_snapshot(self.folder)["counts"]["runs"], 0)
        await plugin.terminate()
        self.assertIsNone(plugin.store)
        with self.assertRaises(sqlite3.ProgrammingError):
            store.db.execute("SELECT 1")

    async def test_broken_optional_store_does_not_raise_into_chat_startup(self):
        self.folder.mkdir()
        (self.folder / DATABASE_NAME).write_bytes(b"broken")
        plugin = self.module.LifeContinuityPlugin(object(), {"enabled": True})
        await plugin.initialize()
        self.assertIsNone(plugin.store)
        self.module.logger.exception.assert_called_once()


class LifeDeploymentTests(unittest.TestCase):
    def test_project_plugin_copy_preserves_existing_configuration_and_data(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / "astrbot/data/config" / f"{PLUGIN_NAME}_config.json"
            config.parent.mkdir(parents=True)
            config.write_text('{"enabled": false, "custom_option": "保留"}', encoding="utf-8")
            state_dir = root / "astrbot/data/plugin_data" / PLUGIN_NAME
            store = LifeStore(state_dir)
            goal = store.create_goal(SCOPE, "old", "现有目标", "原因", "结束条件", "下一步")
            store.close()
            before = (state_dir / DATABASE_NAME).read_bytes()
            config_before = config.read_bytes()
            install.copy_project_plugins(root)
            install.copy_project_plugins(root)
            installed = root / "astrbot/data/plugins" / PLUGIN_NAME
            self.assertTrue((installed / "main.py").is_file())
            schema = json.loads((installed / "_conf_schema.json").read_text(encoding="utf-8"))
            self.assertIs(schema["enabled"]["default"], False)
            self.assertEqual(config.read_bytes(), config_before)
            self.assertEqual((state_dir / DATABASE_NAME).read_bytes(), before)
            # Reopen the actual installed module rather than the source module.
            package = types.ModuleType("installed_life_test")
            package.__path__ = [str(installed)]
            spec = importlib.util.spec_from_file_location(
                "installed_life_test.state", installed / "state.py"
            )
            module = importlib.util.module_from_spec(spec)
            with patch.dict(sys.modules, {package.__name__: package, spec.name: module}):
                spec.loader.exec_module(module)
                installed_store = module.LifeStore(state_dir)
                try:
                    self.assertEqual(installed_store.get_goal(SCOPE, goal["id"])["title"], "现有目标")
                finally:
                    installed_store.close()

    def test_cli_uses_workspace_root_and_never_initializes_missing_state(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder) / "runtime"
            env = dict(os.environ, DAFEIYU_RUNTIME_ROOT=str(runtime), PYTHONUTF8="1")
            script = ROOT / "tools/life_state.py"
            result = subprocess.run([sys.executable, str(script)], cwd=folder, env=env,
                                    text=True, encoding="utf-8", capture_output=True, check=True)
            self.assertFalse(json.loads(result.stdout)["database_exists"])
            self.assertFalse(runtime.exists())
            store = LifeStore(runtime / "astrbot/data/plugin_data" / PLUGIN_NAME)
            store.create_goal(SCOPE, "cli", "查询目标", "原因", "结束条件", "下一步")
            store.close()
            result = subprocess.run([sys.executable, str(script), "--scope", SCOPE],
                                    cwd=folder, env=env, text=True, encoding="utf-8",
                                    capture_output=True, check=True)
            self.assertEqual(json.loads(result.stdout)["goals"][0]["title"], "查询目标")
            bad = subprocess.run([sys.executable, str(script), "--limit", "0"], env=env,
                                 text=True, encoding="utf-8", capture_output=True)
            self.assertNotEqual(bad.returncode, 0)


if __name__ == "__main__":
    unittest.main()

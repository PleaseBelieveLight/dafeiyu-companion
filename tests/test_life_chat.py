"""Scoped chat, genuine owner adjustments and LivingMemory uncertainty regressions."""

import asyncio
import json
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins"))
from astrbot_plugin_life_continuity.chat import LifeChat, is_owner_event, parse_command
from astrbot_plugin_life_continuity.memory_bridge import MemoryBridge
from astrbot_plugin_life_continuity.state import (
    APPLICATION_ID, CHAT_SCHEMA, SCHEMA_VERSION, ConflictError, LifeStore, StateError,
    read_snapshot,
)
from life_test_support import FakeEvent, load_plugin, remove_selection_schema

SCOPE, OTHER = "test:FriendMessage:1001", "test:FriendMessage:1002"


class ChatStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.store = LifeStore(self.folder)
        self.addCleanup(self.store.close)
        self.goal = self.store.create_goal(SCOPE, "goal", "建造机制", "上次有疑问", "有来源解释", "查资料")
        self.chat = LifeChat(self.store)

    def apply(self, text, event_id="m1"):
        return self.chat.apply(FakeEvent(text, event_id=event_id), text, parse_command(text))

    def test_feedback_preserves_owner_message_and_does_not_change_goal_or_preference(self):
        result = self.apply(f"关于目标 {self.goal['id']}：建议先看联机")
        episode = self.store.get_episode(SCOPE, result["episode_id"])
        self.assertEqual(episode["kind"], "owner_feedback")
        self.assertEqual(episode["evidence"]["message_id"], "m1")
        self.assertEqual(episode["evidence"]["actor_id"], "1001")
        self.assertEqual(episode["interpretation"], {})
        self.assertEqual(self.store.get_goal(SCOPE, self.goal["id"])["version"], 1)

    def test_pause_resume_adjust_cancel_and_duplicate_message_are_explicit(self):
        self.apply("暂停这个目标")
        paused = self.store.get_goal(SCOPE, self.goal["id"])
        self.assertEqual(paused["checkpoint"]["resume_policy"], "manual")
        self.apply("继续这个目标", "m2")
        repeated = self.apply("暂停这个目标")
        self.assertTrue(repeated["duplicate"])
        self.assertIn("未重复", self.chat.reply("pause", repeated))
        self.assertEqual(self.store.get_goal(SCOPE, self.goal["id"])["status"], "ready")
        self.apply(f"调整目标 {self.goal['id']}：先看联机", "m3")
        self.assertEqual(self.store.get_goal(SCOPE, self.goal["id"])["next_step"], "先看联机")
        self.apply("取消这个目标", "m4")
        with self.assertRaises(StateError):
            self.apply(f"/life resume {self.goal['id']}", "m5")

    def test_stale_versions_and_message_identity_conflicts_do_not_overwrite_state(self):
        self.apply("暂停这个目标")
        with self.assertRaises(ConflictError):
            self.store.apply_chat_event(SCOPE, "1001", "m2", self.goal["id"], "继续", action="resume",
                                        expected_version=1)
        with self.assertRaises(ConflictError):
            self.apply("继续这个目标")
        self.assertEqual(self.store.get_goal(SCOPE, self.goal["id"])["status"], "paused")

    def test_chat_audit_failure_rolls_back_goal_change(self):
        self.store.db.execute("""CREATE TRIGGER reject_chat_event BEFORE INSERT ON chat_events
            BEGIN SELECT RAISE(ABORT, 'audit write failed'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.apply("暂停这个目标")
        self.assertEqual(self.store.get_goal(SCOPE, self.goal["id"])["status"], "ready")
        self.assertIsNone(self.store.chat_event(SCOPE, "m1"))

    def test_ambiguous_goal_or_running_goal_cannot_be_silently_changed(self):
        self.store.create_goal(SCOPE, "second", "另一个目标", "理由", "条件", "下一步")
        with self.assertRaises(StateError):
            self.apply("暂停这个目标")
        self.store.start_run(SCOPE, self.goal["id"], 1, "run")
        with self.assertRaises(StateError):
            self.apply(f"/life cancel {self.goal['id']}")
        self.assertEqual(self.store.get_goal(SCOPE, self.goal["id"])["status"], "running")

    def test_correction_supersedes_source_and_context_hides_invalid_note_body(self):
        source = self.store.record_episode(SCOPE, self.goal["id"], "read", kind="read",
                                           evidence={"text": "旧的错误结论"})
        self.store.save_note(SCOPE, self.goal["id"], 0, "不应继续引用的笔记", [source["id"]])
        result = self.apply(f"/life correct {source['id']} | 主人纠正后的说法")
        self.assertIsNone(self.store.get_episode(SCOPE, source["id"]))
        self.assertEqual(self.store.get_episode(SCOPE, result["episode_id"])["kind"],
                         "owner_correction")
        block = self.chat.context(SCOPE)
        self.assertNotIn("不应继续引用的笔记", block)
        self.assertNotIn("旧的错误结论", block)
        self.assertIn("sources_current", block)
        self.assertIn("主人纠正后的说法", block)

    def test_scope_isolation_and_literal_query_characters(self):
        other = self.store.create_goal(OTHER, "other", "其他会话", "理由", "条件", "下一步")
        self.store.record_episode(OTHER, other["id"], "read", kind="read", evidence={"text": "联机"})
        with self.assertRaises(StateError):
            self.apply(f"/life pause {other['id']}")
        self.store.record_episode(SCOPE, self.goal["id"], "read", kind="read",
                                  evidence={"text": "100% 完成"})
        self.assertEqual(len(self.store.query_episodes(SCOPE, "%")), 1)
        self.assertEqual(self.store.query_episodes(SCOPE, "_"), [])
        self.assertNotIn("其他会话", self.chat.context(SCOPE))

    def test_commands_need_a_message_id_and_ordinary_chat_is_ignored(self):
        text = "暂停这个目标"
        for identity in (None, "", False):
            with self.assertRaises(StateError):
                self.chat.apply(FakeEvent(text, event_id=identity), text, parse_command(text))
        for text in ("今天天气不错", "他说暂停这个目标", '"暂停这个目标"', "可以"):
            self.assertIsNone(parse_command(text))

    def test_context_is_bounded_and_has_actual_goal_reason_and_next_step(self):
        self.store.save_checkpoint(SCOPE, self.goal["id"], 1, next_step="核对实际演示",
                                   checkpoint={"long": "大" * 15000})
        for index in range(8):
            self.store.record_episode(SCOPE, self.goal["id"], str(index), kind="read",
                                      evidence={"long": "文" * 10000})
        block = self.chat.context(SCOPE)
        self.assertLessEqual(len(block), 6000)
        self.assertIn("上次有疑问", block)
        self.assertIn("核对实际演示", block)


class ChatMigrationTests(unittest.TestCase):
    @staticmethod
    def restore_v1_fixture(store):
        remove_selection_schema(store)
        for table in ("activity_calls", "activity_time", "activity_sessions",
                      "activity_control", "life_deliveries", "chat_events", "memory_exports"):
            store.db.execute(f"DROP TABLE {table}")
        store.db.execute("PRAGMA user_version=1")

    def test_v1_upgrade_backs_up_committed_data_and_readonly_v1_does_not_upgrade(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            store = LifeStore(folder)
            goal = store.create_goal(SCOPE, "old", "原目标", "理由", "条件", "下一步")
            self.restore_v1_fixture(store)
            self.assertEqual(read_snapshot(folder)["schema_version"], 1)
            store.close()
            upgraded = LifeStore(folder)
            try:
                self.assertEqual(read_snapshot(folder)["schema_version"], SCHEMA_VERSION)
                self.assertEqual(upgraded.get_goal(SCOPE, goal["id"])["title"], "原目标")
                with closing(sqlite3.connect(upgraded.schema_backup)) as backup:
                    self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 1)
                    self.assertEqual(backup.execute("SELECT title FROM goals").fetchone()[0], "原目标")
                    self.assertEqual(
                        backup.execute("PRAGMA application_id").fetchone()[0], APPLICATION_ID
                    )
            finally:
                upgraded.close()

    def test_failed_v1_upgrade_preserves_v1_and_existing_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            store = LifeStore(folder)
            store.create_goal(SCOPE, "old", "原目标", "理由", "条件", "下一步")
            self.restore_v1_fixture(store)
            store.close()

            def fail(instance):
                instance.db.execute(CHAT_SCHEMA[0])
                raise RuntimeError("injected failure")

            with patch.object(LifeStore, "_create_schema", fail), self.assertRaises(RuntimeError):
                LifeStore(folder)
            snapshot = read_snapshot(folder)
            self.assertEqual(snapshot["schema_version"], 1)
            self.assertEqual(snapshot["counts"]["goals"], 1)


class ChatPluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.module = load_plugin(Path(self.temp.name))
        self.plugin = self.module.LifeContinuityPlugin(
            object(), {"enabled": True, "owner_scope": SCOPE}
        )
        await self.plugin.initialize()
        self.addAsyncCleanup(self.plugin.terminate)
        self.goal = self.plugin.store.create_goal(SCOPE, "goal", "建造机制", "研究理由", "条件", "下一步")

    def request(self):
        return types.SimpleNamespace(system_prompt="原有人格\n其他插件上下文", contexts=[{"history": "保留"}],
                                     func_tool=types.SimpleNamespace(tools=[
                                         types.SimpleNamespace(name="life_recall"),
                                         types.SimpleNamespace(name="other_tool"),
                                     ]))

    async def test_injection_is_scoped_idempotent_and_preserves_shared_tools_and_history(self):
        request = self.request()
        shared = request.func_tool
        history = list(request.contexts)
        await self.plugin.inject_context(FakeEvent(), request)
        await self.plugin.inject_context(FakeEvent(), request)
        self.assertEqual(request.system_prompt.count("【自主生活当前记录】"), 1)
        self.assertIn("研究理由", request.system_prompt)
        self.assertIn("其他插件上下文", request.system_prompt)
        self.assertEqual(request.contexts, history)
        await self.plugin.inject_context(FakeEvent(scope=OTHER, sender="1002"), request)
        self.assertNotIn("研究理由", request.system_prompt)
        self.assertEqual([tool.name for tool in request.func_tool.tools], ["other_tool"])
        self.assertEqual(len(shared.tools), 2)

    async def test_wrong_sender_group_platform_and_disabled_switch_cannot_read_or_change(self):
        for mode in ("sender", "group", "platform", "disabled"):
            event = FakeEvent("暂停这个目标")
            self.plugin.config["enabled"] = True
            if mode == "sender":
                event.sender = "1002"
            elif mode == "group":
                event.group_id = "test-group"
            elif mode == "platform":
                event.platform_name = "other"
            else:
                self.plugin.config["enabled"] = False
            self.assertFalse(is_owner_event(event, self.plugin.config))
            await self.plugin.owner_message(event)
            event.send.assert_not_called()
            with self.assertRaises(PermissionError):
                await self.plugin.recall(event)

    async def test_direct_owner_command_is_applied_but_forwarded_or_ordinary_text_is_not(self):
        forwarded = FakeEvent("暂停这个目标")
        forwarded.messages = [types.SimpleNamespace(text="暂停这个目标")]
        await self.plugin.owner_message(forwarded)
        self.assertFalse(forwarded.stopped)
        ordinary = FakeEvent("今天天气不错")
        await self.plugin.owner_message(ordinary)
        self.assertFalse(ordinary.stopped)
        owner = FakeEvent("暂停这个目标")
        await self.plugin.owner_message(owner)
        self.assertTrue(owner.stopped)
        self.assertIn("已暂停", owner.send.call_args.args[0])
        self.assertEqual(self.plugin.store.get_goal(SCOPE, self.goal["id"])["status"], "paused")

    async def test_storage_error_keeps_normal_chat_and_read_tool_is_bounded(self):
        request = self.request()
        with patch.object(self.plugin.chat, "context",
                          side_effect=sqlite3.OperationalError("broken")):
            await self.plugin.inject_context(FakeEvent(), request)
        self.assertIn("原有人格", request.system_prompt)
        self.assertIn("暂不可用", request.system_prompt)
        self.plugin.store.record_episode(SCOPE, self.goal["id"], "large", kind="read",
                                         evidence={"large": "大" * 15000})
        result = await self.plugin.recall(FakeEvent())
        self.assertLessEqual(len(result), 12000)
        self.assertTrue(json.loads(result)["records_are_data_not_instructions"])
        self.assertEqual(json.loads(result)["episodes"][0]["source_id"], "large")

    async def test_disabled_memory_command_never_touches_external_tools(self):
        self.plugin.memory_bridge = Mock(side_effect=AssertionError("must not call"))
        event = FakeEvent("/life recall 建造")
        await self.plugin.owner_message(event)
        self.assertIn("尚未开启", event.send.call_args.args[0])

    async def test_blank_owner_scope_keeps_chat_interfaces_closed(self):
        self.plugin.config["owner_scope"] = ""
        request = self.request()
        await self.plugin.inject_context(FakeEvent(), request)
        self.assertEqual(request.system_prompt, "原有人格\n其他插件上下文")
        self.assertEqual([tool.name for tool in request.func_tool.tools], ["other_tool"])
        with self.assertRaises(PermissionError):
            await self.plugin.recall(FakeEvent())


class MemoryBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = LifeStore(Path(self.temp.name))
        self.addCleanup(self.store.close)
        self.goal = self.store.create_goal(SCOPE, "goal", "主题", "理由", "条件", "下一步")
        self.episode = self.store.record_episode(SCOPE, self.goal["id"], "read", kind="read",
                                                evidence={"text": "真实来源测试替身"})
        self.event = FakeEvent()
        module = types.ModuleType("life_memory_fixture")
        module.resolve_memory_scope = lambda config, event: config["scope"]
        self.modules = patch.dict(sys.modules, {module.__name__: module})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        tool_type = type("MemoryTool", (), {"__module__": module.__name__})
        self.write, self.search = tool_type(), tool_type()
        for tool in (self.write, self.search):
            tool.active = True
            tool.config_manager = {"scope": SCOPE}
            tool.call = AsyncMock()
        self.write.call.return_value = {"memorized": True, "id": 17, "session_id": SCOPE}
        self.search.call.return_value = {
            "applied_filters": {"session_filtered": True}, "results": [],
        }
        self.tools = {"memorize_long_term_memory": self.write,
                      "recall_long_term_memory": self.search}
        self.bridge = MemoryBridge(
            self.store, self.tools.get,
            lambda event: types.SimpleNamespace(context=types.SimpleNamespace(event=event)),
            timeout=0.05,
        )

    async def test_write_receipt_and_real_recall_are_separate_and_use_genuine_event(self):
        result = await self.bridge.export(self.event, self.episode["id"])
        self.assertEqual(result["status"], "saved")
        self.assertFalse(result["recall_verified"])
        self.assertIs(self.write.call.call_args.args[0].context.event, self.event)
        self.assertIn(self.episode["id"], self.write.call.call_args.kwargs["memory"])
        await self.bridge.export(self.event, self.episode["id"])
        self.write.call.assert_awaited_once()
        self.search.call.return_value["results"] = [
            {"id": 17, "session_id": SCOPE, "content": "截短摘要"},
        ]
        recalled = await self.bridge.recall(self.event, "来源")
        self.assertEqual(recalled["matches"][0]["episode"]["id"], self.episode["id"])
        self.assertTrue(self.store.memory_export(SCOPE, self.episode["id"])["recall_verified"])

    async def test_missing_or_global_memory_preserves_local_fallback_without_writing(self):
        self.write.config_manager["scope"] = "livingmemory:global"
        result = await self.bridge.export(self.event, self.episode["id"])
        self.assertEqual(result["status"], "pending")
        self.write.call.assert_not_called()
        self.tools.clear()
        recalled = await self.bridge.recall(self.event, "来源")
        self.assertEqual(recalled["local_episodes"][0]["id"], self.episode["id"])

    async def test_timeout_unknown_reply_and_internal_error_are_never_blindly_retried(self):
        self.write.call.side_effect = TimeoutError("response lost")
        result = await self.bridge.export(self.event, self.episode["id"])
        self.assertEqual(result["status"], "uncertain")
        self.write.call.side_effect = None
        await self.bridge.export(self.event, self.episode["id"])
        self.write.call.assert_awaited_once()
        for source, reply in (("invalid", "not json"),
                              ("internal", {"memorized": False, "error": "internal_error"})):
            episode = self.store.record_episode(
                SCOPE, self.goal["id"], source, kind="read", evidence={}
            )
            self.write.call.return_value = reply
            result = await self.bridge.export(self.event, episode["id"])
            self.assertEqual(result["status"], "uncertain")

    async def test_cancelled_write_is_durable_uncertain_and_cancellation_propagates(self):
        self.write.call.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.bridge.export(self.event, self.episode["id"])
        self.assertEqual(self.store.memory_export(SCOPE, self.episode["id"])["status"], "uncertain")

    async def test_close_settles_cancelled_memory_work_before_database_can_close(self):
        started = asyncio.Event()

        async def waiting(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()

        self.write.call.side_effect = waiting
        task = asyncio.create_task(self.bridge.export(self.event, self.episode["id"]))
        await started.wait()
        self.assertTrue(await self.bridge.close(timeout=0.1))
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.store.memory_export(SCOPE, self.episode["id"])["status"], "uncertain")
        self.assertEqual(self.bridge.tasks, set())
        with self.assertRaises(StateError):
            await self.bridge.export(self.event, self.episode["id"])

    async def test_confirmed_not_written_can_retry_only_on_explicit_export(self):
        self.write.call.return_value = {"memorized": False, "error": "memory access is not allowed"}
        result = await self.bridge.export(self.event, self.episode["id"])
        self.assertEqual(result["status"], "failed")
        self.write.call.return_value = {"memorized": True, "id": 18, "session_id": SCOPE}
        result = await self.bridge.export(self.event, self.episode["id"])
        self.assertEqual(result["status"], "saved")
        self.assertEqual(self.write.call.await_count, 2)

    async def test_marker_recall_can_verify_an_unknown_write_without_repeating_it(self):
        self.write.call.side_effect = TimeoutError()
        await self.bridge.export(self.event, self.episode["id"])
        self.search.call.return_value["results"] = [{
            "id": 20, "session_id": SCOPE, "content": f"[DFL:{self.episode['id']}] 来源资料",
        }]
        result = await self.bridge.recall(self.event, "来源")
        self.assertEqual(len(result["matches"]), 1)
        self.assertTrue(self.store.memory_export(SCOPE, self.episode["id"])["recall_verified"])
        self.write.call.assert_awaited_once()

    async def test_stale_foreign_and_untraceable_semantic_results_are_not_returned(self):
        await self.bridge.export(self.event, self.episode["id"])
        self.store.record_episode(SCOPE, self.goal["id"], "correction", kind="owner_correction",
                                  evidence={"text": "修订"}, supersedes_id=self.episode["id"])
        self.search.call.return_value["results"] = [
            {"id": 17, "session_id": SCOPE, "content": "过期结论"},
            {"id": 19, "session_id": OTHER, "content": "其他会话秘密"},
            {"id": 21, "session_id": SCOPE, "content": "无来源结论"},
        ]
        result = await self.bridge.recall(self.event, "修订")
        self.assertEqual(result["matches"], [])
        self.assertNotIn("其他会话秘密", json.dumps(result, ensure_ascii=False))
        self.assertNotIn("过期结论", json.dumps(result, ensure_ascii=False))

    async def test_permission_wrapper_is_preserved_and_scope_failures_fall_back(self):
        wrapped = types.SimpleNamespace(active=True, _wrapped=self.write,
                                        call=AsyncMock(side_effect=self.write.call))
        self.tools["memorize_long_term_memory"] = wrapped
        await self.bridge.export(self.event, self.episode["id"])
        wrapped.call.assert_awaited_once()
        self.tools["recall_long_term_memory"] = Mock(active=True, config_manager=None)
        self.assertEqual((await self.bridge.recall(self.event, "来源"))["status"], "unavailable")

    async def test_concurrent_exports_share_inflight_state_and_write_only_once(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def pending(*args, **kwargs):
            started.set()
            await release.wait()
            return {"memorized": True, "id": 33, "session_id": SCOPE}

        self.bridge.timeout = 2
        self.write.call.side_effect = pending
        first = asyncio.create_task(self.bridge.export(self.event, self.episode["id"]))
        await started.wait()
        second = await self.bridge.export(self.event, self.episode["id"])
        self.assertEqual(second["status"], "sending")
        release.set()
        self.assertEqual((await first)["status"], "saved")
        self.write.call.assert_awaited_once()

    async def test_correction_during_write_does_not_claim_current_source_saved(self):
        async def corrected(*args, **kwargs):
            self.store.record_episode(SCOPE, self.goal["id"], "correction", kind="owner_correction",
                                      evidence={"text": "新资料"}, supersedes_id=self.episode["id"])
            return {"memorized": True, "id": 34, "session_id": SCOPE}

        self.write.call.side_effect = corrected
        result = await self.bridge.export(self.event, self.episode["id"])
        self.assertFalse(result["source_current"])
        self.assertIn("旧版", LifeChat(self.store).reply("remember", result))

    async def test_fallback_refreshes_sources_corrected_during_semantic_recall(self):
        async def corrected(*args, **kwargs):
            self.store.record_episode(SCOPE, self.goal["id"], "correction", kind="owner_correction",
                                      evidence={"text": "最新修订"}, supersedes_id=self.episode["id"])
            return {"applied_filters": {"session_filtered": True}, "results": []}

        self.search.call.side_effect = corrected
        result = await self.bridge.recall(self.event, "")
        self.assertNotIn(self.episode["id"], [row["id"] for row in result["local_episodes"]])
        self.assertEqual(result["local_episodes"][0]["evidence"]["text"], "最新修订")

    async def test_reopened_unsettled_write_is_only_marked_unknown_not_replayed(self):
        self.store.queue_memory(SCOPE, self.episode["id"])
        self.store.update_memory(SCOPE, self.episode["id"], "sending", expected_status="pending")
        self.store.close()
        reopened = LifeStore(Path(self.temp.name))
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.memory_export(SCOPE, self.episode["id"])["status"], "sending")
        bridge = MemoryBridge(reopened, self.tools.get, lambda event: event)
        result = await bridge.export(self.event, self.episode["id"])
        self.assertEqual(result["status"], "uncertain")
        self.write.call.assert_not_called()

    async def test_unscoped_semantic_reply_is_not_used_as_verified_recall(self):
        await self.bridge.export(self.event, self.episode["id"])
        self.search.call.return_value = {
            "applied_filters": {"session_filtered": False},
            "results": [{"id": 17, "session_id": SCOPE, "content": "unscoped"}],
        }
        result = await self.bridge.recall(self.event, "来源")
        self.assertEqual(result["matches"], [])
        self.assertFalse(self.store.memory_export(SCOPE, self.episode["id"])["recall_verified"])


if __name__ == "__main__":
    unittest.main()

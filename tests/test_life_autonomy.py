"""Real execution facts, bounded retrieval and restart-safe daily research."""

import asyncio
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins"))
from astrbot_plugin_life_continuity.activity import ActivityEngine
from astrbot_plugin_life_continuity.activity_state import SHANGHAI
from astrbot_plugin_life_continuity.chat import EXECUTION_RULES
from astrbot_plugin_life_continuity.scheduler import DailyResearch
from astrbot_plugin_life_continuity.search import SearchError, normalize_results, search_web
from astrbot_plugin_life_continuity.state import LifeStore
from life_test_support import FakeEvent, load_plugin

SCOPE = "test:FriendMessage:1001"
RESULT = {"url": "https://example.org/guide", "title": "公开资料", "text": "新资料中记录的有限事实"}


class RetrievalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = LifeStore(self.temp.name)
        self.addCleanup(self.store.close)
        self.goal = self.store.create_goal(SCOPE, "goal", "已有问题", "理由", "有来源解释", "查资料")
        self.now = datetime(2026, 11, 5, 12, tzinfo=SHANGHAI).timestamp()
        self.config = {"enabled": True, "owner_scope": SCOPE, "activity_enabled": True,
                       "web_search_enabled": True, "auto_enabled": True,
                       "share_enabled": True, "chat_cooldown_seconds": 0,
                       "stop_timeout_seconds": 1}
        self.searcher = AsyncMock(return_value=[RESULT])
        self.share = AsyncMock()
        self.prompts = []

        async def agent(context, event, prompt, before, after, config, stop, on_runner):
            self.prompts.append(json.loads(prompt))
            call = before()
            after(call, {"output": 5})
            return {"source_ids": [self.prompts[-1]["sources"][0]["id"]], "note": "有限发现",
                    "questions": "仍待核实", "next_step": "查找下一项事实"}

        self.engine = ActivityEngine(self.store, object(), self.config, lambda: None,
            searcher=self.searcher, agent=agent, share=self.share, clock=lambda: self.now)
        self.addAsyncCleanup(self.engine.close)

    async def run_round(self, key="run"):
        self.engine.start(FakeEvent(), self.goal["id"], key)
        return await self.engine.task

    async def test_empty_goal_retrieves_provenance_then_writes_one_metered_note(self):
        result = await self.run_round()
        self.assertEqual(result["status"], "completed")
        source = self.prompts[0]["sources"][0]
        self.assertEqual(source["url"], RESULT["url"])
        self.assertEqual(source["source_type"], "web_search_snippet")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM activity_calls").fetchone()[0], 1)
        note = self.store.db.execute("SELECT body FROM notes").fetchone()[0]
        self.assertIn(RESULT["url"], note)
        goal = self.store.get_goal(SCOPE, self.goal["id"])
        self.assertEqual(goal["checkpoint"]["last_search"]["new_sources"], 1)
        self.assertTrue(goal["checkpoint"]["progress"])
        self.share.assert_awaited_once()

    async def test_repeated_snippet_from_another_url_creates_no_progress_note_or_share(self):
        await self.run_round()
        self.searcher.return_value = [dict(RESULT, url="https://example.net/repost")]
        result = await self.run_round("second")
        self.assertEqual(result["reason"], "no_new_sources")
        self.assertEqual(len(self.prompts), 1)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM notes").fetchone()[0], 1)
        self.assertEqual(self.store.get_goal(SCOPE, self.goal["id"])["checkpoint"][
            "last_search"]["status"], "no_new_sources")
        self.share.assert_awaited_once()

    async def test_search_failure_is_audited_without_model_calls_or_secret_error_text(self):
        self.searcher.side_effect = RuntimeError("private provider response")
        result = await self.run_round()
        self.assertEqual(result["reason"], "search_failed")
        checkpoint = self.store.get_goal(SCOPE, self.goal["id"])["checkpoint"]
        self.assertEqual(checkpoint["last_search"]["status"], "search_failed")
        self.assertNotIn("private", json.dumps(checkpoint))
        self.assertEqual(self.prompts, [])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM notes").fetchone()[0], 0)
        self.share.assert_not_called()

    async def test_foreground_stop_cancels_retrieval_and_prevents_note_and_share(self):
        entered = asyncio.Event()

        async def waiting(*args):
            entered.set()
            await asyncio.Event().wait()

        self.searcher.side_effect = waiting
        self.engine.start(FakeEvent(), self.goal["id"], "run")
        await entered.wait()
        self.assertTrue(await self.engine.stop("foreground"))
        goal = self.store.get_goal(SCOPE, self.goal["id"])
        self.assertEqual(goal["checkpoint"]["last_search"]["status"], "cancelled")
        self.assertEqual(goal["checkpoint"]["pause_reason"], "foreground")
        self.assertEqual(self.prompts, [])
        self.share.assert_not_called()

    async def test_same_text_can_be_saved_for_two_goals_without_cross_goal_reuse(self):
        await self.run_round()
        self.goal = self.store.create_goal(SCOPE, "other", "另一问题", "理由", "条件", "查资料")
        await self.run_round("other-run")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM notes").fetchone()[0], 2)

    def scheduler(self):
        context = types.SimpleNamespace(get_registered_star=lambda name: types.SimpleNamespace(
            star_cls=types.SimpleNamespace(sender_service=types.SimpleNamespace(
                build_proactive_event=lambda scope: FakeEvent(scope=scope)))))
        plugin = types.SimpleNamespace(config=self.config, engine=self.engine, context=context,
                                       activity_policy=Mock())
        return DailyResearch(plugin)

    async def test_scheduler_runs_once_per_shanghai_day_even_after_failure_and_recreation(self):
        self.searcher.side_effect = SearchError("search_timeout")
        scheduler = self.scheduler()
        self.assertEqual(scheduler.tick()["status"], "started")
        await self.engine.task
        self.assertIsNone(self.scheduler().tick())
        # A fresh connection/engine proves the daily guard is durable, not an in-memory flag.
        reopened = LifeStore(self.temp.name)
        try:
            fresh_engine = ActivityEngine(reopened, object(), self.config, lambda: None,
                clock=lambda: self.now)
            scheduler = self.scheduler()
            scheduler.plugin.engine = fresh_engine
            self.assertIsNone(scheduler.tick())
        finally:
            reopened.close()
        self.now += 86400
        self.assertEqual(self.scheduler().tick()["status"], "started")
        await self.engine.task
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM runs").fetchone()[0], 2)

    async def test_scheduler_honors_manual_run_stop_pause_quiet_hours_and_cooldown(self):
        scheduler = self.scheduler()
        self.config["auto_hour"] = 13
        self.assertIsNone(scheduler.tick())
        self.config["auto_hour"] = 10
        self.engine.ledger.control_update(SCOPE, paused=True)
        self.assertIsNone(scheduler.tick())
        self.engine.ledger.control_update(SCOPE, paused=False)
        self.engine.ledger.control_update(SCOPE, chat=True)
        self.config["chat_cooldown_seconds"] = 300
        self.assertIsNone(scheduler.tick())
        self.now += 301
        scheduler.plugin.activity_policy.side_effect = SearchError("quiet_hours")
        self.assertIsNone(scheduler.tick())
        scheduler.plugin.activity_policy.side_effect = None
        await self.run_round("manual")
        self.assertIsNone(scheduler.tick())
        self.now += 86400
        goal = self.store.get_goal(SCOPE, self.goal["id"])
        self.store.apply_chat_event(SCOPE, "1001", "pause", self.goal["id"], "暂停这个目标",
                                    action="pause", expected_version=goal["version"])
        self.assertIsNone(scheduler.tick())
        self.assertEqual(self.searcher.await_count, 1)

    async def test_scheduler_stops_its_polling_task_on_plugin_unload(self):
        scheduler = self.scheduler()
        self.config["auto_enabled"] = False
        scheduler.start()
        self.assertIsNone(scheduler.task)
        self.config["auto_enabled"] = True
        scheduler.start()
        task = scheduler.task
        await scheduler.close()
        self.assertTrue(task.done())
        self.assertIsNone(scheduler.task)


class SearchAdapterTests(unittest.IsolatedAsyncioTestCase):
    def test_snippets_are_bounded_deduplicated_and_require_public_urls(self):
        rows = [dict(RESULT, snippet=RESULT["text"]),
                {"url": "http://127.0.0.1/private", "snippet": "私有"},
                {"url": "file:///secret", "snippet": "本地"},
                {"url": "https://example.org/empty"},
                dict(RESULT, url="https://example.net/repost", snippet=RESULT["text"])]
        result = normalize_results(json.dumps({"results": rows}))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["text"], RESULT["text"])
        for value in ("Error: key missing", "[]", "{}"):
            with self.assertRaises(SearchError):
                normalize_results(value)

    async def test_only_selected_readonly_builtin_is_called_with_three_results(self):
        tool = types.SimpleNamespace(call=AsyncMock(return_value=json.dumps({"results": [
            {"url": RESULT["url"], "title": RESULT["title"], "snippet": RESULT["text"]}]})))
        builtins = types.ModuleType("astrbot.core.tools.web_search_tools")
        builtins.AnySearchWebSearchTool = Mock(return_value=tool)
        modules = {
            "astrbot.core.astr_agent_context": types.SimpleNamespace(
                AgentContextWrapper=lambda **kw: kw, AstrAgentContext=lambda **kw: kw),
            "astrbot.core.tools": types.SimpleNamespace(web_search_tools=builtins),
        }
        settings = {"web_search": True, "websearch_provider": "anysearch"}
        context = types.SimpleNamespace(get_config=lambda **kw: {"provider_settings": settings})
        with patch.dict(sys.modules, modules):
            result = await search_web(context, FakeEvent(), "有限问题", 1)
            self.assertEqual(result[0]["url"], RESULT["url"])
            self.assertEqual(tool.call.call_args.kwargs, {"query": "有限问题", "max_results": 3})
            settings["web_search"] = False
            with self.assertRaises(SearchError):
                await search_web(context, FakeEvent(), "问题", 1)
            tool.call.assert_awaited_once()


class TruthfulChatTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_execution_rules_override_old_promises_and_are_owner_scoped(self):
        with tempfile.TemporaryDirectory() as folder:
            module = load_plugin(Path(folder))
            plugin = module.LifeContinuityPlugin(object(), {"enabled": True, "owner_scope": SCOPE})
            await plugin.initialize()
            try:
                goal = plugin.store.create_goal(SCOPE, "goal", "问题", "理由", "条件", "明天去查")
                plugin.store.start_run(SCOPE, goal["id"], goal["version"], "old-run")
                request = types.SimpleNamespace(system_prompt="原有人格", func_tool=None,
                    contexts=[{"role": "assistant", "content": "我明天一定查到就告诉你"}])
                await plugin.inject_context(FakeEvent(), request)
                await plugin.inject_context(FakeEvent(), request)
                self.assertEqual(request.system_prompt.count("【研究执行事实约束】"), 1)
                self.assertIn(EXECUTION_RULES, request.system_prompt)
                content = json.dumps(request.extra_user_content_parts, ensure_ascii=False)
                self.assertIn('"worker": "idle"'.replace('"', '\\"'), content)
                self.assertIn("last_run", content)
                self.assertIn("+08:00", content)
                self.assertIn("明天一定", request.contexts[0]["content"])
                await plugin.inject_context(FakeEvent(scope="test:FriendMessage:1002", sender="1002"),
                                            request)
                self.assertEqual(request.system_prompt, "原有人格")
                self.assertNotIn("last_run", json.dumps(request.extra_user_content_parts))
            finally:
                await plugin.terminate()

"""Provider payload regressions use temporary stores and never call a real model."""

import asyncio
import copy
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
sys.path.insert(0, str(ROOT / "overlays/astrbot/astrbot/core/utils"))
sys.path.insert(0, str(ROOT / "tools"))
import chat_efficiency as policy
from context_parts import set_temporary_context
from patch_chat_efficiency import apply_chat_efficiency, configure_policy
from model_usage import attach_browser_metering
from chat_usage import report


class ToolSet:
    def __init__(self, names):
        self.tools = [types.SimpleNamespace(name=n) for n in names]

    def get_func_desc_openai_style(self):
        return [{"function": {"name": t.name}} for t in self.tools]


class ContextPartTests(unittest.TestCase):
    def test_replacement_is_local_temporary_and_does_not_remove_real_user_data(self):
        user = {"type": "text", "text": "[state]\nuser quoted this"}
        other = {"type": "text", "text": "[other]", "_no_save": True}
        req = types.SimpleNamespace(system_prompt="unchanged", extra_user_content_parts=[user, other])
        set_temporary_context(req, "[state]", "old")
        set_temporary_context(req, "[state]", "new")
        self.assertEqual(req.system_prompt, "unchanged")
        self.assertEqual(req.extra_user_content_parts[:2], [user, other])
        self.assertEqual(len(req.extra_user_content_parts), 3)
        self.assertTrue(req.extra_user_content_parts[-1]["_no_save"])
        set_temporary_context(req, "[state]", "")
        self.assertEqual(req.extra_user_content_parts, [user, other])


class WorkingContextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "context.db"
        self.provider = types.SimpleNamespace(provider_config={"companion_context": {
            "enabled": True, "input_budget": 4000, "recent_rounds": 4,
            "summary_input_chars": 24000,
        }}, text_chat=AsyncMock(return_value=types.SimpleNamespace(
            completion_text=json.dumps({"summary": "用户已经吃饭，周五有约定。"}, ensure_ascii=False)
        )))
        self.logger = patch.dict(sys.modules, {"astrbot": types.SimpleNamespace(logger=Mock())})
        self.logger.start()
        self.addCleanup(self.logger.stop)

    def payload(self, rounds=20):
        messages = [{"role": "system", "content": "stable persona"}]
        for i in range(rounds):
            messages += [{"role": "user", "content": f"user-{i}:" + "信息" * 150},
                         {"role": "assistant", "content": f"reply-{i}:" + "回复" * 80}]
        return {"model": "test", "messages": messages}

    async def prepare(self, payload, scope="conversation-a"):
        return await policy.prepare_chat_payload(
            self.provider, payload, None, conversation_id=scope, path=self.path
        )

    async def test_wire_only_summary_keeps_originals_recent_turns_and_cache(self):
        payload = self.payload()
        original = copy.deepcopy(payload)
        result, _ = await self.prepare(payload)
        self.assertEqual(payload, original)
        self.assertLess(len(result["messages"]), len(payload["messages"]))
        self.assertEqual(result["messages"][-8:], payload["messages"][-8:])
        self.assertIn("已经吃饭", result["messages"][1]["content"])
        self.assertNotIn("func_tool", self.provider.text_chat.call_args.kwargs)
        await self.prepare(payload)
        self.provider.text_chat.assert_awaited_once()
        self.assertLess(policy.estimate_input(result), policy.estimate_input(payload))

    async def test_summary_output_cap_is_present_in_actual_wire_payload(self):
        token = policy.SOURCE.set("context_summary")
        cap = policy.SUMMARY_MAX_TOKENS.set(600)
        try:
            result, _ = await self.prepare(self.payload())
        finally:
            policy.SOURCE.reset(token)
            policy.SUMMARY_MAX_TOKENS.reset(cap)
        self.assertEqual(result["max_tokens"], 600)
        self.assertEqual(result["response_format"], {"type": "json_object"})
        self.provider.text_chat.assert_not_awaited()

    async def test_edit_rollback_and_other_conversations_cannot_reuse_stale_summary(self):
        payload = self.payload()
        await self.prepare(payload)
        payload["messages"][1]["content"] = "纠正：周五约定取消。"
        await self.prepare(payload)
        self.assertEqual(self.provider.text_chat.await_count, 2)
        self.assertEqual(self.provider.text_chat.call_args.kwargs["contexts"], [])
        await self.prepare(payload, "conversation-b")
        self.assertEqual(self.provider.text_chat.await_count, 3)

    async def test_incremental_catchup_never_discards_unsummarized_history(self):
        self.provider.provider_config["companion_context"]["summary_input_chars"] = 2200
        payload = self.payload()
        first, _ = await self.prepare(payload)
        self.assertIs(first, payload)
        for _ in range(20):
            result, _ = await self.prepare(payload)
            if result is not payload:
                break
        self.assertIsNot(result, payload)
        self.assertGreater(self.provider.text_chat.await_count, 1)
        second_call = self.provider.text_chat.await_args_list[1].kwargs["prompt"]
        self.assertIn("已经吃饭", second_call)

    async def test_invalid_or_failed_summary_preserves_history_and_cancellation_propagates(self):
        payload = self.payload()
        self.provider.text_chat.return_value.completion_text = "invalid json"
        result, _ = await self.prepare(payload)
        self.assertIs(result, payload)
        with closing(policy.connect(self.path)) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM summaries").fetchone()[0], 0)
        await self.prepare(payload)
        self.provider.text_chat.assert_awaited_once()
        self.provider.text_chat.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.prepare(payload, "other-conversation")

    async def test_tool_call_and_result_pairs_are_retained_together(self):
        payload = self.payload()
        payload["messages"][-1:] = [
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "call1", "function": {"name": "lookup"}}]},
            {"role": "tool", "tool_call_id": "call1", "content": "result"},
        ]
        result, _ = await self.prepare(payload)
        self.assertEqual(result["messages"][-2:], payload["messages"][-2:])

    async def test_image_followup_retains_original_without_base64_in_summary_prompt(self):
        payload = self.payload()
        image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,PRIVATE_IMAGE"}}
        payload["messages"][1]["content"] = [{"type": "text", "text": "old photo"}, image]
        payload["messages"][-2]["content"] = "上次那张照片里是什么？"
        result, _ = await self.prepare(payload)
        self.assertIn("PRIVATE_IMAGE", json.dumps(result))
        self.assertNotIn("PRIVATE_IMAGE", self.provider.text_chat.call_args.kwargs["prompt"])

    def test_greeting_tools_are_small_but_confirmations_and_tasks_keep_capabilities(self):
        tools = ToolSet(["recall_long_term_memory", "get_screenshot", "generate_selfie"])
        selected = policy.route_tools([{"role": "user", "content": "你好！"}], tools)
        self.assertEqual([t.name for t in selected.tools], ["recall_long_term_memory"])
        self.assertEqual(len(tools.tools), 3)
        for text in ("好", "嗯", "看看那张图", "请帮我设置提醒", "你好，发张自拍"):
            self.assertIs(policy.route_tools([{"role": "user", "content": text}], tools), tools)


class AccountingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "context.db"
        self.patch = patch.object(policy, "store_path", return_value=self.path)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.provider = types.SimpleNamespace(provider_config={"id": "test"})

    def rows(self):
        with closing(policy.connect(self.path)) as con:
            con.row_factory = sqlite3.Row
            return [dict(row) for row in con.execute("SELECT * FROM calls ORDER BY started")]

    async def test_exact_cache_usage_and_unknown_failure_are_separate_without_raw_text(self):
        usage = types.SimpleNamespace(prompt_tokens=100, prompt_cache_hit_tokens=60, completion_tokens=7)
        call = AsyncMock(return_value=types.SimpleNamespace(usage=usage))
        payload = {"model": "test", "stream": False, "messages": [{"role": "user", "content": "PRIVATE CHAT"}]}
        await policy.metered_chat_completion(self.provider, call, **payload)
        call.side_effect = TimeoutError("PRIVATE SECRET")
        with self.assertRaises(TimeoutError):
            await policy.metered_chat_completion(self.provider, call, **payload)
        rows = self.rows()
        self.assertEqual((rows[0]["input_other"], rows[0]["input_cached"], rows[0]["output"]), (40, 60, 7))
        self.assertEqual(rows[1]["status"], "unknown_error")
        self.assertIsNone(rows[1]["input_total"])
        self.assertNotIn("PRIVATE", self.path.read_bytes().decode("utf-8", errors="ignore"))

    async def test_stream_usage_is_counted_once_and_underlying_stream_is_closed(self):
        class Stream:
            def __init__(self):
                self.close = AsyncMock()

            async def __aiter__(self):
                yield types.SimpleNamespace(usage=None, choices=[])
                yield types.SimpleNamespace(usage=types.SimpleNamespace(
                    prompt_tokens=20, completion_tokens=3, prompt_tokens_details=None
                ), choices=[])

        stream = Stream()
        result = await policy.metered_chat_completion(
            self.provider, AsyncMock(return_value=stream), model="test", stream=True
        )
        async for _ in result:
            pass
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["input_total"], 20)
        self.assertIsNone(rows[0]["input_cached"])
        stream.close.assert_awaited_once()

    async def test_browser_and_native_calls_share_one_ledger_without_sdk_retry_inflation(self):
        usage = types.SimpleNamespace(prompt_tokens=30, prompt_cache_hit_tokens=10, completion_tokens=5)
        original = AsyncMock(return_value=types.SimpleNamespace(usage=usage))
        client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(
            create=original)), max_retries=2)
        attach_browser_metering(client, self.path)
        await client.chat.completions.create(model="test", messages=[], stream=False)
        self.assertEqual(client.max_retries, 0)
        self.assertEqual(self.rows()[0]["source"], "browser")
        aggregate = report(self.path)
        self.assertEqual(aggregate["groups"][0]["known_input"], 30)

    async def test_early_stream_close_is_unknown_and_does_not_masquerade_as_zero_usage(self):
        class Stream:
            close = AsyncMock()
            async def __aiter__(self):
                yield types.SimpleNamespace(usage=None, choices=[])
                yield types.SimpleNamespace(usage=None, choices=[])
        stream = Stream()
        result = await policy.metered_chat_completion(
            self.provider, AsyncMock(return_value=stream), model="test", stream=True
        )
        await anext(result)
        await result.aclose()
        self.assertEqual(self.rows()[0]["status"], "unknown_stream")
        self.assertIsNone(self.rows()[0]["input_total"])
        stream.close.assert_awaited_once()


class PatchTests(unittest.TestCase):
    def test_deployed_policy_and_user_overrides_survive_repeated_updates(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "astrbot/data/cmd_config.json"
            path.parent.mkdir(parents=True)
            original = {"provider": [{"id": "deepseek_chat", "key": "private-value"},
                                     {"id": "vision", "model": "keep-model"}],
                        "other_setting": "keep-value"}
            path.write_text(json.dumps(original), encoding="utf-8")
            configure_policy(root)
            value = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(value["provider"][0]["companion_context"]["input_budget"], 16000)
            value["provider"][0]["companion_context"]["input_budget"] = 24000
            path.write_text(json.dumps(value), encoding="utf-8")
            configure_policy(root)
            self.assertEqual(value, json.loads(path.read_text(encoding="utf-8")))
            self.assertEqual(value["provider"][1], original["provider"][1])
            self.assertEqual(value["provider"][0]["key"], "private-value")

    def test_patch_is_checked_idempotent_and_covers_both_request_modes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "astrbot/astrbot/core/provider/sources/openai_source.py"
            path.parent.mkdir(parents=True)
            text = "from .request_retry import retry_provider_request\nclass Provider:\n"
            for method in ("_query", "_query_stream"):
                text += (f"    async def {method}(\n        self, payloads, tools, conversation_id=None\n    ):\n"
                         "        if tools:\n            pass\n"
                         "        return await retry_provider_request(\n            'OpenAI',\n"
                         "            lambda: self.client.chat.completions.create(\n"
                         "                **payloads),\n        )\n")
            path.write_text(text, encoding="utf-8")
            apply_chat_efficiency(root)
            first = path.read_text(encoding="utf-8")
            compile(first, str(path), "exec")
            apply_chat_efficiency(root)
            self.assertEqual(first, path.read_text(encoding="utf-8"))
            path.write_text(first.replace("await prepare_chat_payload(", "await broken(", 1), encoding="utf-8")
            with self.assertRaises(RuntimeError):
                apply_chat_efficiency(root)


if __name__ == "__main__":
    unittest.main()

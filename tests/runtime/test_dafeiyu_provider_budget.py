"""Exercise the patched real provider up to a mocked SDK request boundary."""

import asyncio
import copy
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from openai.types.chat.chat_completion import ChatCompletion
from openai.types.chat.chat_completion_chunk import ChatCompletionChunk
from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.provider.entities import LLMResponse, ProviderRequest
from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
from astrbot.core.tools.cron_tools import FutureTaskTool
from astrbot.core.utils import chat_efficiency, conversation_turn
from astrbot.core.utils.conversation_turn import route_capabilities


class ProviderBudgetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "usage.db"
        patcher = patch.object(chat_efficiency, "store_path", return_value=self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        completion = ChatCompletion(id="test", object="chat.completion", created=0,
            model="test", choices=[{"index": 0, "message": {"role": "assistant", "content": "ok"},
                                    "finish_reason": "stop"}],
            usage={"prompt_tokens": 20, "completion_tokens": 2, "total_tokens": 22})
        self.provider = ProviderOpenAIOfficial.__new__(ProviderOpenAIOfficial)
        self.provider.reasoning_key = "reasoning_content"
        self.provider.provider_config = {"id": "test", "companion_context": {
            "enabled": True, "route_simple_chat_tools": True}}
        self.create = AsyncMock(return_value=completion)
        self.provider.client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=self.create)))
        self.provider.default_params = {k: None for k in (
            "model", "messages", "max_tokens", "response_format", "tools", "tool_choice")}
        self.provider._apply_provider_specific_request_overrides = lambda *args: None
        self.provider._sanitize_assistant_messages = lambda *args: None
        self.provider._parse_openai_completion = AsyncMock(return_value=LLMResponse("assistant", completion_text="ok"))

    async def test_summary_cap_reaches_the_actual_sdk_and_usage_is_recorded(self):
        token = chat_efficiency.SOURCE.set("context_summary")
        try:
            await self.provider._query({"model": "test", "messages": []}, None, request_max_retries=1)
        finally:
            chat_efficiency.SOURCE.reset(token)
        self.assertEqual(self.create.await_args.kwargs["max_tokens"], 900)
        self.assertEqual(self.create.await_args.kwargs["response_format"], {"type": "json_object"})
        with closing(sqlite3.connect(self.path)) as con:
            self.assertEqual(con.execute("SELECT source,input_total,output FROM calls").fetchone(),
                             ("context_summary", 20, 2))

    async def test_greeting_omits_execution_schema_without_mutating_original_toolset(self):
        tools = ToolSet([FunctionTool(name=name, description=name, parameters={})
                         for name in ("memorize_long_term_memory", "astrbot_execute_shell")])
        await self.provider._query({"model": "test", "messages": [{"role": "user", "content": "你好"}]},
                                   tools, request_max_retries=1)
        wire = self.create.await_args.kwargs["tools"]
        self.assertEqual([t["function"]["name"] for t in wire], ["memorize_long_term_memory"])
        self.assertEqual(len(tools.tools), 2)

    async def test_reminder_tool_reaches_the_sdk_through_the_agent(self):
        prompt = "请创建一个名为‘体验日程提醒’的一次性任务，十分钟后提醒我休息一下"
        event = SimpleNamespace(get_message_str=lambda: prompt, get_extra=lambda _: None)
        request = ProviderRequest(prompt=prompt, func_tool=ToolSet([FutureTaskTool()]))
        route_capabilities(event, request)

        runner = ToolLoopAgentRunner()
        runner.req = request
        runner.provider = self.provider
        self.provider.provider_config["modalities"] = ["text", "image"]
        self.assertIsNone(runner._func_tool_for_provider())

        self.provider.provider_config["modalities"].append("tool_use")
        await self.provider._query(
            {"model": "deepseek-flash", "messages": [{"role": "user", "content": prompt}]},
            runner._func_tool_for_provider(),
            request_max_retries=1,
        )
        wire = self.create.await_args.kwargs
        self.assertEqual([tool["function"]["name"] for tool in wire["tools"]], ["future_task"])
        self.assertEqual(wire["tool_choice"], "auto")

    async def test_failed_summary_bounds_normal_and_streamed_sdk_requests(self):
        self.provider.provider_config["companion_context"].update(input_budget=4000, recent_rounds=4)
        self.provider.text_chat = AsyncMock(side_effect=TimeoutError())
        tools = ToolSet([FunctionTool(name="lookup", description="工具说明" * 60, parameters={})])
        messages = [{"role": "system", "content": "persona"}]
        image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,OLD_PRIVATE_IMAGE"}}
        for index in range(210):
            content = "历史消息" * 80
            if index < 56:
                content = [{"type": "text", "text": content}, image]
            messages.extend([{"role": "user", "content": content},
                             {"role": "assistant", "content": "历史回复" * 40}])
        messages.append({"role": "user", "content": "我是谁？"})
        payload = {"model": "test", "messages": messages}
        original = copy.deepcopy(payload)

        async def stream():
            yield ChatCompletionChunk(id="test", object="chat.completion.chunk", created=0, model="test",
                                      choices=[{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}])

        for streaming in (False, True):
            with self.subTest(streaming=streaming):
                if streaming:
                    self.create.return_value = stream()
                    async for _ in self.provider._query_stream(payload, tools, conversation_id="budget-stream",
                                                               request_max_retries=1):
                        pass
                else:
                    await self.provider._query(payload, tools, conversation_id="budget-normal", request_max_retries=1)
                wire = self.create.await_args.kwargs
                self.assertLessEqual(chat_efficiency.estimate_input(wire), 4000)
                self.assertEqual(chat_efficiency.image_count(wire["messages"]), 0)
                self.assertLessEqual(len(wire["messages"]), 10)
                self.assertEqual(wire["messages"][-1], messages[-1])
                self.assertEqual(wire["tools"][0]["function"]["name"], "lookup")
                self.assertEqual(payload, original)
        self.assertEqual(self.create.await_count, 2)

    async def test_background_context_events_keep_originating_turn_id(self):
        self.provider.provider_config["companion_context"].update(
            input_budget=4000, recent_rounds=4, summary_background=True)
        self.provider.text_chat = AsyncMock(return_value=LLMResponse("assistant", completion_text='{"summary":"摘要"}'))
        event = Mock()
        token = conversation_turn._TURN.set({"trace_turn_id": "originating-turn", "kind": "text"})
        payload = {"messages": [{"role": "user", "content": "问题" * 100},
                                {"role": "assistant", "content": "回复" * 100}] * 10}
        with patch.object(chat_efficiency, "trace_module", return_value=SimpleNamespace(event=event)):
            try:
                result, _ = await chat_efficiency.prepare_chat_payload(
                    self.provider, payload, None, conversation_id="background-events", path=self.path)
                self.assertLessEqual(chat_efficiency.estimate_input(result), 4000)
                task = chat_efficiency._REFRESHES[chat_efficiency._hash("background-events")]
            finally:
                conversation_turn._TURN.reset(token)
            await asyncio.wait_for(task, timeout=2)
        names = [call.args[2] for call in event.call_args_list]
        self.assertIn("context.summary_ready", names)
        self.assertIn("context.prepared", names)
        self.assertTrue(all(call.kwargs["turn_id"] == "originating-turn" for call in event.call_args_list))


if __name__ == "__main__":
    unittest.main()

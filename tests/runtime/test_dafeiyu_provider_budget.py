"""Exercise the patched real provider up to a mocked SDK request boundary."""

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from openai.types.chat.chat_completion import ChatCompletion
from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
from astrbot.core.provider.entities import LLMResponse
from astrbot.core.utils import chat_efficiency


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


if __name__ == "__main__":
    unittest.main()

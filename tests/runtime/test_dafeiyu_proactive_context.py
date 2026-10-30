"""Search follow-ups retain the actual hooked ProviderRequest without persisting memory."""

import copy
import importlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from astrbot.core.provider.entities import LLMResponse, ProviderRequest

llm_module = importlib.import_module(
    "data.plugins.astrbot_plugin_proactive_chat.core.services.llm_service"
)


class ProactiveContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_retains_hook_memory_prompt_history_model_and_previous_tool_results(self):
        plugin = SimpleNamespace(context=object(), timezone=ZoneInfo("Asia/Shanghai"), telemetry=None,
            context_service=SimpleNamespace(sanitize_history_content=lambda x: list(x),
                make_temp_text_part=lambda text: {"type": "text", "text": text, "_no_save": True}))
        service = llm_module.LlmService(plugin)
        calls, requests = [], []
        tool = SimpleNamespace(parameters={}, call=AsyncMock(return_value="lookup result"))
        tools = SimpleNamespace(empty=lambda: False, get_tool=lambda name: tool)
        provider = SimpleNamespace()

        async def invoke(**kwargs):
            calls.append(copy.deepcopy({k: v for k, v in kwargs.items() if k != "func_tool"}))
            return LLMResponse("assistant", completion_text="" if len(calls) == 1 else "final",
                               tools_call_name=["lookup"] if len(calls) == 1 else [],
                               tools_call_args=[{}], tools_call_ids=["t1"])
        provider.text_chat = invoke
        service.resolve_chat_provider = AsyncMock(return_value=provider)

        async def attach(req, _):
            req.func_tool = tools

        async def hook(event, req):
            req.prompt = "hook changed prompt"
            req.contexts = [*req.contexts, {"role": "assistant", "content": "hook context"}]
            req.extra_user_content_parts.append(
                {"type": "text", "text": "hook memory", "_no_save": True})
            req.model = "selected-model"
            req.tool_calls_result = SimpleNamespace(to_openai_messages=lambda: [
                {"role": "assistant", "content": None, "tool_calls": [{"id": "previous",
                    "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "previous", "content": "previous tool result"},
            ])
            requests.append(req)
            return False
        service.attach_web_search_tools = attach
        service.dispatch_llm_request_hooks = hook
        service.dispatch_llm_response_hooks = AsyncMock(return_value=False)
        history = [{"role": "user", "content": "old message"}]
        response, saved_prompt = await service.generate_llm_response(
            "test", {"proactive_prompt": "original prompt"}, history, "stable persona", 0,
            event=object(), conversation=SimpleNamespace(cid="test-cid"),
        )
        self.assertEqual(response.completion_text, "final")
        self.assertEqual(saved_prompt, "hook changed prompt")
        self.assertEqual(calls[1]["model"], "selected-model")
        self.assertEqual(calls[1]["conversation_id"], "test-cid")
        wire = json.dumps(calls[1]["contexts"], ensure_ascii=False)
        for marker in ("hook changed prompt", "hook context", "hook memory", "lookup result",
                       "previous tool result"):
            self.assertEqual(wire.count(marker), 1)
        self.assertNotIn("original prompt", wire)
        self.assertEqual(history, [{"role": "user", "content": "old message"}])
        self.assertNotIn("hook memory", json.dumps(requests[0].contexts))
        self.assertTrue(requests[0].extra_user_content_parts[-1]["_no_save"])

    async def test_tool_round_exhaustion_never_delivers_an_unfinished_reply(self):
        service = llm_module.LlmService(SimpleNamespace(context=object()))
        tool = SimpleNamespace(parameters={}, call=AsyncMock(return_value="found"))
        req = ProviderRequest(prompt="search", func_tool=SimpleNamespace(
            empty=lambda: False, get_tool=lambda _: tool))
        response = LLMResponse("assistant", completion_text="unfinished", tools_call_name=["lookup"],
                               tools_call_args=[{}], tools_call_ids=["t1"])
        provider = SimpleNamespace(text_chat=AsyncMock(return_value=response))
        result = await service.run_tool_loop(provider, req, response, "test", max_rounds=1)
        self.assertIsNone(result)
        provider.text_chat.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()

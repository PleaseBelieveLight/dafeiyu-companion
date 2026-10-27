"""Exercise delivered selfie recognition, command continuation and memory isolation."""

import copy
import importlib
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.message.components import Image
from astrbot.core.message.message_event_result import MessageEventResult
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.utils import selfie_context
from tests.test_dafeiyu_voice_mode import fixture

omni_module = importlib.import_module("data.plugins.astrbot_plugin_omnidraw.main")


class SelfieContextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        """Build real handlers with only network and storage boundaries mocked."""
        self.storage = {}

        async def get_value(scope, scope_id, key, default=None):
            return copy.deepcopy(self.storage.get((scope_id, key), default))

        async def put_value(scope, scope_id, key, value):
            self.storage[(scope_id, key)] = copy.deepcopy(value)

        self.read = patch.object(
            selfie_context.sp, "get_async", new=AsyncMock(side_effect=get_value)
        )
        self.write = patch.object(
            selfie_context.sp, "put_async", new=AsyncMock(side_effect=put_value)
        )
        self.read.start()
        self.write.start()
        self.addCleanup(self.read.stop)
        self.addCleanup(self.write.stop)
        self.face, self.event, _, _, _, _ = fixture("", {"tts_enabled": False})
        self.event.get_message_str.return_value = "/自拍 靠在窗边"
        self.event.chain_result.side_effect = lambda chain: MessageEventResult(
            chain=chain
        )
        self.event.plain_result.side_effect = lambda text: MessageEventResult().message(
            text
        )
        self.event.request_llm.side_effect = lambda **kwargs: (
            AstrMessageEvent.request_llm(self.event, **kwargs)
        )
        self.conversation = SimpleNamespace(
            cid="conversation-a", history='[{"role":"user","content":"聊到了雨天"}]'
        )
        self.manager = SimpleNamespace(
            get_curr_conversation_id=AsyncMock(return_value=self.conversation.cid),
            get_conversation=AsyncMock(return_value=self.conversation),
            new_conversation=AsyncMock(return_value="new-conversation"),
        )
        self.vision = SimpleNamespace(
            text_chat=AsyncMock(
                return_value=SimpleNamespace(
                    role="assistant",
                    completion_text="蓝发鲸鱼娘穿白色外套靠在窗边，窗外下雨，正对镜头微笑。",
                )
            )
        )
        self.face.context.conversation_manager = self.manager
        self.face.context.get_config.return_value["provider_settings"][
            "default_image_caption_provider_id"
        ] = "test-vision"
        self.face.context.get_provider_by_id = MagicMock(return_value=self.vision)
        self.omni = omni_module.OmniDrawPlugin.__new__(omni_module.OmniDrawPlugin)
        self.omni.context = self.face.context
        self.omni._permission_denied_message = MagicMock(return_value=None)
        self.omni._reserve_image_quota = MagicMock(return_value=(None, 1))
        self.omni._complete_image_quota = MagicMock()
        self.omni._release_image_quota = MagicMock()
        self.omni._extract_command_message = MagicMock(return_value="靠在窗边")
        self.omni.cmd_parser = SimpleNamespace(
            parse=MagicMock(return_value=("靠在窗边", {}))
        )
        self.omni.prompt_optimizer = SimpleNamespace(
            optimize=AsyncMock(return_value=["靠在窗边"])
        )
        self.omni.persona_manager = SimpleNamespace(
            build_persona_prompt=MagicMock(return_value=("生成意图：在窗边", {}))
        )
        self.omni.plugin_config = SimpleNamespace(
            persona_ref_images=["reference-only.png"],
            selfie_pending_message="等我一下，给你拍一张～",
            selfie_error_message="自拍失败：{error}",
            persona_name="大肥鱼",
            verbose_report=False,
            chains={"selfie": ["test-node"]},
            show_generation_time=False,
            show_request_model=False,
        )
        self.omni._get_event_images = MagicMock(return_value=[])
        self.omni._process_and_save_images = AsyncMock(
            return_value=["reference-only.png"]
        )
        self.omni._lease_cache_paths = MagicMock(return_value=[])
        self.omni._release_cache_paths = MagicMock()
        self.omni._send_plain_message = AsyncMock(return_value=None)
        self.generated = "https://example.invalid/generated-selfie.png"
        self.generation = patch.object(
            omni_module.ChainManager,
            "run_chain_with_metadata",
            new=AsyncMock(return_value=self.generated),
        )
        self.generate = self.generation.start()
        self.addCleanup(self.generation.stop)

    async def test_command_recognizes_delivered_image_then_continues_original_conversation(
        self,
    ):
        replies = [item async for item in self.omni.cmd_selfie(self.event)]
        self.assertEqual(len(replies), 1)
        request = replies[0]
        self.assertIsInstance(request, ProviderRequest)
        self.assertIs(request.conversation, self.conversation)
        self.assertIn("雨天", request.conversation.history)
        self.assertEqual(request.prompt, "/自拍 靠在窗边")
        self.assertIn("不是对方上传的照片", request.system_prompt)
        self.assertIn("白色外套", request.system_prompt)
        self.assertNotIn("reference-only.png", request.system_prompt)
        self.vision.text_chat.assert_awaited_once()
        self.assertEqual(
            self.vision.text_chat.await_args.kwargs["image_urls"], [self.generated]
        )
        sent = self.event.send.await_args.args[0].chain
        self.assertEqual(sum(isinstance(item, Image) for item in sent), 1)
        memory = self.storage[
            (self.event.unified_msg_origin, selfie_context.SELFIE_MEMORY_KEY)
        ]
        self.assertEqual(memory[0]["sender"], "assistant")
        self.assertEqual(memory[0]["conversation_id"], "conversation-a")
        request.func_tool = ToolSet(
            [
                FunctionTool(name=name, description=name, parameters={})
                for name in (
                    "generate_selfie",
                    "generate_image",
                    "generate_video",
                    "web_search",
                )
            ]
        )
        node = SimpleNamespace(
            base_url="https://example.invalid", model="test", has_api_key=True
        )
        self.face.context.get_registered_star.return_value = SimpleNamespace(
            activated=True,
            star_cls=SimpleNamespace(
                plugin_config=SimpleNamespace(providers=[node], video_providers=[node])
            ),
        )
        await self.face.inject_voice_status(self.event, request)
        self.assertEqual(request.func_tool.names(), ["web_search"])
        self.assertIn("此前由你发出的自拍", request.system_prompt)
        self.assertIn("voice 必须留空", request.system_prompt)
        self.generate.assert_awaited_once()

    async def test_generation_or_platform_failure_does_not_remember_or_continue(self):
        for failure in ("generation", "platform"):
            with self.subTest(failure=failure):
                self.generate.side_effect = (
                    RuntimeError("HTTP 429") if failure == "generation" else None
                )
                self.event.send.side_effect = (
                    RuntimeError("Platform unavailable")
                    if failure == "platform"
                    else None
                )
                replies = [item async for item in self.omni.cmd_selfie(self.event)]
                self.assertFalse(
                    any(isinstance(item, ProviderRequest) for item in replies)
                )
                self.assertEqual(self.storage, {})
        self.vision.text_chat.assert_not_awaited()

    async def test_vision_failure_keeps_success_and_separates_intent_from_evidence(
        self,
    ):
        self.vision.text_chat.side_effect = TimeoutError("Vision unavailable")
        replies = [item async for item in self.omni.cmd_selfie(self.event)]
        self.assertIsInstance(replies[0], ProviderRequest)
        self.assertIn("画面暂未识别", replies[0].system_prompt)
        self.assertNotIn("白色外套", replies[0].system_prompt)
        record = self.storage[
            (self.event.unified_msg_origin, selfie_context.SELFIE_MEMORY_KEY)
        ][0]
        self.assertEqual(record["visual_description"], "")
        self.assertEqual(record["requested_action"], "靠在窗边")

    async def test_tool_returns_actual_selfie_facts_without_starting_another_chat(self):
        self.omni._normalize_count = MagicMock(return_value=1)
        self.omni._run_selfie_generation = AsyncMock(
            return_value={"results": [self.generated]}
        )
        outcome = await self.omni.tool_generate_selfie(self.event, action="靠在窗边")
        self.assertIn("白色外套", outcome)
        self.assertIn('"sender": "assistant"', outcome)
        self.event.request_llm.assert_not_called()
        self.vision.text_chat.assert_awaited_once()
        self.omni._run_selfie_generation.assert_awaited_once()

    async def test_saved_facts_survive_new_event_but_are_isolated_by_conversation(self):
        await selfie_context.remember_selfie(
            self.face.context,
            self.event,
            [self.generated],
            "靠在窗边",
            "conversation-a",
        )
        new_face, new_event, _, _, _, _ = fixture("", {})
        new_face.context.conversation_manager = self.manager
        for cid, included in [("conversation-a", True), ("conversation-b", False)]:
            with self.subTest(conversation=cid):
                request = SimpleNamespace(
                    system_prompt="Existing persona",
                    func_tool=None,
                    conversation=SimpleNamespace(cid=cid),
                )
                await new_face.inject_voice_status(new_event, request)
                self.assertEqual("白色外套" in request.system_prompt, included)
                self.assertIn("voice 必须留空", request.system_prompt)

    async def test_switching_conversation_during_generation_does_not_inject_old_photo(
        self,
    ):
        self.manager.get_curr_conversation_id.side_effect = [
            "conversation-a",
            "conversation-b",
        ]
        replies = [item async for item in self.omni.cmd_selfie(self.event)]
        self.assertFalse(any(isinstance(item, ProviderRequest) for item in replies))
        memory = self.storage[
            (self.event.unified_msg_origin, selfie_context.SELFIE_MEMORY_KEY)
        ]
        self.assertEqual(memory[0]["conversation_id"], "conversation-a")

    async def test_memory_is_bounded_and_only_delivered_images_are_analyzed(self):
        key = (self.event.unified_msg_origin, selfie_context.SELFIE_MEMORY_KEY)
        self.storage[key] = [{"conversation_id": "old", "sender": "assistant"}] * 15
        await selfie_context.remember_selfie(
            self.face.context,
            self.event,
            [self.generated] * 4,
            "动作要求",
            "conversation-a",
        )
        self.assertEqual(len(self.storage[key]), 12)
        self.assertEqual(len(self.vision.text_chat.await_args.kwargs["image_urls"]), 3)
        self.assertIn("仅识别前三张", self.storage[key][-1]["visual_status"])


if __name__ == "__main__":
    unittest.main()

"""Exercise explicit action routing through the real guarded selfie tool."""

import importlib
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.message.components import Image, Plain, Record
from astrbot.core.message.message_event_result import MessageEventResult
from astrbot.core.pipeline.respond.stage import RespondStage
from astrbot.core.pipeline.result_decorate.stage import ResultDecorateStage
from astrbot.core.provider.func_tool_manager import _PermissionGuardedTool
from tests.test_dafeiyu_voice_mode import envelope, face_module, fixture

omni_module = importlib.import_module("data.plugins.astrbot_plugin_omnidraw.main")


def action_fixture(enabled=True, failure=False, denied=False):
    """Build the real selfie handler with only its external generation mocked.

    Args:
        enabled: Persisted normal-chat voice permission.
        failure: Whether the image API should report a usage limit.
        denied: Whether native tool permissions should reject the request.

    Returns:
        Voice fixture, native drawing plugin, guarded manager and request.
    """
    session = {"tts_enabled": enabled}
    values = fixture(envelope("我只用文字描述跳舞。", ""), session)
    plugin, event, _, _, _, _ = values
    event.get_message_str.return_value = "给我跳个舞看看"
    event.chain_result.side_effect = lambda chain: MessageEventResult(chain=chain)
    omni = omni_module.OmniDrawPlugin.__new__(omni_module.OmniDrawPlugin)
    omni.context = SimpleNamespace(
        conversation_manager=SimpleNamespace(
            get_curr_conversation_id=AsyncMock(return_value="local-test-conversation")
        )
    )
    omni._permission_denied_message = MagicMock(return_value=None)
    omni._normalize_count = lambda count: count
    omni._reserve_image_quota = MagicMock(return_value=(None, 1))
    omni._complete_image_quota = MagicMock()
    omni._release_image_quota = MagicMock()
    omni._run_selfie_generation = AsyncMock(
        return_value={"results": ["https://example.invalid/dance.png"]}
    )
    if failure:
        omni._run_selfie_generation.side_effect = RuntimeError("HTTP 429: usage limit")
    manager = SimpleNamespace(
        _check_tool_permission=AsyncMock(
            return_value="error: Permission denied" if denied else None
        )
    )
    native_tool = FunctionTool(
        name="generate_selfie",
        description="Native selfie",
        parameters={"type": "object", "properties": {}},
        handler=omni.tool_generate_selfie,
    )
    guarded = _PermissionGuardedTool(native_tool, manager)
    tools = ToolSet(
        [
            guarded,
            FunctionTool(
                name="generate_image",
                description="Image",
                parameters={"type": "object", "properties": {}},
            ),
        ]
    )
    manager.get_full_tool_set = MagicMock(return_value=tools)
    plugin.context.get_llm_tool_manager = MagicMock(return_value=manager)
    plugin.context.get_registered_star = MagicMock(
        return_value=SimpleNamespace(
            activated=True,
            star_cls=SimpleNamespace(
                plugin_config=SimpleNamespace(
                    providers=[
                        SimpleNamespace(
                            base_url="https://example.invalid",
                            model="test",
                            has_api_key=True,
                        )
                    ],
                    video_providers=[],
                )
            ),
        )
    )
    request = SimpleNamespace(
        system_prompt="Existing persona.", func_tool=ToolSet(list(tools.tools))
    )
    return values, omni, manager, request


class ActionExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        """Keep the separately covered selfie memory path offline."""
        memory = patch.object(
            omni_module, "remember_selfie", new=AsyncMock(return_value="")
        )
        memory.start()
        self.addCleanup(memory.stop)

    async def test_action_sends_one_real_image_chain_with_text_by_default(self):
        for enabled in (True, False):
            with self.subTest(voice=enabled):
                values, omni, manager, request = action_fixture(enabled)
                plugin, event, result, provider, extras, pipeline = values
                with patch.object(
                    face_module.sp,
                    "get_async",
                    new=AsyncMock(return_value={"tts_enabled": enabled}),
                ):
                    await plugin.inject_voice_status(event, request)
                    await plugin.inject_voice_status(event, request)
                    self.assertTrue(extras["_fy_action_picture"]["sent"])
                    omni._run_selfie_generation.assert_awaited_once()
                    self.assertEqual(omni._run_selfie_generation.await_args.args[2], 1)
                    manager._check_tool_permission.assert_awaited_once()
                    self.assertIsNone(request.func_tool.get_tool("generate_selfie"))
                    response = SimpleNamespace(completion_text="我只打算用文字表演。")
                    await plugin.scrub_drafts(event, response)
                    self.assertNotIn("文字表演", response.completion_text)
                    result.chain = [Plain(response.completion_text)]
                    decorate, deliver = ResultDecorateStage(), RespondStage()
                    await decorate.initialize(pipeline)
                    await deliver.initialize(pipeline)
                    handler = SimpleNamespace(
                        handler=plugin.convert_faces,
                        handler_module_path="test.action",
                        handler_name="action",
                    )
                    with (
                        patch(
                            "astrbot.core.pipeline.result_decorate.stage.star_handlers_registry.get_handlers_by_event_type",
                            return_value=[handler],
                        ),
                        patch.dict(
                            "astrbot.core.pipeline.result_decorate.stage.star_map",
                            {"test.action": SimpleNamespace(name="Test")},
                        ),
                        patch(
                            "astrbot.core.pipeline.respond.stage.call_event_hook",
                            new=AsyncMock(return_value=False),
                        ),
                    ):
                        async for _ in decorate.process(event):
                            pass
                        await deliver.process(event)
                sent = [
                    comp
                    for call in event.send.await_args_list
                    for comp in call.args[0].chain
                ]
                self.assertEqual(sum(isinstance(c, Image) for c in sent), 1)
                self.assertEqual(sum(isinstance(c, Record) for c in sent), 0)
                self.assertEqual(
                    [c.text for c in sent if isinstance(c, Plain)],
                    ["给你一个舞姿～"],
                )
                provider.get_audio.assert_not_awaited()
                self.assertEqual(omni._complete_image_quota.call_args.args[2], 1)

    async def test_api_failure_or_native_permission_denial_is_not_success(self):
        for failure, denied in [(True, False), (False, True)]:
            with self.subTest(failure=failure, denied=denied):
                values, omni, manager, request = action_fixture(
                    failure=failure, denied=denied
                )
                plugin, event, result, provider, extras, _ = values
                with patch.object(
                    face_module.sp,
                    "get_async",
                    new=AsyncMock(return_value={"tts_enabled": True}),
                ):
                    await plugin.inject_voice_status(event, request)
                    await plugin.inject_voice_status(event, request)
                    await plugin.convert_faces(event)
                self.assertFalse(extras["_fy_action_picture"]["sent"])
                self.assertIn("没生成图片", result.get_plain_text())
                event.send.assert_not_awaited()
                provider.get_audio.assert_not_awaited()
                manager._check_tool_permission.assert_awaited_once()
                self.assertEqual(
                    omni._run_selfie_generation.await_count, 0 if denied else 1
                )

    async def test_discussion_negation_and_video_requests_do_not_auto_generate(self):
        for message in [
            "她在跳舞",
            "我同事昨天跳舞很好看",
            "不要给我跳舞",
            "我在学跳舞",
            "你会跳舞吗",
            "只给我视频，不要图片",
            "帮我画一只正在跳舞的猫",
        ]:
            with self.subTest(message=message):
                values, omni, manager, request = action_fixture()
                plugin, event, _, _, extras, _ = values
                event.get_message_str.return_value = message
                with patch.object(
                    face_module.sp,
                    "get_async",
                    new=AsyncMock(return_value={"tts_enabled": True}),
                ):
                    await plugin.inject_voice_status(event, request)
                omni._run_selfie_generation.assert_not_awaited()
                manager._check_tool_permission.assert_not_awaited()
                self.assertNotIn("_fy_action_picture", extras)

    async def test_explicit_variants_use_one_image_each(self):
        for message in [
            "你跳个舞",
            "请给我跳一段舞吧！",
            "[主人] 给我跳个舞看看",
            "大肥鱼来转一圈给我看",
            "摆个姿势",
        ]:
            with self.subTest(message=message):
                values, omni, _, request = action_fixture()
                plugin, event, _, _, _, _ = values
                event.get_message_str.return_value = message
                with patch.object(
                    face_module.sp,
                    "get_async",
                    new=AsyncMock(return_value={"tts_enabled": False}),
                ):
                    await plugin.inject_voice_status(event, request)
                omni._run_selfie_generation.assert_awaited_once()

    async def test_failed_platform_delivery_does_not_claim_an_image_was_sent(self):
        values, _, _, request = action_fixture()
        plugin, event, result, provider, extras, _ = values
        event.send.side_effect = RuntimeError("Platform offline")
        with patch.object(
            face_module.sp,
            "get_async",
            new=AsyncMock(return_value={"tts_enabled": True}),
        ):
            await plugin.inject_voice_status(event, request)
            await plugin.convert_faces(event)
        self.assertFalse(extras["_fy_action_picture"]["sent"])
        self.assertIn("没生成成功", result.get_plain_text())
        provider.get_audio.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

"""Exercise autonomous selfie decisions through real permission, delivery and quota guards."""

import json
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot.core.message.components import Image
from astrbot.core.utils.selfie_intent import SHANGHAI
from tests.test_dafeiyu_action_execution import action_fixture, omni_module
from tests.test_dafeiyu_voice_mode import envelope, face_module


class SelfieIntentIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        memory = patch.object(omni_module, "remember_selfie", new=AsyncMock(return_value=""))
        memory.start()
        self.addCleanup(memory.stop)
        store = patch.object(face_module.sp, "get_async", new=AsyncMock(return_value={}))
        store.start()
        self.addCleanup(store.stop)

    def build(self, message, planned=True, scene="白天坐在窗边看书，抬眼微笑", **kwargs):
        values, omni, manager, request = action_fixture(enabled=False, **kwargs)
        plugin, event, _, _, _, _ = values
        event.get_message_str.return_value = message
        planner = SimpleNamespace(
            text_chat=AsyncMock(
                return_value=SimpleNamespace(
                    completion_text=json.dumps({"generate": planned, "action": scene})
                )
            )
        )
        plugin.context.get_using_provider_async = AsyncMock(return_value=planner)
        request.contexts = []
        return values, omni, manager, request, planner

    async def test_unspecified_selfie_generates_one_image_without_command_or_question(self):
        values, omni, manager, request, planner = self.build("生成一张自拍图片")
        plugin, event, _, _, extras, _ = values
        await plugin.inject_voice_status(event, request)
        await plugin.inject_voice_status(event, request)
        self.assertTrue(extras["_fy_selfie_picture"]["sent"])
        omni._run_selfie_generation.assert_awaited_once()
        self.assertIn("窗边看书", omni._run_selfie_generation.await_args.args[1])
        manager._check_tool_permission.assert_awaited_once()
        planner.text_chat.assert_awaited_once()
        self.assertNotIn("func_tool", planner.text_chat.await_args.kwargs)
        self.assertEqual(len(event.send.await_args_list), 1)
        self.assertIsInstance(event.send.await_args.args[0].chain[0], Image)
        self.assertIsNone(request.func_tool.get_tool("generate_selfie"))
        response = SimpleNamespace(completion_text=envelope("请用 /自拍 场景 发出来", ""))
        await plugin.scrub_drafts(event, response)
        self.assertEqual(response.completion_text, "喏，给你看～")

    async def test_show_me_after_sleeping_uses_recent_role_state_and_preserves_natural_reply(self):
        values, omni, _, request, planner = self.build(
            "真的吗？给我看看", scene="穿睡衣躺在床上抱鲸鱼，困倦微笑"
        )
        plugin, event, _, _, _, _ = values
        request.contexts = [
            {"role": "user", "content": "你现在在做什么"},
            {"role": "assistant", "content": "我在睡觉呢"},
        ]
        await plugin.inject_voice_status(event, request)
        self.assertIn("躺在床上", omni._run_selfie_generation.await_args.args[1])
        prompt = json.loads(planner.text_chat.await_args.kwargs["prompt"])
        self.assertEqual(prompt["dialogue"][-1], {"role": "assistant", "content": "我在睡觉呢"})
        self.assertTrue(prompt["current_time"].endswith("+08:00"))
        response = SimpleNamespace(completion_text=envelope("你看，困得眼睛都快睁不开啦。", ""))
        await plugin.scrub_drafts(event, response)
        self.assertEqual(response.completion_text, "你看，困得眼睛都快睁不开啦。")

    async def test_show_me_code_is_semantically_declined_and_does_not_charge(self):
        values, omni, manager, request, planner = self.build(
            "给我看看报错日志", planned=False, scene=""
        )
        plugin, event, _, _, extras, _ = values
        request.contexts = [{"role": "assistant", "content": "刚才在检查日志。"}]
        await plugin.inject_voice_status(event, request)
        await plugin.inject_voice_status(event, request)
        planner.text_chat.assert_awaited_once()
        omni._run_selfie_generation.assert_not_awaited()
        manager._check_tool_permission.assert_not_awaited()
        self.assertNotIn("_fy_selfie_picture", extras)

    async def test_nonrequests_negation_and_unavailable_tools_skip_scene_planning(self):
        for message, unavailable in [
            ("你现在在做什么", False),
            ("不要给我发自拍", False),
            ("如何生成自拍", False),
            ("给我看看", False),
            ("生成一张自拍图片", True),
        ]:
            with self.subTest(message=message, unavailable=unavailable):
                values, omni, _, request, planner = self.build(message)
                plugin, event, _, _, _, _ = values
                if unavailable:
                    request.func_tool.remove_tool("generate_selfie")
                await plugin.inject_voice_status(event, request)
                planner.text_chat.assert_not_awaited()
                omni._run_selfie_generation.assert_not_awaited()

    async def test_planning_failure_still_executes_explicit_selfie_with_context_fallback(self):
        values, omni, _, request, planner = self.build("生成一张自拍图片")
        plugin, event, _, _, _, _ = values
        planner.text_chat.side_effect = TimeoutError("offline planner")
        request.contexts = [{"role": "assistant", "content": "我在睡觉。"}]
        now = datetime(2026, 10, 29, 10, 30, tzinfo=SHANGHAI)
        with patch.object(face_module, "datetime") as clock:
            clock.now.return_value = now
            await plugin.inject_voice_status(event, request)
        self.assertIn("躺在床上", omni._run_selfie_generation.await_args.args[1])
        self.assertIn("2026-10-29 10:30", request.system_prompt)

    async def test_permission_or_api_failure_never_claims_success_or_retries(self):
        for failure, denied in [(True, False), (False, True)]:
            with self.subTest(failure=failure, denied=denied):
                values, omni, manager, request, _ = self.build(
                    "生成一张自拍图片", failure=failure, denied=denied
                )
                plugin, event, _, _, extras, _ = values
                await plugin.inject_voice_status(event, request)
                await plugin.inject_voice_status(event, request)
                self.assertFalse(extras["_fy_selfie_picture"]["sent"])
                response = SimpleNamespace(completion_text=envelope("已经发给你啦", ""))
                await plugin.scrub_drafts(event, response)
                self.assertIn("没生成图片", response.completion_text)
                manager._check_tool_permission.assert_awaited_once()
                event.send.assert_not_awaited()
                self.assertEqual(omni._run_selfie_generation.await_count, int(not denied))

    async def test_native_quota_failure_is_not_bypassed(self):
        values, omni, manager, request, _ = self.build("生成一张自拍图片")
        plugin, event, _, _, _, _ = values
        omni._reserve_image_quota.return_value = ("今日生图额度不足", 0)
        await plugin.inject_voice_status(event, request)
        response = SimpleNamespace(completion_text="已经发给你了")
        await plugin.scrub_drafts(event, response)
        self.assertIn("额度受限", response.completion_text)
        manager._check_tool_permission.assert_awaited_once()
        omni._run_selfie_generation.assert_not_awaited()
        event.send.assert_not_awaited()

    async def test_semantic_rejection_overrides_a_keyword_candidate(self):
        values, omni, _, request, planner = self.build(
            "你总让我生成自拍，我觉得很麻烦", planned=False, scene=""
        )
        plugin, event, _, _, extras, _ = values
        await plugin.inject_voice_status(event, request)
        planner.text_chat.assert_awaited_once()
        omni._run_selfie_generation.assert_not_awaited()
        self.assertNotIn("_fy_selfie_picture", extras)


if __name__ == "__main__":
    unittest.main()

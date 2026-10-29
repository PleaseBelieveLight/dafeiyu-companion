"""Verify sparse speech, one-channel replies and explicit overrides at delivery."""

import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot.core.message.components import Plain, Record
from tests.test_dafeiyu_voice_mode import envelope, face_module, fixture


class SparseVoicePolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_reported_rephrasings_are_removed_before_storage_and_delivery(self):
        examples = [
            (
                "我就住在这儿的 QQ 里呀，你看不见的地方有个水缸，够我泡着，尾巴还有地方甩。主人找我的时候，我就冒头。",
                "就住在你 QQ 里呀，泡在一个不小的水缸里，你一叫我，我就冒出水面来。",
            ),
            (
                "水缸是不大，可水是活的呀，底下通着一整片海。我平时就在缸里打盹，真游起来就往深处一钻，那片够我甩尾巴的。",
                "缸小归缸小，底下通着一整片海呢，钻下去就宽敞了。",
            ),
        ]
        for enabled in (True, False):
            for text, voice in examples:
                with self.subTest(enabled=enabled, text=text):
                    raw = envelope(text, voice)
                    plugin, event, result, provider, extras, _ = fixture(
                        raw, {"tts_enabled": enabled}
                    )
                    response = SimpleNamespace(completion_text=raw)
                    with patch.object(
                        face_module.sp,
                        "get_async",
                        new=AsyncMock(return_value={"tts_enabled": enabled}),
                    ):
                        await plugin.scrub_drafts(event, response)
                        self.assertEqual(response.completion_text, text)
                        self.assertEqual(extras["_fy_voice_plan"], {"text": text, "voice": ""})
                        result.chain = [Plain(response.completion_text)]
                        await plugin.prepare_contextual_reply(event, result)
                    self.assertEqual(result.get_plain_text(), text)
                    self.assertEqual(len(result.chain), 1)
                    provider.get_audio.assert_not_awaited()
                    # Tool-loop replies can bypass the response hook.
                    _, _, direct, direct_provider, _, _ = await self.deliver(raw, enabled=enabled)
                    self.assertEqual(direct.get_plain_text(), text)
                    direct_provider.get_audio.assert_not_awaited()

    async def test_plain_detailed_reply_keeps_its_length_and_new_information(self):
        text = "水缸底下通着海，我想游的时候就往深处钻。\n今天新添了一盏小灯，晚上可以照着看书。"
        _, _, result, provider, _, _ = await self.deliver(text)
        self.assertEqual(result.get_plain_text(), text)
        provider.get_audio.assert_not_awaited()

    async def test_explicit_voice_uses_the_primary_answer_without_its_rephrasing(self):
        text = "水缸底下通着一整片海，我想游的时候就往深处钻。"
        voice = "缸底连着海呢，钻下去就宽敞啦。"
        _, _, result, provider, _, _ = await self.deliver(
            envelope(text, voice), message="用语音说给我听"
        )
        self.assertFalse(any(isinstance(comp, Plain) for comp in result.chain))
        self.assertEqual([comp.text for comp in result.chain if isinstance(comp, Record)], [text])
        provider.get_audio.assert_awaited_once_with(text)

    async def test_no_rephrased_summary_rule_is_injected_with_voice_on_or_off(self):
        for enabled in (True, False):
            plugin, event, _, _, _, _ = fixture("", {"tts_enabled": enabled})
            request = SimpleNamespace(system_prompt="Existing persona.")
            with patch.object(
                face_module.sp, "get_async", new=AsyncMock(return_value={"tts_enabled": enabled})
            ):
                await plugin.inject_voice_status(event, request)
                await plugin.inject_voice_status(event, request)
            self.assertEqual(request.system_prompt.count("同一条回复中，一个意思只说一次"), 1)

    async def test_quiet_context_uses_compact_text_policy_and_suppresses_speech(self):
        values = fixture(envelope("", "我听着呢。"), {"tts_enabled": True})
        plugin, event, result, provider, _, _ = values
        event.get_message_str.return_value = "嗯"
        request = SimpleNamespace(
            system_prompt="Existing persona.",
            contexts=[{"role": "user", "content": "我在开会，不方便听"}],
        )
        with patch.object(
            face_module.sp, "get_async", new=AsyncMock(return_value={"tts_enabled": True})
        ):
            await plugin.inject_voice_status(event, request)
            self.assertNotIn("<fy_reply>", request.system_prompt)
            await plugin.prepare_contextual_reply(event, result)
        self.assertEqual(result.get_plain_text(), "我听着呢。")
        provider.get_audio.assert_not_awaited()

    async def deliver(
        self, raw, message="你好", recent=False, enabled=True, proactive=False, failure=False
    ):
        values = fixture(raw, {"tts_enabled": enabled}, proactive=proactive, failure=failure)
        plugin, event, result, provider, extras, _ = values
        event.get_message_str.return_value = message
        if recent:
            plugin._last_voice_at = {event.unified_msg_origin: time.monotonic()}
        with patch.object(
            face_module.sp, "get_async", new=AsyncMock(return_value={"tts_enabled": enabled})
        ):
            await plugin.prepare_contextual_reply(event, result)
            await plugin.prepare_contextual_reply(event, result)
        return values

    async def test_plain_replies_and_proactive_greetings_are_not_automatically_spoken(self):
        for proactive in (False, True):
            with self.subTest(proactive=proactive):
                _, _, result, provider, _, _ = await self.deliver(
                    "吃过饭啦，刚歇下来。", proactive=proactive
                )
                self.assertEqual(result.get_plain_text(), "吃过饭啦，刚歇下来。")
                provider.get_audio.assert_not_awaited()

    async def test_recent_implicit_voice_becomes_one_text_message(self):
        _, _, result, provider, extras, _ = await self.deliver(
            envelope("", "慢慢来，我陪你。"), recent=True
        )
        self.assertEqual(result.get_plain_text(), "慢慢来，我陪你。")
        provider.get_audio.assert_not_awaited()
        self.assertFalse(extras["_fy_voice_delivered"])

    async def test_explicit_voice_request_overrides_recent_speech_without_dual_output(self):
        _, _, result, provider, _, _ = await self.deliver(
            "别怕，先缓口气，我在呢。", "用语音说给我听", recent=True
        )
        self.assertEqual(sum(isinstance(c, Record) for c in result.chain), 1)
        self.assertFalse(any(isinstance(c, Plain) for c in result.chain))
        provider.get_audio.assert_awaited_once()

    async def test_listening_conditions_and_disabled_switch_still_win(self):
        for message, enabled in [
            ("只发文字，别发语音", True),
            ("我在开会", True),
            ("用语音说", False),
        ]:
            with self.subTest(message=message):
                _, _, result, provider, _, _ = await self.deliver(
                    envelope("", "先别紧张，我在。"), message, enabled=enabled
                )
                self.assertEqual(result.get_plain_text(), "先别紧张，我在。")
                provider.get_audio.assert_not_awaited()

    async def test_reported_singing_requests_send_one_audio_even_during_cooldown(self):
        text = "小鲸鱼摇摇尾，今天也要开心呀。"
        for message in ("要不你唱两句", "多唱几句，记得发声音", "语音开了，重新唱几句"):
            for raw in (text, envelope(text, "小鲸鱼摇尾巴，开心呀。"), envelope("", text)):
                with self.subTest(message=message, raw=raw):
                    _, _, result, provider, extras, _ = await self.deliver(
                        raw, message, recent=True
                    )
                    self.assertEqual([c.text for c in result.chain if isinstance(c, Record)], [text])
                    self.assertFalse(any(isinstance(c, Plain) for c in result.chain))
                    provider.get_audio.assert_awaited_once_with(text)
                    self.assertTrue(extras["_fy_voice_delivered"])

    async def test_request_response_cleanup_and_delivery_keep_explicit_singing_intent(self):
        text = "我来用声音示意一下，小鲸鱼摇摇尾。"
        values = fixture("", {"tts_enabled": True})
        plugin, event, result, provider, extras, _ = values
        event.get_message_str.return_value = "语音开了，重新唱几句"
        plugin._last_voice_at = {event.unified_msg_origin: time.monotonic()}
        request = SimpleNamespace(system_prompt="Existing persona.", contexts=[])
        response = SimpleNamespace(completion_text=envelope(text, "小鲸鱼摇尾巴。"))
        with patch.object(
            face_module.sp, "get_async", new=AsyncMock(return_value={"tts_enabled": True})
        ):
            await plugin.inject_voice_status(event, request)
            self.assertEqual(extras["_fy_voice_mode"], "voice")
            await plugin.scrub_drafts(event, response)
            result.chain = [Plain(response.completion_text)]
            await plugin.prepare_contextual_reply(event, result)
        provider.get_audio.assert_awaited_once_with(text)
        self.assertEqual([c.text for c in result.chain if isinstance(c, Record)], [text])

    async def test_singing_cannot_override_disabled_or_quiet_session(self):
        for message, enabled in (("重新唱几句", False), ("不要发声音，先别唱了", True)):
            _, _, result, provider, _, _ = await self.deliver(
                envelope("", "小鲸鱼摇摇尾。"), message, recent=True, enabled=enabled
            )
            provider.get_audio.assert_not_awaited()
            self.assertEqual(result.get_plain_text(), "小鲸鱼摇摇尾。")

    async def test_same_purpose_paraphrases_cannot_produce_two_channels(self):
        _, _, result, _, _, _ = await self.deliver(
            envelope("你已经很努力了，慢慢来。", "加油呀，我相信你可以的。")
        )
        self.assertFalse(
            any(isinstance(c, Record) for c in result.chain)
            and any(isinstance(c, Plain) for c in result.chain)
        )

    async def test_distinct_written_details_and_comfort_remain_complementary(self):
        _, _, result, provider, _, _ = await self.deliver(
            envelope("时间：明天10点，地点：会议室A。", "别紧张，你准备得很充分啦。"),
            "明天面试，有点紧张",
        )
        self.assertEqual(sum(isinstance(c, Record) for c in result.chain), 1)
        self.assertEqual(result.get_plain_text(), "时间：明天10点，地点：会议室A。")
        provider.get_audio.assert_awaited_once_with("别紧张，你准备得很充分啦。")

    async def test_urgent_matters_can_bypass_cooldown_and_failed_tts_does_not_start_it(self):
        plugin, _, result, provider, _, _ = await self.deliver(
            envelope("", "别慌，我在听。"), "有件很重要的事", recent=True
        )
        provider.get_audio.assert_awaited_once()
        self.assertTrue(any(isinstance(c, Record) for c in result.chain))
        plugin, _, result, provider, _, _ = await self.deliver(
            envelope("", "我陪你。"), failure=True
        )
        self.assertEqual(result.get_plain_text(), "我陪你。")
        self.assertFalse(getattr(plugin, "_last_voice_at", {}))

    async def test_cooldown_is_isolated_between_sessions(self):
        values = fixture(envelope("", "别怕，我在。"), {"tts_enabled": True})
        plugin, event, result, provider, _, _ = values
        plugin._last_voice_at = {"another-session": time.monotonic()}
        with patch.object(
            face_module.sp, "get_async", new=AsyncMock(return_value={"tts_enabled": True})
        ):
            await plugin.prepare_contextual_reply(event, result)
        provider.get_audio.assert_awaited_once()

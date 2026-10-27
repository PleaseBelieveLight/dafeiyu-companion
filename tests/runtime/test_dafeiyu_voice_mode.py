"""Regression coverage for contextual speech and distinct written messages."""

import copy
import importlib
import json
import re
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from astrbot.core.config.default import DEFAULT_CONFIG
from astrbot.core.message.components import Plain, Record
from astrbot.core.message.message_event_result import (
    MessageEventResult,
    ResultContentType,
)
from astrbot.core.pipeline.respond.stage import RespondStage
from astrbot.core.pipeline.result_decorate.stage import ResultDecorateStage
from astrbot.core.platform.message_type import MessageType
from astrbot.core.utils.voice_reply import parse_voice_reply

face_module = importlib.import_module("data.plugins.astrbot_plugin_face_convert.main")
sender_module = importlib.import_module(
    "data.plugins.astrbot_plugin_proactive_chat.core.services.sender_service"
)


def envelope(text, voice):
    """Create a model output fixture.

    Args:
        text: Written content.
        voice: Spoken content.

    Returns:
        An internal reply envelope.
    """
    return (
        "<fy_reply>"
        + json.dumps({"text": text, "voice": voice}, ensure_ascii=False)
        + "</fy_reply>"
    )


def fixture(text, session=None, proactive=False, proactive_tts=True, failure=False):
    """Build an isolated real decorator and mocked platform event.

    Args:
        text: Model completion.
        session: Persisted service switch.
        proactive: Whether this is an independent proactive message.
        proactive_tts: Proactive plugin's own speech switch.
        failure: Whether synthesis should fail.

    Returns:
        Plugin, event, result, provider, extras and pipeline context.
    """
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["provider_tts_settings"].update(
        enable=True, dual_output=True, trigger_probability=1.0
    )
    config["content_safety"]["also_use_in_response"] = False
    provider = SimpleNamespace(get_audio=AsyncMock(return_value="local-speech.wav"))
    if failure:
        provider.get_audio.side_effect = RuntimeError("Simulated synthesis failure")
    context = SimpleNamespace(
        get_config=MagicMock(return_value=config),
        get_using_tts_provider_async=AsyncMock(return_value=provider),
        get_registered_star=MagicMock(return_value=None),
    )
    plugin = face_module.FaceConvertPlugin.__new__(face_module.FaceConvertPlugin)
    plugin.context = context
    plugin.cfg = {"face_cooldown_minutes": 10}
    plugin.face_map = {}
    plugin._face_history = {}
    plugin.pattern = re.compile(r"\[([^\[\]]+)\]")
    result = MessageEventResult(chain=[Plain(text)]).set_result_content_type(
        ResultContentType.LLM_RESULT
    )
    event = MagicMock()
    event.unified_msg_origin = "qq:FriendMessage:local-test"
    event.plugins_name = set()
    event.is_stopped.return_value = False
    event.get_result.return_value = result
    extras = {"fy_proactive": proactive, "fy_proactive_tts": proactive_tts}
    event.get_extra.side_effect = extras.get
    event.set_extra.side_effect = extras.__setitem__
    event.get_group_id.return_value = ""
    event.get_platform_name.return_value = "aiocqhttp"
    event.get_message_type.return_value = MessageType.FRIEND_MESSAGE
    event.get_sender_id.return_value = "local-test"
    event.get_sender_name.return_value = "Local test"
    event.get_message_str.return_value = "你好"
    event._outline_chain.return_value = "Local test reply"
    event.send = AsyncMock()
    pipeline = SimpleNamespace(
        astrbot_config=config, plugin_manager=SimpleNamespace(context=context)
    )
    return plugin, event, result, provider, extras, pipeline


class ContextualVoiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_pipeline_preserves_selected_channels_without_duplicate_tts(
        self,
    ):
        cases = [
            (
                "text",
                {"tts_enabled": True},
                False,
                True,
                envelope("链接在这里", ""),
                False,
                ["链接在这里"],
                [],
            ),
            (
                "voice",
                {"tts_enabled": True},
                False,
                True,
                envelope("", "我陪着你。"),
                False,
                [],
                ["我陪着你。"],
            ),
            (
                "mixed",
                {"tts_enabled": True},
                False,
                True,
                envelope("歌名：晴天", "忙完了就歇一会儿吧。"),
                False,
                ["歌名：晴天"],
                ["忙完了就歇一会儿吧。"],
            ),
            (
                "closed",
                {"tts_enabled": False},
                False,
                True,
                envelope("歌名：晴天", "先歇一会儿吧。"),
                False,
                ["歌名：晴天\n先歇一会儿吧。"],
                [],
            ),
            ("not_opened", {}, False, True, envelope("", "你好"), False, ["你好"], []),
            (
                "proactive_closed_normal",
                {"tts_enabled": False},
                True,
                True,
                envelope("", "想起来问问你吃饭了吗？"),
                False,
                [],
                ["想起来问问你吃饭了吗？"],
            ),
            (
                "proactive_fallback",
                {"tts_enabled": False},
                True,
                True,
                "午饭记得吃。",
                False,
                [],
                ["午饭记得吃。"],
            ),
            (
                "proactive_disabled",
                {"tts_enabled": True},
                True,
                False,
                envelope("", "你好"),
                False,
                ["你好"],
                [],
            ),
            (
                "repeat_removed",
                {"tts_enabled": True},
                False,
                True,
                envelope("你好！", "你好。"),
                False,
                [],
                ["你好。"],
            ),
            (
                "failure",
                {"tts_enabled": True},
                False,
                True,
                envelope("链接在这里", "我陪着你。"),
                True,
                ["链接在这里\n我陪着你。"],
                [],
            ),
            (
                "unwrapped",
                {"tts_enabled": True},
                False,
                True,
                "普通文字，不随机朗读",
                False,
                ["普通文字，不随机朗读"],
                [],
            ),
        ]
        for (
            name,
            session,
            proactive,
            proactive_tts,
            raw,
            failure,
            expected_text,
            expected_voice,
        ) in cases:
            with self.subTest(mode=name):
                plugin, event, result, provider, extras, context = fixture(
                    raw, session, proactive, proactive_tts, failure
                )
                handler = SimpleNamespace(
                    handler=plugin.convert_faces,
                    handler_module_path="test.voice",
                    handler_name="contextual_voice",
                )
                decorate, delivery = ResultDecorateStage(), RespondStage()
                await decorate.initialize(context)
                await delivery.initialize(context)
                with (
                    patch.object(
                        face_module.sp, "get_async", new=AsyncMock(return_value=session)
                    ),
                    patch(
                        "astrbot.core.pipeline.result_decorate.stage.star_handlers_registry.get_handlers_by_event_type",
                        return_value=[handler],
                    ),
                    patch.dict(
                        "astrbot.core.pipeline.result_decorate.stage.star_map",
                        {"test.voice": SimpleNamespace(name="Contextual test")},
                    ),
                    patch(
                        "astrbot.core.pipeline.respond.stage.call_event_hook",
                        new=AsyncMock(return_value=False),
                    ),
                ):
                    async for _ in decorate.process(event):
                        pass
                    await delivery.process(event)
                sent = [
                    component
                    for call in event.send.await_args_list
                    for component in call.args[0].chain
                ]
                self.assertEqual(
                    [c.text for c in sent if isinstance(c, Plain)], expected_text
                )
                self.assertEqual(
                    [c.text for c in sent if isinstance(c, Record)], expected_voice
                )
                self.assertEqual(
                    provider.get_audio.await_count, int(bool(expected_voice) or failure)
                )
                self.assertTrue(extras["_fy_voice_handled"])

    async def test_request_policy_respects_normal_switch_and_proactive_exception(self):
        cases = [
            ({"tts_enabled": True}, False, True, "根据上下文选择"),
            ({"tts_enabled": False}, False, True, "本次不允许语音"),
            ({}, False, True, "本次不允许语音"),
            ({"tts_enabled": False}, True, True, "独立允许语音"),
            ({"tts_enabled": True}, True, False, "本次不允许语音"),
        ]
        for session, proactive, proactive_tts, expected in cases:
            plugin, event, _, provider, extras, _ = fixture(
                "", session, proactive, proactive_tts
            )
            request = SimpleNamespace(system_prompt="Existing persona.")
            with patch.object(
                face_module.sp, "get_async", new=AsyncMock(return_value=session)
            ):
                await plugin.inject_voice_status(event, request)
                await plugin.inject_voice_status(event, request)
            self.assertIn(expected, request.system_prompt)
            self.assertEqual(request.system_prompt.count("【当前会话语音状态】"), 1)
            self.assertNotIn("tts_trigger_probability", extras)
            provider.get_audio.assert_not_awaited()

    async def test_response_hook_retains_plan_without_showing_protocol(self):
        plugin, event, _, _, extras, _ = fixture("")
        response = SimpleNamespace(
            completion_text=envelope("网址在这里", "先别着急，我陪你。")
        )
        await plugin.scrub_drafts(event, response)
        self.assertEqual(response.completion_text, "网址在这里\n先别着急，我陪你。")
        self.assertEqual(extras["_fy_voice_plan"]["voice"], "先别着急，我陪你。")

    async def test_proactive_sender_delegates_synthesis_once(self):
        raw = envelope("歌名：晴天", "吃过饭就休息会儿吧。")
        plugin, event, result, provider, extras, _ = fixture(
            raw, {"tts_enabled": False}, True
        )
        host = SimpleNamespace(
            config_service=SimpleNamespace(
                get_session_config=lambda _: {
                    "tts_settings": {"enable_tts": True},
                    "segmented_reply_settings": {},
                }
            ),
            session_service=SimpleNamespace(
                get_session_log_str=lambda *args: "Local test",
                parse_session_id=lambda _: ("qq", "FriendMessage", "local-test"),
            ),
            telemetry=None,
        )
        sender = sender_module.SenderService(host)

        async def decorate(_, components):
            result.chain = components
            await plugin.convert_faces(event)
            return result.chain, True

        sender.run_decorating_hooks = AsyncMock(side_effect=decorate)
        sender.send_chain = AsyncMock(return_value=True)
        with (
            patch.object(
                face_module.sp,
                "get_async",
                new=AsyncMock(return_value={"tts_enabled": False}),
            ),
            patch.object(
                sender_module, "dispatch_event_hook", new=AsyncMock(return_value=False)
            ),
        ):
            self.assertTrue(
                await sender.send_proactive_message(
                    event.unified_msg_origin, raw, event
                )
            )
        provider.get_audio.assert_awaited_once_with("吃过饭就休息会儿吧。")
        sender.send_chain.assert_awaited_once()
        self.assertTrue(extras["fy_proactive"])

    def test_parser_handles_duplicates_and_malformed_protocol(self):
        self.assertEqual(
            parse_voice_reply(envelope("你好!", "你好。")),
            {"text": "", "voice": "你好。"},
        )
        self.assertEqual(
            parse_voice_reply('<fy_reply>{"text":"已恢复的文字","voice":'),
            {"text": "已恢复的文字", "voice": ""},
        )
        recovered = parse_voice_reply("<fy_reply>{invalid}</fy_reply>")
        self.assertNotIn("fy_reply", recovered["text"])
        self.assertIsNone(parse_voice_reply("普通回复"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

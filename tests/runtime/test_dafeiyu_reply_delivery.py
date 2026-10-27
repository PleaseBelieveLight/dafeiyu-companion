"""Regression coverage for the installed decorate-to-delivery pipeline."""

import copy
import importlib
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


class ReplyDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_decorated_replies_reach_the_platform(self):
        """Deliver text once, including when synthesis fails or is disabled."""
        text = "Local delivery regression [旺柴]."
        for mode in (
            "disabled",
            "success",
            "failure",
            "forced",
            "voice_only",
            "voice_only_failure",
            "session_off",
            "random_skip",
        ):
            with self.subTest(mode=mode):
                config = copy.deepcopy(DEFAULT_CONFIG)
                config["provider_tts_settings"].update(
                    enable=mode != "disabled",
                    dual_output=True,
                    trigger_probability=1.0 if mode in {"success", "failure"} else 0.35,
                )
                config["content_safety"]["also_use_in_response"] = False
                tts = SimpleNamespace(get_audio=AsyncMock())
                tts.get_audio.return_value = "local-test-audio.wav"
                if mode in {"failure", "voice_only_failure"}:
                    tts.get_audio.side_effect = RuntimeError(
                        "Simulated synthesis failure"
                    )
                context = SimpleNamespace(
                    astrbot_config=config,
                    plugin_manager=SimpleNamespace(
                        context=SimpleNamespace(
                            get_using_tts_provider_async=AsyncMock(return_value=tts)
                        )
                    ),
                )
                result = MessageEventResult(
                    chain=[Plain(text)]
                ).set_result_content_type(ResultContentType.LLM_RESULT)
                event = MagicMock()
                event.unified_msg_origin = "qq:FriendMessage:local-test"
                event.plugins_name = set()
                event.get_result.return_value = result
                extras = {}
                if mode in {
                    "forced",
                    "voice_only",
                    "voice_only_failure",
                    "session_off",
                }:
                    extras["tts_trigger_probability"] = 1.0
                if mode.startswith("voice_only"):
                    extras["tts_dual_output"] = False
                event.get_extra.side_effect = extras.get
                event.get_platform_name.return_value = "aiocqhttp"
                event.get_message_type.return_value = MessageType.FRIEND_MESSAGE
                event.get_sender_id.return_value = "local-test"
                event.get_sender_name.return_value = "Local test"
                event._outline_chain.return_value = "Local test reply"
                event.send = AsyncMock()
                decorate = ResultDecorateStage()
                delivery = RespondStage()
                await decorate.initialize(context)
                await delivery.initialize(context)
                with (
                    patch(
                        "astrbot.core.pipeline.result_decorate.stage."
                        "SessionServiceManager.should_process_tts_request",
                        new=AsyncMock(return_value=mode != "session_off"),
                    ),
                    patch(
                        "astrbot.core.pipeline.result_decorate.stage.random.random",
                        return_value=0.99,
                    ),
                    patch(
                        "astrbot.core.pipeline.result_decorate.stage."
                        "star_handlers_registry.get_handlers_by_event_type",
                        return_value=[],
                    ),
                    patch(
                        "astrbot.core.pipeline.respond.stage.call_event_hook",
                        new=AsyncMock(return_value=False),
                    ),
                ):
                    async for _ in decorate.process(event):
                        pass
                    await delivery.process(event)
                sent = [call.args[0] for call in event.send.await_args_list]
                self.assertEqual(
                    [
                        comp.text
                        for message in sent
                        for comp in message.chain
                        if isinstance(comp, Plain)
                    ],
                    [] if mode == "voice_only" else [text],
                )
                audio = [
                    comp
                    for message in sent
                    for comp in message.chain
                    if isinstance(comp, Record)
                ]
                self.assertEqual(
                    len(audio), 1 if mode in {"success", "forced", "voice_only"} else 0
                )
                attempted = mode not in {"disabled", "session_off", "random_skip"}
                self.assertEqual(tts.get_audio.await_count, int(attempted))
                if attempted:
                    tts.get_audio.assert_awaited_once_with(
                        "Local delivery regression ."
                    )

    async def test_edge_provider_keeps_sdk_websocket_headers(self):
        """Keep the SDK's synthesis handshake consistent with its version token."""
        from edge_tts.constants import WSS_HEADERS

        before = dict(WSS_HEADERS)
        importlib.import_module("astrbot.core.provider.sources.edge_tts_source")
        self.assertEqual(WSS_HEADERS, before)


if __name__ == "__main__":
    unittest.main(verbosity=2)

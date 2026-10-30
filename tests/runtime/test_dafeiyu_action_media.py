"""Cover action pictures, capability filtering and accompanying speech."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.message.components import Image, Plain, Record
from tests.test_dafeiyu_voice_mode import envelope, face_module, fixture, temporary_text


class ActionMediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_uses_only_configured_and_permitted_media_tools(self):
        cases = [
            (True, False, True, True, {"generate_selfie", "generate_image"}),
            (
                True,
                True,
                True,
                True,
                {"generate_selfie", "generate_image", "generate_video"},
            ),
            (False, False, True, True, set()),
            (True, True, False, True, set()),
            (True, True, True, False, {"generate_selfie", "generate_image"}),
        ]
        for image_ready, video_ready, plugin_active, video_active, expected in cases:
            with self.subTest(
                settings=(image_ready, video_ready, plugin_active, video_active)
            ):
                plugin, event, _, _, _, _ = fixture("", {"tts_enabled": True})
                node = SimpleNamespace(
                    base_url="https://example.invalid", model="test", has_api_key=True
                )
                plugin.context.get_registered_star = MagicMock(
                    return_value=SimpleNamespace(
                        activated=plugin_active,
                        star_cls=SimpleNamespace(
                            plugin_config=SimpleNamespace(
                                providers=[node] if image_ready else [],
                                video_providers=[node] if video_ready else [],
                            )
                        ),
                    )
                )
                names = [
                    "generate_selfie",
                    "generate_image",
                    "generate_video",
                    "web_search",
                ]
                registry_tools = [
                    FunctionTool(
                        name=name,
                        description=name,
                        parameters={"type": "object", "properties": {}},
                        active=video_active if name == "generate_video" else True,
                    )
                    for name in names
                ]
                request = SimpleNamespace(
                    system_prompt="Existing persona.",
                    func_tool=ToolSet(registry_tools.copy()),
                )
                with patch.object(
                    face_module.sp,
                    "get_async",
                    new=AsyncMock(return_value={"tts_enabled": True}),
                ):
                    await plugin.inject_voice_status(event, request)
                    await plugin.inject_voice_status(event, request)
                self.assertEqual(
                    set(request.func_tool.names()), expected | {"web_search"}
                )
                self.assertEqual(len(registry_tools), 4)
                self.assertEqual(temporary_text(request).count("本轮可用媒体工具："), 1)
                self.assertIn("generate_selfie，action", request.system_prompt)
                self.assertIn("哒哒哒", request.system_prompt)
                self.assertIn("本轮状态：普通聊天自动语音已允许", temporary_text(request))

    async def test_hook_does_not_grant_missing_tools_or_voice_permission(self):
        plugin, event, _, _, _, _ = fixture("", {"tts_enabled": False})
        request = SimpleNamespace(system_prompt="Existing persona.", func_tool=None)
        with patch.object(
            face_module.sp,
            "get_async",
            new=AsyncMock(return_value={"tts_enabled": False}),
        ):
            await plugin.inject_voice_status(event, request)
        self.assertIsNone(request.func_tool)
        self.assertIn("本轮可用媒体工具：无。", temporary_text(request))
        self.assertTrue(
            temporary_text(request).endswith(
                "本次不允许语音，voice 必须留空；需要表达的话写在 text，图片工具仍可按需使用。"
            )
        )

    async def test_picture_is_preserved_with_speech_and_with_voice_off(self):
        for enabled in (True, False):
            with self.subTest(voice_enabled=enabled):
                session = {"tts_enabled": enabled}
                plugin, event, result, provider, _, _ = fixture(
                    envelope("", "哒哒哒，转一圈～"), session
                )
                picture = Image.fromURL("https://example.invalid/dance.png")
                result.chain.append(picture)
                with patch.object(
                    face_module.sp, "get_async", new=AsyncMock(return_value=session)
                ):
                    await plugin.convert_faces(event)
                    await plugin.convert_faces(event)
                self.assertIn(picture, result.chain)
                self.assertEqual(
                    sum(isinstance(c, Record) for c in result.chain), int(enabled)
                )
                self.assertEqual(
                    [c.text for c in result.chain if isinstance(c, Plain)],
                    [] if enabled else ["哒哒哒，转一圈～"],
                )
                self.assertEqual(provider.get_audio.await_count, int(enabled))


if __name__ == "__main__":
    unittest.main()

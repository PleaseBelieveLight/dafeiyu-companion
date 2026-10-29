"""Keep image inputs and explicit analysis requests intact when guiding chat style."""

import copy
import importlib
import unittest
from types import SimpleNamespace

from astrbot.core.agent.message import TextPart
from astrbot.core.message.components import Image, Plain, Reply
from astrbot.core.provider.entities import ProviderRequest

face_module = importlib.import_module("data.plugins.astrbot_plugin_face_convert.main")


class ImageReplyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plugin = face_module.FaceConvertPlugin.__new__(face_module.FaceConvertPlugin)

    def event(self, components=(), proactive=False):
        return SimpleNamespace(
            get_messages=lambda: list(components),
            get_extra=lambda key: proactive if key == "fy_proactive" else None,
        )

    async def test_direct_quoted_and_captioned_images_receive_guidance(self):
        image = Image.fromURL("https://example.invalid/meme.gif")
        caption = "<image_caption>点头表情，表达赞同。</image_caption>"
        cases = [
            ([image], [], []),
            ([], [image.url], []),
            ([Reply(id="quoted", chain=[image])], [], []),
            ([], [], [TextPart(text=caption)]),
            ([], [], [{"type": "text", "text": caption}]),
        ]
        for components, urls, parts in cases:
            with self.subTest(components=components, caption=bool(parts)):
                req = ProviderRequest(image_urls=urls, extra_user_content_parts=parts)
                await self.plugin.inject_image_reply_style(self.event(components), req)
                self.assertIn(face_module.IMAGE_REPLY_MARKER, req.system_prompt)

    async def test_explicit_analysis_preserves_all_request_content(self):
        req = ProviderRequest(
            prompt="请逐字读出截图中的文字，并解释右下角报错，不要漏掉数字。",
            image_urls=["https://example.invalid/screenshot.png"],
            contexts=[{"role": "user", "content": "正在排查安装问题"}],
            extra_user_content_parts=[TextPart(text="<image_caption>错误 123</image_caption>")],
            system_prompt="原有人格及工具约定",
        )
        before = copy.deepcopy(vars(req))
        await self.plugin.inject_image_reply_style(self.event(), req)
        self.assertTrue(req.system_prompt.startswith(before.pop("system_prompt")))
        self.assertIn("按要求提供完整且必要的信息", req.system_prompt)
        self.assertEqual(
            {key: value for key, value in vars(req).items() if key != "system_prompt"},
            before,
        )

    async def test_text_only_and_proactive_requests_keep_their_prompt(self):
        for components, urls, proactive in [
            ([Plain("你好")], [], False),
            ([Image.fromURL("https://example.invalid/meme.gif")], [], True),
            ([], ["https://example.invalid/selfie.png"], True),
        ]:
            with self.subTest(proactive=proactive, urls=urls):
                req = ProviderRequest(
                    image_urls=urls,
                    system_prompt="保持原有语气",
                    contexts=[{"role": "user", "content": "之前发过一张图片"}],
                )
                await self.plugin.inject_image_reply_style(
                    self.event(components, proactive), req
                )
                self.assertEqual(req.system_prompt, "保持原有语气")

    async def test_repeated_hook_does_not_duplicate_instructions(self):
        req = ProviderRequest(image_urls=["https://example.invalid/meme.gif"])
        for _ in range(2):
            await self.plugin.inject_image_reply_style(self.event(), req)
        self.assertEqual(req.system_prompt.count(face_module.IMAGE_REPLY_MARKER), 1)


if __name__ == "__main__":
    unittest.main()

"""Keep image inputs and explicit analysis requests intact when guiding chat style."""

import copy
import importlib
import json
import unittest
from types import SimpleNamespace

from astrbot.core.agent.message import (
    ImageURLPart,
    TextPart,
    bind_checkpoint_messages,
    dump_messages_with_checkpoints,
)
from astrbot.core.message.components import Image, Plain, Reply
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.utils.image_reply import IMAGE_TURN_MARKER, image_turn_reminder

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
            ([], [], [ImageURLPart(image_url={"url": image.url})]),
            ([], [], [{"type": "image_url", "image_url": {"url": image.url}}]),
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
        self.assertIn("明确要识图、读字、解释或分析时按问题认真展开", req.system_prompt)
        # Only a provider-facing temporary hint is added; original content survives.
        self.assertEqual(
            {
                key: (
                    [part for part in value if not getattr(part, "_no_save", False)]
                    if key == "extra_user_content_parts"
                    else value
                )
                for key, value in vars(req).items()
                if key != "system_prompt"
            },
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
        self.assertEqual(len(req.extra_user_content_parts), 1)

    async def test_runtime_hint_reaches_provider_but_is_not_saved_as_user_speech(self):
        caption = TextPart(text="<image_caption>哭脸</image_caption>")
        req = ProviderRequest(
            prompt="[对方发来一张图片或表情包]",
            extra_user_content_parts=[caption],
            contexts=[{"role": "user", "content": "上班好累啊"}],
        )
        await self.plugin.inject_image_reply_style(self.event(), req)
        assembled = await req.assemble_context()
        self.assertIn(IMAGE_TURN_MARKER, str(assembled))
        self.assertIn("上班好累啊", str(assembled))
        persisted = dump_messages_with_checkpoints(bind_checkpoint_messages([assembled]))
        self.assertNotIn(IMAGE_TURN_MARKER, str(persisted))
        self.assertEqual(
            [part["text"] for part in persisted[0]["content"]],
            [req.prompt, caption.text],
        )

    async def test_same_image_keeps_distinct_context_and_does_not_force_a_meme_intent(self):
        reminders = []
        for topic in ("上班好累啊", "哈哈，刚才逗你的", "帮我排查这个截图的报错"):
            req = ProviderRequest(
                prompt="看看这个",
                image_urls=["https://example.invalid/same-image.png"],
                contexts=[{"role": "user", "content": topic}],
            )
            await self.plugin.inject_image_reply_style(self.event(), req)
            reminder = req.extra_user_content_parts[-1]
            self.assertTrue(reminder._no_save)
            self.assertIn(topic, reminder.text)
            payload = json.loads(reminder.text.split("\n", 1)[1].rsplit("\n", 1)[0])
            self.assertEqual(payload["recent_dialogue"], [{"role": "user", "text": topic}])
            self.assertEqual(payload["last_user_text"], topic)
            self.assertEqual(req.prompt, "看看这个")
            reminders.append(reminder.text)
        self.assertEqual(len(set(reminders)), 3)

    async def test_retry_refreshes_only_owned_hint_and_keeps_other_temporary_parts(self):
        unrelated = TextPart(text="<system_reminder>其它插件提示</system_reminder>").mark_as_temp()
        old = {"type": "text", "text": IMAGE_TURN_MARKER + "旧内容", "_no_save": True}
        req = ProviderRequest(
            image_urls=["https://example.invalid/meme.gif"],
            system_prompt=face_module.IMAGE_REPLY_MARKER + "旧版规则",
            extra_user_content_parts=[old, unrelated],
        )
        await self.plugin.inject_image_reply_style(self.event(), req)
        req.contexts = [{"role": "user", "content": "现在换个话题"}]
        await self.plugin.inject_image_reply_style(self.event(), req)
        self.assertIs(req.extra_user_content_parts[0], unrelated)
        self.assertEqual(len(req.extra_user_content_parts), 2)
        self.assertIn("现在换个话题", req.extra_user_content_parts[-1].text)
        self.assertNotIn("旧版规则", req.system_prompt)


class ImageDialogueTests(unittest.TestCase):
    def test_context_extracts_dialogue_without_paths_runtime_notes_or_tool_output(self):
        req = ProviderRequest(
            prompt="[对方发来一张图片或表情包]",
            contexts=[
                {"role": "system", "content": "不属于对话"},
                {"role": "user", "content": "今天上班好累"},
                {
                    "role": "assistant",
                    "content": '<fy_reply>{"text":"先歇会儿。","voice":""}</fy_reply>',
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "[对方发来一张图片或表情包]"},
                        {"type": "text", "text": "<system_reminder>运行时间</system_reminder>"},
                        {"type": "text", "text": "[Image 1: original path private-file.png]"},
                        {"type": "text", "text": "不保存的摘录", "_no_save": True},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                    ],
                },
                {"role": "tool", "content": "工具返回"},
            ],
        )
        text = image_turn_reminder(req)
        for expected in ("今天上班好累", "先歇会儿"):
            self.assertIn(expected, text)
        for omitted in ("不属于对话", "运行时间", "private-file", "base64", "不保存的摘录", "工具返回"):
            self.assertNotIn(omitted, text)

    def test_consecutive_images_keep_last_user_topic_and_stay_bounded(self):
        history = [{"role": "user", "content": "上班好累"}]
        for _ in range(8):
            history.extend([
                {"role": "user", "content": "[对方发来一张图片或表情包]"},
                {"role": "assistant", "content": "旧的解说" * 300},
            ])
        req = ProviderRequest(conversation=SimpleNamespace(history=json.dumps(history)))
        text = image_turn_reminder(req)
        self.assertIn("上班好累", text)
        payload = json.loads(text.split("\n", 1)[1].rsplit("\n", 1)[0])
        self.assertEqual(payload["last_user_text"], "上班好累")
        self.assertLess(len(text), 1700)

    def test_missing_or_malformed_history_still_allows_natural_image_observation(self):
        for history in ("not JSON", "null", '{}', None):
            req = ProviderRequest(conversation=SimpleNamespace(history=history))
            text = image_turn_reminder(req)
            payload = json.loads(text.split("\n", 1)[1].rsplit("\n", 1)[0])
            self.assertEqual(
                payload, {"recent_dialogue": [], "last_user_text": "", "current_text": ""}
            )


if __name__ == "__main__":
    unittest.main()

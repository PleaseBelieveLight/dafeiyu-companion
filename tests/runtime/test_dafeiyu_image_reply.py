"""Keep image inputs and explicit analysis requests intact when guiding chat style."""

import copy
import importlib
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
from astrbot.core.utils.image_reply import IMAGE_TURN_MARKER, IMAGE_TURN_REMINDER

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

    async def test_bare_meme_treats_its_caption_as_picture_evidence(self):
        caption = TextPart(text="<image_caption>卡通人物举着‘没钱了’的牌子。</image_caption>")
        req = ProviderRequest(prompt="[对方发来一张图片或表情包]", extra_user_content_parts=[caption])
        await self.plugin.inject_image_reply_style(self.event(), req)
        self.assertIn("可以顺梗调侃或试探", req.system_prompt)
        self.assertIn("不当作用户已确认的现实情况或长期记忆", req.system_prompt)
        self.assertIs(req.extra_user_content_parts[0], caption)

    async def test_company_turn_adds_a_temporary_lead_without_a_topic_menu(self):
        text = "不知道说啥，只能找你解解闷了"
        req = ProviderRequest(prompt=text, system_prompt="原有人格")
        await self.plugin.inject_company_style(self.event([Plain(text)]), req)
        self.assertEqual(req.system_prompt, "原有人格")
        assembled = await req.assemble_context()
        self.assertIn("自己先抛一个具体", str(assembled))
        saved = dump_messages_with_checkpoints(bind_checkpoint_messages([assembled]))
        self.assertEqual([part["text"] for part in saved[0]["content"]], [text])

        await self.plugin.inject_company_style(self.event([Plain("帮我找个解闷的游戏")]), req)
        self.assertEqual(req.extra_user_content_parts, [])

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
        self.assertNotIn("上班好累啊", str(assembled))
        self.assertEqual(req.contexts, [{"role": "user", "content": "上班好累啊"}])
        persisted = dump_messages_with_checkpoints(bind_checkpoint_messages([assembled]))
        self.assertNotIn(IMAGE_TURN_MARKER, str(persisted))
        self.assertEqual(
            [part["text"] for part in persisted[0]["content"]],
            [req.prompt, caption.text],
        )

    async def test_same_image_preserves_context_without_repeating_the_previous_topic(self):
        reminders = []
        for topic in ("上班好累啊", "哈哈，刚才逗你的", "帮我排查这个截图的报错"):
            req = ProviderRequest(
                prompt="看看这个",
                image_urls=["https://example.invalid/same-image.png"],
                contexts=[{"role": "user", "content": topic}],
            )
            original = copy.deepcopy(req.contexts)
            await self.plugin.inject_image_reply_style(self.event(), req)
            reminder = req.extra_user_content_parts[-1]
            self.assertTrue(reminder._no_save)
            self.assertNotIn(topic, reminder.text)
            self.assertEqual(req.contexts, original)
            assembled = await req.assemble_context()
            self.assertEqual(str([*req.contexts, assembled]).count(topic), 1)
            self.assertEqual(str(assembled).count(req.prompt), 1)
            self.assertEqual(req.prompt, "看看这个")
            reminders.append(reminder.text)
        self.assertEqual(set(reminders), {IMAGE_TURN_REMINDER})

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
        self.assertEqual(req.extra_user_content_parts[-1].text, IMAGE_TURN_REMINDER)
        self.assertNotIn("旧内容", str(req.extra_user_content_parts))
        self.assertEqual(req.contexts, [{"role": "user", "content": "现在换个话题"}])
        self.assertNotIn("旧版规则", req.system_prompt)

    async def test_consecutive_images_do_not_promote_the_last_text_topic(self):
        history = [
            {"role": "user", "content": "快吃午饭了"},
            {"role": "assistant", "content": "你打算吃什么？"},
        ]
        for caption in ("恋人旺铺招租", "猫咪打游戏抓狂", "小狗困得睁不开眼"):
            req = ProviderRequest(
                prompt="[对方发来一张图片或表情包]",
                contexts=copy.deepcopy(history),
                extra_user_content_parts=[TextPart(text=f"<image_caption>{caption}</image_caption>")],
            )
            await self.plugin.inject_image_reply_style(self.event(), req)
            current = await req.assemble_context()
            self.assertEqual(req.contexts, history)
            self.assertEqual(str([*req.contexts, current]).count("快吃午饭了"), 1)
            self.assertEqual(str(current).count(caption), 1)
            saved = dump_messages_with_checkpoints(bind_checkpoint_messages([current]))
            self.assertNotIn(IMAGE_TURN_MARKER, str(saved))
            history.extend([*saved, {"role": "assistant", "content": "固定测试回复"}])

    async def test_text_topic_switch_and_return_keep_history_without_image_hints(self):
        history = [{"role": "user", "content": "我午饭准备吃面"}]
        for text in ("我准备睡觉了", "最近想谈恋爱了", "那你呢？", "回到刚才午饭那个事"):
            req = ProviderRequest(prompt=text, contexts=copy.deepcopy(history), system_prompt="原有人格")
            await self.plugin.inject_image_reply_style(self.event([Plain(text)]), req)
            self.assertEqual(req.contexts, history)
            self.assertEqual(req.system_prompt, "原有人格")
            self.assertEqual(req.extra_user_content_parts, [])
            current = await req.assemble_context()
            self.assertEqual(str(current).count(text), 1)
            saved = dump_messages_with_checkpoints(bind_checkpoint_messages([current]))
            history.extend([*saved, {"role": "assistant", "content": "固定测试回复"}])


if __name__ == "__main__":
    unittest.main()

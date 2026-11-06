"""Exercise the patched Stealer entry points without downloading or classifying images."""

import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from astrbot.api.message_components import Image, Plain, Reply
from data.plugins.astrbot_plugin_stealer.core.commands.command_handler import CommandHandler
from data.plugins.astrbot_plugin_stealer.core.events.event_handler import EventHandler
from data.plugins.astrbot_plugin_stealer.main import Main


class MemeCaptureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.image = Image.fromURL("https://example.invalid/sticker.gif")
        self.plugin = SimpleNamespace(
            _emoji_sender_engine=Mock(),
            auto_meme_cancel_on_new_message=False,
            steal_meme=False,
            _steal_sticker_impl=Mock(side_effect=self.steal_result),
        )
        self.handler = EventHandler.__new__(EventHandler)
        self.handler.plugin = self.plugin
        self.handler._cleaned = False
        self.handler._force_capture_windows = {}
        self.handler._force_capture_lock = threading.RLock()
        self.handler._extract_store_emoji_urls = Mock(return_value=[])
        self.handler._handle_force_capture = AsyncMock(side_effect=self.capture_result)
        self.plugin.event_handler = self.handler
        self.plugin.begin_force_capture = self.handler.begin_force_capture
        self.plugin.get_force_capture_entry = self.handler.get_force_capture_entry
        self.plugin.consume_force_capture = self.handler.consume_force_capture

    @staticmethod
    async def steal_result(event, image_ref):
        yield "已入库"

    async def capture_result(self, event, plugin, images, store_urls):
        self.assertIsNone(self.handler.get_force_capture_entry(event))
        await event.send("已收录")

    @staticmethod
    def event(*parts, sender="1001", private=True):
        stopped = Mock()
        return SimpleNamespace(
            unified_msg_origin="qq:FriendMessage:1001",
            get_sender_id=lambda: sender,
            get_messages=lambda: list(parts),
            is_private_chat=lambda: private,
            stop_event=stopped,
            is_stopped=lambda: stopped.called,
            plain_result=lambda text: text,
            send=AsyncMock(),
        )

    async def test_direct_and_quoted_requests_dispatch_to_the_capture_workflow(self):
        for part, text in (
            (self.image, "帮我偷一下这张图"),
            (Reply(id="quoted", chain=[self.image]), "帮我偷一下这张图"),
            (Reply(id="quoted", chain=[self.image]), "我的意思是偷这个表情包"),
        ):
            with self.subTest(text=text):
                event = self.event(part, Plain(text=text))
                await Main.on_message(self.plugin, event)
                self.plugin._steal_sticker_impl.assert_called_with(event, self.image.file)
                event.send.assert_awaited_once_with("已入库")
                self.assertTrue(event.is_stopped())

    async def test_ordinary_images_questions_and_group_requests_do_not_capture(self):
        for event in (
            self.event(self.image),
            self.event(self.image, Plain(text="怎么偷这个表情包？")),
            self.event(self.image, Plain(text="别偷这张图")),
            self.event(self.image, Plain(text="帮我偷一下这张图"), private=False),
        ):
            await Main.on_message(self.plugin, event)
            event.send.assert_not_called()
            self.assertFalse(event.is_stopped())
        self.plugin._steal_sticker_impl.assert_not_called()
        self.handler._handle_force_capture.assert_not_awaited()

    async def test_multiple_images_ask_for_one_without_saving_any(self):
        event = self.event(self.image, self.image, Plain(text="帮我偷一下这张图"))
        await Main.on_message(self.plugin, event)
        self.plugin._steal_sticker_impl.assert_not_called()
        self.assertIn("单独", event.send.await_args.args[0])

    async def test_command_window_only_consumes_one_image_from_the_requesting_sender(self):
        command = self.event(Plain(text="/meme 偷"))
        replies = [reply async for reply in CommandHandler.capture(SimpleNamespace(plugin=self.plugin), command)]
        self.assertIn("30 秒", replies[0])

        await Main.on_message(self.plugin, self.event(self.image, sender="1002"))
        await Main.on_message(self.plugin, self.event(Plain(text="我找一下")))
        self.handler._handle_force_capture.assert_not_awaited()

        image = self.event(self.image)
        await Main.on_message(self.plugin, image)
        await Main.on_message(self.plugin, self.event(self.image))
        self.handler._handle_force_capture.assert_awaited_once_with(image, self.plugin, [self.image], [])
        image.send.assert_awaited_once_with("已收录")
        self.assertTrue(image.is_stopped())

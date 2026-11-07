"""Exercise the patched Stealer entry points without downloading or classifying images."""

import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image as PILImage

from astrbot.api.message_components import File, Image, Plain, Reply
from data.plugins.astrbot_plugin_stealer.core.commands.command_handler import CommandHandler
from data.plugins.astrbot_plugin_stealer.core.events.event_handler import EventHandler
from data.plugins.astrbot_plugin_stealer.core.events.image_download_service import ImageDownloadService
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

    async def test_command_window_captures_remote_gif_files_without_ai_fallthrough(self):
        command = self.event(Plain(text="/meme 偷"))
        self.plugin.begin_force_capture(command, 30)
        attachment = File(name="sticker.GIF", url="https://example.invalid/download?id=1")

        other_sender = self.event(attachment, sender="1002")
        await Main.on_message(self.plugin, other_sender)
        self.assertFalse(other_sender.is_stopped())
        self.handler._handle_force_capture.assert_not_awaited()

        event = self.event(attachment)
        await Main.on_message(self.plugin, event)
        self.assertTrue(event.is_stopped())
        self.assertIsNone(self.plugin.get_force_capture_entry(event))
        args = self.handler._handle_force_capture.await_args.args
        self.assertEqual(args[:2], (event, self.plugin))
        self.assertEqual(args[2][0].file, attachment.url)
        event.send.assert_awaited_once_with("已收录")
        self.plugin._steal_sticker_impl.assert_not_called()

        following = self.event(attachment)
        await Main.on_message(self.plugin, following)
        self.assertFalse(following.is_stopped())
        self.handler._handle_force_capture.assert_awaited_once()

    async def test_ordinary_and_expired_file_messages_do_not_capture(self):
        attachment = File(name="sticker.gif", url="https://example.invalid/download")
        ordinary = self.event(attachment)
        await Main.on_message(self.plugin, ordinary)
        self.assertFalse(ordinary.is_stopped())

        self.plugin.begin_force_capture(ordinary, 30)
        document = self.event(File(name="document.pdf", url="https://example.invalid/document"))
        await Main.on_message(self.plugin, document)
        self.assertFalse(document.is_stopped())
        self.assertIsNotNone(self.plugin.get_force_capture_entry(document))

        with patch("time.time", return_value=time.time() + 31):
            expired = self.event(attachment)
            await Main.on_message(self.plugin, expired)
        self.assertFalse(expired.is_stopped())
        self.handler._handle_force_capture.assert_not_awaited()

    async def test_unreadable_gif_file_reports_failure_without_ai_fallthrough(self):
        event = self.event(File(name="sticker.gif"))
        self.plugin.begin_force_capture(event, 30)
        await Main.on_message(self.plugin, event)
        self.assertTrue(event.is_stopped())
        self.assertIsNone(self.plugin.get_force_capture_entry(event))
        self.assertIn("收录失败", event.send.await_args.args[0])
        self.handler._handle_force_capture.assert_not_awaited()

    async def test_private_direct_and_quoted_gif_files_support_explicit_capture(self):
        attachment = File(name="sticker.gif", url="https://example.invalid/download")
        for part in (attachment, Reply(id="quoted", chain=[attachment])):
            with self.subTest(part=type(part).__name__):
                event = self.event(part, Plain(text="帮我偷一下这张图"))
                await Main.on_message(self.plugin, event)
                self.assertTrue(event.is_stopped())
                self.plugin._steal_sticker_impl.assert_called_with(event, attachment.url)
                event.send.assert_awaited_once_with("已入库")

    async def test_local_gif_file_keeps_all_frames_and_preserves_the_original(self):
        with tempfile.TemporaryDirectory() as folder:
            original = Path(folder) / "sticker.gif"
            frames = [PILImage.new("RGB", (2, 2), color) for color in ("red", "blue")]
            frames[0].save(original, save_all=True, append_images=frames[1:], duration=100, loop=0)
            original_bytes = original.read_bytes()
            self.handler._image_download_service = ImageDownloadService()
            self.handler._handle_force_capture = EventHandler._handle_force_capture.__get__(self.handler)

            async def process_image(event, temp_path, **kwargs):
                self.assertTrue(event.is_stopped())
                self.assertIsNone(self.plugin.get_force_capture_entry(event))
                self.assertTrue(kwargs["is_temp"])
                self.assertNotEqual(Path(temp_path), original)
                self.assertEqual(Path(temp_path).read_bytes(), original_bytes)
                with PILImage.open(temp_path) as image:
                    self.assertEqual(image.n_frames, 2)
                Path(temp_path).unlink()
                return True, {"saved.gif": {"category": "happy"}}

            self.plugin._process_image = AsyncMock(side_effect=process_image)
            self.plugin.index_manager = SimpleNamespace(save_index=AsyncMock())
            event = self.event(File(name="sticker.gif", file=str(original)))
            self.plugin.begin_force_capture(event, 30)
            await Main.on_message(self.plugin, event)
            self.plugin._process_image.assert_awaited_once()
            self.plugin.index_manager.save_index.assert_awaited_once()
            self.assertEqual(original.read_bytes(), original_bytes)
            self.assertIn("已收录", event.send.await_args.args[0].chain[0].text)

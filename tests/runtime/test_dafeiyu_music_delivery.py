"""Exercise music delivery with real plugin hooks and mocked QQ/network boundaries."""

import asyncio
import hashlib
import importlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from astrbot.core.message.components import Record
from astrbot.core.message.message_event_result import MessageEventResult
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

sender_module = importlib.import_module("data.plugins.astrbot_plugin_music.core.sender")
delivery = importlib.import_module("data.plugins.astrbot_plugin_music.core.local_delivery")
cache_module = importlib.import_module("data.plugins.astrbot_plugin_music.core.song_cache")
downloader_module = importlib.import_module("data.plugins.astrbot_plugin_music.core.downloader")
Song = importlib.import_module("data.plugins.astrbot_plugin_music.core.model").Song


def fixture(path: Path):
    sender = sender_module.MusicSender.__new__(sender_module.MusicSender)
    sender.cfg = SimpleNamespace(
        real_send_modes=["record_local", "text"], record_unsupported=[], file_unsupported=[],
        recall_select=False, enable_comments=False, enable_lyrics=False,
    )
    sender.downloader = SimpleNamespace(download_song=AsyncMock(return_value=path))
    sender.clear_selection_context = MagicMock()
    sender._recall_selection_message = AsyncMock()
    sender.send_comment = AsyncMock()
    sender.send_lyrics = AsyncMock()
    event = MagicMock(spec=AiocqhttpMessageEvent)
    event.get_platform_name.return_value = "aiocqhttp"
    event.is_private_chat.return_value = True
    event.get_sender_id.return_value = "test-user"
    event.get_group_id.return_value = "test-group"
    event.message_obj = SimpleNamespace(raw_message={"self_id": "test-bot"})
    event.bot = SimpleNamespace(api=SimpleNamespace(
        call_action=AsyncMock(return_value={"message_id": "test-message"})
    ))
    event.plain_result.side_effect = lambda text: MessageEventResult().message(text)
    event.chain_result.side_effect = lambda chain: MessageEventResult(chain=chain)
    event.send = AsyncMock()
    song = Song(id="test-song", name="测试歌曲", audio_url="https://music.example/audio",
                cover_url="https://music.example/cover")
    player = SimpleNamespace(
        platform=SimpleNamespace(name="netease"), fetch_extra=AsyncMock(return_value=song)
    )
    return sender, event, player, song


class MusicDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_audio_is_a_small_file_uri_for_private_and_group_messages(self):
        """A full song never enters Record's expanding WAV/base64 conversion."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "带 空格的歌曲.mp3"
            path.write_bytes(b"ID3-test")
            for private in (True, False):
                with self.subTest(private=private):
                    sender, event, player, song = fixture(path)
                    event.is_private_chat.return_value = private
                    with patch.object(Record, "convert_to_base64", new=AsyncMock()) as convert:
                        await sender.send_song(event, player, song)
                    convert.assert_not_awaited()
                    action = event.bot.api.call_action.await_args
                    self.assertEqual(action.args[0], "send_private_msg" if private else "send_group_msg")
                    self.assertEqual(action.kwargs["message"], [
                        {"type": "record", "data": {"file": path.resolve().as_uri()}}
                    ])
                    self.assertEqual(action.kwargs["self_id"], "test-bot")
                    self.assertEqual(action.kwargs["user_id" if private else "group_id"],
                                     "test-user" if private else "test-group")
                    self.assertLess(len(json.dumps(action.kwargs)), 2048)
                    sender.downloader.download_song.assert_awaited_once_with(
                        song.audio_url, cache_key="netease::test-song"
                    )
                    self.assertIn("正在准备", event.send.await_args_list[0].args[0].chain[0].text)

    async def test_timed_out_voice_falls_back_without_retrying_audio(self):
        sender, event, player, song = fixture(Path("test.mp3"))
        sender.cfg.real_send_modes = ["record_local", "record_link", "text"]
        cancelled = asyncio.Event()

        async def hung(*args, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        event.bot.api.call_action.side_effect = hung
        sender._send_record_link = AsyncMock(return_value=True)
        with patch.object(delivery, "SEND_TIMEOUT", 0.02):
            await asyncio.wait_for(sender.send_song(event, player, song), timeout=1)
        self.assertTrue(cancelled.is_set())
        event.bot.api.call_action.assert_awaited_once()
        sender._send_record_link.assert_not_awaited()
        texts = [call.args[0].chain[0].text for call in event.send.await_args_list]
        self.assertEqual(len(texts), 2)
        self.assertIn("发送超时", texts[-1])
        self.assertIn(song.audio_url, texts[-1])

    async def test_download_failure_falls_back_to_link(self):
        sender, event, player, song = fixture(Path("test.mp3"))
        sender.downloader.download_song.return_value = None
        await sender.send_song(event, player, song)
        event.bot.api.call_action.assert_not_awaited()
        self.assertIn(song.audio_url, event.send.await_args.args[0].chain[0].text)
        self.assertIn("发送失败", event.send.await_args.args[0].chain[0].text)

    async def test_other_platforms_keep_record_component_delivery(self):
        sender, _, player, song = fixture(Path("test.mp3"))
        event = SimpleNamespace(
            send=AsyncMock(), chain_result=lambda chain: MessageEventResult(chain=chain)
        )
        self.assertTrue(await sender._send_record_local(event, player, song))
        self.assertIsInstance(event.send.await_args.args[0].chain[0], Record)

    async def test_optional_recall_cannot_delay_success_for_three_minutes(self):
        sender, event, player, song = fixture(Path("test.mp3"))
        sender.cfg.recall_select = True
        sender.cfg.enable_comments = True

        async def hung(*args):
            await asyncio.Event().wait()

        sender._recall_selection_message.side_effect = hung
        with patch.object(delivery, "OPTIONAL_TIMEOUT", 0.02):
            await asyncio.wait_for(sender.send_song(event, player, song), timeout=1)
        sender.send_comment.assert_awaited_once()
        self.assertEqual(event.send.await_count, 1)  # Only the preparation text.

    async def test_lookup_timeout_returns_an_explicit_failure(self):
        sender, event, player, song = fixture(Path("test.mp3"))
        song.audio_url = None

        async def hung(*args):
            await asyncio.Event().wait()

        player.fetch_extra.side_effect = hung
        with patch.object(delivery, "EXTRA_TIMEOUT", 0.02):
            await asyncio.wait_for(sender.send_song(event, player, song), timeout=1)
        self.assertIn("音频获取失败", event.send.await_args.args[0].chain[0].text)
        sender.downloader.download_song.assert_not_awaited()


def response_for(body=b"ID3-audio", *, status=200, content_type="audio/mpeg"):
    async def chunks(size):
        yield body

    response = MagicMock()
    response.status = status
    response.headers = {"Content-Type": content_type}
    response.content_length = len(body)
    response.content.iter_chunked.side_effect = chunks
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=False)
    return response


class SongCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.response = response_for()
        self.session = SimpleNamespace(get=MagicMock(return_value=self.response))
        self.cache = cache_module.SongCache(self.folder, self.session)

    async def test_same_platform_id_reuses_audio_even_when_signed_url_changes(self):
        paths = await asyncio.gather(
            self.cache.download("https://music.example/first", "netease:song-a"),
            self.cache.download("https://music.example/renewed", "netease:song-a"),
        )
        self.assertEqual(paths[0], paths[1])
        self.assertEqual(paths[0].read_bytes(), b"ID3-audio")
        self.session.get.assert_called_once()
        timeout = self.session.get.call_args.kwargs["timeout"]
        self.assertEqual(timeout.total, 20)
        other = await self.cache.download("https://music.example/other", "qq:song-a")
        self.assertNotEqual(other, paths[0])

    async def test_expired_and_empty_files_are_refetched_but_legacy_cache_is_preserved(self):
        key = "netease:song-a"
        target = self.folder / (hashlib.sha256(key.encode()).hexdigest() + ".mp3")
        target.write_bytes(b"old")
        old = time.time() - cache_module.CACHE_TTL - 1
        os.utime(target, (old, old))
        legacy = self.folder / "existing-legacy.mp3"
        legacy.write_bytes(b"keep")
        self.assertEqual(await self.cache.download("https://music.example/audio", key), target)
        self.assertEqual(target.read_bytes(), b"ID3-audio")
        target.write_bytes(b"")
        await self.cache.download("https://music.example/audio", key)
        self.assertEqual(self.session.get.call_count, 2)
        self.assertEqual(legacy.read_bytes(), b"keep")

    async def test_capacity_evicts_oldest_managed_files(self):
        first = self.folder / ("a" * 64 + ".mp3")
        second = self.folder / ("b" * 64 + ".mp3")
        first.write_bytes(b"x" * 12)
        second.write_bytes(b"y" * 12)
        older = time.time() - 60
        os.utime(first, (older, older))
        with patch.object(cache_module, "CACHE_MAX_BYTES", 30):
            target = await self.cache.download("https://music.example/audio")
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())
        self.assertLessEqual(sum(p.stat().st_size for p in self.folder.glob("*.mp3")), 30)
        self.assertTrue(target.is_file())

    async def test_errors_empty_documents_and_oversize_streams_are_not_cached(self):
        for response in (
            response_for(status=403), response_for(b""),
            response_for(b"<html>error</html>", content_type="text/html"),
            response_for(b"too-large"),
        ):
            with self.subTest(response=response):
                self.session.get.return_value = response
                with patch.object(cache_module, "SONG_MAX_BYTES", 8):
                    self.assertIsNone(await self.cache.download("https://music.example/audio"))
                self.assertEqual(list(self.folder.iterdir()), [])
        self.response.content_length = None
        self.session.get.return_value = self.response
        with patch.object(cache_module, "SONG_MAX_BYTES", 4):
            self.assertIsNone(await self.cache.download("https://music.example/chunked"))
        self.assertEqual(list(self.folder.iterdir()), [])

    async def test_cancelled_download_removes_partial_file_and_allows_retry(self):
        async def cancelled(size):
            yield b"ID3"
            raise asyncio.CancelledError

        self.response.content.iter_chunked.side_effect = cancelled
        with self.assertRaises(asyncio.CancelledError):
            await self.cache.download("https://music.example/audio")
        self.assertEqual(list(self.folder.iterdir()), [])
        self.session.get.return_value = response_for()
        self.assertIsNotNone(await self.cache.download("https://music.example/audio"))

    async def test_installed_downloader_uses_cache_hook(self):
        downloader = downloader_module.Downloader.__new__(downloader_module.Downloader)
        downloader.songs_dir = self.folder
        downloader.session = self.session
        first = await downloader.download_song("https://music.example/audio", cache_key="song-a")
        second = await downloader.download_song("https://music.example/audio", cache_key="song-a")
        self.assertEqual(first, second)
        self.session.get.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)

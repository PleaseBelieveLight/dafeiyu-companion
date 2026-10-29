"""Bound music delivery and pass local audio directly to the same-host NapCat."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from astrbot.api import logger
from astrbot.core.message.components import Record
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent

    from .model import Song
    from .platform import BaseMusicPlayer
    from .sender import MusicSender

SEND_TIMEOUT = 30
EXTRA_TIMEOUT = 10
MESSAGE_TIMEOUT = 5
OPTIONAL_TIMEOUT = 3


async def _notify(event: AstrMessageEvent, text: str) -> bool:
    try:
        async with asyncio.timeout(MESSAGE_TIMEOUT):
            await event.send(event.plain_result(text))
        return True
    except Exception as exc:
        logger.warning("Music notification failed (%s)", type(exc).__name__)
        return False


async def send_local_record(
    sender: MusicSender, event: AstrMessageEvent, player: BaseMusicPlayer, song: Song
) -> bool:
    if not song.audio_url:
        return False
    cache_key = f"{player.platform.name}:{song.source or ''}:{song.id}"
    file_path = await sender.downloader.download_song(song.audio_url, cache_key=cache_key)
    if not file_path:
        return False
    if isinstance(event, AiocqhttpMessageEvent):
        # NapCat shares this Windows filesystem. Avoid Record's WAV/base64 expansion.
        payload = {"message": [{"type": "record", "data": {"file": file_path.resolve().as_uri()}}]}
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if isinstance(raw, dict) and raw.get("self_id"):
            payload["self_id"] = raw["self_id"]
        await sender.send_msg(event, payload)
    else:
        seg = Record.fromFileSystem(str(file_path.resolve()))
        await event.send(event.chain_result([seg]))
    return True


async def send_song(
    sender: MusicSender, event: AstrMessageEvent, player: BaseMusicPlayer, song: Song,
    modes: list[str] | None = None,
) -> None:
    target_modes = list(modes if modes is not None else sender.cfg.real_send_modes)
    await _notify(event, f"正在准备《{song.name or song.title or '歌曲'}》…")
    try:
        if not song.audio_url or not song.cover_url:
            async with asyncio.timeout(EXTRA_TIMEOUT):
                song = await player.fetch_extra(song)
    except Exception as exc:
        logger.warning("Music audio lookup failed (%s)", type(exc).__name__)

    needs_audio = any(mode not in {"card", "ark_card"} for mode in target_modes)
    if needs_audio and not song.audio_url and target_modes[0] not in {"card", "ark_card"}:
        await _notify(event, f"《{song.name}》音频获取失败，请稍后重试。")
        return

    sent = False
    failed = False
    timed_out = False
    for mode in target_modes:
        if timed_out and mode != "text":
            continue  # A timed-out remote send may still complete; never retry its audio.
        if not sender._is_mode_supported(mode, event):
            continue
        deliver = sender._get_sender(mode)
        if deliver is None:
            continue
        try:
            if mode == "text" and failed and song.audio_url:
                reason = "歌曲发送超时" if timed_out else "歌曲发送失败"
                sent = await _notify(event, f"{reason}，先给你播放链接：\n{song.audio_url}")
            else:
                async with asyncio.timeout(MESSAGE_TIMEOUT if mode == "text" else SEND_TIMEOUT):
                    sent = bool(await deliver(event, player, song))
        except TimeoutError:
            timed_out = True
            logger.warning("Music mode %s exceeded its delivery deadline", mode)
        except Exception as exc:
            logger.warning("Music mode %s failed (%s)", mode, type(exc).__name__)
        if sent:
            break
        failed = True

    if not sent:
        await _notify(event, "歌曲发送失败，请稍后重试。")
        sender.clear_selection_context(event)
        return

    sender.clear_selection_context(event)
    # Recall, comments and lyrics must not hold a completed request for 180 seconds.
    optional = []
    if sender.cfg.recall_select:
        optional.append(lambda: sender._recall_selection_message(event))
    if sender.cfg.enable_comments:
        optional.append(lambda: sender.send_comment(event, player, song))
    if sender.cfg.enable_lyrics:
        optional.append(lambda: sender.send_lyrics(event, player, song))
    for operation in optional:
        try:
            async with asyncio.timeout(OPTIONAL_TIMEOUT):
                await operation()
        except Exception as exc:
            logger.debug("Optional music action skipped (%s)", type(exc).__name__)

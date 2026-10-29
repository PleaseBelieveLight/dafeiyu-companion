"""A bounded cache for downloaded music; existing runtime data stays untouched."""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
import uuid
from pathlib import Path
from weakref import WeakValueDictionary

import aiofiles
import aiohttp

from astrbot.api import logger

CACHE_TTL = 7 * 24 * 60 * 60
CACHE_MAX_BYTES = 512 * 1024 * 1024
SONG_MAX_BYTES = 64 * 1024 * 1024
DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=20, connect=5, sock_read=10)


class SongCache:
    def __init__(self, folder: Path, session: aiohttp.ClientSession):
        self.folder = folder
        self.session = session
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        self._active: set[Path] = set()

    def _prune(self, reserve: int = 0) -> None:
        entries = []
        for path in self.folder.glob("*.mp3"):
            # Only this cache's SHA256 names are managed. Preserve older/unrelated files.
            if len(path.stem) != 64 or any(c not in "0123456789abcdef" for c in path.stem):
                continue
            if path in self._active or path.is_symlink():
                continue
            try:
                stat = path.stat()
                if not stat.st_size or time.time() - stat.st_mtime > CACHE_TTL:
                    path.unlink(missing_ok=True)
                else:
                    entries.append((stat.st_mtime, stat.st_size, path))
            except OSError:
                continue
        total = sum(size for _, size, _ in entries)
        for _, size, path in sorted(entries):
            if total + reserve <= CACHE_MAX_BYTES:
                break
            try:
                path.unlink(missing_ok=True)
                total -= size
            except OSError:
                continue

    async def download(self, url: str, cache_key: str | None = None) -> Path | None:
        key = hashlib.sha256((cache_key or url).encode()).hexdigest()
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            self.folder.mkdir(parents=True, exist_ok=True)
            path = self.folder / f"{key}.mp3"
            self._prune()
            if path.is_file() and not path.is_symlink() and path.stat().st_size:
                logger.debug("Music cache hit")
                return path
            partial = self.folder / f"{key}.{uuid.uuid4().hex}.part"
            self._active.add(path)
            try:
                async with self.session.get(url, timeout=DOWNLOAD_TIMEOUT) as response:
                    if response.status != 200:
                        logger.warning("Music download returned HTTP %s", response.status)
                        return None
                    content_type = response.headers.get("Content-Type", "").lower()
                    if any(kind in content_type for kind in ("text/", "json", "xml")):
                        logger.warning("Music download returned a non-audio document")
                        return None
                    if response.content_length and response.content_length > SONG_MAX_BYTES:
                        return None
                    size = 0
                    async with aiofiles.open(partial, "wb") as stream:
                        async for chunk in response.content.iter_chunked(64 * 1024):
                            size += len(chunk)
                            if size > SONG_MAX_BYTES:
                                return None
                            await stream.write(chunk)
                    if not size:
                        return None
                self._prune(reserve=size)
                os.replace(partial, path)
                return path
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Music download failed (%s)", type(exc).__name__)
                return None
            finally:
                partial.unlink(missing_ok=True)
                self._active.discard(path)


async def download_cached_song(downloader, url: str, cache_key: str | None = None) -> Path | None:
    cache = getattr(downloader, "_song_cache", None)
    if cache is None:
        cache = downloader._song_cache = SongCache(downloader.songs_dir, downloader.session)
    return await cache.download(url, cache_key)

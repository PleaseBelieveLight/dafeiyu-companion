"""Apply checked delivery hooks to the locked upstream music plugin."""

import ast
import hashlib
from pathlib import Path


def _replace_method(text: str, name: str, digest: str, replacement: str) -> str:
    matches = [
        node for node in ast.walk(ast.parse(text))
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name
    ]
    if len(matches) != 1:
        raise RuntimeError(f"The pinned music method {name} no longer matches")
    node = matches[0]
    lines = text.splitlines(keepends=True)
    current = "".join(lines[node.lineno - 1:node.end_lineno])
    if current == replacement:
        return text
    if hashlib.sha256(ast.dump(node).encode()).hexdigest() != digest:
        raise RuntimeError(f"The pinned music method {name} changed; review before patching")
    lines[node.lineno - 1:node.end_lineno] = [replacement]
    return "".join(lines)


def apply_music(target: Path) -> None:
    """Keep third-party sources installed, with small idempotent delivery hooks."""
    core = target / "astrbot/data/plugins/astrbot_plugin_music/core"
    replacements = {
        "sender.py": (
            "from .local_delivery import send_local_record, send_song\n",
            [
                (
                    "_send_record_local",
                    "54b87660df030995048745daa0d922099d14f8ea598db6e2cd301ff5cc6d6784",
                    "    async def _send_record_local(\n"
                    "        self, event: AstrMessageEvent, player: BaseMusicPlayer, song: Song\n"
                    "    ) -> bool:\n"
                    "        return await send_local_record(self, event, player, song)\n",
                ),
                (
                    "send_song",
                    "b011ee82bf267872202760d77c7c76a19ade4ca7ef9608fc732de45d480759ba",
                    "    async def send_song(\n"
                    "        self, event: AstrMessageEvent, player: BaseMusicPlayer, song: Song,\n"
                    "        modes: list[str] | None = None,\n"
                    "    ):\n"
                    "        return await send_song(self, event, player, song, modes)\n",
                ),
            ],
        ),
        "downloader.py": (
            "from .song_cache import download_cached_song\n",
            [
                (
                    "download_song",
                    "381d6de3732cca3067769a84661da6f565ec5fcf4e3e6cff337b57d3cbe4158d",
                    "    async def download_song(self, url: str, cache_key: str | None = None) -> Path | None:\n"
                    "        return await download_cached_song(self, url, cache_key)\n",
                ),
            ],
        ),
    }
    pending = {}
    for filename, (imports, methods) in replacements.items():
        path = core / filename
        text = path.read_text(encoding="utf-8-sig")
        for name, digest, replacement in methods:
            text = _replace_method(text, name, digest, replacement)
        if imports not in text:
            text = imports + "\n" + text
        ast.parse(text)
        pending[path] = text
    # Validate all hooks before writing either upstream file.
    for path, text in pending.items():
        path.write_text(text, encoding="utf-8", newline="\n")

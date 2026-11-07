"""Dispatch explicit meme capture requests, including QQ image file attachments."""

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from astrbot.api.message_components import File, Image, Plain, Reply


_CAPTURE_REQUEST = re.compile(
    r"(?:我的意思是\s*)?(?:(?:请|麻烦你?)\s*)?(?:帮我\s*)?"
    r"(?:偷一下|(?:偷(?:一下)?|收(?:了|下|藏)?|存(?:下|一下)?)\s*"
    r"(?:[这那](?:一)?(?:张图(?:片)?|[个张]表情(?:包)?)|表情包))"
    r"(?:吧|呗)?[。！!？?]*"
)
_IMAGE_EXTENSIONS = {".gif", ".png", ".jpg", ".jpeg", ".webp", ".bmp"}


def _is_capture_image(part):
    if isinstance(part, Image):
        return True
    if not isinstance(part, File):
        return False
    # File.file can synchronously download; inspect its stored fields instead.
    for ref in (part.name, part.file_, part.url):
        if not ref:
            continue
        path = urlsplit(ref).path if ref.startswith(("http://", "https://")) else ref
        if Path(unquote(path)).suffix.lower() in _IMAGE_EXTENSIONS:
            return True
    return False


async def _as_capture_image(part):
    if isinstance(part, Image):
        return part
    ref = await part.get_file(allow_return_url=True)
    if not ref:
        raise ValueError("图片文件缺少可用的路径或下载地址")
    return Image(file=ref, url=ref)


async def capture_requested_meme(plugin, event):
    messages = event.get_messages()
    files = [part for part in messages if isinstance(part, File) and _is_capture_image(part)]
    if (files and plugin.event_handler is not None
            and plugin.get_force_capture_entry(event) is not None):
        # Claim the message before resolving media, so failures cannot fall through to AI chat.
        event.stop_event()
        plugin.consume_force_capture(event)
        try:
            image = await _as_capture_image(files[0])
        except Exception:
            await event.send(event.plain_result("❌ 收录失败：无法读取图片文件，请重新发送 /meme 偷 后再发图片。"))
            return
        await plugin.event_handler._handle_force_capture(event, plugin, [image], [])
        return

    if not event.is_private_chat():
        return
    text = "".join(part.text for part in messages if isinstance(part, Plain)).strip()
    if not _CAPTURE_REQUEST.fullmatch(text):
        return

    event.stop_event()
    images = [part for part in messages if _is_capture_image(part)]
    if not images:
        images = [image for part in messages if isinstance(part, Reply)
                  for image in (part.chain or []) if _is_capture_image(image)]
    if len(images) > 1:
        await event.send(event.plain_result("有多张图片，请单独发送或引用你要收录的那一张。"))
        return

    image_ref = ""
    if images:
        try:
            image = await _as_capture_image(images[0])
        except Exception:
            await event.send(event.plain_result("读取图片文件失败，请重新发送这张表情包。"))
            return
        image_ref = image.path or image.url or image.file or ""
        if image_ref.startswith(("base64://", "data:")):
            try:
                image_ref = await image.convert_to_file_path()
            except (OSError, ValueError):
                await event.send(event.plain_result("读取图片失败，请重新发送这张表情包。"))
                return
    async for result in plugin._steal_sticker_impl(event, image_ref):
        await event.send(event.plain_result(result))

"""Dispatch explicit private-chat meme capture requests."""

import re

from astrbot.api.message_components import Image, Plain, Reply


_CAPTURE_REQUEST = re.compile(
    r"(?:我的意思是\s*)?(?:(?:请|麻烦你?)\s*)?(?:帮我\s*)?"
    r"(?:偷一下|(?:偷(?:一下)?|收(?:了|下|藏)?|存(?:下|一下)?)\s*"
    r"(?:[这那](?:一)?(?:张图(?:片)?|[个张]表情(?:包)?)|表情包))"
    r"(?:吧|呗)?[。！!？?]*"
)


async def capture_requested_meme(plugin, event):
    if not event.is_private_chat():
        return
    messages = event.get_messages()
    text = "".join(part.text for part in messages if isinstance(part, Plain)).strip()
    if not _CAPTURE_REQUEST.fullmatch(text):
        return

    event.stop_event()
    images = [part for part in messages if isinstance(part, Image)]
    if not images:
        images = [image for part in messages if isinstance(part, Reply)
                  for image in (part.chain or []) if isinstance(image, Image)]
    if len(images) > 1:
        await event.send(event.plain_result("有多张图片，请单独发送或引用你要收录的那一张。"))
        return

    image_ref = ""
    if images:
        image = images[0]
        image_ref = image.path or image.url or image.file or ""
        if image_ref.startswith(("base64://", "data:")):
            try:
                image_ref = await image.convert_to_file_path()
            except (OSError, ValueError):
                await event.send(event.plain_result("读取图片失败，请重新发送这张表情包。"))
                return
    async for result in plugin._steal_sticker_impl(event, image_ref):
        await event.send(event.plain_result(result))

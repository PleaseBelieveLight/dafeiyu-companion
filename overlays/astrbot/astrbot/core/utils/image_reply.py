# SPDX-License-Identifier: AGPL-3.0-or-later
"""Frame incoming images as part of a conversation without persisting runtime hints."""

import json
import re

from .chat_efficiency import without_internal_cron_history

IMAGE_REPLY_MARKER = "【本轮图片回复方式】"
IMAGE_TURN_MARKER = "<image_chat_turn>"
IMAGE_REPLY_INSTRUCTIONS = (
    IMAGE_REPLY_MARKER
    + "结合当下话题，从一个具体细节接话。表情包台词是画面内容，用户自己确认后才当作现实情况。"
    "有正在处理的求助就继续，明确要识图、读字、解释或分析时按问题认真展开。看不清就指出哪一处。"
)

_RUNTIME_BLOCK = re.compile(
    r"<(system_reminder|system_notice|image_caption|image_chat_turn|think)\b[^>]*>.*?</\1>",
    re.DOTALL,
)
_IMAGE_LABEL = re.compile(
    r"\[对方发来一张图片或表情包\]|\[Image(?:\s[^\]\n]*)?\]|\[图片\]|<attachment>",
    re.IGNORECASE,
)


def _chat_text(content) -> str:
    if isinstance(content, list):
        content = "\n".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict)
            and isinstance(part.get("text"), str)
            and not part.get("_no_save")
        )
    if not isinstance(content, str):
        return ""
    content = _RUNTIME_BLOCK.sub("", content).strip()
    envelope = re.fullmatch(r"<fy_reply>(.*?)</fy_reply>", content, re.DOTALL)
    if envelope:
        try:
            plan = json.loads(envelope[1])
            content = "\n".join(
                plan[key] for key in ("text", "voice") if isinstance(plan.get(key), str)
            )
        except (ValueError, TypeError, AttributeError):
            return ""
    return _IMAGE_LABEL.sub("", content).strip()


def image_turn_reminder(request) -> str:
    """Anchor the current image to bounded recent dialogue, preserving speaker roles."""
    messages = request.contexts
    if not messages:
        history = getattr(getattr(request, "conversation", None), "history", "")
        try:
            messages = json.loads(history) if isinstance(history, str) else history
        except (ValueError, TypeError):
            messages = []
    recent = []
    if isinstance(messages, list):
        for message in without_internal_cron_history(messages)[-24:]:
            if not isinstance(message, dict) or message.get("role") not in {"user", "assistant"}:
                continue
            if message.get("_no_save"):
                continue
            text = _chat_text(message.get("content"))
            if text:
                recent.append({"role": message["role"], "text": text[:240]})
    last_user = next((item for item in reversed(recent) if item["role"] == "user"), None)
    current_text = _chat_text(request.prompt)
    # The normal history already carries the assistant's last reply. Echoing
    # it beside a bare image can make its guesses look like the user's intent.
    selected = [last_user] if last_user is not None else []
    if current_text:
        selected = recent[-2:]
        if last_user is not None and last_user not in selected:
            selected = [last_user, *selected[-1:]]
    data = {
        "recent_dialogue": selected,
        "last_user_text": last_user["text"] if last_user else "",
        "current_text": current_text[:600],
    }
    return (
        IMAGE_TURN_MARKER
        + "用真实用户话题理解这张图。对图意没把握时，说看见的细节就好。\n"
        + json.dumps(data, ensure_ascii=False)
        + "\n</image_chat_turn>"
    )

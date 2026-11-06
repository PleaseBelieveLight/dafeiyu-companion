"""Request-local state that reaches the model without changing the stable system prefix."""

import re

_MEDIA_PLACEHOLDER = re.compile(r"\[对方发来一张图片或表情包\]|\[图片\]|\[Image(?:\s[^\]\n]*)?\]", re.I)
_LOCAL_FOLLOWUP = re.compile(
    r"怎么回事|怎么了|啥情况|什么情况|咋回事|为什么|然后呢|是吗|真的吗|嗯+|好(?:的|吧|啊)?|哦+|行"
)


def skip_automatic_recall(query: str) -> bool:
    """A placeholder or local follow-up has no independent long-term search topic."""
    text = _MEDIA_PLACEHOLDER.sub("", query).strip(" \t\r\n。！？!?，,、.～~…")
    return not text or _LOCAL_FOLLOWUP.fullmatch(text) is not None


def set_temporary_context(request, marker: str, text: str) -> None:
    """Replace only our marked temporary part; preserve other plugins and real user text."""
    parts = []
    for part in getattr(request, "extra_user_content_parts", None) or []:
        temporary = part.get("_no_save", False) if isinstance(part, dict) else getattr(
            part, "_no_save", False
        )
        body = part.get("text", "") if isinstance(part, dict) else getattr(part, "text", "")
        if not (temporary and isinstance(body, str) and body.startswith(marker)):
            parts.append(part)
    if text:
        parts.append({"type": "text", "text": marker + "\n" + text, "_no_save": True})
    request.extra_user_content_parts = parts

"""Request-local state that reaches the model without changing the stable system prefix."""

import re

_MEDIA_PLACEHOLDER = re.compile(r"\[对方发来一张图片或表情包\]|\[图片\]|\[Image(?:\s[^\]\n]*)?\]", re.I)
_LOCAL_FOLLOWUP = re.compile(
    r"喂+|在吗|怎么回事|怎么了|啥情况|什么情况|咋回事|为什么|然后呢|是吗|真的吗|嗯+|好(?:的|吧|啊)?|哦+|行"
)
_COMPANY = re.compile(
    r"(?:不知道(?:说|聊)(?:啥|什么)[，,\s]*)?"
    r"(?:(?:就|只好|只能|想)?(?:来)?找你解(?:解)?闷|(?:你)?陪我(?:聊会儿|聊一会儿|说说话)|"
    r"(?:你)?给我起个话头|(?:我)?(?:好|有点|太)?无聊)(?:吧|了)?"
)
_VERIFY = re.compile(
    r"(?:我相信你[，,\s]+)?(?:(?:那|所以)[，,\s]*)?(?:请|麻烦)?(?:你)?(?:帮我)?"
    r"(?:给(?:我)?(?:一个|个)?(?:准确|确切)(?:的)?(?:结果|结论|答案)|核实一下)(?:吧|呀)?"
)
_GUESS = re.compile(
    r"(?:(?:那就|那|所以)[，,\s]*)?(?:你)?猜(?:一猜|猜|一下|下)?(?:谁(?:能|会)?赢|谁|结果|吧|看)?"
)


def chat_turn_preference(text: str) -> str:
    """Recognize explicit, self-contained conversation cues, not topic keywords."""
    text = text.strip(" \t\r\n。！？!?，,、.～~…")
    for label, pattern in (("company", _COMPANY), ("verify", _VERIFY), ("guess", _GUESS)):
        if pattern.fullmatch(text):
            return label
    return ""


def skip_automatic_recall(query: str) -> bool:
    """A placeholder or local follow-up has no independent long-term search topic."""
    text = _MEDIA_PLACEHOLDER.sub("", query).strip(" \t\r\n。！？!?，,、.～~…")
    return not text or _LOCAL_FOLLOWUP.fullmatch(text) is not None or bool(chat_turn_preference(text))


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

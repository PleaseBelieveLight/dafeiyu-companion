# SPDX-License-Identifier: AGPL-3.0-or-later
# Modified for the Dafeiyu Windows distribution on 2026-10-29.
# Changes: contextual speech, explicit performance requests and reliable delivery.
"""Decode a chat model's separate written and spoken reply channels."""

import json
import re
from difflib import SequenceMatcher
from typing import NotRequired, TypedDict


class VoiceReply(TypedDict):
    text: str
    voice: str
    voice_requested: NotRequired[bool]


VOICE_COOLDOWN_SECONDS = 180

VOICE_REPLY_INSTRUCTIONS = (
    '需要出声时用 <fy_reply>{"voice":"口语内容"}</fy_reply>。'
    '用户明确要听时加 "voice_requested":true。一般只发一种媒介。'
    '链接、代码和需复制的内容可另用 "text"，不用两种渠道重复表达。'
    '朗读和拟声以实际音色为准。合成与发送交给程序。'
)

SELFIE_MEDIA_INSTRUCTIONS = (
    "用户说‘生成一张自拍图片’‘发张自拍’就是请求你实际发图。"
    "可用 generate_selfie 时直接执行，不要求用户改用 /自拍，不索要必填场景，不转交所谓画图那头。"
    "用户没指定场景时，你自己决定：本次要求优先，其次延续最近自己说的状态，"
    "再按当前北京时间选择自然的角色日常，不默认每次站着微笑。"
    "把上下文中的活动、服装、地点、情绪、光线具体写入 action，默认 count=1。"
    "例如你刚说‘我在睡觉’，对方说‘真的吗？给我看看’，就是想看你睡觉的样子，"
    "应调用自拍呈现睡衣、床上休息的场景，图发出后顺着聊天自然接话；不再次追问或教命令。"
    "‘你现在在做什么’本身可自然聊角色日常，不必自动发图；随后要看才展示。"
    "回答当前活动时延续已有状态；没有上下文时依时段安排一件日常小事，"
    "不每次都说‘在这里待着’，也不机械复用同一段台词。"
    "尊重拒绝、取消发图的要求；只讨论自拍功能时不生成图片。"
    "要看题目、代码、报错、屏幕或别人的照片时按原任务处理，不误触发自己的自拍。"
    "这是角色日常的连贯表达；真实天气、位置、设备状态只依据实际资料，不假称已检测环境。"
    "只有工具确认图片已成功发出才接发图成功的话；失败说明实际原因，不反复重试。"
)

ACTION_MEDIA_INSTRUCTIONS = (
    "用户确实要求你展示动作（例如‘给我跳个舞’‘转一圈给我看’）时，"
    "优先用实际媒体回应，不要只写括号动作或用文字假装已经表演。"
    "没有可用视频工具时，除非用户明确只接受视频，否则用一张动作图片替代："
    "展示你自己时调用 generate_selfie，action 描述当前人设、舞姿和场景，count=1；"
    "画其他主体时用 generate_image，count=1。静态图片不能冒充动画或视频。"
    "图片成功发送后默认用文字自然接话，长短随表达需要变化；仅用户明确要求配语音时用拟声或接话，"
    "例如‘哒哒哒，转一圈～’，不要同时重复发文字；这不代表生成了音乐或歌曲。"
    "语音关闭时照常发图，用文字自然回应，不得自行打开语音。"
    "只有讨论跳舞、描述别人正在跳舞、分析图片视频时，不要因此调用生图工具。"
    "普通闲聊也不需要每次配图。这类动作回应默认最多生成一张。"
    "只使用本轮实际可用的工具，未配置的工具不能调用。"
    "生图失败、额度不足或没有成功发送图片时，简短说明失败，不要声称图片已经发出，"
    "不要用动作台词冒充成功，也不要循环重试。"
)


def parse_voice_reply(raw: str) -> VoiceReply | None:
    """Decode a channel envelope without exposing malformed control text.

    Args:
        raw: Model completion or final decorated text.

    Returns:
        Written and spoken content, or None for an ordinary unwrapped response.
    """
    cleaned = re.sub(r"<thought>.*?</thought>", "", raw, flags=re.DOTALL).strip()
    if cleaned.startswith("```") and cleaned.endswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned).strip()
    if not cleaned.startswith("<fy_reply>"):
        return None
    body = cleaned.removeprefix("<fy_reply>").removesuffix("</fy_reply>").strip()
    try:
        value = json.loads(body)
        if (
            not isinstance(value, dict)
            or not any(key in value for key in ("text", "voice"))
            or not all(isinstance(value.get(key, ""), str) for key in ("text", "voice"))
            or ("voice_requested" in value and not isinstance(value["voice_requested"], bool))
        ):
            raise ValueError("Invalid reply channel types")
    except (ValueError, TypeError):
        # Recover complete JSON strings only; never send the control envelope.
        value = {"text": "", "voice": ""}
        for key in value:
            match = re.search(r'"' + key + r'"\s*:\s*', body)
            if match:
                try:
                    item, _ = json.JSONDecoder().raw_decode(body[match.end() :])
                    if isinstance(item, str):
                        value[key] = item
                except ValueError:
                    pass
        if not any(value.values()):
            value["text"] = "这句话我没组织好，你再说一次好吗？"
    plan = {key: value.get(key, "").strip() for key in ("text", "voice")}
    if value.get("voice_requested") is True:
        plan["voice_requested"] = True
    return compact_voice_reply(plan)


def session_voice_enabled(config: dict, session: dict | None = None) -> bool:
    """Use an explicit session switch, otherwise follow the configured TTS API key.

    Only explicit user choices are persisted. Computing a default must not freeze it
    before a key is added, changed or removed later.
    """
    tts = config.get("provider_tts_settings", {})
    if not tts.get("enable", False):
        return False
    selected = (session or {}).get("tts_enabled")
    if isinstance(selected, bool):
        return selected
    provider_id = tts.get("provider_id")
    if not provider_id:
        return False
    for provider in config.get("provider", []):
        if provider.get("id") == provider_id and provider.get("enable", False):
            api_key = provider.get("api_key", "")
            return isinstance(api_key, str) and bool(api_key.strip())
    return False


def _unquoted_voice_text(message: str) -> str:
    # Quoted words can describe a tone; quoted requests are data, not instructions.
    quoted = r'“[^”\n]*”|「[^」\n]*」|『[^』\n]*』|‘[^’\n]*’|"[^"\n]*"' + r"|'[^'\n]*'"
    text = re.sub(quoted, "", message)
    return re.sub(r"[^\S\n]+", "", text)


def voice_request_is_discussion(message: str) -> bool:
    """Keep quotations, reported speech and technical questions out of audio overrides."""
    text = _unquoted_voice_text(str(message or ""))
    if not text or re.search(
        r"^(?:怎么|如何).*(?:语音|声音|唱|哼|模仿|配音|朗读)"
        r"|^(?:他|她|别人|对方|有人)(?:刚才)?(?:说|问|表示)"
        r"|^(?:转述|引用|原文|例句|示例)",
        text,
    ):
        return True
    # A direct channel request can also ask for a technical explanation.
    if re.search(r"用语音(?:说|讲|告诉|回复|回答|解释|聊|读|鼓励|安慰|给我|$)|说给我听|读给我听", text):
        return False
    return bool(re.search(
        r"原理|接口|代码|教程|是什么意思|这句话|这个说法"
        r"|(?:写|编|列|整理|推荐)[^，,。.!?？;；\n]*(?:台词|文案|剧本|朗读稿)"
        r"|(?:不要|不用|别|先别)(?:再)?(?:模仿|配音|朗读)"
        r"|(?:声音|嗓音)[^，,。.!?？;；\n]*(?:特点|区别|分析|看法|意见|评价)", text,
    ))


def voice_preference(message: str, dialogue=()) -> str:
    """Respect listening conditions and explicit requests without another model call."""

    def preference(text):
        text = _unquoted_voice_text(str(text or "")).replace("取消静音", "可以听了")
        if re.search(
            r"不(?:要|用|需要|想听)(?:再)?(?:发|用)?语音|别(?:发|用|再发)语音"
            r"|只(?:发|用|要)文字|文字(?:就好|回复)|用文字(?:说|回复|解释)|静音"
            r"|(?:不要|不用|不想|别|先别)(?:再)?(?:唱|哼|出声|发声音)"
            r"|不想听[^，,。.!?？;；\n]*(?:唱|哼|声音|嗓音)|不方便听|不能放声音|需要安静",
            text,
        ):
            return "quiet"
        if voice_request_is_discussion(text):
            return ""
        if re.search(
            r"用语音(?:说|讲|告诉|回复|回答|解释|聊|读|鼓励|安慰|给我|[，。！!？?\s]|$)"
            r"|发.{0,4}语音|语音(?:说|回复|聊天|聊聊|聊|沟通)(?:给我|一下|吧|呀|嘛|好吗|可以吗|[，。！!？?\s]|$)"
            r"|(?:想|要|让我|给我|能否|能不能|可以)听(?:听|一听|一下)?"
            r"(?:你|您|大肥鱼)(?!对|关于)[^，,。.!?？;；\n]*(?:声音|嗓音|说话)"
            r"|说给我听|读给我听",
            text,
        ):
            return "voice"
        # Audible performances are channel requests even without "想听" or "发语音".
        # Match actions and speech objects, never a particular character or quotation.
        if re.search(
            r"(?:模仿|学(?:一下|一声|一句|两句|着)|演绎|表演)[^，,。.!?？;；\n]*"
            r"(?:声音|嗓音|语气|腔调|口吻|台词)"
            r"|(?:用|以|换成|带着)[^，,。.!?？;；\n]*(?:声音|嗓音|语气|腔调)"
            r"[^，,。.!?？;；\n]*(?:说|讲|念|读|喊)"
            r"|^(?:(?:请|你|帮我|给我|再|重新|继续))*"
            r"(?:朗读|念(?:念|一下|(?:[一两几\d]+)?(?:遍|段|句)|$))"
            r"|(?:给|为)[^，,。.!?？;；\n]*(?:配音|朗读)"
            r"|(?:把|将)[^，,。.!?？;；\n]*(?:读|念|说)(?:出来|出声|给我听)",
            text,
        ):
            return "voice"
        # A request to sing/hum is an audio request even without the word “语音”.
        # Keep capability questions, implementation discussion and quoted terms textual.
        technical_or_capability = re.search(
            r"原理|接口|代码|教程|是什么意思"
            r"|(?:怎么|如何).{0,10}(?:唱|哼|出声|发声音)|(?:会|能)唱歌[吗么嘛?？]",
            text,
        )
        if not technical_or_capability and re.search(
            r"(?:唱|哼)(?:[一二两三四五六七八九十几多\d]+)?(?:句|首|段|小曲|一下)"
            r"|(?:给我|给大家|请|再|重新|继续|想听|要听|能不能|要不你).{0,8}(?:唱歌|哼歌)"
            r"|^(?:你来|你|来)?(?:唱歌|哼歌)(?:吧|呀|啊|嘛|[。！!？?\s])*$"
            r"|(?:唱|哼).{0,8}给我听|发(?:出|点|段|个)?声音"
            r"|出(?:个)?声(?:说|唱|哼|给我听|[。！!呀啊吧\s]|$)",
            text,
        ):
            return "voice"
        if re.search(r"开完会|会议结束|可以听了|现在方便听|取消静音|结束语音", text):
            return "normal"
        if re.search(r"开会|不方便听|不能放声音|需要安静", text):
            return "quiet"
        return ""

    current = preference(str(message or ""))
    if current:
        return current
    for item in reversed(list(dialogue)[-8:]):
        if item.get("role") != "user":
            continue
        text = item.get("content", "")
        mode = preference(text)
        if mode in {"quiet", "normal"}:
            return mode
        if mode == "voice" and re.search(r"接下来|之后|以后|一直|都用|继续|语音聊|语音沟通", text):
            return mode
    return "normal"


def voice_cooling_down(last_sent: float | None, now: float, message: str, mode: str) -> bool:
    if mode == "voice" or re.search(r"紧急|危险|救命|撑不住|很重要|重要的事", str(message or "")):
        return False
    return last_sent is not None and 0 <= now - last_sent < VOICE_COOLDOWN_SECONDS


def _written_detail(text: str) -> bool:
    return bool(
        re.search(
            r"https?://|```|\d|《[^》]+》|(?:歌名|书名|网址|链接|地址|时间|地点|步骤|清单|文件|命令|编号)[：:]?",
            text,
        )
    )


def _similar(left: str, right: str) -> bool:
    left, right = (re.sub(r"[\W_]", "", part).casefold()[:1200] for part in (left, right))
    if not left or not right:
        return False
    if left == right:
        return True
    shorter, longer = sorted((left, right), key=len)
    if len(shorter) < 6:
        return False
    # An extended sentence may add an important detail; never discard it wholesale.
    if shorter in longer:
        return False
    return SequenceMatcher(None, left, right, autojunk=False).ratio() >= 0.76


def _clauses(text: str) -> list[str]:
    # Leave copyable multi-line material intact.
    if "```" in text or re.search(r"https?://", text):
        return [text]
    return [part.strip() for part in re.findall(r"[^。！？!?\n]+[。！？!?]?", text) if part.strip()]


def merge_reply_text(text: str, voice: str) -> str:
    if not text:
        return voice
    if not voice or _similar(text, voice):
        return text
    if not _written_detail(text) and not _written_detail(voice):
        # These fields are alternative chat answers, not two messages to concatenate.
        return _single_chat_answer(text, voice)
    extra = [
        part for part in _clauses(voice) if not any(_similar(part, old) for old in _clauses(text))
    ]
    return "\n".join([text, *extra])


def _single_chat_answer(text: str, voice: str) -> str:
    """Keep the primary answer; a bare acknowledgement is not a complete answer."""
    acknowledgement = re.sub(r"[\W_]", "", text)
    if acknowledgement in {
        "好",
        "好呀",
        "好啊",
        "好的",
        "嗯",
        "嗯嗯",
        "行",
        "可以",
        "收到",
        "没问题",
        "来啦",
        "喏",
    }:
        return voice
    return text


def compact_voice_reply(plan: VoiceReply) -> VoiceReply:
    """Preserve the model's explicit-request marker while compacting the two channels."""
    result = _compact_voice_channels(plan)
    if plan.get("voice_requested") is True:
        result["voice_requested"] = True
    return result


def _compact_voice_channels(plan: VoiceReply) -> VoiceReply:
    """Keep one channel unless written references and speech serve separate purposes."""
    text, voice = (plan.get(key, "").strip() for key in ("text", "voice"))
    if re.search(r"https?://|```", voice):
        return {"text": merge_reply_text(text, voice), "voice": ""}
    if not text or not voice:
        return {"text": text, "voice": voice}
    if not _written_detail(text):
        if _written_detail(voice):
            return {"text": merge_reply_text(text, voice), "voice": ""}
        if _single_chat_answer(text, voice) == voice and text != voice:
            return {"text": "", "voice": voice}
        return {"text": text, "voice": ""}
    text_parts, voice_sentences = _clauses(text), _clauses(voice)
    voice_parts = [
        part for part in re.findall(r"[^，,。！？!?\n]+[，,。！？!?]?", voice) if part.strip()
    ]
    written_parts = [
        part for part in re.findall(r"[^，,。！？!?\n]+[，,。！？!?]?", text) if part.strip()
    ]
    kept_text = [
        part
        for part in text_parts
        if _written_detail(part) or not any(_similar(part, spoken) for spoken in voice_sentences)
    ]
    kept_voice = [
        part
        for part in voice_parts
        if not any(
            _written_detail(written) and _similar(part, written) for written in written_parts
        )
    ]
    return {
        "text": "\n".join(kept_text),
        "voice": voice if len(kept_voice) == len(voice_parts) else "".join(kept_voice).strip(),
    }

# SPDX-License-Identifier: AGPL-3.0-or-later
# Modified for the Dafeiyu Windows distribution on 2026-10-26.
# Changes: local compatibility, contextual speech and reliable media delivery.
"""Decode a chat model's separate written and spoken reply channels."""

import json
import re

VOICE_REPLY_INSTRUCTIONS = (
    "你负责选择自然的聊天表达方式。最终回复使用以下内部格式，"
    '严格输出 <fy_reply>{"text":"实际要发送的文字","voice":"实际要说的话"}</fy_reply>，'
    "两个字段都必须是字符串，可为空；不要附带格式解释、发送动作描述或路由理由。"
    "需要调用工具时正常调用，完成工具后再用此格式回答。"
    "普通聊天允许语音时，根据上下文、情绪、时间、用户偏好决定：可以只发文字、"
    "只发语音，或两者结合，不要每条都发语音，也不要机械地按比例发送。"
    "简短接话、代码、网址、数字、步骤、需要复制保存的信息通常用文字；"
    "亲昵问候、安慰、讲小故事、用户明确想听你说话时适合简短语音；"
    "用户在开会、不方便听、需要安静时优先文字。语音通常控制在一两句。"
    "text 和 voice 必须承担不同信息：不能把文字再读一遍，不能换个说法重复同一意思。"
    "例如 voice 安慰‘今天辛苦了，先喘口气，我陪着你’，text 补充一个歌名或链接；"
    "没有独立补充信息就只保留一种。禁止两段都说同一句问候。"
    "用户明确要求只发语音时，允许语音的前提下 text 留空；语音关闭时 voice 必须留空，"
    "把完整有用的信息写入 text，不得自行打开开关。"
    "系统负责合成和发送 voice，不需要额外语音工具，不要声称无法发语音。"
    "普通语音合成不能当成已经生成带旋律的歌曲；发送完成前不要宣称已发送成功。"
)

ACTION_MEDIA_INSTRUCTIONS = (
    "用户确实要求你展示动作（例如‘给我跳个舞’‘转一圈给我看’）时，"
    "优先用实际媒体回应，不要只写括号动作或用文字假装已经表演。"
    "没有可用视频工具时，除非用户明确只接受视频，否则用一张动作图片替代："
    "展示你自己时调用 generate_selfie，action 描述当前人设、舞姿和场景，count=1；"
    "画其他主体时用 generate_image，count=1。静态图片不能冒充动画或视频。"
    "工具成功生成并实际发送图片后，若本轮允许语音，voice 配一句简短自然的拟声或接话，"
    "例如‘哒哒哒，转一圈～’，text 可留空，不用再把同一句写出来；"
    "这是普通说话和拟声，不代表生成了音乐或带旋律的歌。"
    "语音关闭时照常发图，必要时只补一句文字，不得自行打开语音。"
    "只有讨论跳舞、描述别人正在跳舞、分析图片视频时，不要因此调用生图工具。"
    "普通闲聊也不需要每次配图。这类动作回应默认最多生成一张。"
    "只使用本轮实际可用的工具，未配置的工具不能调用。"
    "生图失败、额度不足或没有成功发送图片时，简短说明失败，不要声称图片已经发出，"
    "不要用动作台词冒充成功，也不要循环重试。"
)


def parse_voice_reply(raw: str) -> dict[str, str] | None:
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
        if not isinstance(value, dict) or not all(
            isinstance(value.get(key), str) for key in ("text", "voice")
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
    text, voice = (value[key].strip() for key in ("text", "voice"))
    normalized = [re.sub(r"[\W_]", "", part).casefold() for part in (text, voice)]
    if all(normalized) and normalized[0] == normalized[1]:
        text = ""
    return {"text": text, "voice": voice}

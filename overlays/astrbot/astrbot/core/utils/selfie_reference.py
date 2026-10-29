# SPDX-License-Identifier: AGPL-3.0-or-later
"""Choose a request-local Dafeiyu reference without changing the active persona."""

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path


REFERENCE_STYLES = {
    "daily": ("01_招手微笑.png", "日常半身日系插画"),
    "meal": ("02_吃饭.png", "日常用餐日系插画"),
    "hug": ("03_抱鲸鱼.png", "温柔抱鲸鱼的日系插画"),
    "wink": ("04_眨眼.png", "俏皮眨眼的日系插画"),
    "standing": ("05_站姿.png", "全身角色立绘"),
    "standing_meal": ("06_捧碗站姿.png", "全身用餐角色立绘"),
    "standing_hug": ("07_抱鲸鱼全身.png", "全身抱鲸鱼角色立绘"),
    "action": ("08_抬手.png", "活泼全身动作立绘"),
    "hands": ("09_双手交叠.png", "文静全身角色立绘"),
    "quiet": ("10_安静微笑.png", "安静可爱的Q版插画"),
    "snack": ("11_吃东西.png", "可爱进食插画"),
    "chibi_hug": ("12_抱鲸鱼Q版.png", "抱鲸鱼的Q版插画"),
    "chibi_wink": ("13_眨眼Q版.png", "俏皮眨眼的Q版插画"),
    "night": ("14_夜间陪伴.png", "夜间暖光陪伴插画"),
    "pajamas": ("15_睡衣晚安.png", "柔和睡衣晚安插画"),
    "bed_hug": ("16_床上抱鲸.png", "温馨卧室抱鲸鱼插画"),
    "blanket": ("17_被窝时刻.png", "温暖被窝日常插画"),
    "bedtime": ("18_睡前时光.png", "放松的睡前日常插画"),
}


@dataclass(frozen=True)
class SelfieReference:
    images: tuple[str, ...]
    style: str = ""
    prompt_suffix: str = ""


def _has(text: str, *phrases: str) -> bool:
    """Match English words at boundaries and Chinese phrases as written."""
    return any(
        re.search(r"(?<![a-z])" + re.escape(phrase) + r"(?![a-z])", text)
        if phrase.isascii()
        else phrase in text
        for phrase in phrases
    )


def _scene_style(action: str) -> str:
    text = unicodedata.normalize("NFKC", str(action or "")).lower()
    # An explicit rejection of a style must not select that style.
    text = re.sub(r"(?:不要|不用|别用|不想要|非)\s*(?:q\s*版|chibi)", "", text)
    wink = _has(text, "眨眼", "wink", "winking")
    hug = _has(text, "抱鲸", "抱着鲸", "鲸鱼玩偶", "鲸鱼抱枕", "whale plush", "hugging")
    meal = _has(
        text,
        "吃饭",
        "用餐",
        "捧碗",
        "端碗",
        "干饭",
        "早餐",
        "午餐",
        "晚餐",
        "米饭",
        "meal",
        "rice",
        "breakfast",
        "lunch",
        "dinner",
    )
    snack = _has(
        text,
        "吃东西",
        "零食",
        "点心",
        "蛋糕",
        "面包",
        "冰淇淋",
        "饼干",
        "snack",
        "cake",
        "bread",
        "eating",
    )
    if _has(text, "q版", "q 版", "chibi", "大头娃娃"):
        if wink:
            return "chibi_wink"
        if meal or snack:
            return "snack"
        return "chibi_hug" if hug else "quiet"

    outdoor = _has(
        text,
        "户外",
        "公园",
        "街头",
        "街上",
        "海边",
        "沙滩",
        "山顶",
        "出门",
        "逛街",
        "散步",
        "旅行",
        "outdoor",
        "park",
        "beach",
        "street",
    )
    if not outdoor:
        if _has(text, "被窝", "被子里", "盖着被子", "under the covers", "under a blanket"):
            return "blanket"
        if _has(text, "睡前", "bedtime"):
            return "bedtime"
        if _has(text, "床上", "床边", "卧室", "bedroom", "in bed"):
            return "bedtime" if _has(text, "手机", "phone") else "bed_hug"
    if _has(text, "睡衣", "pajamas", "pyjamas"):
        return "pajamas"
    if not outdoor:
        if _has(text, "晚安", "睡觉", "准备睡", "去睡", "上床", "good night"):
            return "pajamas"

    action_pose = bool(
        re.search(r"跳(?:个|支|段|一段|一下|一支|一小段)?舞|转(?:个|一|一小)?圈", text)
    ) or _has(
        text, "舞姿", "抬手", "抬腿", "举手", "伸懒腰", "dance", "dancing", "spin", "stretching"
    )
    full_body = action_pose or _has(
        text,
        "全身",
        "站姿",
        "站着",
        "站立",
        "摆姿势",
        "全景",
        "双手交叠",
        "双手交叉",
        "full body",
        "full-body",
        "standing",
        "hands folded",
    )
    if full_body:
        if hug:
            return "standing_hug"
        if meal or snack:
            return "standing_meal"
        if action_pose:
            return "action"
        if _has(text, "双手交叠", "双手交叉", "hands folded"):
            return "hands"
        return "standing"
    if meal:
        return "meal"
    if snack:
        return "snack"
    if hug:
        return "hug"
    if wink:
        return "wink"
    if not outdoor and _has(
        text, "夜间", "夜晚", "晚上", "深夜", "夜里", "熬夜", "night", "evening"
    ):
        return "night"
    if _has(text, "安静", "文静", "quiet"):
        return "quiet"
    return "daily"


def select_selfie_reference(config, action: str, data_dir: str | Path = "") -> SelfieReference:
    """Use bundled scenes for Dafeiyu only; retain configured refs on missing assets."""
    fallback = SelfieReference(tuple(getattr(config, "persona_ref_images", ()) or ()))
    if getattr(config, "active_persona_id", "") != "dafeiyu" or not data_dir:
        return fallback
    root = Path(data_dir) / "selfie_references"
    style = _scene_style(action)
    image = root / REFERENCE_STYLES[style][0]
    if not image.is_file():
        style = "daily"
        image = root / REFERENCE_STYLES[style][0]
    if not image.is_file():
        return fallback
    hint = REFERENCE_STYLES[style][1]
    return SelfieReference(
        images=(str(image.resolve()),),
        style=style,
        prompt_suffix=(
            f"\n采用所附角色参考图的外貌特征及{hint}风格。"
            "参考图中的背景、道具和动作仅作参考，服装、动作、场景和构图以本次要求为准。"
        ),
    )

# SPDX-License-Identifier: AGPL-3.0-or-later
"""Choose a request-local Dafeiyu reference without changing the active persona."""

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path


BASE_REFERENCE = "files/persona_config/persona_ref_image/dafeiyu.png"
SCENE_KEYWORDS = {
    "sleeping": (
        "睡觉", "睡着", "入睡", "睡前", "睡衣", "准备睡", "去睡", "晚安", "被窝", "打瞌睡",
        "sleeping", "asleep", "bedtime", "good night", "pajamas", "pyjamas", "napping",
    ),
    "eating": (
        "吃饭", "用餐", "捧碗", "端碗", "干饭", "早餐", "午餐", "晚餐", "米饭", "吃东西",
        "零食", "点心", "吃蛋糕", "吃面包", "吃冰淇淋", "吃饼干",
        "eating", "meal", "breakfast", "lunch", "dinner", "snack",
    ),
    "dancing": (
        "跳舞", "跳个舞", "跳支舞", "跳一支舞", "跳一段舞", "跳一小段舞", "跳一下舞",
        "转圈", "转个圈", "转一圈", "转一小圈",
        "摆姿势", "摆个姿势", "伸懒腰", "抬手", "抬腿",
        "dance", "dancing", "spin", "spinning", "stretching",
    ),
    "surprised": ("惊讶", "吃惊", "震惊", "惊呆", "惊喜", "surprised", "shocked", "astonished"),
    "confused": ("困惑", "疑惑", "迷茫", "一脸懵", "挠头", "confused", "puzzled", "perplexed"),
    "sitting": (
        "坐姿", "坐着", "坐在", "窗边", "看书", "读书", "看手机", "玩手机", "发呆",
        "sitting", "seated", "reading",
    ),
    "waving": ("招手", "挥手", "打招呼", "微笑", "wave", "waving", "smiling", "smile"),
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
    for scene, keywords in SCENE_KEYWORDS.items():
        if _has(text, *keywords):
            return scene
    return ""


def select_selfie_reference(config, action: str, data_dir: str | Path = "") -> SelfieReference:
    """Use Dafeiyu's bundled scene or base image, then fall back to configured refs."""
    fallback = SelfieReference(tuple(getattr(config, "persona_ref_images", ()) or ()))
    if getattr(config, "active_persona_id", "") != "dafeiyu" or not data_dir:
        return fallback

    root = Path(data_dir)
    style = _scene_style(action)
    image = root / "selfie_references" / f"{style}.png" if style else root / BASE_REFERENCE
    if style and not image.is_file():
        style = ""
        image = root / BASE_REFERENCE
    if not image.is_file():
        return fallback

    return SelfieReference(
        images=(str(image.resolve()),),
        style=style or "base",
        prompt_suffix=(
            "\n采用所附角色参考图的外貌特征。"
            "画风、服装、动作、场景和构图以本次要求为准。"
        ),
    )

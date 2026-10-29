"""Keep speech sparse and complementary using local rules, without model calls."""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "overlays/astrbot/astrbot/core/utils"))
from voice_reply import (
    compact_voice_reply,
    merge_reply_text,
    parse_voice_reply,
    voice_cooling_down,
    voice_preference,
)


class VoiceReplyTests(unittest.TestCase):
    def test_chat_answer_is_not_followed_by_its_spoken_rephrasing(self):
        examples = [
            (
                "我就住在这儿的 QQ 里呀，你看不见的地方有个水缸，够我泡着，尾巴还有地方甩。主人找我的时候，我就冒头。",
                "就住在你 QQ 里呀，泡在一个不小的水缸里，你一叫我，我就冒出水面来。",
            ),
            (
                "水缸是不大，可水是活的呀，底下通着一整片海。我平时就在缸里打盹，真游起来就往深处一钻，那片够我甩尾巴的。",
                "缸小归缸小，底下通着一整片海呢，钻下去就宽敞了。",
            ),
        ]
        for text, voice in examples:
            with self.subTest(text=text):
                plan = {"text": text, "voice": voice}
                expected = {"text": text, "voice": ""}
                self.assertEqual(compact_voice_reply(plan), expected)
                self.assertEqual(merge_reply_text(text, voice), text)
                raw = "<fy_reply>" + json.dumps(plan, ensure_ascii=False) + "</fy_reply>"
                self.assertEqual(parse_voice_reply(raw), expected)
                self.assertEqual(compact_voice_reply(expected), expected)

    def test_acknowledgement_does_not_replace_the_full_answer(self):
        voice = "我住在 QQ 里的小水缸里，底下通着海。"
        self.assertEqual(
            compact_voice_reply({"text": "好呀～", "voice": voice}), {"text": "", "voice": voice}
        )

    def test_secondary_written_information_is_retained(self):
        plan = compact_voice_reply(
            {"text": "我陪你慢慢看。", "voice": "链接：https://example.invalid/docs?x=1"}
        )
        self.assertIn("我陪你慢慢看。", plan["text"])
        self.assertIn("https://example.invalid/docs?x=1", plan["text"])
        self.assertEqual(plan["voice"], "")

    def test_plain_text_needs_no_protocol_and_voice_only_omits_text(self):
        self.assertIsNone(parse_voice_reply("好，明天见。"))
        self.assertEqual(
            parse_voice_reply('<fy_reply>{"voice":"别怕，我在。"}</fy_reply>'),
            {"text": "", "voice": "别怕，我在。"},
        )
        self.assertEqual(
            parse_voice_reply('<fy_reply>{"text":"明天见。"}</fy_reply>'),
            {"text": "明天见。", "voice": ""},
        )

    def test_similar_or_same_purpose_content_never_remains_in_two_channels(self):
        for text, voice in [
            ("别担心，我会一直陪着你。", "我会一直陪着你，别担心。"),
            ("你已经很努力了，慢慢来。", "加油呀，我相信你可以的。"),
            ("你好！", "你好。"),
        ]:
            with self.subTest(text=text):
                plan = compact_voice_reply({"text": text, "voice": voice})
                self.assertFalse(plan["text"] and plan["voice"])
                self.assertTrue(plan["text"] or plan["voice"])

    def test_emotional_speech_can_accompany_copyable_information(self):
        text = "明天上午10点，地点：会议室A。"
        voice = "别紧张，你准备得很充分啦。"
        self.assertEqual(
            compact_voice_reply({"text": text, "voice": voice}), {"text": text, "voice": voice}
        )
        mixed = compact_voice_reply(
            {"text": "别担心，我陪着你。\n歌名：晴天", "voice": "别担心，我陪着你。"}
        )
        self.assertEqual(mixed, {"text": "歌名：晴天", "voice": "别担心，我陪着你。"})

    def test_urls_and_code_are_preserved_and_not_read_twice(self):
        for text in ["链接：https://example.invalid/a?x=1&y=2", "```python\nprint(1)\n```"]:
            with self.subTest(text=text):
                plan = compact_voice_reply({"text": text, "voice": text})
                self.assertEqual(plan, {"text": text, "voice": ""})
                complementary = compact_voice_reply({"text": text, "voice": "别紧张，先试试这个。"})
                self.assertEqual(complementary["text"], text)

    def test_explicit_and_quiet_preferences_include_conversational_context(self):
        for message, expected in [
            ("用语音说给我听", "voice"),
            ("我想听你的声音", "voice"),
            ("别用文字，用语音", "voice"),
            ("只发文字", "quiet"),
            ("我在开会，不方便听", "quiet"),
            ("不需要语音", "quiet"),
            ("取消静音，可以听了", "normal"),
            ("语音回复接口怎么实现", "normal"),
            ("如何使用语音合成", "normal"),
        ]:
            with self.subTest(message=message):
                self.assertEqual(voice_preference(message), expected)
        self.assertEqual(
            voice_preference("好", [{"role": "user", "content": "接下来用语音聊天吧"}]), "voice"
        )
        self.assertEqual(
            voice_preference("好", [{"role": "assistant", "content": "用语音聊天吧"}]), "normal"
        )
        self.assertEqual(
            voice_preference("会议结束，可以听了", [{"role": "user", "content": "我在开会"}]),
            "normal",
        )

    def test_recent_speech_is_suppressed_except_explicit_requests_or_urgency(self):
        self.assertTrue(voice_cooling_down(100, 101, "嗯", "normal"))
        self.assertFalse(voice_cooling_down(100, 280, "嗯", "normal"))
        self.assertFalse(voice_cooling_down(None, 101, "嗯", "normal"))
        self.assertFalse(voice_cooling_down(100, 101, "用语音说", "voice"))
        self.assertFalse(voice_cooling_down(100, 101, "有件很重要的事", "normal"))
        self.assertEqual(merge_reply_text("你好！", "你好。"), "你好！")

    def test_singing_humming_and_sound_requests_override_chat_cooldown(self):
        for message in (
            "要不你唱两句", "多唱几句，记得发声音", "语音开了，重新唱几句",
            "给我唱首歌吧", "我想听你哼一下", "唱歌给我听", "发声音呀", "出个声吧",
        ):
            with self.subTest(message=message):
                mode = voice_preference(message)
                self.assertEqual(mode, "voice")
                self.assertFalse(voice_cooling_down(0, 150, message, mode))

    def test_performance_discussion_does_not_force_audio(self):
        for message in (
            "你会唱歌吗", "你能唱歌吗", "唱歌功能怎么实现", "语音唱歌接口的原理是什么",
            "唱两句是什么意思", "如何唱两句", "这首歌真好听", "她刚才唱了两句",
        ):
            with self.subTest(message=message):
                self.assertEqual(voice_preference(message), "normal")

    def test_declining_speech_still_wins_over_singing_requests(self):
        for message in (
            "不用唱了", "别再唱了", "不想听你唱歌", "不要发声音", "先别出声",
            "我不方便听，你唱两句也先用文字", "不用语音，给我文字歌词",
        ):
            with self.subTest(message=message):
                self.assertEqual(voice_preference(message), "quiet")

    def test_duplicate_removal_keeps_additional_spoken_details(self):
        text = "时间：明天上午10点见。"
        voice = "时间：明天上午10点见，记得带合同。"
        plan = compact_voice_reply({"text": text, "voice": voice})
        self.assertIn("记得带合同", plan["text"] + plan["voice"])
        self.assertIn("记得带合同", merge_reply_text(text, voice))


if __name__ == "__main__":
    unittest.main()

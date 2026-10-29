"""Keep speech sparse and complementary using local rules, without model calls."""

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

    def test_duplicate_removal_keeps_additional_spoken_details(self):
        text = "时间：明天上午10点见。"
        voice = "时间：明天上午10点见，记得带合同。"
        plan = compact_voice_reply({"text": text, "voice": voice})
        self.assertIn("记得带合同", plan["text"] + plan["voice"])
        self.assertIn("记得带合同", merge_reply_text(text, voice))


if __name__ == "__main__":
    unittest.main()

"""Cover bounded selfie intent, speaker ownership and offline fallback scenes."""

import json
import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "overlays/astrbot/astrbot/core/utils"))
from selfie_intent import (
    SHANGHAI,
    fallback_selfie_action,
    parse_selfie_plan,
    recent_selfie_dialogue,
    requested_action,
    selfie_request_kind,
)


class SelfieIntentTests(unittest.TestCase):
    def test_action_requests_accept_showing_a_pose_and_picture_wording(self):
        cases = {
            "给我跳个舞看看": "跳个舞",
            "我要看你跳舞的样子": "跳舞",
            "我想看看你跳舞的样子！": "跳舞",
            "跳舞的图片": "跳舞",
            "给我发张你跳舞的照片": "跳舞",
            "让我看看你转圈的样子": "转圈",
            "大肥鱼来转一圈给我看": "转一圈",
            "请给我跳一段舞吧！": "跳一段舞",
            "摆个姿势": "摆个姿势",
            "[主人的朋友 测试昵称(1000001)] 我要看你跳舞的样子": "跳舞",
        }
        for message, action in cases.items():
            with self.subTest(message=message):
                self.assertEqual(requested_action(message), action)

    def test_action_discussion_negation_other_subjects_and_video_are_not_picture_requests(self):
        for message in (
            "她在跳舞", "我要看她跳舞的样子", "发张猫跳舞的图片", "我在学跳舞",
            "你会跳舞吗", "你跳舞的样子很可爱", "不要给我跳舞", "别发跳舞的图片",
            "只给我视频，不要图片", "我要看你跳舞的视频", "跳舞的图片怎么生成",
            "帮我画一只正在跳舞的猫", "我要看你跳舞的样子，不要发图",
        ):
            with self.subTest(message=message):
                self.assertEqual(requested_action(message), "")

    def test_explicit_requests_and_conversational_followups_are_distinct(self):
        for message in ["生成一张自拍图片", "[主人] 发张自拍", "来个自拍", "发张你的照片", "自拍"]:
            with self.subTest(message=message):
                self.assertEqual(selfie_request_kind(message), "explicit")
        for message in ["真的吗？给我看看", "发来看看", "那拍一张", "让我看看这道题"]:
            with self.subTest(message=message):
                self.assertEqual(selfie_request_kind(message), "contextual")
        for message in [
            "你现在在做什么",
            "真的吗",
            "不要给我发自拍",
            "自拍怎么用",
            "如何生成自拍",
            "帮我写一个生成自拍图片的函数",
            "生成自拍的命令是什么",
            "帮我画一只猫",
        ]:
            with self.subTest(message=message):
                self.assertEqual(selfie_request_kind(message), "")
        self.assertEqual(selfie_request_kind("不要Q版，给我发张自拍"), "explicit")

    def test_dialogue_keeps_only_recent_speakers_and_text_without_mutation(self):
        contexts = [
            {"role": "system", "content": "private system prompt"},
            {"role": "tool", "content": "tool output"},
            {"role": "user", "content": "你在干嘛"},
            {
                "role": "assistant",
                "content": '<think>hidden</think><fy_reply>{"text":"我在睡觉","voice":""}</fy_reply>',
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "真的吗"},
                    {"type": "image_url", "image_url": {"url": "private-image"}},
                ],
            },
        ]
        before = json.dumps(contexts)
        dialogue = recent_selfie_dialogue(SimpleNamespace(contexts=contexts))
        self.assertEqual([item["role"] for item in dialogue], ["user", "assistant", "user"])
        self.assertEqual(dialogue[1]["content"], "我在睡觉")
        self.assertNotIn("private", json.dumps(dialogue))
        self.assertEqual(json.dumps(contexts), before)
        request = SimpleNamespace(
            contexts=[], conversation=SimpleNamespace(history=json.dumps(contexts))
        )
        self.assertEqual(recent_selfie_dialogue(request), dialogue)
        request.conversation.history = "malformed"
        self.assertEqual(recent_selfie_dialogue(request), [])

    def test_fallback_prioritizes_assistant_activity_over_time_and_ignores_user_activity(self):
        morning = datetime(2026, 10, 29, 10, 30, tzinfo=SHANGHAI)
        sleeping = [{"role": "assistant", "content": "我在睡觉呢。"}]
        self.assertIn("躺在床上", fallback_selfie_action("发张自拍", sleeping, morning))
        user_sleeping = [{"role": "user", "content": "我在睡觉"}]
        self.assertIn("白天坐在窗边", fallback_selfie_action("发张自拍", user_sleeping, morning))
        advice = [{"role": "assistant", "content": "我提醒你要睡觉了"}]
        self.assertIn("白天坐在窗边", fallback_selfie_action("发张自拍", advice, morning))
        night = morning.replace(hour=23)
        self.assertIn("睡衣晚安", fallback_selfie_action("发张自拍", [], night))
        self.assertIn("海边", fallback_selfie_action("海边自拍", sleeping, morning))

    def test_plan_requires_a_boolean_and_a_nonempty_scene_for_generation(self):
        self.assertEqual(
            parse_selfie_plan('```json\n{"generate":true,"action":"睡觉"}\n```'), (True, "睡觉")
        )
        self.assertEqual(parse_selfie_plan('{"generate":false,"action":""}'), (False, ""))
        for raw in ['{"generate":"false","action":""}', '{"generate":true}', "[]", "not JSON"]:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_selfie_plan(raw)


if __name__ == "__main__":
    unittest.main()

"""Dynamic program state is scoped, temporary and leaves the cache prefix stable."""

import importlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.agent.message import Message, dump_messages_with_checkpoints
from astrbot.core.utils.history_saver import persist_agent_history
from tests.test_dafeiyu_voice_mode import face_module, fixture, temporary_text

schedule_module = importlib.import_module("data.plugins.astrbot_plugin_schedule_awareness.main")


class ContextStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_media_clock_changes_only_the_temporary_part(self):
        plugin, event, _, _, _, _ = fixture("", {"tts_enabled": False})
        request = ProviderRequest(system_prompt="stable persona")
        with patch.object(face_module.sp, "get_async", new=AsyncMock(return_value={"tts_enabled": False})):
            await plugin.inject_voice_status(event, request)
            before = request.system_prompt
            await plugin.inject_voice_status(event, request)
        self.assertEqual(request.system_prompt, before)
        self.assertNotRegex(request.system_prompt, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}")
        self.assertIn("本次不允许语音", temporary_text(request))
        user = await request.assemble_context()
        self.assertIn("本次不允许语音", str(user))
        # Use the framework's real history cleanup to validate the marker shape.
        cleaned = dump_messages_with_checkpoints([Message.model_validate(user)])
        self.assertNotIn("本次不允许语音", str(cleaned))

    async def test_schedule_refresh_removes_stale_state_and_preserves_other_plugins(self):
        event = self.schedule_event("你的日程是什么", "test:FriendMessage:1")
        jobs = AsyncMock(side_effect=[[self.job("提醒我", event.unified_msg_origin, None)], [],
                                      [self.job("新的提醒", event.unified_msg_origin, "1")]])
        plugin = schedule_module.ScheduleAwarenessPlugin(SimpleNamespace(
            cron_manager=SimpleNamespace(list_jobs=jobs)))
        request = ProviderRequest(system_prompt="stable persona")
        request.extra_user_content_parts.append({"type": "text", "text": "other plugin", "_no_save": True})
        await plugin.inject_schedule(event, request)
        self.assertIn("提醒我", temporary_text(request))
        await plugin.inject_schedule(self.schedule_event("今天好困", event.unified_msg_origin), request)
        self.assertNotIn("提醒我", temporary_text(request))
        jobs.assert_awaited_once()
        await plugin.inject_schedule(event, request)
        self.assertIn("没有可见", temporary_text(request))
        await plugin.inject_schedule(event, request)
        self.assertEqual(request.system_prompt, "stable persona")
        self.assertIn("新的提醒", temporary_text(request))
        self.assertIn("other plugin", temporary_text(request))

    async def test_group_schedule_requires_matching_sender_and_scope(self):
        scope = "test:GroupMessage:20"
        event = self.schedule_event("有什么定时提醒", scope)
        jobs = AsyncMock(return_value=[self.job("我的提醒", scope, "1"),
                                      self.job("别人的提醒", scope, "2"),
                                      self.job("无归属的提醒", scope, None),
                                      self.job("私聊提醒", "test:FriendMessage:1", "1")])
        plugin = schedule_module.ScheduleAwarenessPlugin(SimpleNamespace(
            cron_manager=SimpleNamespace(list_jobs=jobs)))
        request = ProviderRequest()
        await plugin.inject_schedule(event, request)
        context = temporary_text(request)
        self.assertIn("我的提醒", context)
        for other in ("别人的提醒", "无归属的提醒", "私聊提醒"):
            self.assertNotIn(other, context)

    @staticmethod
    def schedule_event(text, scope):
        return SimpleNamespace(unified_msg_origin=scope, get_extra=lambda name: None,
                               get_sender_id=lambda: "1", get_message_str=lambda: text)

    @staticmethod
    def job(name, scope, sender):
        return SimpleNamespace(name=name, enabled=True, payload={"session": scope, "sender_id": sender},
                               timezone="Asia/Shanghai", run_once=False, cron_expression="0 9 * * *",
                               next_run_time=None, description="测试")

    async def test_cron_history_keeps_only_delivered_text_and_latest_conversation(self):
        texts = {"cron_job": {"id": "reminder"}}
        event = SimpleNamespace(unified_msg_origin="test:FriendMessage:1",
                               get_extra=lambda name, default=None: texts.get(name, default))
        request = SimpleNamespace(conversation=SimpleNamespace(cid="chat", history="[]"))
        latest = [{"role": "user", "content": "任务执行期间的新消息"}]
        manager = SimpleNamespace(get_conversation=AsyncMock(return_value=SimpleNamespace(
            history=json.dumps(latest))), update_conversation=AsyncMock())
        await persist_agent_history(manager, event=event, req=request, summary_note="内部细节")
        manager.update_conversation.assert_not_awaited()
        texts["_send_message_to_user_current_session_plain_texts"] = ["提醒正文"]
        await persist_agent_history(manager, event=event, req=request, summary_note="内部细节")
        manager.update_conversation.assert_awaited_once_with(
            event.unified_msg_origin, "chat", history=latest + [{"role": "assistant", "content": "提醒正文"}])


if __name__ == "__main__":
    unittest.main()

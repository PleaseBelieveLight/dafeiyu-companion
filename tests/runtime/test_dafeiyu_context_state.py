"""Dynamic program state is scoped, temporary and leaves the cache prefix stable."""

import asyncio
import importlib
import json
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiocqhttp import Event

from astrbot.core.agent.message import Message, dump_messages_with_checkpoints
from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.conversation_mgr import ConversationManager
from astrbot.core.cron.events import CronMessageEvent
from astrbot.core.cron.manager import CronJobManager, CronJobSchedulingError
from astrbot.core.db.po import CronJob
from astrbot.core.db.sqlite import SQLiteDatabase
from astrbot.core.message.components import At, AtAll, Image, Plain, Reply
from astrbot.core.message.message_event_result import MessageChain, MessageEventResult
from astrbot.core.pipeline.waking_check import stage as waking
from astrbot.core.pipeline.respond.stage import RespondStage
from astrbot.core.pipeline.whitelist_check.stage import WhitelistCheckStage
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import AiocqhttpAdapter
from astrbot.core.provider.entities import LLMResponse, ProviderRequest
from astrbot.core.star.context import Context
from astrbot.core.tools.cron_tools import FutureTaskTool
from astrbot.core.tools.message_tools import SendMessageToUserTool
from astrbot.core.utils import chat_efficiency, chat_scope, conversation_turn, task_receipts
from astrbot.core.utils.history_saver import persist_agent_history
from tests.test_dafeiyu_voice_mode import face_module, fixture, temporary_text

schedule_module = importlib.import_module("data.plugins.astrbot_plugin_schedule_awareness.main")
life_module = importlib.import_module("data.plugins.astrbot_plugin_life_continuity.main")
memory_scope = importlib.import_module("data.plugins.astrbot_plugin_livingmemory.core.memory_scope")


class ContextStateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = self.enterContext(TemporaryDirectory())
        self.folder = Path(folder)
        self.enterContext(patch.object(chat_efficiency, "store_path", return_value=self.folder / "context.db"))

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
        jobs = AsyncMock(side_effect=[[self.job("提醒我", event.unified_msg_origin, "1")], [],
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
        event = self.schedule_event("有什么定时提醒", "test:GroupMessage:1_20")
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
        manager.update_conversation.assert_not_awaited()
        texts["dafeiyu_reminder_run"] = "reserved"
        with patch.object(task_receipts, "confirmed_reminder_texts", return_value=["提醒正文"]):
            await persist_agent_history(manager, event=event, req=request, summary_note="内部细节")
        manager.update_conversation.assert_awaited_once_with(
            event.unified_msg_origin, "chat", history=latest + [{"role": "assistant", "content": "提醒正文"}])

    @staticmethod
    def group_event(sender, message_id, segments, group="20"):
        message = AstrBotMessage()
        message.type, message.group_id, message.self_id = MessageType.GROUP_MESSAGE, group, "99"
        message.sender = MessageMember(user_id=sender, nickname="群成员")
        message.message_id, message.message, message.raw_message = message_id, segments, None
        text = "".join(part.text for part in segments if isinstance(part, Plain))
        return AstrMessageEvent(text, message, PlatformMetadata("aiocqhttp", "test QQ", id="qq"), group)

    async def test_group_pipeline_trusts_platform_wakeup_and_isolates_every_speaker(self):
        group = "qq:GroupMessage:20"
        settings = {"enable_id_white_list": True, "id_whitelist": [group], "id_whitelist_log": False,
                    "wl_ignore_admin_on_group": True, "wl_ignore_admin_on_friend": True}
        context = SimpleNamespace(db_helper=Mock(), astrbot_config_id="test", astrbot_config={
            "platform_settings": settings, "admins_id": ["1"], "wake_prefix": ["大肥鱼", "/"],
        })
        gate, whitelist = waking.WakingCheckStage(), WhitelistCheckStage()
        await gate.initialize(context)
        await whitelist.initialize(context)
        gate._umo_auto_name_recorder = Mock()
        with (
            patch.object(waking.star_handlers_registry, "get_handlers_by_event_type", return_value=[]),
            patch.object(waking.SessionPluginManager, "filter_handlers_by_session", new=AsyncMock(return_value=[])),
        ):
            plain = self.group_event("1", "1", [Plain("[CQ:at,qq=99] 查群日程")])
            everyone = self.group_event("1", "2", [AtAll(), Plain("大家好")])
            other_quote = self.group_event("1", "3", [Reply(id="x", sender_id="2"), Plain("跟他说")])
            for event in (plain, everyone, other_quote):
                await gate.process(event)
                self.assertTrue(event.is_stopped())
            first = self.group_event("1", "5", [At(qq="99"), Plain("问日程")])
            second = self.group_event("2", "6", [Reply(id="bot", sender_id="99"), Plain("聊聊")])
            other_group = self.group_event("1", "7", [Plain("大肥鱼，问个事")], group="21")
            for event in (first, second, other_group):
                await gate.process(event)
                await whitelist.process(event)
            self.assertFalse(first.is_stopped())
            self.assertFalse(second.is_stopped())
            self.assertTrue(other_group.is_stopped())
            self.assertNotEqual(first.unified_msg_origin, second.unified_msg_origin)
            self.assertNotEqual(first.role, "admin")
            config = {"access_control": {"whitelist_enabled": True, "allowed_ids": group},
                      "filtering_settings": {"memory_scope_mode": "global"}}
            self.assertTrue(memory_scope.is_event_memory_allowed(config, first))
            self.assertEqual(memory_scope.resolve_memory_scope(config, first), first.unified_msg_origin)
            names = ("future_task", "recall_long_term_memory", "browser_task", "local_python", "send_message_to_user")
            tools = ToolSet([FunctionTool(name=name, description=name, parameters={}) for name in names])
            request = ProviderRequest(func_tool=tools)
            chat_scope.restrict_group_tools(first, request)
            self.assertEqual(request.func_tool.names(), list(names[:2]))
            self.assertEqual(tools.names(), list(names))
            duplicate = self.group_event("1", "5", [At(qq="99"), Plain("问日程")])
            await gate.process(duplicate)
            self.assertTrue(duplicate.is_stopped())

    async def test_conversation_id_cannot_read_switch_update_or_delete_another_scope(self):
        db = SimpleNamespace(get_conversation_by_id=AsyncMock(return_value=SimpleNamespace(
            user_id="qq:FriendMessage:1")), update_conversation=AsyncMock(), delete_conversation=AsyncMock())
        manager = ConversationManager(db)
        group = "qq:GroupMessage:2_20"
        self.assertIsNone(await manager.get_conversation(group, "private", create_if_not_exists=True))
        await manager.switch_conversation(group, "private")
        await manager.update_conversation(group, "private", history=[])
        await manager.delete_conversation(group, "private")
        db.update_conversation.assert_not_awaited()
        db.delete_conversation.assert_not_awaited()
        self.assertNotIn(group, manager.session_conversations)

    async def test_short_image_burst_joins_once_and_correction_cancels_older_reply_tail(self):
        fragment = self.group_event("1", "fragment", [Plain("给你看")])
        photo = self.group_event("1", "photo", [Image.fromURL("https://example.invalid/image")])
        other = self.group_event("2", "another", [Plain("另一个人在说话")])
        correction = self.group_event("1", "correction", [Plain("不是，先别发图片")])
        for event in (fragment, photo, other, correction):
            event.session_id = f"{event.get_sender_id()}_20"
        waiting = asyncio.create_task(conversation_turn.begin_turn(fragment))
        await asyncio.sleep(0)
        self.assertTrue(await conversation_turn.begin_turn(other))
        self.assertTrue(await conversation_turn.begin_turn(photo))
        self.assertFalse(await waiting)
        self.assertEqual(photo.message_str, "给你看")
        self.assertEqual([type(part) for part in photo.get_messages()], [Plain, Image])
        self.assertTrue(conversation_turn.current_turn(photo))
        await conversation_turn.begin_turn(correction)
        self.assertFalse(conversation_turn.current_turn(photo), (photo.get_extra(), correction.get_extra()))
        _, _, response, _, _, _ = fixture("未发送的旧尾巴")
        self.assertFalse(RespondStage._keep_turn(photo, response))
        self.assertTrue(photo.get_extra("_fy_suppressed_reply"))
        self.assertTrue(conversation_turn.current_turn(other))
        stopped = self.group_event("1", "cancel", [Plain("取消点歌")])
        stopped.session_id = "1_20"
        await conversation_turn.begin_turn(stopped)
        self.assertFalse(conversation_turn.can_finish_media(photo))

    async def test_idle_proactive_paths_exit_before_touching_provider_or_history(self):
        module = importlib.import_module("data.plugins.astrbot_plugin_proactive_chat.core.pipeline.flow_orchestrator")
        flow = module.FlowOrchestrator(SimpleNamespace())
        for source in (module.TriggerSource.SCHEDULE, module.TriggerSource.AUTO_TRIGGER, module.TriggerSource.GROUP_IDLE):
            await flow.run("test", source)

    async def test_video_already_rendering_does_not_send_after_explicit_cancel(self):
        module = importlib.import_module("data.plugins.astrbot_plugin_omnidraw.core.video_manager")
        video = module.VideoManager.__new__(module.VideoManager)
        event = self.group_event("1", "video", [Plain("给我做个视频")])
        cancel = self.group_event("1", "stop-video", [Plain("取消视频")])
        event.session_id = cancel.session_id = "1_20"
        event.send = AsyncMock()
        video._get_video_provider_chain = lambda: [SimpleNamespace(id="test")]
        video._apply_provider_defaults = lambda *_: {}
        video._effective_request_model = lambda *_: "test"
        video._build_success_text = lambda *_, **__: "渲染完成"

        async def render(*args):
            await conversation_turn.begin_turn(cancel)
            return "https://example.invalid/video.mp4"

        video._fetch_video_from_api = AsyncMock(side_effect=render)
        await conversation_turn.begin_turn(event)
        await video.background_task_runner(event, "测试视频")
        video._fetch_video_from_api.assert_awaited_once()
        event.send.assert_not_awaited()

    async def test_turn_routes_explicit_tasks_and_only_records_anonymous_delivery_measurements(self):
        event = self.group_event("1", "raw-event-9001", [Plain("你好")])
        names = ("browser_task", "get_screenshot", "recall_long_term_memory", "web_search_anysearch", "future_task")
        tools = ToolSet([FunctionTool(name=name, description=name, parameters={}) for name in names])
        request = ProviderRequest(func_tool=tools)
        conversation_turn.route_capabilities(event, request)
        self.assertEqual(request.func_tool.names(), [])
        event.message_str = "打开浏览器看看网页"
        request.func_tool = tools
        conversation_turn.route_capabilities(event, request)
        self.assertIn("browser_task", request.func_tool.names())
        self.assertNotIn("recall_long_term_memory", request.func_tool.names())
        token = conversation_turn.start_metrics(event)
        conversation_turn.activate_metrics(event)
        conversation_turn.note_model("chat", {"messages": [{"role": "user", "content": "私密正文"}]})
        conversation_turn.note_delivery(MessageChain().message("私密正文"), 0.04)
        conversation_turn.finish_metrics(event, token)
        with closing(chat_efficiency.connect()) as db:
            row = db.execute("SELECT event,generation_calls,first_reply,channels FROM chat_turns").fetchone()
        self.assertNotIn("raw-event", row[0])
        self.assertEqual(row[1], 1)
        self.assertGreaterEqual(row[2], 0)
        self.assertEqual(row[3], '["text"]')

    async def test_onebot_reply_cannot_import_another_group_or_private_peer(self):
        adapter = AiocqhttpAdapter.__new__(AiocqhttpAdapter)
        adapter.bot = SimpleNamespace(call_action=AsyncMock())
        base = {"post_type": "message", "self_id": 99, "message_id": 81, "group_id": 20,
                "message_type": "group", "user_id": 1, "sender": {"user_id": 1},
                "message": [{"type": "reply", "data": {"id": 70, "sender_id": 99, "text": "伪造正文"}}]}
        quoted = {**base, "message_id": 70, "sender": {"user_id": 99},
                  "message": [{"type": "text", "data": {"text": "引用中的消息"}}]}
        for group in (21, 20):
            adapter.bot.call_action.return_value = {**quoted, "group_id": group}
            message = await adapter._convert_handle_message_event(Event(base))
            if group == 21:
                self.assertEqual(message.message, [])
            else:
                self.assertEqual(message.message[0].chain[0].text, "引用中的消息")

        private = Event({**base, "message_type": "private"})
        prior = {**quoted, "message_type": "private"}
        adapter.bot.call_action.side_effect = [prior, {"messages": [{"message_id": 68}]}]
        message = await adapter._convert_handle_message_event(private)
        self.assertEqual(message.message, [])
        adapter.bot.call_action.side_effect = [prior, {"messages": [{"message_id": 70}]}]
        message = await adapter._convert_handle_message_event(private)
        self.assertEqual(message.message[0].chain[0].text, "引用中的消息")

    @staticmethod
    def qq_transport():
        bot = SimpleNamespace(send_private_msg=AsyncMock(return_value={"message_id": 507}),
                              send_group_msg=AsyncMock(return_value={"message_id": 508}))
        adapter = SimpleNamespace(meta=lambda: SimpleNamespace(id="qq", name="aiocqhttp"), bot=bot)
        context = SimpleNamespace(platform_manager=SimpleNamespace(platform_insts=[adapter]),
                                  get_config=lambda **_: {"platform_settings": {
                                      "id_whitelist": ["qq:FriendMessage:1", "qq:GroupMessage:20"]}})
        return context, bot

    async def test_cron_execution_and_transport_receipts_survive_repeated_job_and_restart(self):
        context, bot = self.qq_transport()
        scope = "qq:GroupMessage:1_20"
        job = CronJob(name="交材料", job_type="active_agent", job_id="once", run_once=True,
                      payload={"session": "qq:GroupMessage:20", "sender_id": "1"})
        db = SimpleNamespace(get_cron_job=AsyncMock(return_value=job), update_cron_job=AsyncMock())
        tool = SendMessageToUserTool()

        async def run(job, start_time, *, run):
            event = CronMessageEvent(context=context, session=MessageSession.from_str(scope), message="交材料",
                                     extras={"cron_job": {"session": job.payload["session"]},
                                             "dafeiyu_reminder_run": run})
            event.role = "admin"
            wrapper = SimpleNamespace(context=SimpleNamespace(context=context, event=event))
            message = [{"type": "plain", "text": "到时间交材料了"}]
            forbidden = await tool.call(wrapper, session="qq:FriendMessage:1", messages=message)
            self.assertTrue(forbidden.startswith("error:"))
            self.assertIn("accepted by QQ", await tool.call(wrapper, messages=message))
            await event.send(MessageChain(chain=[Plain("到时间交材料了")]))

        for _ in range(2):
            manager = CronJobManager(db)
            manager._run_active_agent_job = AsyncMock(side_effect=run)
            task_receipts.recover_reminders()
            await manager._run_job("once", delete_run_once=False)
        bot.send_group_msg.assert_awaited_once()
        self.assertEqual(bot.send_group_msg.call_args.kwargs["group_id"], 20)
        bot.send_private_msg.assert_not_awaited()
        self.assertEqual(task_receipts.confirmed_reminder_texts("once:once"), ["到时间交材料了"])
        self.assertEqual(task_receipts.recent_reminders("qq:GroupMessage:2_20", "2"), [])
        with closing(task_receipts.journal()) as ledger:
            self.assertEqual(ledger.execute("SELECT execution,delivery,message_id FROM reminder_runs").fetchall(),
                             [("completed", "sent", "508")])

    async def test_invalid_schedule_does_not_create_job_and_duplicate_tool_calls_create_once(self):
        db = SimpleNamespace(create_cron_job=AsyncMock())
        scheduler = CronJobManager(db)
        with self.assertRaises(CronJobSchedulingError):
            await scheduler.add_active_job(name="test", cron_expression="invalid", payload={})
        db.create_cron_job.assert_not_awaited()
        event = self.group_event("1", "new-task", [At(qq="99"), Plain("每天提醒我交表")])
        manager = SimpleNamespace(add_active_job=AsyncMock(return_value=SimpleNamespace(
            job_id="job", name="交表", next_run_time=None)), get_next_run_time=Mock(return_value=None))
        context = SimpleNamespace(cron_manager=manager, get_config=lambda **_: {"timezone": "Asia/Shanghai"})
        wrapper = SimpleNamespace(context=SimpleNamespace(context=context, event=event))
        tool = FutureTaskTool()
        arguments = {"action": "create", "name": "交表", "note": "提醒交表", "cron_expression": "0 9 * * *"}
        first = await tool.call(wrapper, **arguments)
        second = await tool.call(wrapper, **arguments)
        self.assertEqual(first, second)
        manager.add_active_job.assert_awaited_once()
        plugin = schedule_module.ScheduleAwarenessPlugin(context)
        request = SimpleNamespace(messages=[Message(role="assistant", content="已设好另一个提醒。")])
        response = LLMResponse(role="assistant", completion_text="已设好另一个提醒。")
        await plugin.use_task_receipt(event, request, response)
        self.assertEqual(response.completion_text, "已安排提醒「交表」。")
        self.assertIn(response.completion_text, str(request.messages[-1].content))

        no_receipt = self.group_event("1", "missing-receipt", [Plain("明天提醒我交表")])
        for draft, expected in (("我会提醒你的。", "没收到日程变更的确认"), ("明天几点？", "明天几点？")):
            response = LLMResponse(role="assistant", completion_text=draft)
            await plugin.use_task_receipt(no_receipt, request, response)
            self.assertIn(expected, response.completion_text)

    async def test_plain_qq_reminder_creates_and_delivers_without_a_model(self):
        transport, bot = self.qq_transport()
        context = Mock(spec=Context)
        context.platform_manager = transport.platform_manager
        context.get_config.return_value = {**transport.get_config(), "timezone": "Asia/Shanghai"}
        db = SQLiteDatabase(str(self.folder / "cron.db"))
        await db.initialize()
        self.addAsyncCleanup(db.engine.dispose)
        scheduler = context.cron_manager = CronJobManager(db)
        await scheduler.start(context)
        self.addAsyncCleanup(scheduler.shutdown)
        scheduler._woke_main_agent = AsyncMock()
        plugin = schedule_module.ScheduleAwarenessPlugin(context)
        request = ProviderRequest(func_tool=ToolSet([FutureTaskTool()]))

        text = "两分钟后提醒我休息一下"
        message = AstrBotMessage()
        message.type, message.message_id = MessageType.FRIEND_MESSAGE, "private-reminder"
        message.sender = MessageMember(user_id="1", nickname="发起人")
        message.message = [Plain(text)]
        event = AstrMessageEvent(text, message, PlatformMetadata("aiocqhttp", "test QQ", id="qq"), "1")
        event.send = AsyncMock()
        await plugin.schedule_explicit_reminder(event, request)

        self.assertTrue(event.is_stopped())
        (job,) = await scheduler.list_jobs("active_agent")
        self.assertTrue(job.run_once)
        self.assertEqual(job.payload["session"], "qq:FriendMessage:1")
        self.assertEqual(job.payload["sender_id"], "1")
        expected = datetime.fromtimestamp(message.timestamp, timezone.utc) + timedelta(minutes=2)
        self.assertEqual(scheduler.get_next_run_time(job.job_id), expected)
        self.assertIn("已安排提醒「休息一下」", event.send.await_args.args[0].get_plain_text())
        bot.send_private_msg.assert_not_awaited()

        await scheduler._run_job(job.job_id, delete_run_once=False)
        await scheduler._run_job(job.job_id, delete_run_once=False)
        scheduler._woke_main_agent.assert_not_awaited()
        bot.send_group_msg.assert_not_awaited()
        bot.send_private_msg.assert_awaited_once()
        self.assertEqual(bot.send_private_msg.await_args.kwargs["user_id"], 1)
        self.assertEqual(task_receipts.confirmed_reminder_texts(f"{job.job_id}:once"), ["到时间了，休息一下。"])

    def test_explicit_reminder_time_and_ambiguous_text(self):
        received = datetime(2026, 11, 6, 15, 39, 25, tzinfo=timezone.utc)
        pattern = schedule_module._REMINDER
        for text, expected in (
            ("请创建一个名为‘体验日程提醒’的一次性任务，十分钟后提醒我休息一下", received + timedelta(minutes=10)),
            ("今天 16:05 提醒我交材料", received.replace(hour=16, minute=5, second=0)),
            ("明天9点提醒我交材料", datetime(2026, 11, 7, 9, tzinfo=timezone.utc)),
        ):
            with self.subTest(text=text):
                self.assertEqual(schedule_module._reminder_time(pattern.fullmatch(text), received), expected)
        for text in ("你能两分钟后提醒我吗？", "别两分钟后提醒我", "两分钟后提醒我", "两分钟后提醒我交材料，再发给同事"):
            self.assertIsNone(pattern.fullmatch(text))

    async def test_reminder_with_lost_ack_keeps_unknown_delivery_without_resending(self):
        context, bot = self.qq_transport()
        bot.send_private_msg.return_value = None
        scope = "qq:FriendMessage:1"
        job = CronJob(name="交表", job_type="active_agent", job_id="lost", run_once=True,
                      payload={"session": scope, "sender_id": "1"})
        started = datetime(2026, 1, 15, 20, 0, tzinfo=timezone.utc)
        key = task_receipts.reserve_reminder(job, started, None, manual=False)
        event = CronMessageEvent(context=context, session=MessageSession.from_str(scope), message="交表",
                                 extras={"cron_job": {"session": scope}, "dafeiyu_reminder_run": key})
        result = MessageChain(chain=[Plain("该交表了")])
        with self.assertRaisesRegex(RuntimeError, "delivery receipt"):
            await event.send(result)
        task_receipts.recover_reminders()
        self.assertIsNone(task_receipts.reserve_reminder(job, started, None, manual=False))
        await event.send(result)
        bot.send_private_msg.assert_awaited_once()
        self.assertEqual(task_receipts.confirmed_reminder_texts(key), [])
        with closing(task_receipts.journal()) as ledger:
            self.assertEqual(ledger.execute("SELECT execution, delivery FROM reminder_runs").fetchone(),
                             ("interrupted", "uncertain"))

    async def test_immediate_search_production_hook_only_delivers_the_verified_result_once(self):
        context, bot = self.qq_transport()
        context.conversation_manager = SimpleNamespace(get_conversation=AsyncMock(return_value=SimpleNamespace(
            history="[]")), update_conversation=AsyncMock())
        plugin = life_module.LifeContinuityPlugin(context, {
            "enabled": True, "owner_scope": "qq:FriendMessage:1", "web_search_enabled": True,
        })
        with patch.object(life_module.StarTools, "get_data_dir", return_value=self.folder / "life"):
            await plugin.initialize()
        self.addAsyncCleanup(plugin.terminate)
        _, event, _, _, _, _ = fixture("", {})
        event.unified_msg_origin = "qq:FriendMessage:1"
        event.get_sender_id.return_value = "1"
        event.message_obj.message_id = "same-qq-event"
        event.get_messages.return_value = [Plain("现在就查赛程")]
        event.plain_result.side_effect = lambda text: MessageEventResult(chain=[Plain(text)])
        plugin.engine.searcher = AsyncMock(return_value=[{"title": "赛程", "text": "周五举行。",
                                                        "url": "https://example.invalid/calendar"}])
        for _ in range(2):
            await plugin.handle_chat_context(event, ProviderRequest(conversation=SimpleNamespace(cid="chat")))
        plugin.engine.searcher.assert_awaited_once()
        bot.send_private_msg.assert_awaited_once()
        self.assertIn("https://example.invalid/calendar", str(bot.send_private_msg.call_args.kwargs["message"]))
        context.conversation_manager.update_conversation.assert_awaited_once()
        self.assertEqual(event.stop_event.call_count, 2)

        event.message_obj.message_id = "another-qq-event"
        plugin.config["web_search_enabled"] = False
        bot.send_private_msg.return_value = {}
        for _ in range(2):
            await plugin.handle_chat_context(event, ProviderRequest(conversation=SimpleNamespace(cid="chat")))
        self.assertEqual(bot.send_private_msg.await_count, 2)
        self.assertIn("用不了搜索", str(bot.send_private_msg.call_args.kwargs["message"]))
        context.conversation_manager.update_conversation.assert_awaited_once()
        self.assertEqual(plugin.store.db.execute("""SELECT status FROM foreground_replies
            WHERE event_id='another-qq-event'""").fetchone()[0], "uncertain")


if __name__ == "__main__":
    unittest.main()

"""Fixed, synthetic two-turn conversations at the provider and delivery boundaries."""

import importlib
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import NamedTuple
from unittest.mock import AsyncMock, patch

from openai import AsyncOpenAI
from openai.types.chat.chat_completion import ChatCompletion

from astrbot.core.agent.message import ImageURLPart, TextPart, bind_checkpoint_messages, dump_messages_with_checkpoints
from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.message.components import Plain, Record
from astrbot.core.pipeline.respond.stage import RespondStage
from astrbot.core.pipeline.result_decorate.stage import ResultDecorateStage
from astrbot.core.platform.message_type import MessageType
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
from astrbot.core.utils import chat_efficiency
from tests.test_dafeiyu_voice_mode import envelope, face_module, fixture

life_module = importlib.import_module("data.plugins.astrbot_plugin_life_continuity.main")
schedule_module = importlib.import_module("data.plugins.astrbot_plugin_schedule_awareness.main")
search_module = importlib.import_module("data.plugins.astrbot_plugin_life_continuity.search")
memory_module = importlib.import_module(
    "data.plugins.astrbot_plugin_livingmemory.core.event_handler_modules.memory_recall"
)
injection_module = importlib.import_module("data.plugins.astrbot_plugin_livingmemory.core.utils.injection_adapter")

REPLAY_MODEL = "fixed-dialogue-model"
REPLAY_PERSONA = "你是大肥鱼。根据对方当下的话和可核对的回执回应。"
REPLAY_EXPERIENCE = True
REVIEW_CONFIG = None
REPLAY_RECORDS = []
OWNER, OTHER = "1001", "1002"
PRIVATE = f"test:FriendMessage:{OWNER}"
GROUP = "test:GroupMessage:2001"
SHANGHAI = timezone(timedelta(hours=8))
START = datetime(2026, 1, 15, 20, 0, tzinfo=SHANGHAI)
IMAGE = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/WpkAAAAASUVORK5CYII="
)


class Case(NamedTuple):
    name: str
    first: str
    first_reply: str
    second: str
    second_reply: str
    signal: str = ""


CASES = {
    "闲聊": (
        Case("greeting", "你好", "来啦。", "今天终于忙完了", "总算能松口气了。"),
        Case("company", "今天的活总算交了", "下班前扳回一局。", "不知道说啥，只能找你解解闷了", "给今天这班起个片名，《差一分钟关电脑》。"),
        Case("banter", "我给你留了一碗饭", "那我勉强坐过来。", "其实就剩一口", "一口也是饭，我先占座。"),
        Case("vent", "这一局一直被抓", "换我也憋屈。", "我就是想吐槽一下", "行，先把这口气吐出来。"),
        Case("topic_switch", "研究进度怎么样了", "可以核对记录。", "我窗边落了一只鸟", "它倒是挑了个好位置。"),
        Case("repair", "桌上的饭还没凉", "那我当你在叫我。", "怎么回事", "我把你的话接成邀请了。"),
    ),
    "任务": (
        Case("search", "查一下公开赛事时间", "可以核对赛程。", "现在就查", "公开日历列的是周五。", "search"),
        Case("timeout", "查一下公开赛事时间", "可以核对赛程。", "现在就查", "这次检索超时了。", "timeout"),
        Case("clarify", "今天的面不错", "听起来挺香。", "现在就查", "你想查哪件事？", "clarify"),
        Case("verify", "铁卫打得过游侠吗？", "我只有个初步判断。", "给我个准确结果吧", "公开攻略有版本限制。", "verify"),
        Case("guess", "铁卫打得过游侠吗？", "双方各有优势。", "那你猜一猜谁能赢", "我猜游侠，因为距离更好拉开。"),
        Case("local_math", "12 乘 13 等于多少？", "可以拆开算。", "核实一下", "12 乘 10 加 12 乘 3，等于 156。"),
    ),
    "媒体": (
        Case("bare_meme", "今天好累", "先靠一会儿。", "[对方发来一张图片或表情包]", "这张图连眼神都在叹气。", "image"),
        Case("meme_caption", "刚讲了个冷笑话", "冷得尾巴一抖。", "[图片]", "这张图上的台词倒挺会接梗。", "caption"),
        Case("image_task", "帮我排查安装错误", "截图可以给我看看。", "请读出截图的报错编号", "截图右下角需要仔细看。", "image"),
        Case("image_reference", "等我补一张图", "好，我等图。", "还是刚才那张，你看看左上角", "我先看左上角。", "image"),
        Case("asr_text", "我说话可能有点含糊", "我会按听到的来。", "[语音转写] 我到门口了", "到了就好。"),
        Case("voice_out", "我想听听你的声音", "可以呀。", "用语音说一句辛苦了", "辛苦了。", "voice"),
    ),
    "群聊": (
        Case("group_own", "我来核对一下", "说吧。", "我的日程有什么", "你自己的提醒在明天上午。", "schedule"),
        Case("group_other", "我也想查一下", "好。", "这群有什么日程", "当前发言人没有可见的提醒。", "foreign"),
        Case("group_guest", "我是来聊天的", "欢迎。", "研究进度怎么样了", "可以聊你当前想问的事。", "foreign"),
        Case("group_no_topic", "刚刚那句是开玩笑", "我接到了。", "这个表情送你", "我把这个梗收下了。"),
        Case("group_share", "今天小组交差了", "终于能歇会儿。", "下班啦", "这次可以痛快下班。"),
        Case("group_read_image", "我们在一起排错", "我听着。", "截图里的字是什么", "按截图来核对。", "image"),
    ),
    "跨天": (
        Case("next_day_reminder", "明天提醒我交表", "这件事要以日程为准。", "今天有什么定时提醒", "你可以核对提交材料的提醒。", "schedule"),
        Case("completed_item", "明天再看书单", "书单这个话题记着。", "书单已经看完了", "那就翻篇。"),
        Case("changed_time", "明天上午再处理", "具体时间以安排为准。", "我的日程改到几点了", "可以按当前提醒时间核对。", "schedule"),
        Case("legacy_cron", "明天记得交表", "我等你下次来核对。", "昨晚那句是什么", "回看我们真正说过的话。", "legacy_cron"),
        Case("continued_research", "明天再问研究结果", "结果得按运行记录核对。", "研究到哪了", "先核对已有记录。"),
        Case("cross_day_pivot", "明天讨论书单", "好。", "今天想聊下雨", "雨点敲窗那一下还挺好听。"),
    ),
}

CONTEXT_MARKERS = {
    "image_chat": "<image_chat_turn>", "company": "【本轮陪聊】",
    "media_state": "【本轮媒体状态】", "schedule": "【本轮程序日程】",
    "answer_mode": "【本轮回答方式】", "research": "【自主生活本轮资料】",
    "search_receipt": "【本轮即时检索回执】", "image_caption": "<image_caption>",
    "audio_transcript": "[语音转写]", "memory": "固定回放记忆",
}


class ReplayClock(datetime):
    moment = START

    @classmethod
    def now(cls, tz=None):
        return cls.moment.astimezone(tz) if tz else cls.moment.replace(tzinfo=None)


class DialogueReplayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.review = None
        if REVIEW_CONFIG:
            self.review = AsyncOpenAI(api_key=REVIEW_CONFIG["api_key"], base_url=REVIEW_CONFIG["base_url"],
                                      max_retries=0, timeout=45)
            self.addAsyncCleanup(self.review.close)
        temp = TemporaryDirectory(prefix="dafeiyu-dialogue-")
        self.addCleanup(temp.cleanup)
        folder = Path(temp.name)
        self.enterContext(patch.object(chat_efficiency, "store_path", return_value=folder / "usage.db"))
        self.enterContext(patch.object(face_module, "datetime", ReplayClock))
        self.enterContext(patch.object(life_module, "datetime", ReplayClock))
        self.life = life_module.LifeContinuityPlugin(SimpleNamespace(), {
            "enabled": True, "owner_scope": PRIVATE, "web_search_enabled": True,
        })
        with patch.object(life_module.StarTools, "get_data_dir", return_value=folder / "life"):
            await self.life.initialize()
        self.addAsyncCleanup(self.life.terminate)
        self.assertIsNotNone(self.life.engine)
        self.life.engine.ledger.clock = lambda: ReplayClock.moment.timestamp()
        title = "星港对战铁卫攻略"
        goal = self.life.store.create_goal(PRIVATE, "matchup", title, "测试目标", "已核对版本", "查看公开说明")
        checkpoint = '{"last_search":{"status":"returned","query":"星港对战铁卫攻略"}}'
        self.life.store.db.execute("UPDATE goals SET checkpoint_json=? WHERE id=?", (checkpoint, goal["id"]))
        self.list_jobs = AsyncMock()
        self.schedule = schedule_module.ScheduleAwarenessPlugin(SimpleNamespace(
            cron_manager=SimpleNamespace(list_jobs=self.list_jobs),
        ))
        self.memory_lookup = AsyncMock(return_value=[SimpleNamespace(
            doc_id="fixture", content="固定回放记忆，之前聊过书单。", final_score=0.8, metadata={},
        )])
        settings = {
            "filtering_settings.memory_scope_mode": "session", "recall_engine.top_k": 1,
            "access_control.whitelist_enabled": True, "access_control.allowed_ids": [OWNER],
        }
        self.memory = memory_module.MemoryRecall(
            None, SimpleNamespace(get=settings.get, filtering_settings={}),
            SimpleNamespace(search_memories=self.memory_lookup),
            SimpleNamespace(add_message_from_event=AsyncMock()),
            SimpleNamespace(get_event_message_str=AsyncMock(side_effect=lambda event: event.get_message_str()),
                            enforce_message_limit=AsyncMock()),
            injection_module.InjectionAdapter(),
        )
        self.enterContext(patch.object(memory_module, "get_persona_id", new=AsyncMock(return_value="dialogue-test")))

    async def test_fixed_dialogues_reach_mock_provider_and_platform(self):
        REPLAY_RECORDS.clear()
        for category, cases in CASES.items():
            for case in cases:
                with self.subTest(category=category, case=case.name):
                    history = []
                    first = await self.replay_turn(case, category, 1, case.first, case.first_reply, "", history)
                    if case.signal == "legacy_cron":
                        history.extend([
                            {"role": "user", "content": "Output your last task result below."},
                            {"role": "assistant", "content": "[CronJob] 内部条目 triggered at 2026-01-15T20:00:00+08:00, 旧结果"},
                        ])
                    second = await self.replay_turn(case, category, 2, case.second, case.second_reply, case.signal, history)
                    REPLAY_RECORDS.append({"case": case.name, "category": category, "turns": [first, second]})
        self.assertEqual(len(REPLAY_RECORDS), 30)

    async def replay_turn(self, case, category, step, text, reply, signal, history):
        elapsed = timedelta(hours=12) if category == "跨天" and step == 2 else timedelta(minutes=step - 1)
        ReplayClock.moment = START + elapsed
        session = {"tts_enabled": signal == "voice"}
        face, event, result, _, _, pipeline = fixture("", session)
        raw = envelope("", reply) if signal == "voice" else reply
        scope = GROUP if category == "群聊" else PRIVATE
        sender = OTHER if signal == "foreign" else OWNER
        event.unified_msg_origin = scope
        event.get_sender_id.return_value = sender
        event.get_message_type.return_value = MessageType.GROUP_MESSAGE if scope == GROUP else MessageType.FRIEND_MESSAGE
        event.get_group_id.return_value = "2001" if scope == GROUP else ""
        event.get_messages.return_value = [Plain(text)]
        event.get_message_str.return_value = text
        event.message_obj.message_id = f"{case.name}:{step}"
        self.list_jobs.return_value = [
            self.job("提交材料", scope, OWNER), self.job("其他发言人的提醒", scope, "1003"),
            self.job("其它会话的提醒", "test:FriendMessage:9999", OWNER),
        ]
        lookup = AsyncMock(return_value=[{"title": "公开日历", "url": "https://example.invalid/calendar", "text": "周五。"}])
        if signal == "timeout":
            lookup.side_effect = search_module.SearchError("search_timeout")
        self.life.engine.searcher = lookup
        req = ProviderRequest(prompt=text, system_prompt=REPLAY_PERSONA, contexts=list(history), func_tool=ToolSet([
            FunctionTool(name=name, description=name, parameters={})
            for name in ("life_recall", "recall_long_term_memory", "web_search_anysearch")
        ]))
        if signal == "image":
            if self.review:
                req.extra_user_content_parts.append(TextPart(text="<image_caption>合成样本：一张猫咪瞪圆眼睛的表情包。</image_caption>"))
            else:
                req.extra_user_content_parts.append(ImageURLPart(image_url={"url": IMAGE}))
        if signal == "caption":
            req.extra_user_content_parts.append(TextPart(text="<image_caption>表情包里的虚构台词。</image_caption>"))
        if REPLAY_EXPERIENCE:
            from astrbot.core.utils.conversation_turn import route_capabilities

            route_capabilities(event, req)
        self.memory_lookup.reset_mock()
        await self.memory.handle_memory_recall(event, req)
        with patch.object(face_module.sp, "get_async", new=AsyncMock(return_value=session)):
            await face.inject_image_reply_style(event, req)
            if company_hook := getattr(face, "inject_company_style", None):
                await company_hook(event, req)
            await face.inject_voice_status(event, req)
        await self.schedule.inject_schedule(event, req)
        await self.life.inject_context(event, req)
        user = await req.assemble_context()
        response, wire = await self.complete(req, user, raw, case.name)
        if self.review:
            raw = response.completion_text or ""
        else:
            self.assertEqual(response.completion_text, raw)
        await face.scrub_drafts(event, response)
        result.chain = list(response.result_chain.chain) if response.result_chain else [Plain(response.completion_text or "")]
        sent = await self.deliver(face, event, pipeline, session)
        if not self.review:
            self.assertEqual(sent, [{"type": "voice" if signal == "voice" else "text", "text": reply}])

        history.extend(dump_messages_with_checkpoints(bind_checkpoint_messages([user])))
        history.append({"role": "assistant", "content": response.completion_text})
        current = wire["messages"][-1]["content"]
        texts = [part.get("text", "") for part in current] if isinstance(current, list) else [current]
        self.assertIn(text, "\n".join(texts))
        blocks = {name: block for name, marker in CONTEXT_MARKERS.items()
                  if (block := next((value for value in texts if marker in value), None)) is not None}
        context = sorted(blocks)
        self.assertIn("media_state", context)
        self.assertEqual("memory" in context, self.memory_lookup.await_count == 1)
        if signal in {"image", "caption"}:
            self.assertIn("image_chat", context)
        if signal in {"search", "timeout", "clarify"}:
            self.assertIn("search_receipt", context)
            self.assertEqual(lookup.await_count, int(signal != "clarify"))
        if signal in {"schedule", "foreign"} and "日程" in text:
            self.assertIn("schedule", context)
        if signal == "legacy_cron":
            self.assertNotIn("旧结果", str(wire["messages"]))
        receipt = blocks.get("search_receipt")
        receipt_status = json.loads(receipt.rsplit("\n", 1)[-1])["status"] if receipt else None
        return {
            "at": ReplayClock.moment.isoformat(), "user": text, "context": context,
            "history_roles": [entry["role"] for entry in wire["messages"][1:-1]],
            "tools": [item["function"]["name"] for item in wire.get("tools", [])],
            "lookup_calls": lookup.await_count, "memory_calls": self.memory_lookup.await_count,
            "receipt_status": receipt_status,
            "generation_calls": 1,
            "schedule_items": [line[2:] for line in blocks.get("schedule", "").splitlines() if line.startswith("- ")],
            "model_reply": raw, "sent": sent,
        }

    @staticmethod
    def job(name, scope, sender):
        return SimpleNamespace(name=name, enabled=True, payload={"session": scope, "sender_id": sender},
                               timezone="Asia/Shanghai", run_once=True, cron_expression="",
                               next_run_time="2026-01-16T09:00:00+08:00", description="固定回放提醒")

    async def complete(self, req, user, text, case_name):
        sdk = AsyncMock(return_value=ChatCompletion(
            id="dialogue-test", object="chat.completion", created=0, model=REPLAY_MODEL,
            choices=[{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        ))
        if self.review:
            async def live_completion(**payload):
                payload.pop("tools", None)
                payload.pop("tool_choice", None)
                return await self.review.chat.completions.create(
                    **payload, temperature=0.0, max_tokens=500, extra_body=REVIEW_CONFIG["extra_body"],
                )
            sdk.side_effect = live_completion
        provider = ProviderOpenAIOfficial.__new__(ProviderOpenAIOfficial)
        provider.provider_config = {"id": "dialogue-test", "companion_context": {"enabled": True}}
        provider.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=sdk)))
        provider.default_params = {"model", "messages", "tools", "tool_choice"}
        provider.reasoning_key = "reasoning_content"
        messages = [{"role": "system", "content": req.system_prompt}, *req.contexts, user]
        response = await provider._query({"model": REPLAY_MODEL, "messages": messages}, req.func_tool,
                                         request_max_retries=1, conversation_id=case_name)
        sdk.assert_awaited_once()
        self.assertEqual(sdk.await_args.kwargs["model"], REPLAY_MODEL)
        return response, sdk.await_args.kwargs

    async def deliver(self, face, event, pipeline, session):
        handler = SimpleNamespace(handler=face.convert_faces, handler_module_path="test.dialogue", handler_name="faces")
        decorator, responder = ResultDecorateStage(), RespondStage()
        await decorator.initialize(pipeline)
        await responder.initialize(pipeline)
        with (
            patch.object(face_module.sp, "get_async", new=AsyncMock(return_value=session)),
            patch("astrbot.core.pipeline.result_decorate.stage.star_handlers_registry.get_handlers_by_event_type",
                  return_value=[handler]),
            patch.dict("astrbot.core.pipeline.result_decorate.stage.star_map",
                       {"test.dialogue": SimpleNamespace(name="dialogue replay")}),
            patch("astrbot.core.pipeline.respond.stage.call_event_hook", new=AsyncMock(return_value=False)),
        ):
            async for _ in decorator.process(event):
                pass
            await responder.process(event)
        if not self.review:
            event.send.assert_awaited_once()
        return [
            {"type": "voice" if isinstance(part, Record) else "text", "text": part.text}
            for call in event.send.await_args_list for part in call.args[0].chain if isinstance(part, (Plain, Record))
        ]

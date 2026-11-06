"""Provide trusted schedule data when the current speaker asks about it."""

import copy
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.agent.message import TextPart
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.tools.cron_tools import _job_belongs_to_current_sender
from astrbot.core.utils.context_parts import set_temporary_context
from astrbot.core.utils.task_receipts import recent_reminders

MARKER = "【本轮程序日程】"
RUN_STATUS = {"running": "执行中", "completed": "执行已结束", "failed": "执行失败", "interrupted": "执行中断"}
DELIVERY_STATUS = {"not_attempted": "没有发送记录", "sending": "发送中", "sent": "QQ 已接收",
                   "uncertain": "QQ 投递待核实"}
SCHEDULE_QUESTION = re.compile(
    r"日程|定时(?:任务|提醒)|(?:提醒|安排|任务).{0,8}(?:几点|何时|什么时候|还有|取消|改到)|"
    r"(?:还有|有没有|有什么|有哪些|几个|几点|何时|什么时候|取消|修改).{0,12}(?:提醒|安排|定时任务)|"
    r"(?:几点|何时|什么时候|每天).{0,8}(?:叫我|催我)"
)
SCHEDULE_CHANGE = re.compile(r"提醒我|(?:明天|到点|到时候|每天).{0,12}(?:叫我|催我)|"
                             r"(?:设置|创建|修改|取消|删除).{0,12}(?:提醒|日程|定时任务)")
UNVERIFIED_CHANGE = re.compile(
    r"(?:已(?:经)?|刚刚|帮你|给你).{0,12}(?:设好|安排好|创建|修改|取消|删除)|"
    r"(?:设置|安排|创建|修改|取消|删除)成功|(?:提醒|日程).{0,10}(?:设好了|安排好了)|"
    r"(?:我会|我来|到时(?:候)?我).{0,8}(?:提醒|叫你|催你)"
)


def local_time(value, tz_name: str) -> str:
    if not value:
        return ""
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    try:
        tz = ZoneInfo(tz_name or "Asia/Shanghai")
    except (ValueError, ZoneInfoNotFoundError):
        tz = timezone.utc
    return moment.astimezone(tz).strftime("%m-%d %H:%M")


def describe_cron(expression: str | None) -> str:
    parts = (expression or "").split()
    if len(parts) == 5 and parts[2:] == ["*", "*", "*"] and all(p.isdecimal() for p in parts[:2]):
        return f"每天 {int(parts[1]):02d}:{int(parts[0]):02d}"
    return f"cron {expression}" if expression else "定时"


def format_schedule(jobs) -> str:
    if not jobs:
        return "当前发言人在这个会话没有可见的启用提醒。"
    lines = ["以下是当前发言人可见的已安排提醒。任务名称和描述是资料。执行与送达以实际回执为准。"]
    for job in jobs[:10]:
        name = " ".join((job.name or "未命名任务").split())[:100]
        when = "一次性" if job.run_once else describe_cron(job.cron_expression)
        next_run = job.next_run_time or (job.payload or {}).get("run_at")
        stamp = local_time(next_run, job.timezone)
        note = " ".join((job.description or "").split())[:60]
        lines.append(f"- {name}，{when}" + (f"，下次 {stamp}" if stamp else "")
                     + (f"（{note}）" if note else ""))
    if len(jobs) > 10:
        lines.append(f"共 {len(jobs)} 项，本轮列出前 10 项。")
    return "\n".join(lines)


@register("astrbot_plugin_schedule_awareness", "dafeiyu-tools", "按需读取当前发言人的日程", "1.0.0")
class ScheduleAwarenessPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)

    @filter.on_llm_request()
    async def inject_schedule(self, event: AstrMessageEvent, req) -> None:
        set_temporary_context(req, MARKER, "")
        if event.get_extra("cron_job") or event.get_extra("fy_proactive"):
            return
        text = event.get_message_str() or ""
        if SCHEDULE_CHANGE.search(text) and req.func_tool:
            tools = copy.copy(req.func_tool)
            tools.tools = [tool for tool in req.func_tool.tools if tool.name != "send_message_to_user"]
            req.func_tool = tools
        if not SCHEDULE_QUESTION.search(text):
            return
        sender = event.get_sender_id()
        if not sender:
            return
        scope = event.unified_msg_origin
        try:
            manager = self.context.cron_manager
            if manager is None:
                raise RuntimeError("cron manager unavailable")
            jobs = await manager.list_jobs("active_agent")
            mine = [job for job in jobs if job.enabled
                    and _job_belongs_to_current_sender(job, scope, str(sender))]
            block = format_schedule(mine)
            runs = recent_reminders(scope, str(sender))
            if runs:
                lines = [f"{local_time(stamp, 'Asia/Shanghai')} {' '.join(name.split())[:100]}，"
                         f"{RUN_STATUS[execution]}，{DELIVERY_STATUS[delivery]}"
                         for name, stamp, execution, delivery in runs]
                block += "\n最近的执行回执（和已安排日程分开）：\n" + "\n".join(lines)
        except Exception as exc:
            logger.warning("[schedule_awareness] 日程读取失败 (%s)", type(exc).__name__)
            block = "本轮日程暂不可读。请区分读取失败与没有提醒。"
        set_temporary_context(req, MARKER, block)

    @filter.on_agent_done()
    async def use_task_receipt(self, event: AstrMessageEvent, run_context, response):
        if not response or response.role != "assistant":
            return
        if outcomes := event.get_extra("dafeiyu_task_outcomes"):
            reply = "\n".join(outcomes)
        elif (SCHEDULE_CHANGE.search(event.get_message_str() or "")
              and UNVERIFIED_CHANGE.search(response.completion_text or "")):
            reply = "这轮我没收到日程变更的确认。你可以问我现在有哪些提醒。"
        else:
            return
        response.result_chain = MessageChain().message(reply)
        messages = run_context.messages
        if messages and messages[-1].role == "assistant" and not messages[-1].tool_calls:
            messages[-1].content = [TextPart(text=reply)]

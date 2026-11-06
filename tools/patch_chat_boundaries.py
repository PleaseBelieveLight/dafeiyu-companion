"""Install the conversation and reminder hooks for the pinned AstrBot revision."""

import json
from pathlib import Path


CORE = "astrbot/astrbot/core/"
WAKE = CORE + "pipeline/waking_check/stage.py"
WHITELIST = CORE + "pipeline/whitelist_check/stage.py"
CRON_TOOLS = CORE + "tools/cron_tools.py"
CRON_MANAGER = CORE + "cron/manager.py"
CRON_EVENTS = CORE + "cron/events.py"
MESSAGE_TOOLS = CORE + "tools/message_tools.py"
CONVERSATIONS = CORE + "conversation_mgr.py"
QQ_ADAPTER = CORE + "platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py"
MAIN_AGENT = CORE + "astr_main_agent.py"
MEMORY_SCOPE = "astrbot/data/plugins/astrbot_plugin_livingmemory/core/memory_scope.py"
PATCHED_PATHS = (WAKE, WHITELIST, CRON_TOOLS, CRON_MANAGER, CRON_EVENTS, MESSAGE_TOOLS,
                 CONVERSATIONS, QQ_ADAPTER, MAIN_AGENT)


def patch(target, relative, *replacements, reverse=False):
    path = target / relative
    original = text = path.read_text(encoding="utf-8-sig")
    for old, new in reversed(replacements) if reverse else replacements:
        if reverse:
            text = text.replace(new, old, 1)
            continue
        if new in text:
            continue
        if text.count(old) != 1:
            raise RuntimeError(f"Pinned chat boundary changed in {relative}: {old.strip().splitlines()[0]}")
        text = text.replace(old, new, 1)
    if original != text:
        path.write_text(text, encoding="utf-8", newline="\n")


def apply_chat_boundaries(target: Path) -> None:
    patch(target, WAKE,
        ("from astrbot import logger\n",
         "from astrbot import logger\nfrom astrbot.core.utils.chat_scope import claim_qq_event, qq_group_scope\n"),
        ('        event.set_extra("_session_isolated", False)\n        if self.unique_session',
         '        event.set_extra("_session_isolated", False)\n'
         '        qq_group = event.get_platform_name() == "aiocqhttp" and event.message_obj.type == MessageType.GROUP_MESSAGE\n'
         '        if qq_group and (not qq_group_scope(event) or not str(event.get_sender_id()).isdigit()):\n'
         '            event.stop_event()\n            return\n'
         '        if (self.unique_session or qq_group)'),
        ('                api_key_allow_admin_role is not False\n',
         '                not qq_group and api_key_allow_admin_role is not False\n'),
        ('            if message_str.startswith(wake_prefix):\n',
         '            if (wake_prefix or not qq_group) and message_str.startswith(wake_prefix):\n'),
        ('                    and isinstance(messages[0], At)\n',
         '                    and messages and isinstance(messages[0], At)\n'),
        ('or (isinstance(message, AtAll) and not self.ignore_at_all)',
         'or (isinstance(message, AtAll) and not (self.ignore_at_all or qq_group))'),
        ('        # 检查插件的 handler filter\n',
         '        if (qq_group and not is_wake) or not claim_qq_event(event):\n'
         '            event.stop_event()\n            return\n\n'
         '        # 检查插件的 handler filter\n'),
    )
    patch(target, WHITELIST,
        ("from astrbot.core import logger\n", "from astrbot.core import logger\nfrom astrbot.core.utils.chat_scope import qq_group_scope\n"),
        ('        if not self.enable_whitelist_check:\n',
         '        qq_group = qq_group_scope(event)\n        if not self.enable_whitelist_check and not qq_group:\n'),
        ('        if len(self.whitelist) == 0:', '        if len(self.whitelist) == 0 and not qq_group:'),
        ('        if self.wl_ignore_admin_on_group:', '        if self.wl_ignore_admin_on_group and not qq_group:'),
        ('            event.unified_msg_origin not in self.whitelist\n',
         '            event.unified_msg_origin not in self.whitelist\n            and qq_group not in self.whitelist\n'),
    )
    patch(target, CONVERSATIONS,
        ('        if not conv and create_if_not_exists:\n',
         '        if conv and conv.user_id != unified_msg_origin:\n            return None\n'
         '        if not conv and create_if_not_exists:\n'),
        ('        self.session_conversations[unified_msg_origin] = conversation_id\n',
         '        if not await self.get_conversation(unified_msg_origin, conversation_id):\n            return\n'
         '        self.session_conversations[unified_msg_origin] = conversation_id\n'),
        ('        if conversation_id:\n            await self.db.delete_conversation(cid=conversation_id)\n',
         '        if conversation_id and await self.get_conversation(unified_msg_origin, conversation_id):\n'
         '            await self.db.delete_conversation(cid=conversation_id)\n'),
        ('        if conversation_id:\n            await self.db.update_conversation(\n',
         '        if conversation_id:\n'
         '            stored = await self.db.get_conversation_by_id(cid=conversation_id)\n'
         '            if not stored or stored.user_id != unified_msg_origin:\n                return\n'
         '            await self.db.update_conversation(\n'),
    )
    patch(target, QQ_ADAPTER,
        ('from astrbot.core.platform.astr_message_event import MessageSesion\n',
         'from astrbot.core.platform.astr_message_event import MessageSesion\n'
         'from astrbot.core.utils.chat_scope import qq_quote_matches\n'),
        ('                    if not get_reply:\n                        a = ComponentTypes[t](**m["data"])\n'
         '                        abm.message.append(a)\n',
         '                    if not get_reply:\n                        continue\n'),
        ('                            # 添加必要的 post_type 字段，防止 Event.from_payload 报错\n',
         '                            if not await qq_quote_matches(self.bot, event, reply_event_data, routing_params):\n'
         '                                continue\n'
         '                            # 添加必要的 post_type 字段，防止 Event.from_payload 报错\n'),
        ('                            logger.error(f"获取引用消息失败: {e}。")\n'
         '                            a = ComponentTypes[t](**m["data"])\n'
         '                            abm.message.append(a)\n',
         '                            logger.warning("无法核验 QQ 引用归属 (%s)", type(e).__name__)\n'),
    )
    patch(target, MAIN_AGENT,
        ('from astrbot.core.platform.message_type import MessageType\n',
         'from astrbot.core.platform.message_type import MessageType\n'
         'from astrbot.core.utils.chat_scope import restrict_group_tools\n'),
        ('    fallback_providers = _get_fallback_chat_providers(\n',
         '    restrict_group_tools(event, req)\n\n    fallback_providers = _get_fallback_chat_providers(\n'),
    )
    patch(target, CRON_TOOLS,
        ("from astrbot import logger\n", "from astrbot import logger\nfrom astrbot.core.utils.chat_scope import owns_chat_job\n"),
        ('        _extract_job_session(job) == current_umo\n        and _extract_job_sender(job) == current_sender_id',
         '        owns_chat_job(_extract_job_session(job), _extract_job_sender(job), current_umo, current_sender_id)'),
        ('        cron_mgr = context.context.context.cron_manager\n',
         '        event = context.context.event\n'
         '        if event.get_platform_name() == "cron" or event.get_extra("fy_proactive"):\n'
         '            return "error: scheduled jobs cannot manage other schedules."\n'
         '        if str(kwargs.get("action") or "").strip().lower() == "create":\n'
         '            from astrbot.core.utils.task_receipts import create_task_once\n\n'
         '            return await create_task_once(event, kwargs, lambda: self._call(context, **kwargs))\n'
         '        return await self._call(context, **kwargs)\n\n'
         '    async def _call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:\n'
         '        cron_mgr = context.context.context.cron_manager\n'),
        ('            return f"Scheduled future task {job.job_id} ({job.name}) {suffix}."\n',
         '            from astrbot.core.utils.task_receipts import record_task_outcome\n\n'
         '            when = f"，下次 {next_run:%Y-%m-%d %H:%M}" if next_run else ""\n'
         '            record_task_outcome(context.context.event, f"已安排提醒「{job.name}」{when}。")\n'
         '            return f"Scheduled future task {job.job_id} ({job.name}) {suffix}."\n'),
        ('            return f"Updated future task {job.job_id} ({job.name})."\n',
         '            from astrbot.core.utils.task_receipts import record_task_outcome\n\n'
         '            record_task_outcome(context.context.event, f"已修改提醒「{job.name}」。")\n'
         '            return f"Updated future task {job.job_id} ({job.name})."\n'),
        ('            return f"Deleted cron job {job_id}."\n',
         '            from astrbot.core.utils.task_receipts import record_task_outcome\n\n'
         '            record_task_outcome(context.context.event, f"已取消提醒「{job.name}」。")\n'
         '            return f"Deleted cron job {job_id}."\n'),
    )
    patch(target, CRON_MANAGER,
        ("from astrbot.core.utils.history_saver import persist_agent_history\n",
         "from astrbot.core.utils.history_saver import persist_agent_history\n"
         "from astrbot.core.utils.task_receipts import recover_reminders, reserve_reminder, settle_reminder\n"),
        ('            await self.sync_from_db()\n', '            recover_reminders()\n            await self.sync_from_db()\n'),
        ('        start_time = datetime.now(timezone.utc)\n        await self.db.update_cron_job(\n',
         '        start_time = datetime.now(timezone.utc)\n'
         '        run = None\n'
         '        if job.job_type == "active_agent":\n'
         '            trigger = None if ignore_enabled or job.run_once else self._build_trigger(job)\n'
         '            run = reserve_reminder(job, start_time, trigger, manual=ignore_enabled)\n'
         '            if run is None:\n                return\n'
         '        await self.db.update_cron_job(\n'),
        ('                await self._run_active_agent_job(job, start_time=start_time)\n',
         '                await self._run_active_agent_job(job, start_time=start_time, run=run)\n'),
        ('        except Exception as e:  # noqa: BLE001\n            status = "failed"\n',
         '        except asyncio.CancelledError:\n            status = "interrupted"\n            raise\n'
         '        except Exception as e:  # noqa: BLE001\n            status = "failed"\n'),
        ('        finally:\n            next_run = self._get_next_run_time(job_id)\n',
         '        finally:\n            if run:\n                settle_reminder(run, status)\n'
         '            next_run = self._get_next_run_time(job_id)\n'),
        ('    async def _run_active_agent_job(self, job: CronJob, start_time: datetime) -> None:\n',
         '    async def _run_active_agent_job(self, job: CronJob, start_time: datetime, *, run: str) -> None:\n'),
        ('        if run_once and run_at:\n            payload = {**payload, "run_at": run_at.isoformat()}\n',
         '        if run_once and run_at:\n            payload = {**payload, "run_at": run_at.isoformat()}\n'
         '        if enabled:\n'
         '            trigger = self._build_trigger(CronJob(name=name, job_type="active_agent",\n'
         '                cron_expression=cron_expression, payload=payload, timezone=timezone, run_once=run_once))\n'
         '            if isinstance(trigger, DateTrigger) and trigger.run_date <= datetime.now(trigger.run_date.tzinfo):\n'
         '                raise CronJobSchedulingError("One-time task must be scheduled in the future")\n'),
        ('        note = payload.get("note") or job.description or job.name\n',
         '        if delivery_session_str:\n'
         '            platform, kind, destination = delivery_session_str.split(":", 2)\n'
         '            if kind == "GroupMessage":\n'
         '                actor = str(payload.get("sender_id") or "")\n'
         '                group = destination.rsplit("_", 1)[-1]\n'
         '                session_str = f"{platform}:{kind}:{actor if actor.isdigit() else job.job_id}_{group}"\n'
         '        note = payload.get("note") or job.description or job.name\n'),
        ('            "cron_payload": payload,\n', '            "cron_payload": payload,\n            "dafeiyu_reminder_run": run,\n'),
        ('        if cron_payload.get("origin", "tool") == "api":\n            cron_event.role = "admin"\n',
         '        if session.message_type == MessageType.GROUP_MESSAGE:\n            cron_event.role = "member"\n'
         '        elif cron_payload.get("origin", "tool") == "api":\n            cron_event.role = "admin"\n'),
        ('        if runner.state == AgentState.ERROR:\n',
         '        await persist_agent_history(self.ctx.conversation_manager, event=cron_event, req=req, summary_note="")\n'
         '        if runner.state == AgentState.ERROR:\n'),
        ('        cron_meta = extras.get("cron_job", {}) if extras else {}\n'
         '        summary_note = (\n'
         '            f"[CronJob] {cron_meta.get(\'name\') or cron_meta.get(\'id\', \'unknown\')}: {cron_meta.get(\'description\', \'\')} "\n'
         '            f" triggered at {cron_meta.get(\'run_started_at\', \'unknown time\')}, "\n'
         '        )\n'
         '        if llm_resp and llm_resp.role == "assistant":\n'
         '            summary_note += (\n'
         '                f"I finished this job, here is the result: {llm_resp.completion_text}"\n'
         '            )\n\n'
         '        await persist_agent_history(\n'
         '            self.ctx.conversation_manager,\n'
         '            event=cron_event,\n'
         '            req=req,\n'
         '            summary_note=summary_note,\n'
         '        )\n'
         '        if not llm_resp:\n            logger.warning("Cron job agent got no response")\n            return\n',
         '        if not llm_resp:\n            raise RuntimeError("Cron job agent got no response")\n'),
    )
    patch(target, CRON_EVENTS,
        ('from astrbot.core.platform.platform_metadata import PlatformMetadata\n',
         'from astrbot.core.platform.platform_metadata import PlatformMetadata\n'
         'from astrbot.core.utils.task_receipts import send_reminder_once\n'),
        ('        if not await self.context_obj.send_message(self.session, message):\n',
         '        if self.get_extra("cron_job"):\n'
         '            sent = await send_reminder_once(self.context_obj, self, message)\n'
         '        else:\n            sent = await self.context_obj.send_message(self.session, message)\n'
         '        if not sent:\n'),
    )
    patch(target, MESSAGE_TOOLS,
        ('        if session != current_session:\n',
         '        reminder = context.context.event.get_extra("cron_job")\n'
         '        if reminder and session not in (current_session, reminder.get("session")):\n'
         '            return "error: reminders can only deliver to their configured session."\n'
         '        if not reminder and session != current_session:\n'),
        ('            sent = await context.context.context.send_message(\n'
         '                target_session,\n                message_chain,\n            )\n',
         '            if reminder:\n'
         '                from astrbot.core.utils.task_receipts import send_reminder_once\n\n'
         '                sent = await send_reminder_once(context.context.context, context.context.event, message_chain)\n'
         '                if sent:\n                    context.context.event._has_send_oper = True\n'
         '                return ("Reminder accepted by QQ." if sent else\n'
         '                        "No new delivery: this reminder has a prior attempt or no configured destination.")\n'
         '            sent = await context.context.context.send_message(target_session, message_chain)\n'),
    )
    if (target / MEMORY_SCOPE).is_file():
        patch(target, MEMORY_SCOPE,
            ('import re\n', 'import re\n\nfrom astrbot.core.utils.chat_scope import qq_group_scope\n'),
            ('    if not _config_get(config, "access_control.whitelist_enabled", False):\n',
             '    if getattr(event, "get_extra", lambda key: None)("cron_job"):\n        return False\n'
             '    if not _config_get(config, "access_control.whitelist_enabled", False):\n'),
            ('    candidates = {\n        sender_id,\n',
             '    candidates = {\n        qq_group_scope(event),\n        sender_id,\n'),
            ('    isolated_sessions = set(\n',
             '    if _event_value(event, "get_platform_name") == "aiocqhttp":\n        return session_id\n'
             '    isolated_sessions = set(\n'),
        )
    path = target / "astrbot/data/cmd_config.json"
    if path.is_file():
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if configure_group_settings(value):
            temporary = path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)


def configure_group_settings(config):
    settings = config.setdefault("provider_ltm_settings", {})
    active = settings.setdefault("active_reply", {})
    changed = any(item.get(key) is not False for item, key in (
        (settings, "group_icl_enable"), (settings, "group_message_history_enable"), (active, "enable"),
    )) or "possibility_reply" in active
    settings.update(group_icl_enable=False, group_message_history_enable=False)
    active["enable"] = False
    active.pop("possibility_reply", None)
    return changed

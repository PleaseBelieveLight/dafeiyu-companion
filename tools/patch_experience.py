"""Configure event-driven contact and retire automatic add-ons in the pinned runtime."""

import json
from functools import partial

from patch_chat_boundaries import MAIN_AGENT, patch as patch_file
from patch_chat_efficiency import MEMORY_RECALL_PATH


PLUGINS = "astrbot/data/plugins/"
PROACTIVE = PLUGINS + "astrbot_plugin_proactive_chat/"
PROACTIVE_MAIN = PROACTIVE + "main.py"
PROACTIVE_START = PROACTIVE + "core/services/lifecycle_service.py"
PROACTIVE_FLOW = PROACTIVE + "core/pipeline/flow_orchestrator.py"
PIPELINE = "astrbot/astrbot/core/pipeline/scheduler.py"
RESPOND = "astrbot/astrbot/core/pipeline/respond/stage.py"
QQ_EVENT = "astrbot/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_message_event.py"
INTERNAL = "astrbot/astrbot/core/pipeline/process_stage/method/agent_sub_stages/internal.py"
AGENT_HOOKS = "astrbot/astrbot/core/astr_agent_hooks.py"
PARSER = PLUGINS + "astrbot_plugin_parser/main.py"
MUSIC = PLUGINS + "astrbot_plugin_music/main.py"
POKE = PLUGINS + "astrbot_plugin_pokepro/main.py"
OMNIDRAW = PLUGINS + "astrbot_plugin_omnidraw/main.py"
VIDEO = PLUGINS + "astrbot_plugin_omnidraw/core/video_manager.py"
STEALER = PLUGINS + "astrbot_plugin_stealer/"
STEALER_MAIN = STEALER + "main.py"
STEALER_EVENTS = STEALER + "core/events/event_handler.py"
INTERACTION_PLUGINS = ("parser", "music", "pokepro", "omnidraw", "stealer")
PLUGIN_CONFIGS = ("proactive_chat", "stealer", "life_continuity", "pokepro")
CONFIG_PATHS = tuple(f"astrbot/data/config/astrbot_plugin_{name}_config.json" for name in PLUGIN_CONFIGS)
EXPERIENCE_PATHS = (PROACTIVE_MAIN, PROACTIVE_START, PROACTIVE_FLOW, PIPELINE, RESPOND, QQ_EVENT,
                    INTERNAL, AGENT_HOOKS, PARSER, MUSIC, POKE, OMNIDRAW, VIDEO,
                    STEALER_MAIN, STEALER_EVENTS, *CONFIG_PATHS)


def configure_experience_plugin(name, values):
    if name == "proactive_chat":
        for key in ("friend_settings", "group_settings"):
            settings = values.setdefault(key, {})
            settings.setdefault("auto_trigger_settings", {})["enable_auto_trigger"] = False
            if key == "group_settings":
                settings["enable"] = False
    elif name == "stealer":
        values.update(auto_send_meme=False, meme_chance=0.0)
    elif name == "life_continuity" and values.get("enabled") and values.get("owner_scope"):
        values.setdefault("followups_enabled", True)
    elif name == "pokepro":
        values["poke_keywords"] = []


def apply_experience(target, *, reverse=False):
    patch = partial(patch_file, reverse=reverse)
    patch(target, MAIN_AGENT,
        ('from astrbot.core.utils.chat_scope import restrict_group_tools\n',
         'from astrbot.core.utils.chat_scope import restrict_group_tools\n'
         'from astrbot.core.utils.conversation_turn import route_capabilities, wants_knowledge\n'),
        ('    if not config.kb_agentic_mode:\n',
         '    if not wants_knowledge(event.get_message_str()):\n        return\n    if not config.kb_agentic_mode:\n'),
        ('    selected_provider = _select_image_chat_provider(provider, req, fallback_providers)\n',
         '    route_capabilities(event, req)\n'
         '    selected_provider = _select_image_chat_provider(provider, req, fallback_providers)\n'),
    )
    patch(target, PIPELINE,
        ('from astrbot.core.utils.active_event_registry import active_event_registry\n',
         'from astrbot.core.utils.active_event_registry import active_event_registry\n'
         'from astrbot.core.utils.conversation_turn import activate_metrics, begin_turn, finish_metrics, start_metrics\n'),
        ('            stage = self.stages[i]  # 获取当前要执行的阶段\n',
         '            stage = self.stages[i]  # 获取当前要执行的阶段\n'
         '            if stage.__class__.__name__ == "ProcessStage":\n'
         '                if not await begin_turn(event):\n                    return\n'
         '                activate_metrics(event)\n'),
        ('        active_event_registry.register(event)\n',
         '        token = start_metrics(event)\n        active_event_registry.register(event)\n'),
        ('                event.cleanup_temporary_local_files()\n',
         '                finish_metrics(event, token)\n                event.cleanup_temporary_local_files()\n'),
    )
    patch(target, RESPOND,
        ('    async def process(\n',
         '    @staticmethod\n    def _keep_turn(event, result):\n'
         '        from astrbot.core.utils.conversation_turn import current_turn\n\n'
         '        if (result.is_model_result() or result.result_content_type == ResultContentType.STREAMING_RESULT) and not current_turn(event):\n'
         '            event.set_extra("_fy_suppressed_reply", True)\n            return False\n'
         '        return True\n\n    async def process(\n'),
        ('        if result is None:\n            return\n',
         '        if result is None or not self._keep_turn(event, result):\n            return\n'),
        ('                    await asyncio.sleep(i)\n                    try:\n',
         '                    await asyncio.sleep(i)\n'
         '                    if not self._keep_turn(event, result):\n                        break\n'
         '                    try:\n'),
        ('                for comp in sep_comps:\n',
         '                for comp in sep_comps:\n'
         '                    if not self._keep_turn(event, result):\n                        break\n'),
        ('                if result.chain and len(result.chain) > 0:\n',
         '                if result.chain and self._keep_turn(event, result):\n'),
    )
    patch(target, QQ_EVENT,
        ('    async def send(self, message: MessageChain) -> None:\n        """发送消息"""\n',
         '    async def send(self, message: MessageChain) -> None:\n        """发送消息"""\n'
         '        import time\n\n        from astrbot.core.utils.conversation_turn import note_delivery\n\n'
         '        started = time.monotonic()\n'),
        ('        await super().send(message)\n',
         '        note_delivery(message, time.monotonic() - started)\n'
         '        text = message.get_plain_text().strip()\n'
         '        if text:\n'
         '            delivered = self.get_extra("_fy_delivered_text", [])\n'
         '            self.set_extra("_fy_delivered_text", [*delivered, text])\n'
         '        await super().send(message)\n'),
    )
    patch(target, INTERNAL,
        ('        checkpoint_id = event.get_extra("llm_checkpoint_id")\n        has_checkpoint = ',
         '        if (event.get_extra("_fy_suppressed_reply") and messages_to_save\n'
         '                and messages_to_save[-1].role == "assistant" and not messages_to_save[-1].tool_calls):\n'
         '            from astrbot.core.agent.message import TextPart\n\n'
         '            sent = event.get_extra("_fy_delivered_text", [])\n'
         '            if sent:\n                messages_to_save[-1].content = [TextPart(text="\\n".join(sent))]\n'
         '            else:\n                messages_to_save.pop()\n\n'
         '        checkpoint_id = event.get_extra("llm_checkpoint_id")\n        has_checkpoint = '),
    )
    patch(target, AGENT_HOOKS,
        ('        run_context.context.event.clear_result()\n',
         '        from astrbot.core.utils.conversation_turn import note_result\n\n'
         '        note_result("tool_missing" if tool_result is None else\n'
         '                    "tool_error" if getattr(tool_result, "isError", False) else "tool_returned")\n'
         '        if tool.name in {"search_meme", "send_meme", "steal_meme"}:\n'
         '            note_result(tool.name)\n'
         '        run_context.context.event.clear_result()\n'),
    )
    if (target / MEMORY_RECALL_PATH).is_file():
        patch(target, MEMORY_RECALL_PATH,
            ('from astrbot.core.utils.context_parts import skip_automatic_recall\n',
             'from astrbot.core.utils.context_parts import skip_automatic_recall\n'
             'from astrbot.core.utils.conversation_turn import has_recall_topic, select_memories\n'),
            ('                # 获取过滤配置\n',
             '                if not has_recall_topic(actual_query):\n                    return\n\n'
             '                # 获取过滤配置\n'),
            ('                    k=self.config_manager.get("recall_engine.top_k", 5),\n',
             '                    k=min(top_k, 3),\n'),
            ('                if recalled_memories:\n',
             '                recalled_memories = select_memories(recalled_memories, recall_session_id)\n'
             '                if recalled_memories:\n'),
        )
    if (target / PARSER).is_file():
        patch(target, PARSER,
            ('        text = event.message_str\n',
             '        from astrbot.core.utils.conversation_turn import latest_turn, media_cancelled\n\n'
             '        text = event.message_str\n'
             '        explicit = bool(re.match(r"(?:请|帮我|麻烦|你){0,3}(?:解析|下载|展开|提取)", text.strip()))\n'
             '        bare_link = re.fullmatch(r"https?://\\S+", text.strip())\n'
             '        if media_cancelled(text) or not (explicit or bare_link or\n'
             '                (not text.strip() and any(isinstance(seg, Json) for seg in chain))):\n'
             '            return\n'),
            ('        if self.cfg.enable_reply_parse and reply_seg and reply_seg.chain:\n',
             '        if explicit and self.cfg.enable_reply_parse and reply_seg and reply_seg.chain:\n'),
            ('        await self.sender.send_parse_result(event, parse_res)\n',
             '        if latest_turn(event):\n            await self.sender.send_parse_result(event, parse_res)\n'),
            ('        result = await self.parse_url(url)\n        return await self.sender.send_parse_result(event, result)\n',
             '        from astrbot.core.utils.conversation_turn import can_finish_media\n\n'
             '        result = await self.parse_url(url)\n'
             '        return can_finish_media(event) and await self.sender.send_parse_result(event, result)\n'),
            ('            await self.parse_and_send(event, url)\n            return "链接解析成功并已发送"\n',
             '            sent = await self.parse_and_send(event, url)\n'
             '            return "链接解析成功并已发送" if sent else "解析结果未发送"\n'),
        )
    if (target / MUSIC).is_file():
        patch(target, MUSIC,
            ('        # 搜索歌曲\n        logger.debug(f"正在通过',
             '        from astrbot.core.utils.conversation_turn import latest_turn, media_cancelled\n\n'
             '        # 搜索歌曲\n        logger.debug(f"正在通过'),
            ('        if not songs:\n            yield event.plain_result(f"搜索【{song_name}】无结果")\n',
             '        if not latest_turn(event):\n            return\n'
             '        if not songs:\n            yield event.plain_result(f"搜索【{song_name}】无结果")\n'),
            ('            arg = event.message_str.strip()\n            arg_lower = arg.lower()\n',
             '            arg = event.message_str.strip()\n'
             '            if media_cancelled(arg):\n'
             '                self.sender.clear_selection_context(event)\n'
             '                controller.stop()\n                event.stop_event()\n                return\n'
             '            arg_lower = arg.lower()\n'),
        )
    if (target / POKE).is_file():
        patch(target, POKE,
            ('        gid = event.get_group_id()\n        self_id = event.get_self_id()\n',
             '        self_id = event.get_self_id()\n'),
            ('        if not target_ids:\n            result: dict = await event.bot.get_group_msg_history(group_id=int(gid))\n'
             '            target_ids = [str(msg["sender"]["user_id"]) for msg in result["messages"]]\n',
             '        # A poke needs a named target or an explicit request to poke the speaker.\n'),
            ('        # 关键字触发戳一戳\n        if event.is_at_or_wake_command and self.cfg.poke_keywords:\n'
             '            if self.cfg.hit_poke_keywords(event.message_str):\n'
             '                await self.sender.event_send(\n                    event,\n'
             '                    target_ids=[event.get_sender_id()],\n                    times=1,\n                )\n',
             '        # Pokes require an explicit command or an incoming poke.\n'),
        )
    if (target / OMNIDRAW).is_file():
        patch(target, OMNIDRAW,
            ('        sent = 0\n        for result in results:\n            if not self._get_image_result_url(result):\n',
             '        from astrbot.core.utils.conversation_turn import can_finish_media\n\n'
             '        sent = 0\n        for result in results:\n'
             '            if not can_finish_media(event):\n                break\n'
             '            if not self._get_image_result_url(result):\n'),
            ('            await event.send(event.chain_result(\n'
             '                self._build_image_success_components(result, time.perf_counter() - started_at)\n',
             '            from astrbot.core.utils.conversation_turn import can_finish_media\n\n'
             '            if not can_finish_media(event):\n'
             '                self._complete_image_quota(event, reserved_count, 1)\n                return\n'
             '            await event.send(event.chain_result(\n'
             '                self._build_image_success_components(result, time.perf_counter() - started_at)\n'),
        )
    if (target / VIDEO).is_file():
        patch(target, VIDEO,
            ('        start_time = time.perf_counter()\n        providers = self._get_video_provider_chain()\n',
             '        from astrbot.core.utils.conversation_turn import can_finish_media\n\n'
             '        async def send(result):\n'
             '            if can_finish_media(event):\n                await event.send(result)\n\n'
             '        if not can_finish_media(event):\n            return\n'
             '        start_time = time.perf_counter()\n        providers = self._get_video_provider_chain()\n'),
            ('                for index, provider in enumerate(providers, start=1):\n',
             '                for index, provider in enumerate(providers, start=1):\n'
             '                    if not can_finish_media(event):\n                        return\n'),
            ('await event.send(event.chain_result([\n', 'await send(event.chain_result([\n'),
            ('await event.send(event.plain_result("❌ 抱歉，管理员尚未配置可用的视频渲染节点。"))',
             'await send(event.plain_result("❌ 抱歉，管理员尚未配置可用的视频渲染节点。"))'),
            ('await event.send(event.plain_result(f"❌ 视频生成失败: {safe_error}"))',
             'await send(event.plain_result(f"❌ 视频生成失败: {safe_error}"))'),
            ('await event.send(event.plain_result(f"❌ 后台视频渲染引擎发生错误：{safe_error}"))',
             'await send(event.plain_result(f"❌ 后台视频渲染引擎发生错误：{safe_error}"))'),
        )
    if (target / STEALER_MAIN).is_file():
        legacy_capture = '        # Passive collection is retired; explicit commands still use the library.\n'
        explicit_capture = (
            '        if self.event_handler is not None:\n'
            '            await self.event_handler.on_message(event, force_only=True)\n'
            '        if not event.is_stopped():\n'
            '            await capture_requested_meme(self, event)\n'
        )
        if legacy_capture in (target / STEALER_MAIN).read_text(encoding="utf-8-sig"):
            patch_file(target, STEALER_MAIN, (legacy_capture, explicit_capture))
        patch(target, STEALER_MAIN,
            ('from .task_scheduler import TaskScheduler\n',
             'from .capture_request import capture_requested_meme\nfrom .task_scheduler import TaskScheduler\n'),
            ('        event_handler = self._get_event_handler(\n'
             '            log_message="[Stealer] event_handler 未初始化，跳过消息处理",\n'
             '            log_level="debug",\n        )\n'
             '        if event_handler is None:\n            return\n'
             '        try:\n            await event_handler.on_message(event)\n'
             '        except Exception as e:\n'
             '            logger.error(f"[Stealer] 处理消息时发生错误: {e}", exc_info=True)\n',
             explicit_capture),
            ('        """Apply the passive-emoji gates and schedule at most one task per turn."""\n',
             '        """Apply the passive-emoji gates and schedule at most one task per turn."""\n'
             '        if not getattr(self, "auto_send_meme", False):\n            return False\n'),
            ('        - 你看到当前消息里有适合作为表情包的图片时，也可以调用本工具补充素材库。\n',
             '        - 只有用户明确要求入库时才调用。\n'),
        )
        patch(target, STEALER_EVENTS,
            ('    async def on_message(self, event: AstrMessageEvent):\n',
             '    async def on_message(self, event: AstrMessageEvent, *, force_only: bool = False):\n'),
            ('        force_active = force_entry is not None\n',
             '        force_active = force_entry is not None\n'
             '        if force_only and not force_active:\n            return\n'),
            ('            await self._handle_force_capture(event, plugin_instance, imgs, store_urls)\n',
             '            plugin_instance.consume_force_capture(event)\n'
             '            event.stop_event()\n'
             '            await self._handle_force_capture(event, plugin_instance, imgs, store_urls)\n'),
            ('            await event.send(MessageChain([Plain(text=f"❌ 收录失败：{e}")]))\n'
             '        finally:\n            try:\n                plugin_instance.consume_force_capture(event)\n'
             '            except Exception:\n                pass\n\n    async def _clean_raw_directory',
             '            await event.send(MessageChain([Plain(text=f"❌ 收录失败：{e}")]))\n'
             '\n    async def _clean_raw_directory'),
        )
    if (target / PROACTIVE_MAIN).is_file():
        patch(target, PROACTIVE_MAIN,
            ('        await self.event_handlers.on_friend_message(event)\n',
             '        # Event-driven contact is managed by life_continuity.\n        return None\n'),
            ('        await self.event_handlers.on_group_message(event)\n',
             '        # Group messages do not start a silence timer.\n        return None\n'),
            ('        await self.event_handlers.on_after_message_sent(event)\n',
             '        # A reply does not create another outreach timer.\n        return None\n'),
        )
        patch(target, PROACTIVE_START,
            ('            await plugin.scheduler_service.init_jobs_from_data()\n',
             '            # Keep old data readable without restoring idle chat jobs.\n'),
            ('            await plugin.scheduler_service.setup_auto_triggers_for_enabled_sessions()\n',
             '            # Explicit reminders and follow-ups have their own scheduler.\n'),
        )
        patch(target, PROACTIVE_FLOW,
            ('        无论成败都会释放手动触发占用状态。\n        """\n        plugin = self.plugin\n'
             '        if getattr(plugin, "_terminating", False):\n',
             '        无论成败都会释放手动触发占用状态。\n        """\n'
             '        if trigger is not TriggerSource.MANUAL:\n            return\n'
             '        plugin = self.plugin\n        if getattr(plugin, "_terminating", False):\n'),
        )
    if reverse:
        return
    for name, relative in zip(PLUGIN_CONFIGS, CONFIG_PATHS, strict=True):
        path = target / relative
        if not path.is_file():
            continue
        values = json.loads(path.read_text(encoding="utf-8-sig"))
        before = json.dumps(values, ensure_ascii=False, indent=2)
        configure_experience_plugin(name, values)
        after = json.dumps(values, ensure_ascii=False, indent=2)
        if after != before:
            temporary = path.with_suffix(".json.tmp")
            temporary.write_text(after, encoding="utf-8")
            temporary.replace(path)

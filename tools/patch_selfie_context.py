"""Apply small checked substitutions to the pinned upstream drawing plugin."""

from pathlib import Path

IMPORT = (
    "from astrbot.core.utils.selfie_context import (\n"
    "    SELFIE_REPLY_INSTRUCTIONS,\n"
    "    remember_selfie,\n"
    ")\n"
)
BIND = """            conversation_manager = self.context.conversation_manager
            selfie_cid = await conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
            if not selfie_cid:
                selfie_cid = await conversation_manager.new_conversation(event.unified_msg_origin)
"""
COMMAND_SUCCESS = """            await event.send(event.chain_result(
                self._build_image_success_components(result, time.perf_counter() - started_at)
            ))
            self._complete_image_quota(event, reserved_count, 1)
            note = await remember_selfie(
                self.context, event, [self._get_image_result_url(result)], user_input, selfie_cid
            )
            try:
                current_cid = await conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
                if current_cid == selfie_cid:
                    conversation = await conversation_manager.get_conversation(event.unified_msg_origin, selfie_cid)
                    if conversation is not None:
                        event.set_extra("_fy_selfie_followup", True)
                        yield event.request_llm(
                            prompt=event.get_message_str(),
                            conversation=conversation,
                            system_prompt=SELFIE_REPLY_INSTRUCTIONS + "\\n" + note,
                        )
            except Exception as exc:
                logger.warning("Selfie follow-up unavailable (%s).", type(exc).__name__)
"""
TOOL_SUCCESS = """            note = await remember_selfie(
                self.context, event,
                [self._get_image_result_url(result) for result in valid_results[:sent]],
                action, selfie_cid,
            )
            return f"系统提示：已成功生成并下发了 {sent} 张图。\\n{note}\\n{SELFIE_REPLY_INSTRUCTIONS}"
"""


def apply_selfie_context(target: Path) -> None:
    """Patch command and tool results without redistributing upstream sources.

    Args:
        target: Installation root containing the pinned drawing plugin.

    Raises:
        RuntimeError: If the upstream function bodies no longer match.
    """
    path = target / "astrbot/data/plugins/astrbot_plugin_omnidraw/main.py"
    text = path.read_text(encoding="utf-8-sig")
    if IMPORT in text:
        if COMMAND_SUCCESS not in text or TOOL_SUCCESS not in text:
            raise RuntimeError("The selfie context patch is incomplete; review before retrying")
        return
    command_start = text.index('    @filter.command("自拍")\n')
    command_end = text.index('    @filter.command("视频")\n', command_start)
    command = text[command_start:command_end]
    old = "        try:\n            started_at = time.perf_counter()\n"
    if command.count(old) != 1:
        raise RuntimeError("The pinned selfie command no longer matches its binding patch")
    command = command.replace(
        old, "        try:\n" + BIND + "            started_at = time.perf_counter()\n", 1
    )
    old = """            self._complete_image_quota(event, reserved_count, 1)
            yield event.chain_result(self._build_image_success_components(result, time.perf_counter() - started_at))
"""
    if command.count(old) != 1:
        raise RuntimeError("The pinned selfie command no longer matches its delivery patch")
    command = command.replace(old, COMMAND_SUCCESS, 1)
    text = text[:command_start] + command + text[command_end:]
    tool_start = text.index('    @llm_tool(name="generate_selfie")\n')
    tool_end = text.index('    @llm_tool(name="generate_image")\n', tool_start)
    tool = text[tool_start:tool_end]
    old = "            generation = await self._run_selfie_generation(\n"
    if tool.count(old) != 1:
        raise RuntimeError("The pinned selfie tool no longer matches its binding patch")
    tool = tool.replace(old, BIND + old, 1)
    old = '            return f"系统提示：已成功生成并下发了 {sent} 张图。"\n'
    if tool.count(old) != 1:
        raise RuntimeError("The pinned selfie tool no longer matches its result patch")
    tool = tool.replace(old, TOOL_SUCCESS, 1)
    text = text[:tool_start] + tool + text[tool_end:]
    anchor = "from .constants import (\n"
    if text.count(anchor) != 1:
        raise RuntimeError("The pinned drawing plugin import anchor changed")
    text = text.replace(anchor, IMPORT + "\n" + anchor, 1)
    text = "# Dafeiyu Windows: retain delivered selfie context; modified 2026-10-27.\n" + text
    path.write_text(text, encoding="utf-8")

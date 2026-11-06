"""Apply small checked boundary hooks to the pinned OpenAI-compatible provider."""

from pathlib import Path
import json

IMPORT = (
    "from astrbot.core.utils.chat_efficiency import (\n"
    "    metered_chat_completion, prepare_chat_payload,\n"
    ")\n"
)
PREPARE = (
    "        payloads, tools = await prepare_chat_payload(\n"
    "            self, payloads, tools, conversation_id=conversation_id\n"
    "        )\n"
)
MEMORY_RECALL_PATH = (
    "astrbot/data/plugins/astrbot_plugin_livingmemory/core/event_handler_modules/memory_recall.py"
)
MEMORY_IMPORT = "from astrbot.core.utils.context_parts import skip_automatic_recall\n"
MEMORY_GUARD = (
    "                if skip_automatic_recall(actual_query):\n"
    "                    logger.info(\"本轮缺少独立检索主题，跳过自动长期记忆召回\")\n"
    "                    return\n\n"
)


def apply_chat_efficiency(target: Path) -> None:
    path = target / "astrbot/astrbot/core/provider/sources/openai_source.py"
    text = path.read_text(encoding="utf-8-sig")
    if IMPORT in text:
        if text.count(PREPARE) != 2 or text.count("lambda: metered_chat_completion(") != 2:
            raise RuntimeError("The chat efficiency patch is incomplete")
        configure_policy(target)
        return
    anchor = "from .request_retry import retry_provider_request\n"
    if text.count(anchor) != 1:
        raise RuntimeError("The pinned provider imports no longer match")
    text = text.replace(anchor, anchor + IMPORT, 1)
    # Both non-streaming and streaming paths accept the same initial arguments.
    for name in ("_query", "_query_stream"):
        start = text.index(f"    async def {name}(\n")
        body = text.index("        if tools:\n", start)
        text = text[:body] + PREPARE + text[body:]
    old = "            lambda: self.client.chat.completions.create(\n"
    if text.count(old) != 2:
        raise RuntimeError("The pinned provider request boundaries no longer match")
    text = text.replace(old, "            lambda: metered_chat_completion(\n"
                        "                self, self.client.chat.completions.create,\n")
    path.write_text(text, encoding="utf-8")
    configure_policy(target)


def apply_memory_recall_focus(target: Path) -> None:
    """Keep LivingMemory storage and explicit tools, and skip ungrounded automatic recall."""
    path = target / MEMORY_RECALL_PATH
    if not path.is_file():
        return
    text = path.read_text(encoding="utf-8-sig")
    if MEMORY_IMPORT in text or MEMORY_GUARD in text:
        if text.count(MEMORY_IMPORT) != 1 or text.count(MEMORY_GUARD) != 1:
            raise RuntimeError("The LivingMemory focus patch is incomplete")
        return
    import_anchor = "from astrbot.core.agent.message import TextPart\n"
    guard_anchor = "                # 获取过滤配置\n"
    if text.count(import_anchor) != 1 or text.count(guard_anchor) != 1:
        raise RuntimeError("The pinned LivingMemory recall boundaries no longer match")
    text = text.replace(import_anchor, import_anchor + MEMORY_IMPORT, 1)
    text = text.replace(guard_anchor, MEMORY_GUARD + guard_anchor, 1)
    path.write_text(text, encoding="utf-8", newline="\n")


def configure_policy(target: Path) -> None:
    """Deploy just the new provider policy, preserving all existing user configuration."""
    path = target / "astrbot/data/cmd_config.json"
    if not path.exists():
        return
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    changed = False
    for provider in value.get("provider", []):
        if provider.get("id") == "deepseek_chat" and "companion_context" not in provider:
            provider["companion_context"] = {
                "enabled": True, "input_budget": 16000, "recent_rounds": 12,
                "summary_max_tokens": 900, "summary_input_chars": 24000,
                "route_simple_chat_tools": True,
            }
            changed = True
        if provider.get("id") == "deepseek_chat" and isinstance(provider.get("companion_context"), dict):
            if "summary_background" not in provider["companion_context"]:
                provider["companion_context"]["summary_background"] = True
                changed = True
    if changed:
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

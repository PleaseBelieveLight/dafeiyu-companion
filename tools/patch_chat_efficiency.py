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
    if changed:
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

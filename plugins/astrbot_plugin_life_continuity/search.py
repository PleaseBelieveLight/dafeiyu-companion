"""Bounded read-only retrieval through AstrBot's configured search provider."""

import asyncio
import ipaddress
import json
from urllib.parse import urlsplit, urlunsplit

from .selection_state import fingerprint
from .state import StateError


class SearchError(StateError):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


# Instantiate only a known read-only builtin, never the global agent's tool set.
PROVIDERS = {
    "anysearch": ("AnySearchWebSearchTool", "max_results"),
    "tavily": ("TavilyWebSearchTool", "max_results"),
    "bocha": ("BochaWebSearchTool", "count"),
    "brave": ("BraveWebSearchTool", "count"),
    "firecrawl": ("FirecrawlWebSearchTool", "limit"),
    "baidu_ai_search": ("BaiduWebSearchTool", "top_k"),
    "exa": ("ExaWebSearchTool", "num_results"),
}


def public_url(value):
    if not isinstance(value, str) or len(value) > 2000:
        return None
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if (parsed.scheme not in {"http", "https"} or not host or parsed.username
                or parsed.password or host.lower() == "localhost" or "." not in host):
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            if host.lower().endswith((".local", ".localhost", ".internal")):
                return None
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
    except ValueError:
        return None


def normalize_results(payload):
    """Keep actual snippets and provenance; a URL alone is not a read source."""
    if not isinstance(payload, str) or len(payload) > 200000:
        raise SearchError("search_invalid_response")
    try:
        rows = json.loads(payload).get("results")
    except (ValueError, AttributeError):
        raise SearchError("search_invalid_response") from None
    if not isinstance(rows, list):
        raise SearchError("search_invalid_response")
    result, seen = [], set()
    for row in rows[:20]:
        if not isinstance(row, dict):
            continue
        url, text = public_url(row.get("url")), row.get("snippet")
        if not url or not isinstance(text, str) or not text.strip():
            continue
        text = text.strip()[:2000]
        key = fingerprint(text)
        if key in seen:
            continue
        seen.add(key)
        title = row.get("title")
        result.append({"url": url, "text": text,
                       "title": title[:200] if isinstance(title, str) else ""})
        if len(result) == 3:
            break
    return result


async def search_web(context, event, query, timeout):
    from astrbot.core.astr_agent_context import AgentContextWrapper, AstrAgentContext
    from astrbot.core.tools import web_search_tools

    settings = context.get_config(umo=event.unified_msg_origin).get("provider_settings", {})
    provider = PROVIDERS.get(settings.get("websearch_provider"))
    if settings.get("web_search") is not True or provider is None:
        raise SearchError("search_unavailable")
    name, count_key = provider
    tool = getattr(web_search_tools, name, None)
    if tool is None:
        raise SearchError("search_unavailable")
    wrapper = AgentContextWrapper(context=AstrAgentContext(context=context, event=event))
    try:
        async with asyncio.timeout(timeout):
            payload = await tool().call(wrapper, query=query, **{count_key: 3})
        return normalize_results(payload)
    except SearchError:
        raise
    except TimeoutError:
        raise SearchError("search_timeout") from None
    except Exception:
        # Provider errors can contain credentials or request bodies.
        raise SearchError("search_failed") from None

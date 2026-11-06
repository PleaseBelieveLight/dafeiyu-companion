"""Bounded read-only retrieval through AstrBot's configured search provider."""

import asyncio
import hashlib
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
        if isinstance(payload, str) and payload.startswith("Error:") and payload.endswith("does not return any results."):
            return []
        return normalize_results(payload)
    except SearchError:
        raise
    except TimeoutError:
        raise SearchError("search_timeout") from None
    except Exception:
        # Provider errors can contain credentials or request bodies.
        raise SearchError("search_failed") from None


async def foreground_search(store, event, text, query, lookup):
    """Reserve a real chat event before querying; reuse its result after retries."""
    identity = foreground_identity(event)
    if identity is None:
        return {"status": "missing_event"}
    signature = hashlib.sha256(text.encode()).hexdigest()
    with store.transaction():
        # The optional journal is additive and leaves existing research tables intact.
        store.db.execute("""CREATE TABLE IF NOT EXISTS foreground_searches (
            scope TEXT NOT NULL, event_id TEXT NOT NULL, signature TEXT NOT NULL,
            receipt TEXT, PRIMARY KEY(scope,event_id))""")
        store.db.execute("""CREATE TABLE IF NOT EXISTS foreground_replies (
            scope TEXT NOT NULL, event_id TEXT NOT NULL, status TEXT NOT NULL,
            message_id TEXT, PRIMARY KEY(scope,event_id))""")
        old = store.db.execute("""SELECT signature,receipt FROM foreground_searches
            WHERE scope=? AND event_id=?""", identity).fetchone()
        if old:
            if old["signature"] != signature:
                return {"status": "conflicting_event"}
            return json.loads(old["receipt"]) if old["receipt"] else {"status": "unconfirmed"}
        store.db.execute("INSERT INTO foreground_searches VALUES (?,?,?,NULL)", (*identity, signature))
        store.db.execute("INSERT INTO foreground_replies VALUES (?,?,'ready',NULL)", identity)
    try:
        if query:
            results = await lookup(query)
            receipt = {"status": "returned" if results else "empty", "query": query, "results": results}
        else:
            receipt = {"status": "needs_topic"}
    except SearchError as exc:
        receipt = {"status": exc.reason, "query": query}
    except Exception:
        receipt = {"status": "unconfirmed", "query": query}
    with store.transaction():
        store.db.execute("UPDATE foreground_searches SET receipt=? WHERE scope=? AND event_id=?",
                         (json.dumps(receipt, ensure_ascii=False), *identity))
    return receipt


def foreground_identity(event):
    event_id = getattr(event.message_obj, "message_id", None)
    if type(event_id) not in (str, int) or not str(event_id).strip():
        return None
    return event.unified_msg_origin, str(event_id)


def search_reply(receipt):
    status = receipt["status"]
    if status == "returned":
        snippets = [f"{row['title'] or '网页摘要'}\n「{' '.join(row['text'].split())[:200]}」\n{row['url']}"
                    for row in receipt["results"][:3]]
        return "我查到这些公开摘要。能确认的范围就到这里。\n\n" + "\n\n".join(snippets)
    return {
        "needs_topic": "你想让我现在查哪件事？",
        "empty": "这次没查到能用的公开资料，先别下结论。",
        "search_timeout": "这次查超时了，没拿到结果。",
        "search_unavailable": "这会儿用不了搜索，没法替你核实。",
        "search_invalid_response": "搜索回来了，但没有可核对的结果。",
        "previous_work_unconfirmed": "前一项查询还没收尾，这次没再发起搜索。",
    }.get(status, "这次查询没拿到可核对的结果。")


async def deliver_search_reply(store, event, text, receipt, dispatch):
    identity = foreground_identity(event)
    if identity is None or receipt["status"] == "conflicting_event":
        return None
    signature = hashlib.sha256(text.encode()).hexdigest()
    with store.transaction():
        ready = store.db.execute("""UPDATE foreground_replies SET status='sending'
            WHERE scope=? AND event_id=? AND status='ready' AND EXISTS (
                SELECT 1 FROM foreground_searches AS search
                WHERE search.scope=foreground_replies.scope AND search.event_id=foreground_replies.event_id
                    AND search.signature=? AND search.receipt IS NOT NULL)""", (*identity, signature)).rowcount
        if not ready:
            return None
        saved = store.db.execute("SELECT receipt FROM foreground_searches WHERE scope=? AND event_id=?", identity)
        receipt = json.loads(saved.fetchone()[0])
    message_id = None
    try:
        reply = search_reply(receipt)
        message_id = await dispatch(reply)
    finally:
        with store.transaction():
            store.db.execute("UPDATE foreground_replies SET status=?,message_id=? WHERE scope=? AND event_id=?",
                             ("sent" if message_id else "uncertain", message_id, *identity))
    return reply if message_id else None

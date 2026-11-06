"""Trust the platform's session and sender, including legacy QQ group jobs."""

import asyncio
import copy
import re
import time
from contextlib import closing

from astrbot.core.platform.message_type import MessageType

GROUP_TOOLS = {"future_task", "astr_kb_search", "recall_long_term_memory", "read_memory_evidence"}


def qq_group_scope(event):
    if event.get_platform_name() != "aiocqhttp" or event.get_message_type() != MessageType.GROUP_MESSAGE:
        return None
    group = str(event.get_group_id())
    return f"{event.get_platform_id()}:GroupMessage:{group}" if group.isdecimal() else None


def restrict_group_tools(event, req):
    scheduled_group = (event.get_message_type() == MessageType.GROUP_MESSAGE
                       and event.get_extra("cron_job"))
    if not req.func_tool or not (scheduled_group or qq_group_scope(event)):
        return
    tools = copy.copy(req.func_tool)
    if scheduled_group:
        tools.tools = [tool for tool in req.func_tool.tools if tool.name == "send_message_to_user"]
    else:
        tools.tools = [tool for tool in req.func_tool.tools
                       if tool.name in GROUP_TOOLS or tool.name.startswith("web_search_")]
    req.func_tool = tools


def owns_chat_job(job_scope, job_sender, current_scope, sender):
    if not sender or sender != job_sender or not job_scope:
        return False
    if job_scope == current_scope:
        return True
    # Before sender isolation, QQ jobs used the bare group ID in the session.
    isolated = re.fullmatch(r"([^:]+):GroupMessage:([0-9]+)_([0-9]+)", current_scope)
    return bool(isolated and isolated[2] == sender
                and job_scope == f"{isolated[1]}:GroupMessage:{isolated[3]}")


async def qq_quote_matches(bot, incoming, quoted, routing):
    """OneBot get_msg is keyed by ID, so verify the peer before using its content."""
    kind = incoming.get("message_type")
    account = str(incoming.get("self_id") or "")
    if not account or str(quoted.get("self_id")) != account or quoted.get("message_type") != kind:
        return False
    if kind == "group":
        return bool(incoming.get("group_id") and str(quoted.get("group_id")) == str(incoming["group_id"]))
    if kind != "private":
        return False
    peer = str(incoming.get("user_id") or "")
    sender = str((quoted.get("sender") or {}).get("user_id") or "")
    if not peer or not sender or sender not in {peer, account}:
        return False
    if sender == peer:
        return True
    target = quoted.get("target_id")
    if target:
        return str(target) == peer
    # NapCat does not include a recipient in every outgoing private get_msg.
    # Check for the exact ID in the current peer's history before trusting it.
    identity = str(quoted.get("message_id") or "")
    if not identity:
        return False
    async with asyncio.timeout(3):
        history = await bot.call_action("get_friend_msg_history", user_id=peer, message_seq=identity,
                                        count=3, disable_get_url=True, parse_mult_msg=False, **routing)
    return any(str(row.get("message_id")) == identity for row in history.get("messages", [])
               if isinstance(row, dict))


def claim_qq_event(event):
    """Accept the same OneBot event only once, including a reconnect or restart."""
    if event.get_platform_name() != "aiocqhttp":
        return True
    message_id = getattr(event.message_obj, "message_id", None)
    if type(message_id) not in (str, int) or not str(message_id).strip() or str(message_id) == "0":
        return True
    from .chat_efficiency import connect

    now = time.time()
    with closing(connect()) as db, db:
        db.execute("""CREATE TABLE IF NOT EXISTS received_qq_events (
            scope TEXT NOT NULL, sender TEXT NOT NULL, event_id TEXT NOT NULL, received REAL NOT NULL,
            PRIMARY KEY(scope,sender,event_id))""")
        db.execute("CREATE INDEX IF NOT EXISTS qq_event_expiry ON received_qq_events(received)")
        db.execute("DELETE FROM received_qq_events WHERE received<?", (now - 86400,))
        return bool(db.execute("INSERT OR IGNORE INTO received_qq_events VALUES (?,?,?,?)",
                               (event.unified_msg_origin, str(event.get_sender_id()), str(message_id), now)).rowcount)

"""Durable scheduler execution and OneBot delivery receipts."""

import hashlib
import json
import re
import uuid
from contextlib import closing
from datetime import timedelta

from astrbot.core.message.components import File, Node, Nodes
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

from .chat_efficiency import connect
from .chat_scope import owns_chat_job


def journal():
    db = connect()
    db.execute("""CREATE TABLE IF NOT EXISTS reminder_runs (
        id TEXT PRIMARY KEY, job_id TEXT NOT NULL, name TEXT NOT NULL, scope TEXT NOT NULL, actor TEXT NOT NULL,
        started TEXT NOT NULL, execution TEXT NOT NULL, delivery TEXT NOT NULL DEFAULT 'not_attempted',
        texts TEXT, message_id TEXT)""")
    return db


def reserve_reminder(job, start, trigger, *, manual):
    if manual:
        occurrence = uuid.uuid4().hex
    elif job.run_once:
        occurrence = "once"
    else:
        earliest = start - timedelta(seconds=300)
        occurrence = trigger.get_next_fire_time(None, earliest)
        if occurrence is None or occurrence > start:
            raise RuntimeError("Scheduled occurrence could not be identified")
        while next_fire := trigger.get_next_fire_time(occurrence, occurrence):
            if next_fire > start:
                break
            occurrence = next_fire
        occurrence = str(int(occurrence.timestamp()))
    key = f"{job.job_id}:{occurrence}"
    payload = job.payload or {}
    with closing(journal()) as db, db:
        inserted = db.execute("""INSERT OR IGNORE INTO reminder_runs
            (id, job_id, name, scope, actor, started, execution)
            SELECT ?, ?, ?, ?, ?, ?, 'running' WHERE NOT EXISTS (
                SELECT 1 FROM reminder_runs WHERE job_id=? AND execution='running')""",
            (key, job.job_id, job.name, str(payload.get("session") or ""),
             str(payload.get("sender_id") or ""), start.isoformat(), job.job_id)).rowcount
    return key if inserted else None


def settle_reminder(key, execution):
    with closing(journal()) as db, db:
        db.execute("""UPDATE reminder_runs SET execution=?,
            delivery=CASE WHEN delivery='sending' THEN 'uncertain' ELSE delivery END WHERE id=?""",
            (execution, key))


def recover_reminders():
    with closing(journal()) as db, db:
        db.execute("""UPDATE reminder_runs SET execution='interrupted',
            delivery=CASE WHEN delivery='sending' THEN 'uncertain' ELSE delivery END
            WHERE execution='running'""")


def confirmed_reminder_texts(key):
    with closing(journal()) as db:
        row = db.execute("SELECT texts FROM reminder_runs WHERE id=? AND delivery='sent'", (key,)).fetchone()
    return json.loads(row[0]) if row else []


def recent_reminders(scope, actor):
    legacy_scope = scope.split(":", 2)
    if len(legacy_scope) == 3 and legacy_scope[1] == "GroupMessage":
        legacy_scope[2] = legacy_scope[2].rsplit("_", 1)[-1]
    with closing(journal()) as db:
        rows = db.execute("""SELECT scope, name, started, execution, delivery FROM reminder_runs
            WHERE actor=? AND scope IN (?,?) ORDER BY started DESC LIMIT 5""",
            (actor, scope, ":".join(legacy_scope))).fetchall()
    return [row[1:] for row in rows if owns_chat_job(row[0], actor, scope, actor)]


async def create_task_once(event, kwargs, create):
    event_id = getattr(event.message_obj, "message_id", None)
    if type(event_id) not in (int, str) or not str(event_id).strip():
        return "error: cannot create a task without a trusted message ID."
    args = json.dumps(kwargs, ensure_ascii=False, sort_keys=True).encode()
    key = (event.unified_msg_origin, str(event.get_sender_id()), str(event_id), hashlib.sha256(args).hexdigest())
    with closing(connect()) as db, db:
        db.execute("""CREATE TABLE IF NOT EXISTS future_task_creates (
            scope TEXT NOT NULL, actor TEXT NOT NULL, event_id TEXT NOT NULL, args TEXT NOT NULL,
            receipt TEXT, PRIMARY KEY(scope,actor,event_id,args))""")
        reserved = db.execute("INSERT OR IGNORE INTO future_task_creates VALUES (?,?,?,?,NULL)", key).rowcount
        if not reserved:
            row = db.execute("""SELECT receipt FROM future_task_creates
                WHERE scope=? AND actor=? AND event_id=? AND args=?""", key).fetchone()
            return row[0] or "This task creation is still unconfirmed. Check the schedule before trying again."
    result = await create()
    with closing(connect()) as db, db:
        db.execute("""UPDATE future_task_creates SET receipt=?
            WHERE scope=? AND actor=? AND event_id=? AND args=?""", (result, *key))
    return result


def record_task_outcome(event, text):
    outcomes = event.get_extra("dafeiyu_task_outcomes") or []
    if text not in outcomes:
        event.set_extra("dafeiyu_task_outcomes", [*outcomes, text])


async def send_qq_with_receipt(context, scope, chain, *, self_id=None):
    """Send one OneBot message and require the transport's returned message ID."""
    session = MessageSession.from_str(scope)
    if session.message_type not in (MessageType.GROUP_MESSAGE, MessageType.FRIEND_MESSAGE):
        raise ValueError("Invalid QQ delivery session")
    group = session.message_type == MessageType.GROUP_MESSAGE
    target = session.session_id.rsplit("_", 1)[-1] if group else session.session_id
    if not re.fullmatch(r"[0-9]+", target):
        raise ValueError("Invalid QQ delivery target")
    allowlist = context.get_config(umo=scope)["platform_settings"]["id_whitelist"]
    canonical = f"{session.platform_id}:{session.message_type.value}:{target}"
    if canonical not in allowlist and (not group or target not in allowlist):
        raise PermissionError("Delivery target is outside the configured QQ allowlist")
    if not chain.chain or any(isinstance(part, (File, Node, Nodes)) for part in chain.chain):
        raise ValueError("A tracked delivery requires one OneBot message chain")
    adapter = next((platform for platform in context.platform_manager.platform_insts
                    if platform.meta().id == session.platform_id and platform.meta().name == "aiocqhttp"), None)
    if adapter is None:
        raise RuntimeError("QQ adapter unavailable")
    message = await AiocqhttpMessageEvent._parse_onebot_json(chain)
    if not message:
        raise ValueError("QQ message has no sendable content")
    route = {"self_id": int(self_id)} if self_id and str(self_id).isdigit() else {}
    if group:
        result = await adapter.bot.send_group_msg(group_id=int(target), message=message, **route)
    else:
        result = await adapter.bot.send_private_msg(user_id=int(target), message=message, **route)
    message_id = result.get("message_id") if isinstance(result, dict) else None
    if type(message_id) not in (int, str) or not re.fullmatch(r"-?[1-9][0-9]*", str(message_id)):
        raise RuntimeError("OneBot did not return a delivery receipt")
    return str(message_id)


async def send_reminder_once(context, event, chain):
    key = event.get_extra("dafeiyu_reminder_run")
    with closing(journal()) as db, db:
        row = db.execute("SELECT scope FROM reminder_runs WHERE id=?", (key,)).fetchone()
        if not row or not row[0] or not db.execute("""UPDATE reminder_runs SET delivery='sending'
                WHERE id=? AND execution='running' AND delivery='not_attempted'""", (key,)).rowcount:
            return False
        scope = row[0]
    try:
        message_id = await send_qq_with_receipt(context, scope, chain)
    except BaseException:
        with closing(journal()) as db, db:
            db.execute("UPDATE reminder_runs SET delivery='uncertain' WHERE id=?", (key,))
        raise
    text = chain.get_plain_text().strip()
    with closing(journal()) as db, db:
        db.execute("UPDATE reminder_runs SET delivery='sent', texts=?, message_id=? WHERE id=?",
                   (json.dumps([text] if text else [], ensure_ascii=False), message_id, key))
    return True

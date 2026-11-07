import json

from astrbot import logger
from astrbot.core.conversation_mgr import ConversationManager
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.utils.session_lock import session_lock_manager


async def persist_agent_history(
    conversation_manager: ConversationManager,
    *,
    event: AstrMessageEvent,
    req: ProviderRequest,
    summary_note: str,
) -> None:
    """Persist agent interaction into conversation history."""
    if not req or not req.conversation:
        return

    scope, cid = event.unified_msg_origin, req.conversation.cid
    if event.get_extra("cron_job"):
        from astrbot.core.utils.task_receipts import confirmed_reminder_texts

        run = event.get_extra("dafeiyu_reminder_run")
        sent = confirmed_reminder_texts(run) if run else []
    else:
        sent = event.get_extra("_send_message_to_user_current_session_plain_texts", [])
    if not isinstance(sent, list):
        return
    delivered = [{"role": "assistant", "content": text} for text in sent
                 if isinstance(text, str) and text.strip()]
    if not delivered:
        return
    # Tasks run outside the foreground lock. Read the latest history under
    # that lock so a message received during execution is never overwritten.
    async with session_lock_manager.acquire_lock(scope):
        conversation = await conversation_manager.get_conversation(scope, cid)
        if conversation is None:
            return
        try:
            history = json.loads(conversation.history or "[]")
        except (TypeError, ValueError):
            logger.warning("Unable to append delivered task output to conversation history")
            return
        if isinstance(history, list):
            await conversation_manager.update_conversation(scope, cid, history=history + delivered)

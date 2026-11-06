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
        sent = event.get_extra("_send_message_to_user_current_session_plain_texts", [])
        if not isinstance(sent, list):
            return
        delivered = [{"role": "assistant", "content": text} for text in sent
                     if isinstance(text, str) and text.strip()]
        if not delivered:
            return
        # The job runs outside the foreground chat lock. Re-read under the same
        # lock before appending, so a chat during the job is never overwritten.
        async with session_lock_manager.acquire_lock(scope):
            conversation = await conversation_manager.get_conversation(scope, cid)
            if conversation is None:
                return
            try:
                history = json.loads(conversation.history or "[]")
            except (TypeError, ValueError):
                logger.warning("Unable to append delivered reminder to conversation history")
                return
            if isinstance(history, list):
                await conversation_manager.update_conversation(scope, cid, history=history + delivered)
        return

    history = []
    try:
        history = json.loads(req.conversation.history or "[]")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to parse conversation history: %s", exc)
    history.append({"role": "user", "content": "Output your last task result below."})
    history.append({"role": "assistant", "content": summary_note})
    await conversation_manager.update_conversation(scope, cid, history=history)

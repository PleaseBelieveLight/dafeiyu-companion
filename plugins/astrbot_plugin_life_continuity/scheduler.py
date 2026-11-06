"""One current-day round after the configured Shanghai hour; never catch up."""

import asyncio
from datetime import datetime

from .activity_state import SHANGHAI, day_at, limits
from .state import StateError


class DailyResearch:
    def __init__(self, plugin):
        self.plugin, self.task, self.last_issue = plugin, None, ""

    def start(self):
        if any(self.plugin.config.get(key) is True for key in ("auto_enabled", "followups_enabled")) and self.task is None:
            self.task = asyncio.create_task(self._loop())

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    async def _loop(self):
        while True:
            try:
                self.tick()
                await self.plugin.followup_due()
            except StateError:
                pass
            except Exception as exc:
                self.last_issue = type(exc).__name__
            await asyncio.sleep(60)

    def tick(self):
        plugin, cfg = self.plugin, self.plugin.config
        if (cfg.get("enabled") is not True or cfg.get("activity_enabled") is not True
                or cfg.get("auto_enabled") is not True or plugin.engine is None):
            return None
        scope = cfg.get("owner_scope", "")
        engine, now = plugin.engine, plugin.engine.ledger.clock()
        hour = cfg.get("auto_hour", 10)
        if type(hour) is not int or not 0 <= hour <= 23:
            raise ValueError("invalid automatic research hour")
        if not scope or datetime.fromtimestamp(now, SHANGHAI).hour < hour:
            return None
        if engine.task is not None and not engine.task.done():
            return None
        status = engine.ledger.snapshot(scope, cfg)
        control = status["control"]
        if (status["started_rounds"] or not status["remaining_model_calls"]
                or not status["remaining_seconds"] or control["paused"]
                or now - control["last_chat"] < limits(cfg)["chat_cooldown_seconds"]):
            return None
        candidates = engine.ledger.goals.candidates(
            scope, now, allow_empty=cfg.get("web_search_enabled") is True)
        if not candidates:
            return None
        try:
            plugin.activity_policy()  # Quiet hours and transport compatibility.
            metadata = plugin.context.get_registered_star("astrbot_plugin_proactive_chat")
            event = metadata.star_cls.sender_service.build_proactive_event(scope)
            if event is None or event.unified_msg_origin != scope:
                raise StateError("owner event unavailable")
            result = engine.start(event, candidates[0]["id"], "daily-research:" + day_at(now)[0])
            self.last_issue = ""
            return result
        except StateError as exc:
            # Quiet hours/cooldown may end later today; do not consume a round.
            self.last_issue = str(exc)[:250]
            return None

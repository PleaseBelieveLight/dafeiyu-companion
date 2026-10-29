"""One shared, durable gate for autonomous notes and proactive chat delivery."""

import asyncio

from .activity_state import limits
from .activity import ProcessLease


class DeliveryGate:
    def __init__(self, ledger, config, policy):
        self.ledger, self.config, self.policy = ledger, config, policy
        self.pending = set()
        self.lease = ProcessLease(ledger.store.folder, "delivery.lock")

    async def send(self, scope, source, key, text, dispatch):
        self.policy()
        control = self.ledger.control(scope)
        if (control["paused"] or self.ledger.clock() - control["last_chat"]
                < limits(self.config)["chat_cooldown_seconds"]):
            return False
        self.lease.acquire()
        task = None
        try:
            ticket = self.ledger.reserve_delivery(scope, source, key, text, self.config)
            if ticket["duplicate"]:
                self.lease.release()
                return False
            task = asyncio.create_task(dispatch())
            self.pending.add(task)
        except BaseException:
            self.lease.release()
            raise
        receipt, confirmed = {}, False
        try:
            done, _ = await asyncio.wait(
                {task}, timeout=limits(self.config)["stop_timeout_seconds"]
            )
            if not done:
                task.cancel()
                return False
            result = task.result()
            confirmed = result is True or (isinstance(result, dict)
                                          and result.get("transport_ack") is True)
            receipt = result if isinstance(result, dict) else {"transport_ack": confirmed}
            receipt["recipient_confirmed"] = False
            return confirmed
        finally:
            try:
                self.ledger.settle_delivery(ticket["id"], confirmed=confirmed, receipt=receipt)
            finally:
                if task.done():
                    self._release(task)
                else:
                    task.cancel()
                    task.add_done_callback(self._release)

    def _release(self, task):
        try:
            task.result()
        except BaseException:
            pass
        self.pending.discard(task)
        self.lease.release()

    async def close(self):
        for task in self.pending:
            task.cancel()
        if self.pending:
            _, pending = await asyncio.wait(
                self.pending, timeout=limits(self.config)["stop_timeout_seconds"]
            )
            return not pending
        return True


def quiet_now(specification, now):
    """Match the existing proactive hour syntax, rejecting invalid configurations."""
    try:
        start, end = (int(value) for value in specification.split("-"))
    except (AttributeError, TypeError, ValueError):
        raise ValueError("现有免打扰配置无法核验。") from None
    if not 0 <= start <= 23 or not 0 <= end <= 24:
        raise ValueError("现有免打扰时段超出范围。")
    hour = now.hour
    return start <= hour < end if start <= end else hour >= start or hour < end

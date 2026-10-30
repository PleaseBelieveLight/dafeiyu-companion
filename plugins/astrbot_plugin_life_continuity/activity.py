"""Single-flight research with a process lease and bounded cancellation waits."""

import asyncio
import json
import os
import time
from pathlib import Path

from .activity_state import ActivityLedger, limits
from .native_agent import choose_native, run_native
from .state import ConflictError, StateError


class ProcessLease:
    def __init__(self, folder, name="activity.lock"):
        self.path, self.handle = Path(folder) / name, None

    def acquire(self):
        if self.handle is not None:
            raise StateError("已有活动持有执行租约。")
        handle = self.path.open("a+b")
        try:
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            handle.close()
            raise StateError("另一个执行者尚未释放活动租约。") from None
        self.handle = handle

    def release(self):
        if self.handle is not None:
            self.handle.close()  # OS releases the lock, including on process death.
            self.handle = None


class ActivityEngine:
    def __init__(self, store, context, config, policy, *, agent=run_native, share=None,
                 selector=choose_native, clock=time.time, monotonic=time.monotonic):
        self.store, self.context, self.config, self.policy = store, context, config, policy
        self.agent, self.share, self.monotonic = agent, share, monotonic
        self.selector = selector
        self.ledger, self.lease = ActivityLedger(store, clock), ProcessLease(store.folder)
        self.task, self.work, self.runner, self.job = None, None, None, None
        self.stop_signal, self.reason, self.manual = asyncio.Event(), "", False
        self.closing = False

    def permit(self):
        if self.stop_signal.is_set() or self.config.get("activity_enabled") is not True:
            raise asyncio.CancelledError()
        self.policy()

    def recover(self, scope):
        self.lease.acquire()
        try:
            return self._recover_locked(scope)
        finally:
            self.lease.release()

    def _recover_locked(self, scope):
        sending = ProcessLease(self.store.folder, "delivery.lock")
        sending.acquire()
        try:
            return self.ledger.recover(scope)
        finally:
            sending.release()

    def start(self, event, goal_id, trigger):
        if self.config.get("activity_enabled") is not True or self.closing:
            raise StateError("单次活动开关尚未开启。")
        if self.task is not None and not self.task.done():
            raise StateError("已有活动或在途调用，不能启动第二个执行者。")
        if self.work is not None and not self.work.done():
            raise StateError("上次调用尚未确认结束。")
        self.policy()
        self.lease.acquire()
        try:
            # Owning the OS lock proves there is no old local worker to race.
            self._recover_locked(event.unified_msg_origin)
            scope = event.unified_msg_origin
            previous = self.ledger.goals.choice(scope, trigger)
            if previous:
                mode = "selection" if goal_id is None else "manual"
                if (previous["request_mode"] != mode
                        or (goal_id is not None and previous["goal_id"] != goal_id)):
                    raise ConflictError("trigger already belongs to another choice")
                self.lease.release()
                return {"duplicate": True, "run_id": previous["run_id"],
                        "status": "recorded", "reason": previous["reason"]}
            old_run = self.store.db.execute("SELECT * FROM runs WHERE scope=? AND source_id=?",
                                            (scope, trigger)).fetchone()
            if old_run:
                if goal_id is not None and old_run["goal_id"] != goal_id:
                    raise ConflictError("trigger already belongs to another goal")
                self.lease.release()
                return {"duplicate": True, "run_id": old_run["id"], "status": old_run["status"]}
            candidates = None
            if goal_id is None:
                candidates = self.ledger.goals.candidates(scope, self.ledger.clock())
                if not candidates:
                    with self.store.transaction():
                        self.ledger.goals.record_choice(scope, trigger, None, None,
                            "没有符合暂停、来源及搁置条件的候选，等待新资料或明确指引", [],
                            self.ledger.clock(), mode="selection")
                    self.lease.release()
                    return {"status": "waiting", "duplicate": False}
                if (limits(self.config)["max_model_calls_per_run"] < 2
                        or self.ledger.snapshot(scope, self.config)["remaining_model_calls"] < 2):
                    raise StateError("选择并研究至少需预留两次模型额度；可指定目标直接 run。")
                goal_id = candidates[0]["id"]
            job = self.ledger.begin(scope, goal_id, trigger, self.config,
                                    selection=candidates is not None)
            if job["duplicate"]:
                self.lease.release()
                return job
            self.job = job
            job["candidates"] = candidates
            self.stop_signal, self.reason, self.manual = asyncio.Event(), "", False
            self.task = asyncio.create_task(self._supervise(event, job))
            self.task.add_done_callback(self._observe)
            return {"run_id": job["run_id"], "status": "started", "duplicate": False}
        except BaseException:
            self.lease.release()
            raise

    async def _perform(self, event, job):
        self.permit()
        researching = False

        def charge():
            self.permit()
            identity = self.ledger.charge_model(job["scope"], job["run_id"], self.config)
            if researching:
                job["research_called"] = True
            return identity

        if job.get("candidates") is not None:
            decision = await self.selector(
                self.context, event, json.dumps(job["candidates"], ensure_ascii=False),
                charge, self.ledger.record_usage, job["limits"], self.stop_signal,
                lambda runner: setattr(self, "runner", runner),
            )
            self.permit()
            if not self.ledger.select(job, job["candidates"], decision):
                self.ledger.finish(job, "choice_wait", 0)
                job["settled"] = True
                return {"waited": True}
        researching = True
        sources = [{"id": row["id"], "kind": row["kind"],
                    "text": row["evidence"]["text"][:2000],
                    "range_chars": [0, min(len(row["evidence"]["text"]), 2000)],
                    "total_chars": len(row["evidence"]["text"])} for row in job["sources"]]
        job["read_ranges"] = {row["id"]: row["range_chars"] for row in sources}
        previous = self.store.db.execute("""SELECT body,evidence_ids_json FROM notes
            WHERE scope=? AND goal_id=? ORDER BY version DESC LIMIT 1""",
            (job["scope"], job["goal_id"])).fetchone()
        note = ""
        if previous and all(self.store.get_episode(job["scope"], identity)
                            for identity in json.loads(previous["evidence_ids_json"])):
            note = previous["body"][:2500]

        goal = {key: job["goal"][key] for key in
                ("id", "title", "why", "completion_criteria", "next_step")}
        result = await self.agent(
            self.context, event,
            json.dumps({"goal": goal, "sources": sources, "previous_note": note},
                       ensure_ascii=False),
            charge, self.ledger.record_usage, job["limits"], self.stop_signal,
            lambda runner: setattr(self, "runner", runner),
        )
        self.permit()
        body = ("## 资料来源\n" + "\n".join(f"[E:{identity}]" for identity in result["source_ids"])
                + "\n\n## 我的想法\n" + result["note"]
                + "\n\n## 待核实\n" + result["questions"])
        updated = self.ledger.finish(
            job, "step_completed", 0, note=body,
            source_ids=result["source_ids"], next_step=result["next_step"],
            interest=result.get("interest", 0),
        )
        job["settled"] = True
        if (self.share is not None and self.config.get("share_enabled") is True
                and updated["checkpoint"].get("progress") is not False):
            self.permit()
            try:
                await self.share(event, job, updated, body)
            except Exception:
                pass  # Delivery has its own durable unknown-result ledger.
        return updated

    def _request_stop(self, reason, manual):
        self.reason, self.manual = reason, self.manual or manual
        self.stop_signal.set()
        if self.runner is not None:
            self.runner.request_stop()
        if self.work is not None and not self.work.done():
            self.work.cancel()

    async def stop(self, reason="foreground", *, manual=False):
        if self.task is None or self.task.done():
            return self.work is None or self.work.done()
        self._request_stop(reason, manual)
        done, _ = await asyncio.wait(
            {self.task}, timeout=limits(self.config)["stop_timeout_seconds"]
        )
        stopped = bool(done) and (self.work is None or self.work.done())
        if not stopped and self.job is not None:
            self.ledger.block(self.job, "inflight_stop")
        return stopped

    async def close(self):
        self.closing = True
        return await self.stop("shutdown")

    async def _supervise(self, event, job):
        started = self.monotonic()
        self.work = asyncio.create_task(self._perform(event, job))
        pending = False
        result = None
        reason = "failed"
        try:
            done, _ = await asyncio.wait({self.work}, timeout=job["duration"])
            if not done:
                self._request_stop("timeout", False)
                done, _ = await asyncio.wait(
                    {self.work}, timeout=job["limits"]["stop_timeout_seconds"]
                )
            if not done:
                pending = True
                self.work.add_done_callback(self._release_late)
                self.ledger.block(job, "inflight_timeout")
                return {"status": "blocked"}
            result = self.work.result()
            reason = "step_completed"
        except asyncio.CancelledError:
            reason = self.reason or "cancelled"
        except Exception as exc:
            reason = self.reason or type(exc).__name__
        try:
            if not job.get("settled"):
                self.ledger.finish(job, reason, self.monotonic() - started, manual=self.manual)
            self.ledger.settle_time(job, self.monotonic() - started)
        except Exception as exc:
            # A revised source or failed final transaction must not create a note.
            try:
                self.ledger.finish(job, type(exc).__name__, self.monotonic() - started,
                                   manual=self.manual)
            except Exception:
                self.ledger.block(job, "settlement_failed")
            reason = type(exc).__name__
        finally:
            if not pending:
                self.lease.release()
        return {"status": "waiting" if result and result.get("waited") else
                "completed" if result is not None and reason == "step_completed"
                else "interrupted", "reason": reason}

    def _release_late(self, task):
        try:
            task.result()
        except BaseException:
            pass
        self.lease.release()

    @staticmethod
    def _observe(task):
        # The owner polls durable state; retrieve unexpected task errors even
        # when no caller awaits the background task directly.
        if not task.cancelled():
            task.exception()

"""Reservations, cancellation, crash recovery and shared transport regressions."""

import ast
import asyncio
import copy
import json
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins"))
from astrbot_plugin_life_continuity.activity import ActivityEngine, ProcessLease
from astrbot_plugin_life_continuity.activity_state import (
    ACTIVITY_SCHEMA, SHANGHAI, ActivityLedger, BudgetExceeded, day_at,
)
from astrbot_plugin_life_continuity.delivery import DeliveryGate, quiet_now
from astrbot_plugin_life_continuity.native_agent import MeteredProvider
from astrbot_plugin_life_continuity.state import ConflictError, LifeStore, StateError, read_snapshot
from life_test_support import FakeEvent, load_plugin

SCOPE = "test:FriendMessage:1001"
PROACTIVE = ROOT / "overlays/astrbot/data/plugins/astrbot_plugin_proactive_chat"
SENDER = PROACTIVE / "core/services/sender_service.py"
EVENT = PROACTIVE / "core/adapters/astrbot_event.py"


class Clock:
    def __init__(self, value=None):
        self.now = value or datetime(2026, 10, 29, 12, tzinfo=SHANGHAI).timestamp()

    def __call__(self):
        return self.now


def extract_method(path, name, extra=None):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    method = next(node for node in ast.walk(tree)
                  if isinstance(node, ast.AsyncFunctionDef) and node.name == name)
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[
        ast.alias(name="annotations")], level=0), copy.deepcopy(method)], type_ignores=[])
    namespace = dict(extra or {})
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    return namespace[name]


class LedgerFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.store = LifeStore(self.folder)
        self.addCleanup(self.store.close)
        self.clock = Clock()
        self.ledger = ActivityLedger(self.store, self.clock)
        self.cfg = {"chat_cooldown_seconds": 0, "max_run_seconds": 120,
                    "model_timeout_seconds": 120, "stop_timeout_seconds": 1}
        self.goal = self.store.create_goal(SCOPE, "goal", "主题", "理由", "条件", "下一步")
        self.source = self.store.record_episode(SCOPE, self.goal["id"], "source", kind="source",
                                                evidence={"text": "主人提供的文字资料"})

    def begin(self, key="run"):
        return self.ledger.begin(SCOPE, self.goal["id"], key, self.cfg)

    def finish(self, job, seconds=10):
        self.ledger.finish(job, "test", seconds)
        self.ledger.settle_time(job, seconds)


class BudgetTests(LedgerFixture):
    def test_midnight_time_is_split_and_model_calls_belong_to_the_actual_call_day(self):
        self.clock.now = datetime(2026, 10, 29, 23, 59, 30, tzinfo=SHANGHAI).timestamp()
        job = self.begin()
        rows = self.store.db.execute(
            "SELECT day,seconds FROM activity_time ORDER BY day"
        ).fetchall()
        self.assertEqual([(row[0], row[1]) for row in rows],
                         [("2026-10-29", 30), ("2026-10-30", 90)])
        first = self.ledger.charge_model(SCOPE, job["run_id"], self.cfg)
        self.clock.now += 45
        second = self.ledger.charge_model(SCOPE, job["run_id"], self.cfg)
        self.assertNotEqual(first, second)
        self.finish(job, 45)
        rows = self.store.db.execute("SELECT seconds FROM activity_time ORDER BY day").fetchall()
        self.assertEqual([row[0] for row in rows], [30, 15])
        self.assertEqual(self.ledger.snapshot(SCOPE, self.cfg)["model_calls"], 1)

    def test_daily_and_run_call_limits_are_persistent_and_zero_budget_cannot_start(self):
        self.cfg.update(max_model_calls_per_run=1, daily_model_calls=1)
        job = self.begin()
        self.ledger.charge_model(SCOPE, job["run_id"], self.cfg)
        with self.assertRaises(BudgetExceeded):
            self.ledger.charge_model(SCOPE, job["run_id"], self.cfg)
        self.finish(job)
        with self.assertRaises(BudgetExceeded):
            self.begin("second")
        self.assertEqual(self.ledger.snapshot(SCOPE, self.cfg)["model_calls"], 1)
        self.clock.now += 86400
        self.cfg["daily_minutes"] = 0
        with self.assertRaises(BudgetExceeded):
            self.begin("third")
        self.assertEqual(read_snapshot(self.folder)["counts"]["runs"], 1)

    def test_clock_rollback_never_refunds_the_whole_reservation(self):
        job = self.begin()
        self.clock.now -= 30
        self.finish(job, 0)
        self.assertEqual(self.ledger.snapshot(SCOPE, self.cfg)["reserved_or_used_seconds"], 120)

    def test_lowering_live_limits_during_a_run_blocks_the_next_call(self):
        job = self.begin()
        self.ledger.charge_model(SCOPE, job["run_id"], self.cfg)
        self.cfg["daily_model_calls"] = 1
        with self.assertRaises(BudgetExceeded):
            self.ledger.charge_model(SCOPE, job["run_id"], self.cfg)

    def test_duplicate_trigger_and_no_sources_cannot_reserve_another_run(self):
        first = self.begin()
        duplicate = self.begin()
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(first["run_id"], duplicate["run_id"])
        self.finish(first)
        empty = self.store.create_goal(SCOPE, "empty", "空目标", "理由", "条件", "下一步")
        with self.assertRaises(StateError):
            self.ledger.begin(SCOPE, empty["id"], "empty-run", self.cfg)
        self.assertEqual(read_snapshot(self.folder)["counts"]["activity_sessions"], 1)

    def test_crash_recovery_retains_budget_and_never_restarts_or_clears_manual_pause(self):
        job = self.begin()
        self.ledger.charge_model(SCOPE, job["run_id"], self.cfg)
        self.ledger.control_update(SCOPE, paused=True, reason="owner_stop")
        self.store.close()
        reopened = LifeStore(self.folder)
        self.addCleanup(reopened.close)
        ledger = ActivityLedger(reopened, self.clock)
        self.assertEqual(reopened.get_goal(SCOPE, self.goal["id"])["status"], "running")
        self.assertEqual(ledger.recover(SCOPE), 1)
        self.assertEqual(ledger.snapshot(SCOPE, self.cfg)["reserved_or_used_seconds"], 120)
        self.assertTrue(ledger.control(SCOPE)["paused"])
        self.assertEqual(reopened.get_goal(SCOPE, self.goal["id"])["checkpoint"]["resume_policy"],
                         "manual")
        self.assertEqual(ledger.recover(SCOPE), 0)

    def test_recovery_never_revives_a_cancelled_goal(self):
        job = self.begin()
        self.store.db.execute("UPDATE goals SET status='cancelled' WHERE id=?", (self.goal["id"],))
        self.ledger.recover(SCOPE)
        self.assertEqual(self.store.get_goal(SCOPE, self.goal["id"])["status"], "cancelled")

    def test_time_reservation_rolls_back_when_run_insert_fails(self):
        self.store.db.execute("""CREATE TRIGGER fail_session BEFORE INSERT ON activity_sessions
            BEGIN SELECT RAISE(ABORT, 'disk failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.begin()
        self.assertEqual(self.store.get_goal(SCOPE, self.goal["id"])["status"], "ready")
        self.assertEqual(read_snapshot(self.folder)["counts"]["runs"], 0)

    def test_source_revisions_and_invented_ids_cannot_create_a_note(self):
        job = self.begin()
        with self.assertRaises(StateError):
            self.ledger.finish(job, "test", 1, note="虚构", next_step="下一步", source_ids=["invented"])
        self.store.record_episode(SCOPE, self.goal["id"], "correct", kind="owner_correction",
                                  evidence={"text": "纠正"}, supersedes_id=self.source["id"])
        with self.assertRaises(ConflictError):
            self.ledger.finish(job, "test", 1, note="过期", next_step="下一步",
                               source_ids=[self.source["id"]])
        self.assertEqual(read_snapshot(self.folder)["counts"]["notes"], 0)

    def test_quiet_hours_use_shanghai_and_invalid_specs_fail_closed(self):
        for hour, expected in ((0, True), (8, False), (23, True)):
            self.assertEqual(quiet_now("23-8", datetime(2026, 10, 29, hour, tzinfo=SHANGHAI)),
                             expected)
        with self.assertRaises(ValueError):
            quiet_now("invalid", datetime.now(SHANGHAI))
        self.assertEqual(day_at(datetime(2026, 10, 29, 0, tzinfo=SHANGHAI).timestamp())[0],
                         "2026-10-29")

    def test_process_lease_prevents_another_process_until_the_holder_exits(self):
        script = ("import sys; from pathlib import Path; "
                  "sys.path.insert(0,sys.argv[1]); "
                  "from astrbot_plugin_life_continuity.activity import ProcessLease; "
                  "lock=ProcessLease(Path(sys.argv[2])); lock.acquire(); "
                  "print('ready',flush=True); sys.stdin.readline()")
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(ROOT / "plugins"), str(self.folder)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )
        try:
            self.assertEqual(process.stdout.readline().strip(), "ready")
            lock = ProcessLease(self.folder)
            with self.assertRaises(StateError):
                lock.acquire()
            process.communicate("done\n", timeout=5)
            lock.acquire()
            lock.release()
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdin.close()
            process.stdout.close()


class ActivityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = LifeStore(Path(self.temp.name))
        self.addCleanup(self.store.close)
        self.goal = self.store.create_goal(SCOPE, "goal", "主题", "理由", "条件", "下一步")
        self.source = self.store.record_episode(SCOPE, self.goal["id"], "source", kind="source",
                                                evidence={"text": "来源文字"})
        self.config = {"activity_enabled": True, "chat_cooldown_seconds": 0,
                       "stop_timeout_seconds": 1, "max_model_calls_per_run": 1}
        self.payload = {"source_ids": [self.source["id"]], "note": "有限想法",
                        "questions": "待核实", "next_step": "下一步核验"}

        async def agent(context, event, prompt, before, after, config, stop, on_runner):
            identity = before()
            after(identity, {"input_other": 5, "output": 3})
            return self.payload

        self.engine = ActivityEngine(self.store, object(), self.config, lambda: None, agent=agent)
        self.addAsyncCleanup(self.engine.close)

    async def test_one_round_creates_note_and_next_round_continues_the_same_goal(self):
        self.engine.start(FakeEvent(), self.goal["id"], "run-1")
        self.assertEqual((await self.engine.task)["status"], "completed")
        first = self.store.get_goal(SCOPE, self.goal["id"])
        self.assertEqual(first["checkpoint"]["note_version"], 1)
        self.assertEqual(first["checkpoint"]["resume_policy"], "read_only")
        self.engine.start(FakeEvent(), self.goal["id"], "run-2")
        await self.engine.task
        self.assertEqual(self.store.get_goal(SCOPE, self.goal["id"])["checkpoint"]["note_version"],
                         2)
        self.assertEqual(read_snapshot(self.store.folder)["counts"]["goals"], 1)

    async def test_duplicate_trigger_does_not_generate_another_note_or_call(self):
        self.engine.start(FakeEvent(), self.goal["id"], "run-1")
        await self.engine.task
        result = self.engine.start(FakeEvent(), self.goal["id"], "run-1")
        self.assertTrue(result["duplicate"])
        self.assertEqual(read_snapshot(self.store.folder)["counts"]["activity_calls"], 1)

    async def test_foreground_stop_preserves_sources_and_prevents_new_calls(self):
        started = asyncio.Event()

        async def agent(context, event, prompt, before, after, config, stop, on_runner):
            before()
            started.set()
            await asyncio.Event().wait()

        self.engine.agent = agent
        self.engine.start(FakeEvent(), self.goal["id"], "run")
        await started.wait()
        self.assertTrue(await self.engine.stop("foreground", manual=True))
        snapshot = read_snapshot(self.store.folder, scope=SCOPE)
        self.assertEqual(snapshot["counts"]["notes"], 0)
        self.assertEqual(snapshot["counts"]["episodes"], 1)
        self.assertEqual(snapshot["goals"][0]["checkpoint"]["resume_policy"], "manual")
        with self.assertRaises(StateError):
            self.engine.start(FakeEvent(), self.goal["id"], "another")

    async def test_noncooperative_call_keeps_lease_and_blocks_a_second_operator(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def stubborn(*args):
            started.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    pass
            return self.payload

        self.engine.agent = stubborn
        self.engine.start(FakeEvent(), self.goal["id"], "run")
        await started.wait()
        try:
            self.assertFalse(await self.engine.stop("owner_stop", manual=True))
            self.assertEqual(self.store.get_goal(SCOPE, self.goal["id"])["status"], "blocked")
            with self.assertRaises(StateError):
                ProcessLease(self.store.folder).acquire()
            with self.assertRaises(StateError):
                self.engine.start(FakeEvent(), self.goal["id"], "second")
        finally:
            release.set()
            await self.engine.task
        self.assertEqual(self.engine.recover(SCOPE), 1)

    async def test_round_timeout_stops_without_a_note(self):
        self.config.update(max_run_seconds=1, model_timeout_seconds=1)

        async def delayed(*args):
            await asyncio.Event().wait()

        self.engine.agent = delayed
        self.engine.start(FakeEvent(), self.goal["id"], "run")
        result = await self.engine.task
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(read_snapshot(self.store.folder)["counts"]["notes"], 0)

    async def test_revised_source_during_model_call_does_not_commit_stale_output(self):
        async def corrected(*args):
            self.store.record_episode(SCOPE, self.goal["id"], "correction", kind="owner_correction",
                                      evidence={"text": "新来源"}, supersedes_id=self.source["id"])
            return self.payload

        self.engine.agent = corrected
        self.engine.start(FakeEvent(), self.goal["id"], "run")
        await self.engine.task
        self.assertEqual(read_snapshot(self.store.folder)["counts"]["notes"], 0)

    async def test_recovery_does_not_touch_a_live_delivery_in_another_executor(self):
        delivery_lease = ProcessLease(self.store.folder, "delivery.lock")
        delivery_lease.acquire()
        try:
            with self.assertRaises(StateError):
                self.engine.recover(SCOPE)
            with self.assertRaises(StateError):
                self.engine.start(FakeEvent(), self.goal["id"], "run")
            self.assertEqual(read_snapshot(self.store.folder)["counts"]["runs"], 0)
        finally:
            delivery_lease.release()


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = LifeStore(Path(self.temp.name))
        self.addCleanup(self.store.close)
        self.clock = Clock()
        self.ledger = ActivityLedger(self.store, self.clock)
        self.cfg = {"daily_shares": 1, "chat_cooldown_seconds": 0, "stop_timeout_seconds": 1}
        self.gate = DeliveryGate(self.ledger, self.cfg, lambda: None)
        self.addAsyncCleanup(self.gate.close)

    async def test_life_and_proactive_chat_share_the_same_daily_quota(self):
        first, second = AsyncMock(return_value=True), AsyncMock(return_value=True)
        self.assertTrue(await self.gate.send(SCOPE, "life", "note:1", "一份进展", first))
        with self.assertRaises(BudgetExceeded):
            await self.gate.send(SCOPE, "proactive", "chat:1", "另一条消息", second)
        second.assert_not_called()
        self.assertEqual(self.ledger.snapshot(SCOPE, self.cfg)["shares"], 1)

    async def test_unknown_send_is_not_retried_by_key_or_content(self):
        first = AsyncMock(side_effect=TimeoutError("lost reply"))
        with self.assertRaises(TimeoutError):
            await self.gate.send(SCOPE, "life", "note:1", "同一条消息", first)
        self.assertFalse(await self.gate.send(SCOPE, "life", "note:1", "同一条消息", first))
        self.assertFalse(await self.gate.send(SCOPE, "proactive", "new-key", "同一条消息", first))
        first.assert_awaited_once()
        delivery = read_snapshot(self.store.folder, scope=SCOPE)["life_deliveries"][0]
        self.assertEqual(delivery["status"], "uncertain")

    async def test_share_limits_survive_reopen_and_day_rollover(self):
        await self.gate.send(SCOPE, "life", "note:1", "原进展", AsyncMock(return_value=True))
        reopened = LifeStore(self.store.folder)
        self.addCleanup(reopened.close)
        ledger = ActivityLedger(reopened, self.clock)
        self.assertEqual(ledger.snapshot(SCOPE, self.cfg)["remaining_shares"], 0)
        self.clock.now += 86400
        self.assertEqual(ledger.snapshot(SCOPE, self.cfg)["remaining_shares"], 1)

    async def test_cooldown_manual_stop_and_quiet_policy_do_not_reserve_or_send(self):
        self.cfg["chat_cooldown_seconds"] = 300
        self.ledger.control_update(SCOPE, chat=True)
        dispatch = AsyncMock(return_value=True)
        self.assertFalse(await self.gate.send(SCOPE, "life", "one", "文本", dispatch))
        self.clock.now += 301
        self.ledger.control_update(SCOPE, paused=True, reason="owner_stop")
        self.assertFalse(await self.gate.send(SCOPE, "life", "two", "文本", dispatch))
        dispatch.assert_not_called()
        self.assertEqual(self.ledger.snapshot(SCOPE, self.cfg)["shares"], 0)

    async def test_noncooperative_transport_holds_its_own_lease_after_timeout(self):
        release = asyncio.Event()

        async def stubborn():
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    pass
            return True

        self.assertFalse(await self.gate.send(SCOPE, "life", "one", "文本", stubborn))
        try:
            with self.assertRaises(StateError):
                ProcessLease(self.store.folder, "delivery.lock").acquire()
            self.assertEqual(self.ledger.snapshot(SCOPE, self.cfg)["shares"], 1)
        finally:
            release.set()
            await asyncio.gather(*self.gate.pending)
            await asyncio.sleep(0)

    async def test_proactive_wrapper_uses_gate_and_default_path_stays_unchanged(self):
        method = extract_method(SENDER, "send_proactive_message")
        event = FakeEvent()
        life = types.SimpleNamespace(wants_proactive_gate=lambda scope: True)

        async def guarded(scope, key, text, dispatch):
            return await self.gate.send(scope, "proactive", key, text, dispatch)

        life.guarded_proactive = guarded
        metadata = types.SimpleNamespace(name="astrbot_plugin_life_continuity", activated=True,
                                         star_cls=life)
        sender = types.SimpleNamespace(
            plugin=types.SimpleNamespace(context=types.SimpleNamespace(
                get_registered_star=lambda name: metadata,
            )),
            _send_proactive_message=AsyncMock(return_value=True),
            build_proactive_event=lambda scope: event,
        )
        self.assertTrue(await method(sender, SCOPE, "主动消息"))
        self.assertTrue(event.get_extra("_life_delivery_guarded"))
        self.assertFalse(await method(sender, SCOPE, "主动消息"))
        sender._send_proactive_message.assert_awaited_once()
        metadata.activated = False
        self.assertTrue(await method(sender, SCOPE, "原有路径"))
        self.assertEqual(sender._send_proactive_message.await_count, 2)

    async def test_guarded_sender_exception_does_not_fall_back_to_second_transport(self):
        method = extract_method(SENDER, "send_chain", {"MessageChain": lambda values: values})
        event = FakeEvent()
        event.set_extra("_life_delivery_guarded", True)
        event.send.side_effect = TimeoutError("response lost")
        sender = types.SimpleNamespace(send_chain_direct=AsyncMock(),
                                       _mark_event_send_failed=Mock())
        self.assertFalse(await method(sender, SCOPE, event, ["text"]))
        sender.send_chain_direct.assert_not_called()
        self.assertTrue(event.get_extra("_life_delivery_uncertain"))

    async def test_guarded_event_exception_never_calls_core_fallback(self):
        method = extract_method(EVENT, "_send_life_guarded", {
            "PlatformStatus": types.SimpleNamespace(RUNNING="running"),
            "MessageType": types.SimpleNamespace(FRIEND_MESSAGE="friend", GROUP_MESSAGE="group"),
            "MessageSession": lambda **kwargs: kwargs,
        })
        platform = types.SimpleNamespace(
            status="running", meta=lambda: types.SimpleNamespace(id="test"),
            send_by_session=AsyncMock(side_effect=TimeoutError()),
        )
        event = FakeEvent()
        event._resolve_platform = lambda: platform
        event._proactive_is_group, event._proactive_target_id = False, "1001"
        event._send_via_core_api = AsyncMock(return_value=True)
        event._persist_sent_chain = AsyncMock()
        self.assertFalse(await method(event, ["text"]))
        event._send_via_core_api.assert_not_called()


class MeterTests(unittest.IsolatedAsyncioTestCase):
    async def test_proxy_meters_calls_without_changing_shared_provider(self):
        provider = types.SimpleNamespace(provider_config={"id": "test"}, text_chat=AsyncMock(
            return_value=types.SimpleNamespace(usage=types.SimpleNamespace(
                input_other=5, input_cached=2, output=3,
            ))))
        original = provider.text_chat
        before, after = Mock(return_value="call"), Mock()
        proxy = MeteredProvider(provider, Mock(), before, after, 1)
        await proxy.text_chat()
        before.assert_called_once()
        after.assert_called_once_with("call", {"input_other": 5, "input_cached": 2, "output": 3})
        self.assertIs(provider.text_chat, original)

    async def test_budget_exception_blocks_hidden_retries_before_provider_is_called(self):
        provider = types.SimpleNamespace(text_chat=AsyncMock())
        runner = Mock()
        proxy = MeteredProvider(provider, runner, Mock(side_effect=BudgetExceeded("limit")),
                                Mock(), 1)
        for _ in range(2):
            with self.assertRaises(BudgetExceeded):
                await proxy.text_chat()
        provider.text_chat.assert_not_called()
        runner.request_stop.assert_called_once()


class ActivityMigrationTests(unittest.TestCase):
    def test_v2_upgrade_preserves_data_and_backups_and_readonly_does_not_upgrade(self):
        with tempfile.TemporaryDirectory() as folder:
            store = LifeStore(Path(folder))
            goal = store.create_goal(SCOPE, "old", "原目标", "理由", "条件", "下一步")
            for table in ("activity_calls", "activity_time", "activity_sessions",
                          "activity_control",
                          "life_deliveries"):
                store.db.execute(f"DROP TABLE {table}")
            store.db.execute("PRAGMA user_version=2")
            self.assertEqual(read_snapshot(Path(folder))["schema_version"], 2)
            store.close()
            upgraded = LifeStore(Path(folder))
            try:
                self.assertEqual(upgraded.get_goal(SCOPE, goal["id"])["title"], "原目标")
                with closing(sqlite3.connect(upgraded.schema_backup)) as backup:
                    self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 2)
                self.assertEqual(read_snapshot(Path(folder))["schema_version"], 3)
            finally:
                upgraded.close()

    def test_v2_partial_ddl_failure_rolls_back_without_clearing_goals(self):
        with tempfile.TemporaryDirectory() as folder:
            store = LifeStore(Path(folder))
            store.create_goal(SCOPE, "old", "原目标", "理由", "条件", "下一步")
            for table in ("activity_calls", "activity_time", "activity_sessions",
                          "activity_control",
                          "life_deliveries"):
                store.db.execute(f"DROP TABLE {table}")
            store.db.execute("PRAGMA user_version=2")
            store.close()

            def fail(instance):
                instance.db.execute(ACTIVITY_SCHEMA[0])
                raise RuntimeError("interrupted migration")

            with patch.object(LifeStore, "_create_schema", fail), self.assertRaises(RuntimeError):
                LifeStore(Path(folder))
            self.assertEqual(read_snapshot(Path(folder))["schema_version"], 2)
            self.assertEqual(read_snapshot(Path(folder))["counts"]["goals"], 1)


class ActivityPluginTests(unittest.IsolatedAsyncioTestCase):
    async def test_incomplete_proactive_patch_prevents_activity_start(self):
        with tempfile.TemporaryDirectory() as folder:
            module = load_plugin(Path(folder))
            old = types.SimpleNamespace(sender_service=object())
            context = types.SimpleNamespace(get_registered_star=lambda name: types.SimpleNamespace(
                activated=True, star_cls=old,
            ))
            plugin = module.LifeContinuityPlugin(context, {"enabled": True, "owner_scope": SCOPE,
                                                           "activity_enabled": True})
            await plugin.initialize()
            try:
                with self.assertRaisesRegex(StateError, "补丁"):
                    plugin.activity_policy()
            finally:
                await plugin.terminate()

    async def test_lost_command_reply_is_not_sent_twice(self):
        with tempfile.TemporaryDirectory() as folder:
            module = load_plugin(Path(folder))
            plugin = module.LifeContinuityPlugin(object(), {"enabled": True, "owner_scope": SCOPE})
            await plugin.initialize()
            try:
                event = FakeEvent("/life list")
                event.send.side_effect = TimeoutError("lost reply")
                await plugin.owner_message(event)
                event.send.assert_awaited_once()
            finally:
                await plugin.terminate()

    async def test_unavailable_database_does_not_interrupt_normal_chat(self):
        with tempfile.TemporaryDirectory() as folder:
            module = load_plugin(Path(folder))
            plugin = module.LifeContinuityPlugin(object(), {"enabled": True, "owner_scope": SCOPE})
            await plugin.initialize()
            try:
                plugin.store.close()
                request = types.SimpleNamespace(system_prompt="正常聊天人格", func_tool=None)
                await plugin.inject_context(FakeEvent(), request)
                self.assertIn("正常聊天人格", request.system_prompt)
                self.assertIn("暂不可用", request.system_prompt)
            finally:
                await plugin.terminate()

    async def test_stop_signal_is_still_sent_when_pause_persistence_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            module = load_plugin(Path(folder))
            plugin = module.LifeContinuityPlugin(object(), {"enabled": True, "owner_scope": SCOPE})
            await plugin.initialize()
            try:
                plugin.engine.stop = AsyncMock(return_value=True)
                with patch.object(plugin.engine.ledger, "control_update",
                                  side_effect=sqlite3.OperationalError("disk full")):
                    await plugin.owner_message(FakeEvent("/life stop"))
                plugin.engine.stop.assert_awaited_once_with("owner_stop", manual=True)
            finally:
                await plugin.terminate()

    async def test_explicit_false_send_result_is_not_marked_as_delivered(self):
        with tempfile.TemporaryDirectory() as folder:
            module = load_plugin(Path(folder))
            plugin = module.LifeContinuityPlugin(object(), {"enabled": True, "owner_scope": SCOPE})
            await plugin.initialize()
            try:
                plugin.delivery.policy = lambda: None
                event = FakeEvent()
                event.send.return_value = False
                goal = {"id": "test-goal", "title": "主题", "checkpoint": {"note_version": 1}}
                self.assertFalse(await plugin.share_note(event, {}, goal, "笔记"))
                row = read_snapshot(Path(folder), scope=SCOPE)["life_deliveries"][0]
                self.assertEqual(row["status"], "uncertain")
                self.assertFalse(row["receipt"]["transport_ack"])
            finally:
                await plugin.terminate()

    async def test_owner_can_prepare_sources_but_activity_switch_defaults_off(self):
        with tempfile.TemporaryDirectory() as folder:
            module = load_plugin(Path(folder))
            plugin = module.LifeContinuityPlugin(object(), {"enabled": True, "owner_scope": SCOPE})
            await plugin.initialize()
            try:
                event = FakeEvent("/life new 主题 | 理由 | 结束条件 | 下一步")
                await plugin.owner_message(event)
                goal = plugin.chat.goals(SCOPE)[0]
                source = FakeEvent(f"/life source {goal['id']} | 主人提供的资料", event_id="source")
                await plugin.owner_message(source)
                self.assertEqual(plugin.engine.ledger.sources(SCOPE, goal["id"])[0]["kind"],
                                 "source")
                run = FakeEvent(f"/life run {goal['id']}", event_id="run")
                await plugin.owner_message(run)
                self.assertEqual(read_snapshot(Path(folder))["counts"]["runs"], 0)
                self.assertIsNone(plugin.engine.task)
            finally:
                await plugin.terminate()


if __name__ == "__main__":
    unittest.main()

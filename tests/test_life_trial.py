"""Strict trial limits and safe single-shot local acceptance operations."""

import copy
import hashlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins"), str(ROOT / "tools")]
from astrbot_plugin_life_continuity.activity_state import ActivityLedger, limits
from astrbot_plugin_life_continuity.delivery import DeliveryGate
from astrbot_plugin_life_continuity.state import LifeStore, StateError
from astrbot_plugin_life_continuity.trial import PROBE_TEXT, TrialControl
from astrbot_plugin_life_continuity.native_agent import validate_research
from life_test_support import FakeEvent
import life_trial


SCOPE = "qq:FriendMessage:1001"


class TrialLimitsTests(unittest.TestCase):
    def test_question_arrays_are_bounded_normalized_without_coercing_other_fields(self):
        result = {"note": "来源想法", "questions": ["问题一", "问题二"], "next_step": "补充资料",
                  "source_ids": ["real-episode"], "interest": 1}
        self.assertEqual(validate_research(result)["questions"], "- 问题一\n- 问题二")
        self.assertEqual(result["questions"], ["问题一", "问题二"])
        for invalid in ([], [""], [{"question": "不能强转对象"}], ["问题"] * 9, ["长" * 1001]):
            with self.subTest(invalid_type=type(invalid).__name__), self.assertRaises(ValueError):
                validate_research(dict(result, questions=invalid))
        with self.assertRaises(ValueError):
            validate_research(dict(result, note=["不能强转想法"]))

    def test_trial_limits_only_tighten_existing_budgets_and_cooldown(self):
        config = {key: 1000 for key in limits({})}
        config.update(trial_mode=True, chat_cooldown_seconds=0)
        result = limits(config)
        self.assertEqual(result, limits({}))
        config.update(daily_model_calls=0, daily_shares=0, daily_minutes=0,
                      chat_cooldown_seconds=900)
        result = limits(config)
        self.assertEqual((result["daily_model_calls"], result["daily_shares"],
                          result["daily_minutes"], result["chat_cooldown_seconds"]), (0, 0, 0, 900))
        with self.assertRaises(ValueError):
            limits({"trial_mode": "false"})

    def test_daily_round_is_not_refunded_after_failure_reopen_or_config_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            store = LifeStore(folder)
            try:
                goal = store.create_goal(SCOPE, "goal", "问题", "理由", "条件", "下一步")
                store.record_episode(SCOPE, goal["id"], "source", kind="source",
                                     evidence={"text": "实际资料"})
                now = [2000000000.0]
                ledger = ActivityLedger(store, lambda: now[0])
                config = {"trial_mode": True}
                job = ledger.begin(SCOPE, goal["id"], "first", config)
                ledger.finish(job, "TimeoutError", 0)
                ledger.settle_time(job, 0)
                self.assertTrue(ledger.begin(SCOPE, goal["id"], "first", config)["duplicate"])
                with self.assertRaisesRegex(StateError, "当天"):
                    ledger.begin(SCOPE, goal["id"], "second", dict(config, daily_model_calls=100))
                store.close()
                store = LifeStore(folder)
                ledger = ActivityLedger(store, lambda: now[0])
                with self.assertRaises(StateError):
                    ledger.begin(SCOPE, goal["id"], "third", config)
                now[0] += 86400
                ledger.begin(SCOPE, goal["id"], "tomorrow", config)
                self.assertEqual(ledger.snapshot(SCOPE, config)["started_rounds"], 1)
            finally:
                store.close()


class TrialControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = LifeStore(self.temp.name)
        self.addCleanup(self.store.close)
        self.event = FakeEvent(scope=SCOPE)
        self.event.send.return_value = True
        self.ledger = ActivityLedger(self.store)
        self.config = {"enabled": True, "activity_enabled": True, "trial_mode": True,
                       "owner_scope": SCOPE, "share_enabled": False}
        self.plugin = types.SimpleNamespace(
            store=self.store, config=self.config, activity_policy=lambda: None,
            engine=types.SimpleNamespace(ledger=self.ledger, start=Mock()),
            context=types.SimpleNamespace(get_registered_star=lambda name:
                types.SimpleNamespace(star_cls=types.SimpleNamespace(sender_service=
                    types.SimpleNamespace(build_proactive_event=lambda scope: self.event)))))
        self.plugin.delivery = DeliveryGate(self.ledger, self.config, lambda: None)
        self.addAsyncCleanup(self.plugin.delivery.close)
        self.control = TrialControl(self.plugin)

    async def prepare(self):
        return await self.control.dispatch({"operation": "prepare", "source_name": "公开说明.md",
                                            "source_text": "事实资料，不是主人的QQ发言"})

    async def test_prepare_records_operator_provenance_and_replays_without_new_goals(self):
        result = await self.prepare()
        repeated = await self.prepare()
        self.assertEqual(result, repeated)
        source = self.store.get_episode(SCOPE, result["episode_id"])
        self.assertEqual(source["evidence"]["source_type"], "operator_document")
        self.assertNotIn("message_id", source["evidence"])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM goals").fetchone()[0], 1)

    async def test_send_requires_consent_and_never_repeats_by_a_different_invocation(self):
        with self.assertRaises(StateError):
            await self.control.dispatch({"operation": "send"})
        self.event.send.assert_not_called()
        request = {"operation": "send", "confirm": "SEND_ONE_FIXED_MESSAGE"}
        first = await self.control.dispatch(request)
        second = await self.control.dispatch(request)
        self.assertTrue(first["transport_ack"])
        self.assertFalse(second["transport_ack"])
        self.assertFalse(first["recipient_confirmed"])
        self.event.send.assert_awaited_once_with(PROBE_TEXT)

    async def test_trial_operations_cannot_enable_note_sharing_or_override_recipient(self):
        await self.prepare()
        self.config["share_enabled"] = True
        with self.assertRaises(StateError):
            await self.control.dispatch({"operation": "run", "trial_id": "a" * 32})
        self.plugin.engine.start.assert_not_called()
        self.config["trial_mode"] = False
        with self.assertRaises(StateError):
            await self.control.dispatch({"operation": "send", "confirm": "SEND_ONE_FIXED_MESSAGE"})

    async def test_repeated_daily_trigger_is_stable_but_advances_on_a_real_new_day(self):
        await self.prepare()
        now = [2000000000.0]
        self.ledger.clock = lambda: now[0]
        request = {"operation": "run", "trial_id": "a" * 32}
        await self.control.dispatch(request)
        first = self.plugin.engine.start.call_args.args[2]
        await self.control.dispatch(request)
        self.assertEqual(self.plugin.engine.start.call_args.args[2], first)
        now[0] += 86400
        await self.control.dispatch(request)
        self.assertNotEqual(self.plugin.engine.start.call_args.args[2], first)

    async def test_trial_web_rejects_unauthenticated_and_remote_requests_before_dispatch(self):
        for host, username in (("127.0.0.1", None), ("192.0.2.1", "admin")):
            request = types.SimpleNamespace(client_host=host, username=username)
            module = types.ModuleType("astrbot.api.web")
            module.request = request
            with patch.dict(sys.modules, {"astrbot.api.web": module}):
                result = await self.control.web()
            self.assertEqual(result["status"], "error")
        self.plugin.engine.start.assert_not_called()

    async def test_status_redacts_scope_and_document_text(self):
        await self.prepare()
        status = json.dumps(self.control.status(), ensure_ascii=False)
        self.assertNotIn(SCOPE, status)
        self.assertNotIn("事实资料", status)

    async def test_review_reuses_returned_response_without_rewriting_run_or_budget(self):
        source = await self.prepare()
        job = self.ledger.begin(SCOPE, source["goal_id"], "phase5:run:" + "a" * 32, self.config)
        call = self.ledger.charge_model(SCOPE, job["run_id"], self.config)
        self.ledger.record_usage(call, {"output": 5})
        self.ledger.finish(job, "ValueError", 0)
        self.ledger.settle_time(job, 1)
        raw = json.dumps({"note": "模型想法", "questions": ["问题一"], "next_step": "继续核验",
                          "source_ids": [source["episode_id"]]})
        folder = self.store.folder / "acceptance_diagnostics"
        folder.mkdir()
        life_trial.write(folder / (job["run_id"] + ".json"), {
            "run_id": job["run_id"], "goal_id": source["goal_id"], "goal_version": job["version"],
            "read_ranges": {source["episode_id"]: [0, 10]}, "completion_text": raw,
            "sha256": hashlib.sha256(raw.encode()).hexdigest()})
        result = await self.control.dispatch({"operation": "review", "run_id": job["run_id"]})
        self.assertEqual(result["note_version"], 1)
        self.assertEqual(result["new_model_calls"], 0)
        self.assertTrue((await self.control.dispatch({"operation": "review",
                                                     "run_id": job["run_id"]}))["duplicate"])
        status = self.control.status()
        self.assertEqual(status["runs"][0]["status"], "interrupted")
        self.assertEqual(status["budget"]["model_calls"], 1)
        self.assertEqual(status["counts"]["goal_attempts"], 0)
        self.assertEqual(status["counts"]["interest_evidence"], 0)
        self.plugin.engine.start.assert_not_called()


class TrialToolsTests(unittest.TestCase):
    def test_deploy_rejects_redirection_even_within_the_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            relative = next(iter(life_trial.code_files()))
            original = Path.resolve

            def resolve(path, *args, **kwargs):
                return root / "unrelated.py" if path == root / relative else original(
                    path, *args, **kwargs)

            with patch.object(Path, "resolve", resolve), self.assertRaises(RuntimeError):
                life_trial.deploy_closed(root)
            self.assertEqual(list(root.iterdir()), [])

    def test_deploy_copies_only_feature_code_and_keeps_other_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unrelated = root / "config/settings.json"
            unrelated.parent.mkdir()
            unrelated.write_text('{"private":"preserve"}', encoding="utf-8")
            result = life_trial.deploy_closed(root)
            self.assertTrue(result["deployed_closed"])
            config = life_trial.read(root / life_trial.CONFIG)
            self.assertFalse(config["enabled"])
            self.assertFalse(config["activity_enabled"])
            self.assertTrue(config["trial_mode"])
            self.assertEqual(unrelated.read_text(encoding="utf-8"), '{"private":"preserve"}')
            config["enabled"] = True
            life_trial.write(root / life_trial.CONFIG, config)
            with self.assertRaises(RuntimeError):
                life_trial.deploy_closed(root)

    def test_report_counts_recorded_days_without_declaring_acceptance_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = {"offline": False, "observed_at_utc": "2026-10-30T02:00:00+00:00",
                     "status": {"runs": [], "notes": [], "deliveries": [], "memory_exports": []}}
            life_trial.write(root / "first.json", entry)
            life_trial.write(root / "same_day.json", entry)
            offline = copy.deepcopy(entry)
            offline.update(offline=True, observed_at_utc="2026-11-15T02:00:00+00:00")
            life_trial.write(root / "offline.json", offline)
            result = life_trial.report(root)
            self.assertEqual(result["recorded_days"], 1)
            self.assertFalse(result["recipient_confirmed"])
            self.assertFalse(result["observation_window_started"])
            self.assertEqual(result["acceptance"], "incomplete_manual_review_required")


if __name__ == "__main__":
    unittest.main()

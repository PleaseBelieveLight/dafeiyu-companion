"""Evidence, lifecycle and metered candidate-selection regressions, entirely offline."""

import asyncio
import json
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins"))
from astrbot_plugin_life_continuity.activity import ActivityEngine
from astrbot_plugin_life_continuity.activity_state import ActivityLedger
from astrbot_plugin_life_continuity.chat import parse_command
from astrbot_plugin_life_continuity.selection_state import GoalPolicy, research_sources
from astrbot_plugin_life_continuity.state import (
    SCHEMA_VERSION, ConflictError, LifeStore, StateError, read_snapshot,
)
from life_test_support import FakeEvent, load_plugin, remove_selection_schema

SCOPE, OTHER = "test:FriendMessage:1001", "test:FriendMessage:1002"


class Fixture:
    def setup_store(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = LifeStore(Path(self.temp.name))
        self.addCleanup(self.store.close)
        self.now = 2000000000.0
        self.ledger = ActivityLedger(self.store, lambda: self.now)
        self.policy = GoalPolicy(self.store)
        self.config = {"activity_enabled": True, "chat_cooldown_seconds": 0,
                       "max_run_seconds": 10, "model_timeout_seconds": 10,
                       "stop_timeout_seconds": 1, "daily_model_calls": 100}
        self.serial = 0

    def goal(self, title="研究机制", *, owner="agent", scope=SCOPE):
        return self.store.create_goal(scope, title, title, "理解有限问题", "解释需有来源", "核对资料",
                                      owner_kind=owner)

    def source(self, goal, text="来源甲：先小规模核对", **kwargs):
        self.serial += 1
        return self.store.record_episode(goal["scope"], goal["id"], str(self.serial),
                                         kind="source", evidence={"text": text}, **kwargs)

    def step(self, goal, *, interest=1, failure=None):
        self.serial += 1
        job = self.ledger.begin(goal["scope"], goal["id"], f"run:{self.serial}", self.config)
        job["research_called"] = True
        call = self.ledger.charge_model(goal["scope"], job["run_id"], self.config)
        if not failure:
            self.ledger.record_usage(call, {"output": 5})
        result = self.ledger.finish(job, failure or "step_completed", 0,
            note=None if failure else "变化的文字不一定是新证据：" + str(self.serial),
            source_ids=[row["id"] for row in job["sources"]], next_step="继续核验",
            interest=interest)
        self.ledger.settle_time(job, 0.01)
        return result

    def review(self, goal, action, payload=None, *, event_id=None):
        self.serial += 1
        current = self.store.get_goal(goal["scope"], goal["id"])
        return self.policy.review(goal["scope"], goal["id"], "1001", event_id or str(self.serial),
            action, payload or {"reason": "主人明确评议"}, current["version"], self.now)


class SelectionStateTests(Fixture, unittest.TestCase):
    def setUp(self):
        self.setup_store()

    def test_same_topic_deduplicates_across_messages_without_reopening_terminal_goals(self):
        goal = self.goal("ABC 建造！")
        self.review(goal, "abandon")
        duplicate = self.store.create_goal(SCOPE, "new-message", "ａｂｃ建造", "理由", "条件", "下一步")
        self.assertEqual(duplicate["id"], goal["id"])
        self.assertEqual(duplicate["status"], "abandoned")
        self.assertEqual(read_snapshot(self.store.folder)["counts"]["goals"], 1)

    def test_three_open_goal_cap_includes_shelved_and_is_isolated_by_scope(self):
        first = self.goal("第一题")
        self.goal("第二题")
        self.goal("第三题")
        self.review(first, "shelve")
        with self.assertRaisesRegex(StateError, "三个"):
            self.goal("第四题")
        self.goal("第四题", scope=OTHER)
        self.review(first, "abandon")
        self.goal("第四题")
        with self.assertRaises(StateError):
            self.review(first, "reopen")

    def test_meaningful_topic_symbols_do_not_merge_different_research_questions(self):
        ids = {self.goal(title)["id"] for title in ("学习C", "学习C++", "学习C#")}
        self.assertEqual(len(ids), 3)

    def test_manual_pause_terminal_and_missing_sources_never_enter_candidates(self):
        eligible, paused, empty = self.goal("可执行"), self.goal("手动暂停"), self.goal("无来源")
        self.source(eligible)
        self.source(paused)
        self.store.apply_chat_event(SCOPE, "1001", "pause", paused["id"], "暂停", action="pause",
                                    expected_version=1)
        self.assertEqual([row["id"] for row in self.policy.candidates(SCOPE, self.now)],
                         [eligible["id"]])
        self.review(eligible, "abandon")
        self.assertEqual(self.policy.candidates(SCOPE, self.now), [])
        self.assertEqual(self.store.get_goal(SCOPE, empty["id"])["status"], "ready")

    def test_rewording_and_copied_sources_do_not_inflate_progress_or_interest(self):
        goal = self.goal()
        self.source(goal)
        self.step(goal)
        self.source(goal)  # A different episode ID for the same supplied text.
        result = self.step(goal)
        self.assertFalse(result["checkpoint"]["progress"])
        self.assertEqual(result["checkpoint"]["streak"], 1)
        summary = self.policy.interests(SCOPE, goal["id"], self.now)
        self.assertEqual(summary["agent"]["sources"], 1)
        self.assertEqual(summary["owner"]["sources"], 0)

    def test_owner_feedback_never_becomes_preference_or_research_after_correction(self):
        goal = self.goal()
        result = self.store.apply_chat_event(SCOPE, "1001", "feedback", goal["id"], "建议先看资料")
        self.store.apply_chat_event(SCOPE, "1001", "correct", goal["id"], "纠正建议",
            action="correct", episode_id=result["episode_id"], next_step="依然只是建议")
        self.assertEqual(research_sources(self.store, SCOPE, goal["id"]), [])
        self.assertEqual(self.policy.interests(SCOPE, goal["id"], self.now)["owner"]["sources"], 0)
        self.source(goal)
        advice = self.policy.candidates(SCOPE, self.now)[0]["owner_advice"]
        self.assertEqual(advice[0]["text"], "依然只是建议")

    def test_explicit_preferences_are_separate_deduplicated_and_decay(self):
        goal = self.goal()
        payload = {"stance": -1, "reason": "我不喜欢这个主题"}
        self.review(goal, "prefer", payload)
        self.review(goal, "prefer", payload)
        before = self.policy.interests(SCOPE, goal["id"], self.now)
        after = self.policy.interests(SCOPE, goal["id"], self.now + 2592000)
        self.assertEqual(before["owner"], {"score": -1, "sources": 1})
        self.assertEqual(after["owner"]["score"], -0.5)
        self.assertEqual(before["agent"]["sources"], 0)
        self.assertEqual(research_sources(self.store, SCOPE, goal["id"]), [])

    def test_revised_evidence_is_removed_from_interest_scores(self):
        goal = self.goal()
        source = self.source(goal)
        self.step(goal)
        self.store.record_episode(SCOPE, goal["id"], "revision", kind="owner_correction",
            evidence={"text": "原资料错误"}, supersedes_id=source["id"])
        self.assertEqual(self.policy.interests(SCOPE, goal["id"], self.now)["agent"]["sources"], 0)

    def test_three_no_progress_rounds_shelve_until_review_and_new_evidence(self):
        goal = self.goal()
        self.source(goal)
        for _ in range(4):
            result = self.step(goal)
        self.assertEqual(result["status"], "shelved")
        self.assertEqual(result["checkpoint"]["streak"], 3)
        self.assertEqual(self.policy.candidates(SCOPE, self.now), [])
        self.source(goal, "新来源乙：限制与规模有关")
        self.assertEqual(self.policy.candidates(SCOPE, self.now), [])
        self.now += 86401
        self.assertEqual(self.policy.candidates(SCOPE, self.now)[0]["id"], goal["id"])
        resumed = self.step(goal)
        self.assertTrue(resumed["checkpoint"]["progress"])
        self.assertEqual(resumed["checkpoint"]["streak"], 0)

    def test_owner_goal_waits_for_guidance_instead_of_being_abandoned(self):
        goal = self.goal(owner="user")
        self.source(goal)
        for _ in range(4):
            result = self.step(goal, interest=-1)
        self.assertEqual(result["status"], "paused")
        self.assertEqual(result["checkpoint"]["resume_policy"], "manual")
        self.now += 172800
        self.source(goal, "新资料也不能绕过手动暂停")
        self.assertEqual(self.policy.candidates(SCOPE, self.now), [])

    def test_dependency_failure_and_budget_rejection_do_not_count_as_stagnation(self):
        goal = self.goal()
        self.source(goal)
        self.step(goal, failure="TimeoutError")
        self.assertEqual(self.policy.profile(SCOPE, goal["id"])["streak"], 0)
        self.assertEqual(read_snapshot(self.store.folder)["counts"]["goal_attempts"], 0)
        self.config["daily_model_calls"] = 0
        with self.assertRaises(StateError):
            self.step(goal)
        self.assertEqual(self.policy.profile(SCOPE, goal["id"])["streak"], 0)

    def test_manual_shelving_stays_manual_after_review_date_and_explicit_resume_resets(self):
        goal = self.goal()
        self.source(goal)
        self.review(goal, "shelve")
        self.now += 172800
        self.source(goal, "新资料")
        self.assertEqual(self.policy.candidates(SCOPE, self.now), [])
        current = self.store.get_goal(SCOPE, goal["id"])
        self.store.apply_chat_event(SCOPE, "1001", "resume", goal["id"], "继续", action="resume",
                                    expected_version=current["version"])
        self.assertEqual(self.policy.candidates(SCOPE, self.now)[0]["id"], goal["id"])

    def test_completion_requires_owner_criteria_and_current_note_evidence(self):
        goal = self.goal()
        source = self.source(goal)
        payload = {"criteria": goal["completion_criteria"], "source_ids": [source["id"]],
                   "reason": "主人核对已满足条件"}
        with self.assertRaises(StateError):
            self.review(goal, "complete", payload)
        self.step(goal)
        with self.assertRaises(StateError):
            self.review(goal, "complete", dict(payload, criteria="模型说已完成"))
        result = self.review(goal, "complete", payload, event_id="complete")
        self.assertEqual(result["status"], "completed")
        self.assertTrue(self.review(goal, "complete", payload, event_id="complete")["duplicate"])
        self.store.apply_chat_event(SCOPE, "1001", "revised", goal["id"], "纠正资料",
            action="correct", episode_id=source["id"], next_step="原资料不成立")
        self.assertEqual(self.store.get_goal(SCOPE, goal["id"])["status"], "paused")
        with self.assertRaises(StateError):
            self.review(goal, "complete", payload)

    def test_review_conflicts_and_audit_failure_roll_back_lifecycle(self):
        goal = self.goal()
        self.review(goal, "shelve", event_id="same")
        with self.assertRaises(ConflictError):
            self.review(goal, "abandon", event_id="same")
        self.store.db.execute("""CREATE TRIGGER fail_review BEFORE INSERT ON goal_reviews
            BEGIN SELECT RAISE(ABORT,'audit failed'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.review(goal, "abandon")
        self.assertEqual(self.store.get_goal(SCOPE, goal["id"])["status"], "shelved")

    def test_any_source_revision_invalidates_completion_and_preference_export_is_attributed(self):
        goal = self.goal()
        source = self.source(goal)
        self.step(goal)
        self.review(goal, "complete", {"criteria": goal["completion_criteria"],
            "source_ids": [source["id"]], "reason": "主人已核验"})
        self.store.record_episode(SCOPE, goal["id"], "revision", kind="owner_correction",
            evidence={"text": "原依据有误"}, supersedes_id=source["id"])
        current = self.store.get_goal(SCOPE, goal["id"])
        self.assertEqual((current["status"], current["checkpoint"]["resume_policy"]),
                         ("paused", "manual"))
        self.review(goal, "prefer", {"stance": 1, "reason": "我明确喜欢这个主题"})
        episode = self.store.db.execute("SELECT id FROM episodes WHERE kind='owner_interest'")
        queued = self.store.queue_memory(SCOPE, episode.fetchone()[0])
        self.assertIn("主人明确表达", queued["body"])

    def test_reopen_and_evidence_survive_restarting_the_store(self):
        goal = self.goal()
        self.source(goal)
        self.step(goal)
        self.review(goal, "abandon", {"reason": "证据保留，暂不继续"})
        self.review(goal, "reopen", {"reason": "主人明确重新开启"})
        self.store.close()
        reopened = LifeStore(self.store.folder)
        try:
            self.assertEqual(reopened.get_goal(SCOPE, goal["id"])["status"], "ready")
            self.assertEqual(GoalPolicy(reopened).interests(SCOPE, goal["id"], self.now)
                             ["agent"]["sources"], 1)
            self.assertEqual(read_snapshot(reopened.folder)["counts"]["notes"], 1)
        finally:
            reopened.close()

    def test_note_attempt_and_interest_roll_back_as_one_transaction(self):
        goal = self.goal()
        self.source(goal)
        self.store.db.execute("""CREATE TRIGGER fail_interest BEFORE INSERT ON interest_evidence
            BEGIN SELECT RAISE(ABORT,'full'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.step(goal)
        counts = read_snapshot(self.store.folder)["counts"]
        self.assertEqual((counts["notes"], counts["goal_attempts"], counts["interest_evidence"]),
                         (0, 0, 0))
        self.assertEqual(self.policy.profile(SCOPE, goal["id"])["streak"], 0)


class SelectionEngineTests(Fixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.setup_store()
        self.goal1, self.goal2 = self.goal("第一题"), self.goal("第二题")
        self.source(self.goal1, "资料一")
        self.source(self.goal2, "资料二")

        async def choose(context, event, prompt, before, after, config, stop, on_runner):
            identity = before()
            after(identity, {"output": 3})
            return {"goal_id": self.goal2["id"], "reason": "有明确的资料边界与下一步"}

        async def research(context, event, prompt, before, after, config, stop, on_runner):
            identity = before()
            after(identity, {"output": 6})
            data = json.loads(prompt)
            return {"note": "资料想法", "questions": "未知边界", "next_step": "补充来源",
                    "source_ids": [data["sources"][0]["id"]], "interest": 1}

        self.engine = ActivityEngine(self.store, object(), self.config, lambda: None,
            agent=research, selector=choose, clock=lambda: self.now)
        self.addAsyncCleanup(self.engine.close)

    async def test_model_selects_within_candidates_and_both_calls_share_one_run_budget(self):
        # Force reservation of the other candidate to exercise atomic retargeting.
        original = self.policy.candidates(SCOPE, self.now)
        original.sort(key=lambda row: row["id"] != self.goal1["id"])
        with patch.object(self.engine.ledger.goals, "candidates", return_value=original):
            self.engine.start(FakeEvent(), None, "pick")
        self.assertEqual((await self.engine.task)["status"], "completed")
        snapshot = read_snapshot(self.store.folder, scope=SCOPE)
        self.assertEqual(snapshot["runs"][0]["goal_id"], self.goal2["id"])
        self.assertEqual(snapshot["counts"]["activity_calls"], 2)
        self.assertEqual(snapshot["notes"][0]["goal_id"], self.goal2["id"])
        self.assertEqual(self.store.get_goal(SCOPE, self.goal1["id"])["status"], "ready")
        self.assertEqual(snapshot["goal_choices"][0]["reason"], "有明确的资料边界与下一步")
        self.assertTrue(self.engine.start(FakeEvent(), None, "pick")["duplicate"])
        self.assertEqual(read_snapshot(self.store.folder)["counts"]["activity_calls"], 2)

    async def test_no_candidates_wait_without_creating_goals_or_calling_models(self):
        for goal in (self.goal1, self.goal2):
            self.review(goal, "shelve")
        self.engine.selector = AsyncMock()
        self.assertEqual(self.engine.start(FakeEvent(), None, "empty")["status"], "waiting")
        self.engine.selector.assert_not_called()
        self.assertEqual(read_snapshot(self.store.folder)["counts"]["runs"], 0)
        self.assertTrue(self.engine.start(FakeEvent(), None, "empty")["duplicate"])

    async def test_model_wait_and_outside_candidate_are_bounded_and_create_no_attempt(self):
        for identity in ("", "not-a-candidate"):
            async def choose(context, event, prompt, before, *args):
                before()
                return {"goal_id": identity, "reason": "等待更多资料"}
            self.engine.selector = choose
            self.engine.start(FakeEvent(), None, "pick:" + identity)
            result = await self.engine.task
            self.assertEqual(result["status"], "waiting" if not identity else "interrupted")
        counts = read_snapshot(self.store.folder)["counts"]
        self.assertEqual((counts["notes"], counts["goal_attempts"], counts["activity_calls"]),
                         (0, 0, 2))

    async def test_choice_with_insufficient_budget_never_reserves_a_run(self):
        self.config["max_model_calls_per_run"] = 1
        with self.assertRaisesRegex(StateError, "两次"):
            self.engine.start(FakeEvent(), None, "pick")
        self.assertEqual(read_snapshot(self.store.folder)["counts"]["runs"], 0)

    async def test_trigger_cannot_be_reused_for_a_different_goal_or_selection_mode(self):
        self.engine.start(FakeEvent(), self.goal1["id"], "same")
        await self.engine.task
        with self.assertRaises(ConflictError):
            self.engine.start(FakeEvent(), self.goal2["id"], "same")
        with self.assertRaises(ConflictError):
            self.engine.start(FakeEvent(), None, "same")
        self.assertEqual(read_snapshot(self.store.folder)["counts"]["activity_calls"], 1)

    async def test_crash_before_choice_record_never_replays_the_same_trigger(self):
        self.ledger.begin(SCOPE, self.goal1["id"], "crashed", self.config, selection=True)
        self.engine.selector = AsyncMock()
        result = self.engine.start(FakeEvent(), None, "crashed")
        self.assertTrue(result["duplicate"])
        self.engine.selector.assert_not_called()
        self.assertEqual(self.store.get_goal(SCOPE, self.goal1["id"])["checkpoint"]
                         ["resume_policy"], "manual")

    async def test_choice_audit_failure_rolls_back_retargeting_before_research(self):
        original = self.policy.candidates(SCOPE, self.now)
        original.sort(key=lambda row: row["id"] != self.goal1["id"])
        self.store.db.execute("""CREATE TRIGGER fail_choice BEFORE INSERT ON goal_choices
            BEGIN SELECT RAISE(ABORT,'full'); END""")
        with patch.object(self.engine.ledger.goals, "candidates", return_value=original):
            self.engine.start(FakeEvent(), None, "pick")
        await self.engine.task
        snapshot = read_snapshot(self.store.folder, scope=SCOPE)
        self.assertEqual(snapshot["runs"][0]["goal_id"], self.goal1["id"])
        self.assertEqual(self.store.get_goal(SCOPE, self.goal2["id"])["status"], "ready")
        self.assertEqual(snapshot["counts"]["notes"], 0)
        self.assertEqual(snapshot["counts"]["activity_calls"], 1)

    async def test_goal_revision_during_selection_prevents_stale_choice(self):
        async def choose(context, event, prompt, before, *args):
            before()
            goal = self.store.get_goal(SCOPE, self.goal2["id"])
            if goal["status"] == "running":
                self.store.db.execute("UPDATE goals SET version=version+1 WHERE id=?",
                                      (goal["id"],))
            else:
                self.store.save_checkpoint(SCOPE, goal["id"], goal["version"],
                    next_step="主人新方向", checkpoint={"resume_policy": "manual"})
            return {"goal_id": goal["id"], "reason": "旧选择"}
        self.engine.selector = choose
        self.engine.start(FakeEvent(), None, "pick")
        self.assertEqual((await self.engine.task)["status"], "interrupted")
        self.assertEqual(read_snapshot(self.store.folder)["counts"]["notes"], 0)

    async def test_foreground_stop_during_selection_does_not_count_as_lost_interest(self):
        started = asyncio.Event()
        async def choose(context, event, prompt, before, *args):
            before()
            started.set()
            await asyncio.Event().wait()
        self.engine.selector = choose
        self.engine.start(FakeEvent(), None, "pick")
        await started.wait()
        self.assertTrue(await self.engine.stop("foreground"))
        self.assertEqual(read_snapshot(self.store.folder)["counts"]["goal_attempts"], 0)
        self.assertEqual(self.policy.profile(SCOPE, self.goal1["id"])["streak"], 0)


class SelectionMigrationTests(unittest.TestCase):
    def test_v3_readonly_and_upgrade_preserve_budget_and_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            store = LifeStore(folder)
            goal = store.create_goal(SCOPE, "old", "旧目标", "理由", "条件", "下一步")
            store.record_episode(SCOPE, goal["id"], "source", kind="source",
                                 evidence={"text": "旧资料"})
            ledger = ActivityLedger(store)
            job = ledger.begin(SCOPE, goal["id"], "old-run", {"chat_cooldown_seconds": 0})
            ledger.charge_model(SCOPE, job["run_id"], {})
            reserved = ledger.snapshot(SCOPE, {})["reserved_or_used_seconds"]
            remove_selection_schema(store)
            store.db.execute("PRAGMA user_version=3")
            store.db.execute("INSERT INTO activity_control VALUES (?,1,'manual',1)", (SCOPE,))
            store.close()
            self.assertEqual(read_snapshot(folder)["schema_version"], 3)
            upgraded = LifeStore(folder)
            try:
                self.assertEqual(read_snapshot(folder)["schema_version"], SCHEMA_VERSION)
                self.assertEqual(GoalPolicy(upgraded).profile(SCOPE, goal["id"])["streak"], 0)
                self.assertTrue(ActivityLedger(upgraded).control(SCOPE)["paused"])
                self.assertEqual(read_snapshot(folder)["counts"]["activity_calls"], 1)
                self.assertEqual(ActivityLedger(upgraded).snapshot(SCOPE, {})
                                 ["reserved_or_used_seconds"], reserved)
                self.assertEqual(upgraded.get_goal(SCOPE, goal["id"])["status"], "running")
                with closing(sqlite3.connect(upgraded.schema_backup)) as backup:
                    self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 3)
            finally:
                upgraded.close()

    def test_failed_v3_migration_rolls_back_without_changing_existing_goals(self):
        with tempfile.TemporaryDirectory() as folder:
            store = LifeStore(folder)
            store.create_goal(SCOPE, "old", "旧目标", "理由", "条件", "下一步")
            remove_selection_schema(store)
            store.db.execute("PRAGMA user_version=3")
            store.close()
            def fail(instance):
                instance.db.execute("CREATE TABLE partial(value TEXT)")
                raise RuntimeError("migration failed")
            with patch.object(LifeStore, "_create_schema", fail), self.assertRaises(RuntimeError):
                LifeStore(folder)
            snapshot = read_snapshot(folder)
            self.assertEqual((snapshot["schema_version"], snapshot["counts"]["goals"]), (3, 1))


class SelectionPluginTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_direct_owner_can_review_and_read_interests(self):
        with tempfile.TemporaryDirectory() as folder:
            module = load_plugin(Path(folder))
            plugin = module.LifeContinuityPlugin(object(), {"enabled": True, "owner_scope": SCOPE})
            await plugin.initialize()
            try:
                goal = plugin.store.create_goal(SCOPE, "goal", "主题", "理由", "条件", "下一步")
                text = f"/life prefer {goal['id']} | 喜欢 | 我喜欢研究这个主题"
                await plugin.owner_message(FakeEvent(text, scope=OTHER, sender="1002"))
                self.assertEqual(read_snapshot(folder)["counts"]["interest_evidence"], 0)
                await plugin.owner_message(FakeEvent(text))
                self.assertEqual(read_snapshot(folder)["counts"]["interest_evidence"], 1)
                read = FakeEvent(f"/life interests {goal['id']}", event_id="read")
                await plugin.owner_message(read)
                self.assertIn('"owner"', read.send.call_args.args[0])
                self.assertEqual(parse_command('/life pick'), {"action": "pick"})
            finally:
                await plugin.terminate()


if __name__ == "__main__":
    unittest.main()

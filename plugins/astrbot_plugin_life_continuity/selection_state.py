"""Evidence-based candidates and explicit lifecycle decisions; no background scheduler."""

import hashlib
import json
import unicodedata
import uuid

from .state import ConflictError, StateError, _json, _row, _text

TERMINAL = {"completed", "cancelled", "abandoned"}
SELECTION_SCHEMA = (
    """CREATE TABLE goal_profiles (
        scope TEXT NOT NULL, goal_id TEXT NOT NULL, topic_key TEXT NOT NULL,
        streak INTEGER NOT NULL DEFAULT 0, progress_at REAL NOT NULL DEFAULT 0,
        review_at REAL NOT NULL DEFAULT 0, condition TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL DEFAULT '', PRIMARY KEY(scope,goal_id),
        FOREIGN KEY(scope,goal_id) REFERENCES goals(scope,id))""",
    "CREATE INDEX goal_topics ON goal_profiles(scope,topic_key)",
    """CREATE TABLE goal_attempts (
        scope TEXT NOT NULL, run_id TEXT NOT NULL, goal_id TEXT NOT NULL,
        fingerprints_json TEXT NOT NULL, progress INTEGER NOT NULL, created REAL NOT NULL,
        PRIMARY KEY(scope,run_id), FOREIGN KEY(scope,run_id) REFERENCES runs(scope,id),
        FOREIGN KEY(scope,goal_id) REFERENCES goals(scope,id))""",
    """CREATE TABLE interest_evidence (
        id TEXT PRIMARY KEY, scope TEXT NOT NULL, goal_id TEXT NOT NULL,
        subject TEXT NOT NULL CHECK(subject IN ('agent','owner')),
        stance INTEGER NOT NULL CHECK(stance IN (-1,1)), episode_id TEXT NOT NULL,
        fingerprint TEXT NOT NULL, created REAL NOT NULL,
        UNIQUE(scope,goal_id,subject,fingerprint),
        FOREIGN KEY(scope,goal_id) REFERENCES goals(scope,id),
        FOREIGN KEY(scope,episode_id) REFERENCES episodes(scope,id))""",
    """CREATE TABLE goal_choices (
        scope TEXT NOT NULL, request_key TEXT NOT NULL, goal_id TEXT, run_id TEXT,
        reason TEXT NOT NULL, candidates_json TEXT NOT NULL, created REAL NOT NULL,
        request_mode TEXT NOT NULL,
        PRIMARY KEY(scope,request_key), FOREIGN KEY(scope,goal_id) REFERENCES goals(scope,id),
        FOREIGN KEY(scope,run_id) REFERENCES runs(scope,id))""",
    """CREATE TABLE goal_reviews (
        scope TEXT NOT NULL, event_id TEXT NOT NULL, goal_id TEXT NOT NULL,
        actor_id TEXT NOT NULL, payload_json TEXT NOT NULL, result_json TEXT NOT NULL,
        created REAL NOT NULL, PRIMARY KEY(scope,event_id),
        FOREIGN KEY(scope,goal_id) REFERENCES goals(scope,id))""",
)
SELECTION_COLUMNS = {
    "goal_profiles": {"scope", "goal_id", "topic_key", "streak", "progress_at", "review_at",
                      "condition", "reason"},
    "goal_attempts": {"scope", "run_id", "goal_id", "fingerprints_json", "progress", "created"},
    "interest_evidence": {"id", "scope", "goal_id", "subject", "stance", "episode_id",
                          "fingerprint", "created"},
    "goal_choices": {"scope", "request_key", "goal_id", "run_id", "reason", "candidates_json",
                     "created", "request_mode"},
    "goal_reviews": {"scope", "event_id", "goal_id", "actor_id", "payload_json", "result_json",
                     "created"},
}


def topic_key(title):
    text = unicodedata.normalize("NFKC", title).casefold()
    # Preserve meaningful symbols: C, C++ and C# must remain different topics.
    return "".join(text.split()).rstrip("!?。.,，;；") or text.strip()


def fingerprint(text):
    text = " ".join(unicodedata.normalize("NFKC", text).split())
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def origin_kind(store, scope, row):
    original, visited = row, set()
    while original and original["kind"] == "owner_correction":
        if original["id"] in visited or len(visited) >= 64:
            return None
        visited.add(original["id"])
        original = store.get_episode(scope, original["supersedes_id"], current=False)
    return original["kind"] if original else None


def research_sources(store, scope, goal_id):
    rows = store.db.execute("""SELECT e.* FROM episodes e WHERE e.scope=? AND e.goal_id=?
        AND e.kind IN ('source','read','owner_correction') AND NOT EXISTS
        (SELECT 1 FROM episodes n WHERE n.supersedes_id=e.id)
        ORDER BY e.occurred_at DESC,e.id""", (scope, goal_id))
    result = []
    for raw in rows:
        row = _row(raw)
        if (origin_kind(store, scope, row) in {"source", "read"}
                and isinstance(row["evidence"].get("text"), str)
                and row["evidence"]["text"].strip()):
            result.append(row)
            if len(result) == 3:
                break
    return result


class GoalPolicy:
    def __init__(self, store):
        self.store, self.db = store, store.db

    def profile(self, scope, goal_id):
        row = self.db.execute("SELECT * FROM goal_profiles WHERE scope=? AND goal_id=?",
                              (scope, goal_id)).fetchone()
        if not row:
            raise StateError("目标选择记录不完整，请核对数据库版本。")
        return dict(row)

    def prepare_goal(self, scope, title):
        """Inside the goal writer's transaction: deduplicate before applying the cap."""
        old = self.db.execute("""SELECT goal_id FROM goal_profiles
            WHERE scope=? AND topic_key=? ORDER BY goal_id LIMIT 1""",
            (scope, topic_key(title))).fetchone()
        if old:
            return self.store.get_goal(scope, old[0])
        self.check_capacity(scope)
        return None

    def check_capacity(self, scope):
        count = self.db.execute("""SELECT COUNT(*) FROM goals WHERE scope=?
            AND status NOT IN ('completed','cancelled','abandoned')""", (scope,)).fetchone()[0]
        if count >= 3:
            raise StateError("未结束目标已达三个，请先结束已有目标；搁置目标也占名额。")

    def seen(self, scope, goal_id):
        rows = self.db.execute("""SELECT fingerprints_json FROM goal_attempts
            WHERE scope=? AND goal_id=?""", (scope, goal_id))
        return {value for row in rows for value in json.loads(row[0])}

    def eligible(self, goal, now):
        if not goal or not goal["next_step"].strip():
            return False
        sources = research_sources(self.store, goal["scope"], goal["id"])
        if not sources:
            return False
        if goal["status"] == "ready" or (goal["status"] == "paused"
                and goal["checkpoint"].get("resume_policy") == "read_only"):
            return True
        if (goal["status"] != "shelved"
                or goal["checkpoint"].get("resume_policy") != "new_source_after_review"):
            return False
        profile = self.profile(goal["scope"], goal["id"])
        new = {fingerprint(row["evidence"]["text"][:2000]) for row in sources}
        return (now >= profile["review_at"] and profile["condition"] == "new_source"
                and bool(new - self.seen(goal["scope"], goal["id"])))

    def interests(self, scope, goal_id, now):
        rows = self.db.execute("""SELECT * FROM interest_evidence
            WHERE scope=? AND goal_id=? ORDER BY created,id""", (scope, goal_id))
        result = {"agent": {"score": 0, "sources": 0}, "owner": {"score": 0, "sources": 0}}
        for row in rows:
            if not self.store.get_episode(scope, row["episode_id"]):
                continue
            target = result[row["subject"]]
            target["score"] += row["stance"] / (1 + max(0, now - row["created"]) / 2592000)
            target["sources"] += 1
        for value in result.values():
            value["score"] = round(value["score"], 3)
        return result

    def candidates(self, scope, now):
        goals = [_row(row) for row in self.db.execute("""SELECT * FROM goals WHERE scope=?
            AND status NOT IN ('completed','cancelled','abandoned')""", (scope,))]
        result = []
        for goal in goals:
            if not self.eligible(goal, now):
                continue
            profile = self.profile(scope, goal["id"])
            fresh = {fingerprint(row["evidence"]["text"][:2000])
                     for row in research_sources(self.store, scope, goal["id"])} - self.seen(
                         scope, goal["id"])
            interests = self.interests(scope, goal["id"], now)
            feedback = [{"episode_id": row["id"], "text": row["evidence"].get("text", "")[:400]}
                        for row in self.store.query_episodes(scope, goal_id=goal["id"], limit=20)
                        if origin_kind(self.store, scope, row) == "owner_feedback"
                        and isinstance(row["evidence"].get("text"), str)][:2]
            result.append({key: goal[key] for key in
                           ("id", "version", "title", "why", "next_step", "completion_criteria")}
                          | {"new_sources": len(fresh), "progress_at": profile["progress_at"],
                             "interests": interests, "owner_advice": feedback,
                             "rule_reason": "有效资料与明确下一步；"
                             + ("存在尚未读取的新来源" if fresh else "继续核对已有资料")})
        result.sort(key=lambda row: (-row["new_sources"], -row["progress_at"],
                                    -row["interests"]["agent"]["score"],
                                    -row["interests"]["owner"]["score"], row["id"]))
        return result[:3]

    def choice(self, scope, key):
        return _row(self.db.execute("SELECT * FROM goal_choices WHERE scope=? AND request_key=?",
                                    (scope, key)).fetchone())

    def record_choice(self, scope, key, goal_id, run_id, reason, candidates, now, *, mode="manual"):
        self.db.execute("INSERT INTO goal_choices VALUES (?,?,?,?,?,?,?,?)",
                        (scope, key, goal_id, run_id, _text(reason, "choice reason", 500),
                         _json(candidates, list), now, mode))

    def on_step(self, job, source_ids, interest, now):
        """Called in the note transaction, only after a real research call succeeded."""
        if not job.get("research_called"):
            return None
        scope, goal_id = job["scope"], job["goal_id"]
        sources = [row for row in job["sources"] if row["id"] in source_ids]
        keys = {fingerprint(row["evidence"]["text"][:2000]) for row in sources}
        progress = bool(keys - self.seen(scope, goal_id))
        profile = self.profile(scope, goal_id)
        streak = 0 if progress else profile["streak"] + 1
        self.db.execute("INSERT INTO goal_attempts VALUES (?,?,?,?,?,?)",
                        (scope, job["run_id"], goal_id, _json(sorted(keys), list),
                         int(progress), now))
        self.db.execute("""UPDATE goal_profiles SET streak=?,progress_at=?,reason='',
            review_at=0,condition='' WHERE scope=? AND goal_id=?""",
            (streak, now if progress else profile["progress_at"], scope, goal_id))
        if interest in {-1, 1} and type(interest) is int:
            for row in sources:
                self.db.execute("""INSERT INTO interest_evidence VALUES (?,?,?,'agent',?,?,?,?)
                    ON CONFLICT(scope,goal_id,subject,fingerprint) DO UPDATE SET
                    episode_id=excluded.episode_id,stance=excluded.stance WHERE EXISTS
                    (SELECT 1 FROM episodes n
                     WHERE n.supersedes_id=interest_evidence.episode_id)""",
                    (uuid.uuid4().hex, scope, goal_id, interest, row["id"],
                     fingerprint(row["evidence"]["text"][:2000]), now))
        goal = self.store.get_goal(scope, goal_id)
        if streak < 3:
            return {"status": "paused", "pause_reason": "step_completed",
                    "resume_policy": "read_only", "progress": progress, "streak": streak}
        user = goal["owner_kind"] == "user"
        self.db.execute("""UPDATE goal_profiles SET reason=?,review_at=?,condition=?
            WHERE scope=? AND goal_id=?""", ("连续三轮没有新来源推进",
            0 if user else now + 86400, "owner_guidance" if user else "new_source", scope, goal_id))
        return {"status": "paused" if user else "shelved", "pause_reason": "no_progress",
                "resume_policy": "manual" if user else "new_source_after_review",
                "progress": progress, "streak": streak}

    def review(self, scope, goal_id, actor_id, event_id, action, payload, expected_version, now):
        actor_id, event_id = _text(actor_id, "actor", 100), _text(event_id, "message", 250)
        if action not in {"shelve", "abandon", "complete", "reopen", "prefer"}:
            raise ValueError("invalid goal review")
        with self.store.transaction():
            old = _row(self.db.execute("SELECT * FROM goal_reviews WHERE scope=? AND event_id=?",
                                       (scope, event_id)).fetchone())
            data = {"action": action, **payload}
            if old:
                if (old["goal_id"], old["actor_id"], old["payload"]) != (goal_id, actor_id, data):
                    raise ConflictError("review message has different content")
                return dict(old["result"], duplicate=True)
            goal = self.store.get_goal(scope, goal_id)
            if not goal or goal["version"] != expected_version:
                raise ConflictError("goal changed before review")
            if goal["status"] in {"running", "blocked", "waiting_approval"}:
                raise StateError("目标仍在运行或待核实，不能评议。")
            reason = _text(payload.get("reason"), "reason", 500)
            checkpoint, status = dict(goal["checkpoint"]), goal["status"]
            if action == "prefer":
                if type(payload.get("stance")) is not int or payload["stance"] not in {-1, 1}:
                    raise ValueError("invalid preference")
                identity = uuid.uuid4().hex
                evidence = {"source_type": "owner_message", "actor_id": actor_id,
                            "message_id": event_id, "text": reason, "stance": payload["stance"]}
                self.db.execute("""INSERT INTO episodes (id,scope,goal_id,source_id,kind,
                    occurred_at,evidence_json,interpretation_json) VALUES (?,?,?,?,'owner_interest',
                    ?,?,'{}')""", (identity, scope, goal_id, "preference:" + event_id, now,
                                  _json(evidence)))
                self.db.execute("""INSERT INTO interest_evidence VALUES (?,?,?,'owner',?,?,?,?)
                    ON CONFLICT(scope,goal_id,subject,fingerprint) DO UPDATE SET
                    episode_id=excluded.episode_id WHERE EXISTS
                    (SELECT 1 FROM episodes n
                     WHERE n.supersedes_id=interest_evidence.episode_id)""",
                    (uuid.uuid4().hex, scope, goal_id, payload["stance"], identity,
                     fingerprint(str(payload["stance"]) + ":" + reason), now))
            else:
                if action == "reopen":
                    if status not in TERMINAL:
                        raise StateError("仅已结束目标需要明确重开；搁置或暂停请用 resume。")
                    self.check_capacity(scope)
                    status = "ready"
                    checkpoint.update(resume_policy="eligible", pause_reason="owner_reopened")
                elif status in TERMINAL:
                    raise StateError("目标已结束，不重复结束或搁置；需要时请明确重开。")
                elif action == "shelve":
                    status = "shelved"
                    checkpoint.update(resume_policy="manual", pause_reason="owner_shelved")
                elif action == "abandon":
                    status = "abandoned"
                    checkpoint.update(resume_policy="never", pause_reason="owner_abandoned")
                elif action == "complete":
                    note = self._verify_completion(goal, payload)
                    status = "completed"
                    checkpoint.update(resume_policy="never", pause_reason="owner_verified",
                                      completion_evidence=note["evidence_ids"],
                                      completion_note_version=note["version"])
                checkpoint["decision_reason"] = reason
                self.db.execute("""UPDATE goals SET status=?,checkpoint_json=?,
                    version=version+1,updated_at=? WHERE scope=? AND id=?""",
                    (status, _json(checkpoint), now, scope, goal_id))
                self.db.execute("""UPDATE goal_profiles SET streak=0,review_at=?,reason=?,
                    condition=? WHERE scope=? AND goal_id=?""",
                    (now + 86400 if action == "shelve" else 0, reason,
                     "owner_resume" if action == "shelve" else "", scope, goal_id))
            result = {"action": action, "goal_id": goal_id, "status": status, "reason": reason}
            self.db.execute("INSERT INTO goal_reviews VALUES (?,?,?,?,?,?,?)",
                            (scope, event_id, goal_id, actor_id, _json(data), _json(result), now))
            return result

    def _verify_completion(self, goal, payload):
        if payload.get("criteria") != goal["completion_criteria"]:
            raise StateError("请明确确认目标当前的结束条件原文，不能只凭模型自报完成。")
        ids = payload.get("source_ids")
        note = _row(self.db.execute("""SELECT * FROM notes WHERE scope=? AND goal_id=?
            ORDER BY version DESC LIMIT 1""", (goal["scope"], goal["id"])).fetchone())
        if (not isinstance(ids, list) or not ids or not note
                or not set(ids).issubset(note["evidence_ids"])):
            raise StateError("完成需引用最近笔记中的有效经历标识。")
        for identity in note["evidence_ids"]:
            row = self.store.get_episode(goal["scope"], identity)
            if not row or row["goal_id"] != goal["id"]:
                raise StateError("笔记来源已失效，请先重新核对。")
        return note

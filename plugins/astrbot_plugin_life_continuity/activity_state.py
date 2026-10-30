"""Transactional reservations and recovery for bounded, read-only research rounds."""

import hashlib
import time
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .state import ConflictError, StateError, _json, _row, _text
from .selection_state import GoalPolicy, research_sources

try:
    SHANGHAI = ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError:
    # Windows source checks need no installed packages. Contemporary Shanghai
    # activity uses UTC+8; installed AstrBot uses the pinned IANA tzdata above.
    SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")

ACTIVITY_SCHEMA = (
    """CREATE TABLE activity_control (
        scope TEXT PRIMARY KEY, paused INTEGER NOT NULL DEFAULT 0,
        reason TEXT NOT NULL DEFAULT '', last_chat REAL NOT NULL DEFAULT 0)""",
    """CREATE TABLE activity_sessions (
        scope TEXT NOT NULL, run_id TEXT NOT NULL, started REAL NOT NULL,
        deadline REAL NOT NULL, finished REAL, limits_json TEXT NOT NULL,
        PRIMARY KEY(scope,run_id), FOREIGN KEY(scope,run_id) REFERENCES runs(scope,id))""",
    """CREATE TABLE activity_time (
        scope TEXT NOT NULL, run_id TEXT NOT NULL, day TEXT NOT NULL,
        seconds REAL NOT NULL CHECK(seconds>=0), PRIMARY KEY(scope,run_id,day),
        FOREIGN KEY(scope,run_id) REFERENCES activity_sessions(scope,run_id))""",
    """CREATE TABLE activity_calls (
        id TEXT PRIMARY KEY, scope TEXT NOT NULL, run_id TEXT NOT NULL, day TEXT NOT NULL,
        status TEXT NOT NULL, usage_json TEXT NOT NULL DEFAULT '{}', created REAL NOT NULL,
        FOREIGN KEY(scope,run_id) REFERENCES activity_sessions(scope,run_id))""",
    """CREATE TABLE life_deliveries (
        id TEXT PRIMARY KEY, scope TEXT NOT NULL, day TEXT NOT NULL, source TEXT NOT NULL,
        request_key TEXT NOT NULL, fingerprint TEXT NOT NULL, status TEXT NOT NULL,
        receipt_json TEXT NOT NULL DEFAULT '{}', created REAL NOT NULL, updated REAL NOT NULL,
        UNIQUE(scope,request_key), UNIQUE(scope,day,fingerprint))""",
)
ACTIVITY_COLUMNS = {
    "activity_control": {"scope", "paused", "reason", "last_chat"},
    "activity_sessions": {"scope", "run_id", "started", "deadline", "finished", "limits_json"},
    "activity_time": {"scope", "run_id", "day", "seconds"},
    "activity_calls": {"id", "scope", "run_id", "day", "status", "usage_json", "created"},
    "life_deliveries": {"id", "scope", "day", "source", "request_key", "fingerprint", "status",
                        "receipt_json", "created", "updated"},
}
DEFAULTS = {"daily_minutes": 10, "max_run_seconds": 300, "max_model_calls_per_run": 10,
            "daily_model_calls": 20, "daily_shares": 1, "model_timeout_seconds": 60,
            "stop_timeout_seconds": 5, "chat_cooldown_seconds": 300}


class BudgetExceeded(StateError):
    """No further background work can be reserved."""


def limits(config):
    if type(config.get("trial_mode", False)) is not bool:
        raise ValueError("invalid trial mode")
    result = {}
    for key, default in DEFAULTS.items():
        value = config.get(key, default)
        minimum = 1 if key in {"max_run_seconds", "model_timeout_seconds",
                               "stop_timeout_seconds", "max_model_calls_per_run"} else 0
        if type(value) is not int or not minimum <= value <= 3600:
            raise ValueError(f"invalid activity limit: {key}")
        result[key] = value
    if config.get("trial_mode") is True:
        for key, maximum in DEFAULTS.items():
            if key == "chat_cooldown_seconds":
                result[key] = max(result[key], maximum)
            else:
                result[key] = min(result[key], maximum)
    return result


def day_at(now):
    local = datetime.fromtimestamp(now, SHANGHAI)
    midnight = datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), SHANGHAI)
    return local.date().isoformat(), midnight.timestamp()


def time_chunks(start, end):
    while start < end:
        day, midnight = day_at(start)
        stop = min(end, midnight)
        yield day, stop - start
        start = stop


class ActivityLedger:
    def __init__(self, store, clock=time.time):
        self.store, self.db, self.clock = store, store.db, clock
        self.goals = GoalPolicy(store)

    def control(self, scope):
        row = self.db.execute("SELECT * FROM activity_control WHERE scope=?", (scope,)).fetchone()
        return dict(row) if row else {"paused": False, "reason": "", "last_chat": 0}

    def control_update(self, scope, *, paused=None, reason="", chat=False):
        with self.store.transaction():
            self.db.execute("INSERT OR IGNORE INTO activity_control(scope) VALUES (?)", (scope,))
            if paused is not None:
                self.db.execute("UPDATE activity_control SET paused=?,reason=? WHERE scope=?",
                                (int(paused), reason, scope))
            if chat:
                self.db.execute("UPDATE activity_control SET last_chat=? WHERE scope=?",
                                (self.clock(), scope))

    def snapshot(self, scope, config):
        cfg, now = limits(config), self.clock()
        day = day_at(now)[0]
        used = self.db.execute("""SELECT COALESCE(SUM(seconds),0) FROM activity_time
            WHERE scope=? AND day=?""", (scope, day)).fetchone()[0]
        calls = self.db.execute("SELECT COUNT(*) FROM activity_calls WHERE scope=? AND day=?",
                                (scope, day)).fetchone()[0]
        shares = self.db.execute("SELECT COUNT(*) FROM life_deliveries WHERE scope=? AND day=?",
                                 (scope, day)).fetchone()[0]
        midnight = day_at(now)[1]
        runs = self.db.execute("""SELECT COUNT(*) FROM activity_sessions
            WHERE scope=? AND started>=? AND started<?""",
            (scope, midnight - 86400, midnight)).fetchone()[0]
        return {"day": day, "reserved_or_used_seconds": used, "model_calls": calls,
                "shares": shares, "remaining_seconds": max(0, cfg["daily_minutes"] * 60 - used),
                "remaining_model_calls": max(0, cfg["daily_model_calls"] - calls),
                "remaining_shares": max(0, cfg["daily_shares"] - shares),
                "started_rounds": runs,
                "daily_round_limit": 1 if config.get("trial_mode") else None,
                "control": self.control(scope)}

    def sources(self, scope, goal_id):
        return research_sources(self.store, scope, goal_id)

    def begin(self, scope, goal_id, source_id, config, *, selection=False):
        cfg, now = limits(config), self.clock()
        with self.store.transaction():
            old = self.db.execute("SELECT * FROM runs WHERE scope=? AND source_id=?",
                                  (scope, source_id)).fetchone()
            if old:
                if old["goal_id"] != goal_id:
                    raise ConflictError("trigger belongs to another goal")
                return {"duplicate": True, "run_id": old["id"], "status": old["status"]}
            control = self.control(scope)
            if control["paused"] or now - control["last_chat"] < cfg["chat_cooldown_seconds"]:
                raise StateError("后台已暂停或仍在聊天冷却期。")
            if config.get("trial_mode") is True and self.snapshot(scope, config)["started_rounds"]:
                raise BudgetExceeded("试运行当天已开始过一轮，失败、中断和重启都不退还该名额。")
            goal = self.store.get_goal(scope, goal_id)
            if not goal or (goal["status"] not in {"ready", "paused", "shelved"}):
                raise StateError("目标尚不可运行；手动暂停需明确继续，待核实状态需先恢复核对。")
            sources = self.sources(scope, goal_id)
            if not sources:
                raise StateError("没有可读取的资料，请先提供该目标的文字来源。")
            if not self.goals.eligible(goal, now):
                raise StateError("目标已暂停或搁置，尚不满足重新开启条件。")
            if self.snapshot(scope, config)["remaining_model_calls"] <= 0:
                raise BudgetExceeded("当天模型调用额度已耗尽。")
            cursor, chunks = now, []
            duration = min(cfg["max_run_seconds"], cfg["model_timeout_seconds"])
            for day, wanted in time_chunks(now, now + duration):
                used = self.db.execute("""SELECT COALESCE(SUM(seconds),0) FROM activity_time
                    WHERE scope=? AND day=?""", (scope, day)).fetchone()[0]
                granted = min(wanted, max(0, cfg["daily_minutes"] * 60 - used))
                if granted:
                    chunks.append((day, granted))
                    cursor += granted
                if granted < wanted:
                    break
            if cursor <= now:
                raise BudgetExceeded("当天活动时间已耗尽。")
            run_id = uuid.uuid4().hex
            self.db.execute("""INSERT INTO runs
                (id,scope,goal_id,source_id,status,started_at) VALUES (?,?,?,?,'running',?)""",
                (run_id, scope, goal_id, source_id, now))
            self.db.execute("""INSERT INTO activity_sessions
                (scope,run_id,started,deadline,limits_json) VALUES (?,?,?,?,?)""",
                (scope, run_id, now, cursor, _json(cfg)))
            self.db.executemany("INSERT INTO activity_time VALUES (?,?,?,?)",
                                [(scope, run_id, day, seconds) for day, seconds in chunks])
            self.db.execute("""UPDATE goals SET status='running',version=version+1,updated_at=?
                WHERE scope=? AND id=?""", (now, scope, goal_id))
            if not selection:
                self.goals.record_choice(scope, source_id, goal_id, run_id, "主人明确指定目标",
                                         [{"id": goal_id, "version": goal["version"]}], now)
            return {"run_id": run_id, "scope": scope, "goal_id": goal_id,
                    "version": goal["version"] + 1, "duration": cursor - now, "started": now,
                    "goal": goal, "sources": sources, "limits": cfg, "duplicate": False,
                    "trigger": source_id}

    def select(self, job, candidates, decision):
        identity = decision.get("goal_id")
        reason = _text(decision.get("reason"), "choice reason", 500)
        ids = {row["id"]: row for row in candidates}
        if not isinstance(identity, str) or (identity and identity not in ids):
            raise StateError("模型选择了候选外目标，未执行。")
        scope, now = job["scope"], self.clock()
        updated = {}
        with self.store.transaction():
            anchor = self.store.get_goal(scope, job["goal_id"])
            if anchor["status"] != "running" or anchor["version"] != job["version"]:
                raise ConflictError("selection reservation changed")
            if identity and identity != anchor["id"]:
                goal = self.store.get_goal(scope, identity)
                if (not goal or goal["version"] != ids[identity]["version"]
                        or not self.goals.eligible(goal, now)):
                    raise ConflictError("selected goal changed")
                self.db.execute("""UPDATE goals SET status=?,version=version+1,updated_at=?
                    WHERE scope=? AND id=?""", (job["goal"]["status"], now, scope, anchor["id"]))
                self.db.execute("UPDATE runs SET goal_id=? WHERE scope=? AND id=?",
                                (identity, scope, job["run_id"]))
                self.db.execute("""UPDATE goals SET status='running',version=version+1,
                    updated_at=? WHERE scope=? AND id=?""", (now, scope, identity))
                updated = {"goal_id": identity, "goal": goal, "version": goal["version"] + 1,
                           "sources": self.sources(scope, identity)}
            elif identity:
                # A source correction during selection must not be silently replaced.
                if any(not self.store.get_episode(scope, row["id"]) for row in job["sources"]):
                    raise ConflictError("selection sources changed")
            self.goals.record_choice(scope, job["trigger"], identity or None, job["run_id"],
                                     reason, candidates, now, mode="selection")
        job.update(updated)
        return bool(identity)

    def charge_model(self, scope, run_id, config):
        cfg, now = limits(config), self.clock()
        with self.store.transaction():
            row = self.db.execute("""SELECT s.*,r.status FROM activity_sessions s JOIN runs r
                ON r.scope=s.scope AND r.id=s.run_id WHERE s.scope=? AND s.run_id=?""",
                (scope, run_id)).fetchone()
            if (not row or row["status"] != "running" or row["finished"] is not None
                    or not row["started"] <= now < row["deadline"]):
                raise BudgetExceeded("活动没有有效时间预留。")
            original = _row(row)["limits"]
            daily_limit = min(cfg["daily_model_calls"], original["daily_model_calls"])
            per_run = min(cfg["max_model_calls_per_run"], original["max_model_calls_per_run"])
            count = self.db.execute(
                "SELECT COUNT(*) FROM activity_calls WHERE scope=? AND run_id=?",
                (scope, run_id),
            ).fetchone()[0]
            if count >= per_run or self.snapshot(scope, config)["model_calls"] >= daily_limit:
                raise BudgetExceeded("模型调用额度已耗尽。")
            identity = uuid.uuid4().hex
            self.db.execute("""INSERT INTO activity_calls
                (id,scope,run_id,day,status,created) VALUES (?,?,?,?,'reserved',?)""",
                (identity, scope, run_id, day_at(now)[0], now))
            return identity

    def record_usage(self, identity, usage):
        with self.store.transaction():
            self.db.execute("UPDATE activity_calls SET status='returned',usage_json=? WHERE id=?",
                            (_json(usage), identity))

    def finish(self, job, reason, elapsed, *, note=None, source_ids=None, next_step=None,
               manual=False, interest=0):
        now, scope, run_id = self.clock(), job["scope"], job["run_id"]
        with self.store.transaction():
            run = self.db.execute("SELECT * FROM runs WHERE scope=? AND id=?",
                                  (scope, run_id)).fetchone()
            if not run or run["status"] != "running":
                raise ConflictError("run is no longer active")
            goal = self.store.get_goal(scope, job["goal_id"])
            if goal["version"] != job["version"] or goal["status"] != "running":
                raise ConflictError("goal changed during the activity")
            checkpoint = dict(goal["checkpoint"])
            assessment = None
            if note is not None:
                note = _text(note, "note", 12000)
                next_step = _text(next_step, "next step")
                allowed = {row["id"] for row in job["sources"]}
                if not source_ids or not set(source_ids).issubset(allowed):
                    raise StateError("result cites unknown sources")
                if any(not self.store.get_episode(scope, identity) for identity in source_ids):
                    raise ConflictError("a source was revised while the model was running")
                version = self.db.execute("""SELECT COALESCE(MAX(version),0)+1 FROM notes
                    WHERE scope=? AND goal_id=?""", (scope, goal["id"])).fetchone()[0]
                self.db.execute("INSERT INTO notes VALUES (?,?,?,?,?,?)",
                                (scope, goal["id"], version, note, _json(source_ids, list), now))
                checkpoint.update(note_version=version, source_ids=source_ids,
                                  read_ranges=job.get("read_ranges", {}))
                assessment = self.goals.on_step(job, source_ids, interest, now)
            checkpoint.update(last_run_id=run_id, pause_reason=reason,
                              resume_policy="manual" if manual else "read_only")
            status = "paused"
            if assessment:
                status = assessment.pop("status")
                checkpoint.update(assessment)
            self.db.execute("""UPDATE goals SET status=?,next_step=?,checkpoint_json=?,
                version=version+1,updated_at=? WHERE scope=? AND id=?""",
                (status, next_step if note is not None else goal["next_step"], _json(checkpoint),
                 now, scope, goal["id"]))
            self.db.execute("UPDATE runs SET status=?,ended_at=?,summary=? WHERE id=?", (
                "completed" if note is not None else "interrupted", now, reason, run_id,
            ))
            self.db.execute("""UPDATE activity_calls SET status='unknown'
                WHERE scope=? AND run_id=? AND status='reserved'""", (scope, run_id))
            return self.store.get_goal(scope, goal["id"])

    def settle_time(self, job, elapsed):
        scope, run_id, now = job["scope"], job["run_id"], self.clock()
        with self.store.transaction():
            end = job["started"] + min(job["duration"], max(0, elapsed))
            if now < job["started"]:
                end = job["started"] + job["duration"]
            charged = dict(time_chunks(job["started"], end))
            self.db.execute("UPDATE activity_sessions SET finished=? WHERE scope=? AND run_id=?",
                            (now, scope, run_id))
            for row in self.db.execute("SELECT day FROM activity_time WHERE scope=? AND run_id=?",
                                       (scope, run_id)).fetchall():
                self.db.execute(
                    "UPDATE activity_time SET seconds=? WHERE scope=? AND run_id=? AND day=?",
                    (charged.get(row["day"], 0), scope, run_id, row["day"]),
                )

    def block(self, job, reason):
        with self.store.transaction():
            self.db.execute("UPDATE runs SET status='blocked',summary=? WHERE scope=? AND id=?",
                            (reason, job["scope"], job["run_id"]))
            self.db.execute("""UPDATE goals SET status='blocked',version=version+1,updated_at=?
                WHERE scope=? AND id=? AND status IN ('running','paused')
                AND version IN (?,?)""",
                (self.clock(), job["scope"], job["goal_id"], job["version"], job["version"] + 1))

    def recover(self, scope):
        """Caller holds activity/delivery leases; no refund or replay after a crash."""
        with self.store.transaction():
            rows = self.db.execute("""SELECT r.* FROM runs r JOIN activity_sessions s
                ON s.scope=r.scope AND s.run_id=r.id
                WHERE r.scope=? AND r.status IN ('running','blocked')""", (scope,)).fetchall()
            for row in rows:
                goal = self.store.get_goal(scope, row["goal_id"])
                checkpoint = dict(goal["checkpoint"])
                checkpoint.update(pause_reason="restart", resume_policy="manual")
                self.db.execute("UPDATE runs SET status='interrupted',ended_at=? WHERE id=?",
                                (self.clock(), row["id"]))
                self.db.execute("""UPDATE goals SET status='paused',checkpoint_json=?,
                    version=version+1,updated_at=? WHERE scope=? AND id=?
                    AND status IN ('running','blocked')""",
                    (_json(checkpoint), self.clock(), scope, row["goal_id"]))
                self.db.execute(
                    "UPDATE activity_sessions SET finished=? WHERE scope=? AND run_id=?",
                    (self.clock(), scope, row["id"]),
                )
            self.db.execute("""UPDATE activity_calls SET status='unknown'
                WHERE scope=? AND status='reserved'""", (scope,))
            self.db.execute("""UPDATE life_deliveries SET status='uncertain',updated=?
                WHERE scope=? AND status='sending'""", (self.clock(), scope))
            return len(rows)

    def reserve_delivery(self, scope, source, key, text, config):
        limits(config)
        now = self.clock()
        fingerprint = hashlib.sha256(text.strip().encode("utf-8")).hexdigest()
        with self.store.transaction():
            old = self.db.execute("""SELECT * FROM life_deliveries
                WHERE scope=? AND (request_key=? OR (day=? AND fingerprint=?))""",
                (scope, key, day_at(now)[0], fingerprint)).fetchone()
            if old:
                return dict(_row(old), duplicate=True)
            if self.snapshot(scope, config)["remaining_shares"] <= 0:
                raise BudgetExceeded("当天主动分享额度已耗尽。")
            identity = uuid.uuid4().hex
            self.db.execute("""INSERT INTO life_deliveries
                (id,scope,day,source,request_key,fingerprint,status,created,updated)
                VALUES (?,?,?,?,?,?,'sending',?,?)""",
                (identity, scope, day_at(now)[0], source, key, fingerprint, now, now))
            return {"id": identity, "status": "sending", "duplicate": False}

    def settle_delivery(self, identity, *, confirmed=False, receipt=None):
        with self.store.transaction():
            self.db.execute("""UPDATE life_deliveries SET status=?,receipt_json=?,updated=?
                WHERE id=? AND status='sending'""", (
                "sent" if confirmed else "uncertain", _json(receipt or {}), self.clock(), identity,
            ))

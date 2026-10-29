"""Scoped, versioned state with explicit writes and side-effect-free queries.

Goal/episode concepts follow the MIT Dafeiyu reference's bili_activity/life.py.
This separate ledger has no dependency on Bili, AstrBot or its runtime database.
Opening a writer migrates schema only; interruption recovery belongs to stage 3.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

PLUGIN_NAME = "astrbot_plugin_life_continuity"
DATABASE_NAME = "life.sqlite3"
SCHEMA_VERSION = 1
APPLICATION_ID = 0x44464C31
GOAL_STATES = {"ready", "running", "paused", "waiting_approval", "blocked",
               "completed", "cancelled"}
TRANSITIONS = {
    "ready": {"ready", "paused", "blocked", "cancelled"},
    "paused": {"paused", "ready", "blocked", "cancelled"},
    "blocked": {"blocked", "paused", "ready", "cancelled"},
    "waiting_approval": {"waiting_approval", "paused", "blocked", "cancelled"},
    "completed": set(), "cancelled": set(),
}
SCHEMA = (
    """CREATE TABLE goals (
        id TEXT PRIMARY KEY, scope TEXT NOT NULL, source_id TEXT NOT NULL,
        source_fingerprint TEXT NOT NULL,
        owner_kind TEXT NOT NULL CHECK(owner_kind IN ('agent','user')),
        title TEXT NOT NULL, why TEXT NOT NULL, completion_criteria TEXT NOT NULL,
        next_step TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'ready',
        checkpoint_json TEXT NOT NULL DEFAULT '{}', version INTEGER NOT NULL DEFAULT 1,
        created_at REAL NOT NULL, updated_at REAL NOT NULL,
        UNIQUE(scope,source_id), UNIQUE(scope,id))""",
    "CREATE INDEX goals_scope_status ON goals(scope,status,updated_at)",
    """CREATE TABLE runs (
        id TEXT PRIMARY KEY, scope TEXT NOT NULL, goal_id TEXT NOT NULL,
        source_id TEXT NOT NULL, status TEXT NOT NULL, started_at REAL NOT NULL,
        ended_at REAL, summary TEXT NOT NULL DEFAULT '',
        UNIQUE(scope,source_id), UNIQUE(scope,id),
        FOREIGN KEY(scope,goal_id) REFERENCES goals(scope,id))""",
    "CREATE UNIQUE INDEX one_running_run ON runs(scope) WHERE status='running'",
    """CREATE TABLE episodes (
        id TEXT PRIMARY KEY, scope TEXT NOT NULL, goal_id TEXT NOT NULL,
        run_id TEXT, source_id TEXT NOT NULL, kind TEXT NOT NULL,
        occurred_at REAL NOT NULL, evidence_json TEXT NOT NULL,
        interpretation_json TEXT NOT NULL, supersedes_id TEXT,
        UNIQUE(scope,kind,source_id), UNIQUE(scope,id), UNIQUE(supersedes_id),
        FOREIGN KEY(scope,goal_id) REFERENCES goals(scope,id),
        FOREIGN KEY(scope,run_id) REFERENCES runs(scope,id),
        FOREIGN KEY(scope,supersedes_id) REFERENCES episodes(scope,id))""",
    "CREATE INDEX episodes_goal_time ON episodes(scope,goal_id,occurred_at)",
    """CREATE TABLE notes (
        scope TEXT NOT NULL, goal_id TEXT NOT NULL, version INTEGER NOT NULL,
        body TEXT NOT NULL, evidence_ids_json TEXT NOT NULL, created_at REAL NOT NULL,
        PRIMARY KEY(scope,goal_id,version),
        FOREIGN KEY(scope,goal_id) REFERENCES goals(scope,id))""",
)
# A declared version must not silently conceal an incomplete migration.
REQUIRED_COLUMNS = {
    "goals": {"id", "scope", "source_id", "source_fingerprint", "owner_kind", "title", "why",
              "completion_criteria", "next_step", "status", "checkpoint_json", "version",
              "created_at", "updated_at"},
    "runs": {"id", "scope", "goal_id", "source_id", "status", "started_at", "ended_at",
             "summary"},
    "episodes": {"id", "scope", "goal_id", "run_id", "source_id", "kind", "occurred_at",
                 "evidence_json", "interpretation_json", "supersedes_id"},
    "notes": {"scope", "goal_id", "version", "body", "evidence_ids_json", "created_at"},
}


class StateError(RuntimeError):
    """The ledger is incompatible or an update conflicts with durable state."""


class ConflictError(StateError):
    """An idempotency key or expected version no longer matches."""


def _text(value, name, maximum=1000, *, empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise ValueError(f"invalid {name}")
    return value.strip()


def _version(value):
    if type(value) is not int or value < 0:
        raise ValueError("invalid expected version")
    return value


def _json(value, expected=dict):
    if not isinstance(value, expected):
        raise ValueError("invalid structured state")
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    if len(encoded.encode("utf-8")) > 64 * 1024:
        raise ValueError("structured state exceeds 64 KiB")
    return encoded


def _row(row):
    if row is None:
        return None
    result = dict(row)
    for key in list(result):
        if key.endswith("_json"):
            result[key[:-5]] = json.loads(result.pop(key))
    return result


def _connect(path, *, readonly=False):
    target = path.resolve().as_uri() + "?mode=ro" if readonly else str(path)
    db = sqlite3.connect(target, uri=readonly, isolation_level=None, timeout=5)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        if readonly:
            db.execute("PRAGMA query_only=ON")
    except BaseException:
        db.close()
        raise
    return db


def _inspect_schema(db):
    version = db.execute("PRAGMA user_version").fetchone()[0]
    application = db.execute("PRAGMA application_id").fetchone()[0]
    if version > SCHEMA_VERSION or application not in {0, APPLICATION_ID}:
        raise StateError("unsupported life database; refusing to change its ledger")
    if version == 0:
        tables = db.execute("SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")
        if tables.fetchone():
            raise StateError("unversioned nonempty database is not a life ledger")
        return version
    if application != APPLICATION_ID:
        raise StateError("life database application ID is missing")
    for table, required in REQUIRED_COLUMNS.items():
        actual = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        if not required.issubset(actual):
            raise StateError(f"incomplete life database schema: {table}")
    indexes = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    if not {"goals_scope_status", "one_running_run", "episodes_goal_time"}.issubset(indexes):
        raise StateError("incomplete life database indexes")
    if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise StateError("life database integrity check failed")
    if db.execute("PRAGMA foreign_key_check").fetchone():
        raise StateError("life database contains broken references")
    return version


class LifeStore:
    """Explicit writer; closing/reopening never replays or recovers an activity."""

    def __init__(self, folder):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / DATABASE_NAME
        existed = self.path.exists()
        self.db = _connect(self.path)
        self.schema_backup = None
        try:
            with self.transaction():
                version = _inspect_schema(self.db)
                if version < SCHEMA_VERSION:
                    # Reserve the writer before snapshotting through a separate reader.
                    # The online backup includes committed WAL, without backing up an
                    # active write transaction on the same connection (which would wait).
                    if existed:
                        self.schema_backup = self._backup_schema(version)
                    self._create_schema()
                    self.db.execute(f"PRAGMA application_id={APPLICATION_ID}")
                    self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                    _inspect_schema(self.db)
            self.db.execute("PRAGMA journal_mode=WAL")
        except BaseException:
            self.db.close()
            raise

    def _create_schema(self):
        for statement in SCHEMA:
            self.db.execute(statement)

    def _backup_schema(self, version):
        folder = self.folder / "schema_backups"
        folder.mkdir(exist_ok=True)
        path = folder / f"life-v{version}-{uuid.uuid4().hex}.sqlite3"
        try:
            with closing(_connect(self.path, readonly=True)) as source:
                with closing(sqlite3.connect(path)) as destination:
                    source.backup(destination)
                    if destination.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise StateError("schema backup integrity check failed")
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return path

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    def close(self):
        self.db.close()

    def get_goal(self, scope, goal_id):
        return _row(self.db.execute(
            "SELECT * FROM goals WHERE scope=? AND id=?", (scope, goal_id)
        ).fetchone())

    def create_goal(self, scope, source_id, title, why, completion_criteria, next_step,
                    *, owner_kind="agent"):
        values = {
            "scope": _text(scope, "scope", 250), "source_id": _text(source_id, "source", 250),
            "title": _text(title, "title", 160), "why": _text(why, "why"),
            "completion_criteria": _text(completion_criteria, "completion criteria"),
            "next_step": _text(next_step, "next step"), "owner_kind": owner_kind,
        }
        if owner_kind not in {"agent", "user"}:
            raise ValueError("invalid goal owner")
        fingerprint = hashlib.sha256(_json(values).encode("utf-8")).hexdigest()
        with self.transaction():
            old = self.db.execute("SELECT * FROM goals WHERE scope=? AND source_id=?",
                                  (values["scope"], values["source_id"])).fetchone()
            if old:
                if old["source_fingerprint"] != fingerprint:
                    raise ConflictError("goal source already has different content")
                return _row(old)
            identity, now = uuid.uuid4().hex, time.time()
            self.db.execute("""INSERT INTO goals
                (id,scope,source_id,title,why,completion_criteria,next_step,owner_kind,
                 source_fingerprint,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (identity, *(values[key] for key in ("scope", "source_id", "title", "why",
                 "completion_criteria", "next_step", "owner_kind")), fingerprint, now, now))
            return self.get_goal(values["scope"], identity)

    def save_checkpoint(self, scope, goal_id, expected_version, *, next_step, checkpoint,
                        status="paused"):
        """CAS update of an idle goal. Active execution must settle its run first."""
        _version(expected_version)
        next_step = _text(next_step, "next step", empty=True)
        checkpoint = _json(checkpoint)
        if status not in GOAL_STATES:
            raise ValueError("invalid goal status")
        with self.transaction():
            goal = self.get_goal(scope, goal_id)
            if not goal or goal["version"] != expected_version:
                raise ConflictError("goal is missing or its version has changed")
            if status not in TRANSITIONS.get(goal["status"], set()):
                raise StateError("goal transition requires execution or explicit recovery")
            self.db.execute("""UPDATE goals SET next_step=?,checkpoint_json=?,status=?,
                version=version+1,updated_at=? WHERE scope=? AND id=? AND version=?""",
                (next_step, checkpoint, status, time.time(), scope, goal_id, expected_version))
            return self.get_goal(scope, goal_id)

    def start_run(self, scope, goal_id, expected_version, source_id):
        """Record a run, without executing tools; source_id is the trigger's stable key."""
        _version(expected_version)
        source_id = _text(source_id, "run source", 250)
        with self.transaction():
            old = self.db.execute("SELECT * FROM runs WHERE scope=? AND source_id=?",
                                  (scope, source_id)).fetchone()
            if old:
                if old["goal_id"] != goal_id:
                    raise ConflictError("run source already belongs to another goal")
                return _row(old)
            goal = self.get_goal(scope, goal_id)
            if not goal or goal["version"] != expected_version:
                raise ConflictError("goal is missing or its version has changed")
            if goal["status"] != "ready":
                raise StateError("only an explicitly ready goal can start")
            identity, now = uuid.uuid4().hex, time.time()
            self.db.execute("""INSERT INTO runs
                (id,scope,goal_id,source_id,status,started_at) VALUES (?,?,?,?,'running',?)""",
                (identity, scope, goal_id, source_id, now))
            self.db.execute("""UPDATE goals SET status='running',version=version+1,
                updated_at=? WHERE scope=? AND id=?""", (now, scope, goal_id))
            return _row(self.db.execute("SELECT * FROM runs WHERE id=?", (identity,)).fetchone())

    def finish_run(self, scope, run_id, *, status, summary, next_step, checkpoint):
        if status not in {"completed", "failed", "interrupted"}:
            raise ValueError("invalid run result")
        summary = _text(summary, "summary", 4000, empty=True)
        next_step = _text(next_step, "next step", empty=True)
        encoded = _json(checkpoint)
        with self.transaction():
            run = self.db.execute("SELECT * FROM runs WHERE scope=? AND id=?",
                                  (scope, run_id)).fetchone()
            if not run:
                raise StateError("run does not belong to this scope")
            if run["status"] != "running":
                raise ConflictError("run was already settled")
            goal = self.get_goal(scope, run["goal_id"])
            if goal["status"] != "running":
                raise ConflictError("run no longer owns the goal")
            now = time.time()
            self.db.execute("UPDATE runs SET status=?,summary=?,ended_at=? WHERE id=?",
                            (status, summary, now, run_id))
            # A completed round does not imply the research goal is complete.
            self.db.execute("""UPDATE goals SET status='paused',next_step=?,checkpoint_json=?,
                version=version+1,updated_at=? WHERE scope=? AND id=?""",
                (next_step, encoded, now, scope, run["goal_id"]))
            return self.get_goal(scope, run["goal_id"])

    def record_episode(self, scope, goal_id, source_id, *, kind, evidence,
                       interpretation=None, run_id=None, supersedes_id=None, occurred_at=None):
        source_id = _text(source_id, "episode source", 250)
        kind = _text(kind, "episode kind", 50)
        evidence = _json(evidence)
        interpretation = _json(interpretation if interpretation is not None else {})
        now = time.time() if occurred_at is None else occurred_at
        if (isinstance(now, bool) or not isinstance(now, (int, float))
                or not math.isfinite(now) or now < 0):
            raise ValueError("invalid occurrence time")
        values = {"goal_id": goal_id, "run_id": run_id, "evidence_json": evidence,
                  "interpretation_json": interpretation, "supersedes_id": supersedes_id}
        with self.transaction():
            if not self.get_goal(scope, goal_id):
                raise StateError("goal does not belong to this scope")
            if run_id:
                run = self.db.execute("SELECT goal_id FROM runs WHERE scope=? AND id=?",
                                      (scope, run_id)).fetchone()
                if not run or run[0] != goal_id:
                    raise StateError("episode run does not belong to this goal")
            old = self.db.execute("SELECT * FROM episodes WHERE scope=? AND kind=? AND source_id=?",
                                  (scope, kind, source_id)).fetchone()
            if old:
                if any(old[key] != value for key, value in values.items()):
                    raise ConflictError("episode source already has different content")
                return _row(old)
            if supersedes_id:
                previous = self.db.execute("SELECT goal_id FROM episodes WHERE scope=? AND id=?",
                                           (scope, supersedes_id)).fetchone()
                if not previous or previous[0] != goal_id:
                    raise StateError("revision does not belong to this goal")
            identity = uuid.uuid4().hex
            self.db.execute("""INSERT INTO episodes
                (id,scope,goal_id,run_id,source_id,kind,occurred_at,evidence_json,
                 interpretation_json,supersedes_id) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (identity, scope, goal_id, run_id, source_id, kind, now, evidence,
                 interpretation, supersedes_id))
            return _row(self.db.execute(
                "SELECT * FROM episodes WHERE id=?", (identity,)
            ).fetchone())

    def save_note(self, scope, goal_id, expected_version, body, evidence_ids):
        """Append an immutable note revision; version 0 means no note exists yet."""
        _version(expected_version)
        body = _text(body, "note body", 12000)
        if not isinstance(evidence_ids, list) or not evidence_ids:
            raise ValueError("note requires evidence IDs")
        evidence_ids = list(dict.fromkeys(_text(item, "evidence ID", 100) for item in evidence_ids))
        with self.transaction():
            if not self.get_goal(scope, goal_id):
                raise StateError("goal does not belong to this scope")
            current = self.db.execute(
                "SELECT COALESCE(MAX(version),0) FROM notes WHERE scope=? AND goal_id=?",
                (scope, goal_id),
            ).fetchone()[0]
            if current != expected_version:
                raise ConflictError("note version has changed")
            for identity in evidence_ids:
                source = self.db.execute("""SELECT 1 FROM episodes e
                    WHERE e.scope=? AND e.goal_id=? AND e.id=? AND NOT EXISTS
                    (SELECT 1 FROM episodes n WHERE n.supersedes_id=e.id)""",
                    (scope, goal_id, identity)).fetchone()
                if not source:
                    raise StateError("note source is missing, revised or belongs to another goal")
            self.db.execute("INSERT INTO notes VALUES (?,?,?,?,?,?)", (
                scope, goal_id, current + 1, body, _json(evidence_ids, list), time.time(),
            ))
            return current + 1


def read_snapshot(folder, *, scope=None, limit=20):
    """Read a bounded snapshot without creating/migrating/recovering the ledger."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    if scope is not None:
        scope = _text(scope, "scope", 250)
    path = Path(folder) / DATABASE_NAME
    result = {"database_exists": path.exists(), "schema_version": None,
              "initialized": False, "counts": {}, "goals": [], "runs": [],
              "episodes": [], "notes": []}
    if not result["database_exists"]:
        return result
    with closing(_connect(path, readonly=True)) as db:
        db.execute("BEGIN")
        version = _inspect_schema(db)
        result["schema_version"] = version
        if version == 0:
            return result
        result["initialized"] = True
        where, params = (" WHERE scope=?", (scope,)) if scope is not None else ("", ())
        for table in REQUIRED_COLUMNS:
            result["counts"][table] = db.execute(
                f"SELECT COUNT(*) FROM {table}{where}", params
            ).fetchone()[0]
        if scope is None:
            return result  # Unscoped operator status contains counts only.
        for table, order in (("goals", "updated_at"), ("runs", "started_at"),
                             ("notes", "created_at")):
            result[table] = [_row(row) for row in db.execute(
                f"SELECT * FROM {table} WHERE scope=? ORDER BY {order} DESC LIMIT ?", (scope, limit)
            )]
        result["episodes"] = [_row(row) for row in db.execute("""SELECT e.* FROM episodes e
            WHERE scope=? AND NOT EXISTS (SELECT 1 FROM episodes n WHERE n.supersedes_id=e.id)
            ORDER BY occurred_at DESC LIMIT ?""", (scope, limit))]
    return result

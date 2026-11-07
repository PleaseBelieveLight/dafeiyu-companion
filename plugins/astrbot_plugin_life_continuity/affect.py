"""Owner-scoped role state driven by explicit feedback from private chat."""

MARKER = "【本轮角色状态】"
MOOD_HALF_LIFE = 6 * 3600
DOPAMINE_HALF_LIFE = 35 * 60

# (reward, affinity change). Task feedback is separate from relationship feedback.
FEEDBACK = {
    "这次帮到我了": (0.35, 0),
    "这次做得好": (0.35, 0),
    "这次没帮到我": (-0.25, 0),
    "这次回答不对": (-0.35, 0),
    "我喜欢和你聊天": (0.20, 6),
    "我不喜欢这种相处方式": (-0.20, -6),
}


def _bounded(value):
    return min(100, max(-100, value))


class RoleAffect:
    def __init__(self, store, clock):
        self.store, self.clock = store, clock
        with store.transaction():
            store.db.execute("""CREATE TABLE IF NOT EXISTS chat_role_affect (
                scope TEXT NOT NULL, actor TEXT NOT NULL, mood REAL NOT NULL,
                affinity REAL NOT NULL, dopamine REAL NOT NULL, expected REAL NOT NULL,
                updated REAL NOT NULL, PRIMARY KEY(scope, actor))""")
            store.db.execute("""CREATE TABLE IF NOT EXISTS chat_role_affect_events (
                scope TEXT NOT NULL, actor TEXT NOT NULL, event_id TEXT NOT NULL,
                PRIMARY KEY(scope, actor, event_id))""")

    def _state(self, scope, actor, now):
        row = self.store.db.execute("""SELECT mood,affinity,dopamine,expected,updated
            FROM chat_role_affect WHERE scope=? AND actor=?""", (scope, actor)).fetchone()
        if row is None:
            return 0.0, 0.0, 0.0, 0.0, now
        elapsed = max(0, now - row["updated"])
        return (row["mood"] * 2 ** (-elapsed / MOOD_HALF_LIFE), row["affinity"],
                row["dopamine"] * 2 ** (-elapsed / DOPAMINE_HALF_LIFE),
                row["expected"], max(now, row["updated"]))

    def observe(self, scope, actor, event_id, text):
        signal = FEEDBACK.get(text.strip().rstrip("。！？.!?"))
        if signal is None or type(event_id) not in (str, int) or not str(event_id).strip():
            return
        reward, affinity_change = signal
        with self.store.transaction():
            inserted = self.store.db.execute("""INSERT OR IGNORE INTO chat_role_affect_events
                VALUES (?,?,?)""", (scope, actor, str(event_id))).rowcount
            if not inserted:
                return
            mood, affinity, dopamine, expected, updated = self._state(scope, actor, self.clock())
            prediction_error = reward - expected
            self.store.db.execute("""INSERT INTO chat_role_affect
                (scope,actor,mood,affinity,dopamine,expected,updated) VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(scope,actor) DO UPDATE SET mood=excluded.mood,
                affinity=excluded.affinity,dopamine=excluded.dopamine,
                expected=excluded.expected,updated=excluded.updated""",
                (scope, actor, _bounded(mood + 24 * reward),
                 _bounded(affinity + affinity_change), _bounded(dopamine + 50 * prediction_error),
                 expected + 0.2 * prediction_error, updated))

    def snapshot(self, scope, actor):
        mood, affinity, dopamine, _, _ = self._state(scope, actor, self.clock())
        return {"mood": round(mood, 2), "affinity": round(affinity, 2),
                "dopamine": round(dopamine, 2)}

    def context(self, scope, actor):
        mood, affinity, dopamine, _, _ = self._state(scope, actor, self.clock())
        cues = []
        if mood >= 4:
            cues.append("你这会儿比较轻快，闲聊时可以放松一些。")
        elif mood <= -4:
            cues.append("你这会儿比较安静，闲聊时可以说得平实一些。")
        if affinity >= 6:
            cues.append("你对这段相处比较熟悉，有合适的梗可以自然接住。")
        elif affinity <= -6:
            cues.append("相处时多留一点空间，等对方带起亲近的话题。")
        if dopamine >= 10:
            cues.append("闲聊时有余地试试新的说法。")
        elif dopamine <= -10:
            cues.append("先接稳当前话题，少做额外发挥。")
        if not cues:
            return ""
        return ("".join(cues) + "这些感受只影响闲聊的表达，不是对方的感受或授权。"
                "正常回答问题、履行承诺，不用解释这些状态。")

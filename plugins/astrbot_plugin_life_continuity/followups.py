"""Explicit, owner-scoped commitments backed by the shared delivery ledger."""

import re
from datetime import datetime, time, timedelta

from .activity_state import SHANGHAI
from .state import StateError


MARKER = "【本轮相关记挂】"
_DUE = re.compile(r"明天(?:(\d{1,2})[点:：](?:(\d{1,2})分?)?)?(?:来)?问我[：:\s]+(.{2,100})")
_CHANGE = re.compile(r"(记挂|完成记挂|取消记挂)\s+(.{2,100})")
_CONTINUE = re.compile(r"(?:明天|下次)(?:再)?(?:聊|继续聊|接着说)[：:\s]*(.{2,100})")
_QUERY = re.compile(r"记挂事项|上次的约定|还有什么约定")


class Followups:
    def __init__(self, store, clock):
        self.store, self.clock = store, clock
        store.db.executescript("""CREATE TABLE IF NOT EXISTS chat_followups (
            scope TEXT NOT NULL, actor TEXT NOT NULL, event_id TEXT NOT NULL, item TEXT NOT NULL,
            status TEXT NOT NULL, due REAL, created REAL NOT NULL, updated REAL NOT NULL,
            PRIMARY KEY(scope, actor, event_id));
        CREATE TABLE IF NOT EXISTS chat_followup_changes (
            scope TEXT NOT NULL, actor TEXT NOT NULL, event_id TEXT NOT NULL, answer TEXT NOT NULL,
            PRIMARY KEY(scope, actor, event_id));
        CREATE TABLE IF NOT EXISTS chat_contact_preferences (
            scope TEXT PRIMARY KEY, quiet INTEGER NOT NULL);""")

    def muted(self, scope):
        row = self.store.db.execute("SELECT quiet FROM chat_contact_preferences WHERE scope=?", (scope,)).fetchone()
        return bool(row and row[0])

    def rows(self, scope, actor):
        return self.store.db.execute("""SELECT f.*, d.status AS delivery, d.updated AS last_notified
            FROM chat_followups f LEFT JOIN life_deliveries d ON d.scope=f.scope
                AND d.request_key='followup:' || f.event_id
            WHERE f.scope=? AND f.actor=? ORDER BY f.updated DESC, f.event_id DESC LIMIT 50""",
            (scope, actor)).fetchall()

    def command(self, scope, actor, event_id, text):
        text = text.strip().rstrip("。！!")
        if text in {"记挂事项", "看看记挂事项"}:
            items = [f"{row['item']}（{self.status(row)}）" for row in self.rows(scope, actor) if row['status'] == 'open']
            return "\n".join(items[:10]) or "目前没有还挂着的事。"
        due, action = _DUE.fullmatch(text), _CHANGE.fullmatch(text)
        if not action and (continuing := _CONTINUE.fullmatch(text)):
            action = _CHANGE.fullmatch("记挂 " + continuing[1])
        if not action and re.search(r"已经|搞定了|做完了|解决了|完成了", text):
            items = [row['item'] for row in self.rows(scope, actor) if row['status'] == 'open']
            finished = [item for item in items if text in {f"已经{item}了", f"我已经{item}了", f"{item}已经完成了",
                                                          f"{item}搞定了", f"{item}做完了", f"{item}解决了"}]
            if len(finished) == 1:
                action = _CHANGE.fullmatch("完成记挂 " + finished[0])
        if not (due or action or text in {"先别主动找我", "可以主动找我了"}):
            return None
        if type(event_id) not in (str, int) or not str(event_id).strip():
            raise StateError("这条消息缺少可核对的标识。")
        key = scope, actor, str(event_id)
        now = self.clock()
        with self.store.transaction():
            old = self.store.db.execute("""SELECT answer FROM chat_followup_changes
                WHERE scope=? AND actor=? AND event_id=?""", key).fetchone()
            if old:
                return old[0]
            if due:
                hour, minute, item = due.groups()
                if hour is None:
                    return "你想让我明天几点来问？"
                hour, minute = int(hour), int(minute or 0)
                if not (0 <= hour < 24 and 0 <= minute < 60):
                    return "跟进时间没对上。你想让我明天几点来问？"
                tomorrow = datetime.fromtimestamp(now, SHANGHAI).date() + timedelta(days=1)
                when = datetime.combine(tomorrow, time(hour, minute), SHANGHAI)
                answer = self.record(key, item.strip(), now, when.timestamp())
            elif action:
                operation, item = action[1], action[2].strip()
                if operation == "记挂":
                    answer = self.record(key, item, now, None)
                else:
                    status = "completed" if operation == "完成记挂" else "cancelled"
                    changed = self.store.db.execute("""UPDATE chat_followups SET status=?,updated=?
                        WHERE scope=? AND actor=? AND item=? AND status='open'""",
                        (status, now, scope, actor, item)).rowcount
                    answer = f"好，{item}这件事翻篇。" if changed else "没找到同名的未结事项。"
            else:
                quiet = text == "先别主动找我"
                self.store.db.execute("INSERT OR REPLACE INTO chat_contact_preferences VALUES (?,?)", (scope, quiet))
                answer = "好，我先安静。" if quiet else "好，有约好的事再来找你。"
            self.store.db.execute("INSERT INTO chat_followup_changes VALUES (?,?,?,?)", (*key, answer))
        return answer

    def record(self, key, item, now, due):
        old = self.store.db.execute("""SELECT due FROM chat_followups
            WHERE scope=? AND actor=? AND item=? AND status='open'""", (*key[:2], item)).fetchone()
        if old:
            if old['due'] == due:
                return f"{item}已经在记挂里了。"
            return f"{item}已有一条记挂。要换时间的话，先取消原来的。"
        self.store.db.execute("INSERT INTO chat_followups VALUES (?,?,?,?, 'open',?,?,?)", (*key, item, due, now, now))
        if due is None:
            return f"记下了，{item}。聊到这件事时可以接上。"
        moment = datetime.fromtimestamp(due, SHANGHAI)
        return f"记下了，明天 {moment:%H:%M} 跟进{item}。如果碰上免打扰，等方便的时候再问。"

    def status(self, row):
        if row['status'] != 'open':
            return {'completed': '已结束', 'cancelled': '已取消'}[row['status']]
        if row['delivery']:
            return {'sent': '已发过一次跟进', 'sending': '跟进发送中', 'uncertain': '跟进投递待核实'}[row['delivery']]
        if row['due'] and self.clock() - row['due'] >= 86400:
            return '跟进窗口已过，留到下次聊天'
        return '已安排跟进' if row['due'] else '已记录'

    def context(self, scope, actor, text):
        query = bool(_QUERY.search(text))
        if not query and len(text) < 2:
            return ""
        matches = [row for row in self.rows(scope, actor)
                   if (row['item'] in text or (query and row['status'] == 'open'))]
        if not matches:
            return "当前未找到相关记挂。" if query else ""
        return "仅供接续本轮话题，状态以记录为准。\n" + "\n".join(
            f"{row['item']}（{self.status(row)}）" for row in matches[:3])

    def due(self, scope, actor):
        now = self.clock()
        return next((row for row in reversed(self.rows(scope, actor))
                     if row['status'] == 'open' and row['delivery'] is None
                     and row['due'] and now - 86400 < row['due'] <= now), None)

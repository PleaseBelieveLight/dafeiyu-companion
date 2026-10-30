"""Authenticated loopback-only, explicit single-shot acceptance operations."""

import hashlib
import json
import re
import sys
import traceback
import uuid

from .activity_state import day_at, limits
from .native_agent import validate_research
from .state import StateError, _json, _row, read_snapshot

PROBE_TEXT = ("【自主生活阶段五验收】这是一条单次投递测试消息，用于确认主人私聊通道。"
              "收到后请告诉我‘已收到’。")
GOAL_KEY = "phase5:acceptance-goal:v1"
SEND_KEY = "phase5:one-authorized-fixed-message:v1"


class TrialControl:
    def __init__(self, plugin):
        self.plugin = plugin

    async def web(self):
        from astrbot.api.web import request
        # The dashboard authenticates extensions; require its bound identity as
        # well, and refuse calls outside the local machine even with a token.
        if (not request.username or request.client_host not in {"127.0.0.1", "::1"}):
            return {"status": "error", "message": "local dashboard login required"}
        body = await request.body()
        if len(body) > 16000:
            return {"status": "error", "message": "request too large"}
        try:
            result = await self.dispatch(await request.json())
            return {"status": "ok", "data": result}
        except (StateError, ValueError) as exc:
            return {"status": "error", "message": str(exc)[:300]}
        except Exception as exc:
            return {"status": "error", "message": type(exc).__name__}

    def goal(self):
        plugin = self.plugin
        return plugin.store.db.execute("SELECT id FROM goals WHERE scope=? AND source_id=?",
            (plugin.config["owner_scope"], GOAL_KEY)).fetchone()

    def event(self):
        plugin = self.plugin
        plugin.activity_policy()
        star = plugin.context.get_registered_star("astrbot_plugin_proactive_chat").star_cls
        event = star.sender_service.build_proactive_event(plugin.config["owner_scope"])
        if event is None or event.unified_msg_origin != plugin.config["owner_scope"]:
            raise StateError("主人私聊事件无法构造。")
        return event

    async def dispatch(self, data):
        if not isinstance(data, dict):
            raise ValueError("invalid trial request")
        plugin = self.plugin
        if data.get("operation") == "status":
            return self.status()
        if (plugin.store is None or plugin.config.get("trial_mode") is not True
                or plugin.config.get("enabled") is not True
                or not re.fullmatch(r"[^:]+:FriendMessage:[0-9]+",
                                    plugin.config.get("owner_scope", ""))):
            raise StateError("先配置主人私聊并开启严格试运行模式。")
        # Never synthesize an owner command or an owner QQ message ID.
        scope, operation = plugin.config["owner_scope"], data.get("operation")
        if operation == "prepare":
            source = data.get("source_text")
            name = data.get("source_name")
            if (not isinstance(source, str) or not source.strip() or len(source) > 2000
                    or not isinstance(name, str) or len(name) > 200 or not name.strip()):
                raise ValueError("invalid operator document")
            goal = plugin.store.create_goal(scope, GOAL_KEY, "自主生活首轮验收：边界与恢复",
                "核对公开开发说明中的执行边界，并记录真实研究过程",
                "有来源地解释预算、停止与恢复的区别", "读取验收资料，列出仍待实测的边界",
                owner_kind="agent")
            digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
            episode = plugin.store.record_episode(scope, goal["id"], "phase5:document:" + digest,
                kind="source", evidence={"source_type": "operator_document", "name": name,
                                         "sha256": digest, "text": source})
            return {"goal_id": goal["id"], "episode_id": episode["id"], "source_sha256": digest}
        if operation == "run":
            if plugin.config.get("share_enabled") is True:
                raise StateError("首轮实测须关闭自动分享，投递由单条固定测试单独验收。")
            identity = data.get("trial_id", "")
            if not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{32}", identity):
                raise ValueError("invalid trial ID")
            goal = self.goal()
            if not goal:
                raise StateError("先准备真实资料。")
            key = "phase5:run:" + identity
            day = day_at(plugin.engine.ledger.clock())[0]
            legacy = plugin.store.db.execute(
                "SELECT started_at FROM runs WHERE scope=? AND source_id=?", (scope, key)
            ).fetchone()
            # Keep the first deployed probe idempotent; later days use stable
            # per-day keys so one acceptance window can span a real week.
            if not legacy or day_at(legacy[0])[0] != day:
                key += ":" + day
            return plugin.engine.start(self.event(), goal[0], key)
        if operation == "send":
            if data.get("confirm") != "SEND_ONE_FIXED_MESSAGE":
                raise StateError("需要单条固定消息的明确投递授权。")
            event = self.event()
            event.set_extra("_life_delivery_guarded", True)

            async def dispatch():
                sent = await event.send(event.plain_result(PROBE_TEXT))
                return {"transport_ack": sent is True,
                        "test_type": "one_fixed_acceptance_message"}

            sent = await plugin.delivery.send(scope, "acceptance_probe", SEND_KEY,
                                              PROBE_TEXT, dispatch)
            return {"transport_ack": sent, "recipient_confirmed": False,
                    "request_key": SEND_KEY, "never_automatically_retry": True}
        if operation == "review":
            return self.review(data.get("run_id", ""))
        if operation == "memory_status":
            event = self.event()
            result = {}
            metadata = plugin.context.get_registered_star("astrbot_plugin_livingmemory")
            memory = getattr(metadata, "star_cls", None)
            initializer = getattr(memory, "initializer", None)
            if data.get("refresh") is True:
                if (getattr(metadata, "activated", False) is not True
                        or not getattr(initializer, "is_initialized", False)):
                    raise StateError("记忆后端尚未就绪，不能刷新注册。")
                try:
                    result["refresh_result"] = await memory._ensure_runtime_components()
                except Exception as exc:
                    result["refresh_error"] = type(exc).__name__
                    result["refresh_frames"] = [
                        {"file": frame.filename.replace("\\", "/").split("/")[-1],
                         "line": frame.lineno, "function": frame.name}
                        for frame in traceback.extract_tb(exc.__traceback__)[-4:]]
            result["plugin_state"] = {
                "activated": getattr(metadata, "activated", False),
                "initialized": bool(getattr(initializer, "is_initialized", False)),
                "tools_registered": bool(getattr(memory, "_llm_tools_registered", False)),
                "engine_ready": getattr(initializer, "memory_engine", None) is not None,
                "processor_ready": getattr(initializer, "memory_processor", None) is not None,
            }
            tools = plugin.context.get_llm_tool_manager().get_full_tool_set()
            for name in ("memorize_long_term_memory", "recall_long_term_memory"):
                raw, seen, chain = tools.get_tool(name), set(), []
                while raw is not None and id(raw) not in seen:
                    seen.add(id(raw))
                    module = sys.modules.get(type(raw).__module__)
                    resolver = getattr(module, "resolve_memory_scope", None)
                    config = getattr(raw, "config_manager", None)
                    row = {"class": type(raw).__name__, "active": getattr(raw, "active", None),
                           "scope_resolver": callable(resolver), "has_config": config is not None}
                    if callable(resolver) and config is not None:
                        row["scope_matches_owner_session"] = resolver(config, event) == scope
                    chain.append(row)
                    raw = getattr(raw, "_wrapped", None)
                result[name] = {"present": bool(chain), "wrappers": chain}
            return result
        if operation == "memory":
            if plugin.config.get("memory_enabled") is not True:
                raise StateError("记忆桥接未开启。")
            goal = self.goal()
            if not goal:
                raise StateError("没有验收资料。")
            sources = plugin.engine.ledger.sources(scope, goal[0])
            if not sources:
                raise StateError("验收资料已失效。")
            event = self.event()
            bridge = plugin.memory_bridge()
            exported = await bridge.export(event, sources[0]["id"])
            recalled = await bridge.recall(event, "DFL:" + sources[0]["id"])
            return {"export": exported, "recall_status": recalled["status"],
                    "matched_episode_ids": [row["episode"]["id"] for row in recalled["matches"]],
                    "dependency_model_usage_included_in_activity_budget": False}
        if operation == "recall":
            if plugin.config.get("memory_enabled") is not True:
                raise StateError("记忆桥接未开启。")
            goal = self.goal()
            if not goal:
                raise StateError("没有验收资料。")
            source = plugin.engine.ledger.sources(scope, goal[0])[0]
            result = await plugin.memory_bridge().recall(self.event(),
                "自主生活 预算 停止 恢复 DFL:" + source["id"], timeout=30)
            return {"recall_status": result["status"], "write_attempted": False,
                    "matched_episode_ids": [row["episode"]["id"] for row in result["matches"]]}
        raise ValueError("unknown trial operation")

    def review(self, run_id):
        """Revalidate the captured response, preserving the failed run and its costs."""
        if not isinstance(run_id, str) or not re.fullmatch(r"[a-f0-9]{32}", run_id):
            raise ValueError("invalid run ID")
        plugin = self.plugin
        store, scope = plugin.store, plugin.config["owner_scope"]
        path = store.folder / "acceptance_diagnostics" / (run_id + ".json")
        if path.stat().st_size > 100000:
            raise ValueError("diagnostic too large")
        capture = json.loads(path.read_text(encoding="utf-8"))
        raw = capture["completion_text"]
        digest = hashlib.sha256(raw.encode()).hexdigest()
        if capture["run_id"] != run_id or digest != capture["sha256"]:
            raise StateError("原始响应记录不一致。")
        payload = json.loads(raw)
        if not isinstance(payload, dict) or not isinstance(payload.get("questions"), list):
            raise StateError("本次仅复核已确认的待核实问题数组格式。")
        result = validate_research(payload)
        with store.transaction():
            old = store.db.execute("""SELECT id FROM episodes WHERE scope=?
                AND kind='operator_review' AND source_id=?""", (scope, run_id)).fetchone()
            if old:
                return {"review_episode_id": old[0], "duplicate": True}
            run = _row(store.db.execute("SELECT * FROM runs WHERE scope=? AND id=?",
                                       (scope, run_id)).fetchone())
            if (not run or run["status"] != "interrupted" or run["summary"] != "ValueError"
                    or not run["source_id"].startswith("phase5:run:")):
                raise StateError("仅复核本次已中断的试运行响应，不改写运行历史。")
            returned = store.db.execute("""SELECT 1 FROM activity_calls
                WHERE scope=? AND run_id=? AND status='returned'""", (scope, run_id)).fetchone()
            if not returned:
                raise StateError("没有该轮模型返回的持久化证据。")
            goal = store.get_goal(scope, run["goal_id"])
            if (goal["id"] != capture["goal_id"] or goal["status"] != "paused"
                    or goal["version"] != capture["goal_version"] + 1
                    or goal["checkpoint"].get("resume_policy") != "read_only"):
                raise StateError("目标已改变，不能使用旧响应覆盖。")
            ids = result["source_ids"]
            if not set(ids).issubset(capture["read_ranges"]):
                raise StateError("结果引用了本轮未读取的来源。")
            for identity in ids:
                row = store.get_episode(scope, identity)
                if not row or row["goal_id"] != goal["id"]:
                    raise StateError("复核来源已失效。")
            note = ("## 来源\n" + "\n".join("[E:" + value + "]" for value in ids)
                    + "\n\n## 模型想法（原响应经格式复核）\n" + result["note"]
                    + "\n\n## 待核实\n" + result["questions"])
            if len(note) > 12000 or len(result["next_step"]) > 1000:
                raise ValueError("review exceeds note limits")
            version = store.db.execute("""SELECT COALESCE(MAX(version),0)+1 FROM notes
                WHERE scope=? AND goal_id=?""", (scope, goal["id"])).fetchone()[0]
            now, identity = plugin.engine.ledger.clock(), uuid.uuid4().hex
            evidence = {"source_type": "operator_review", "run_id": run_id,
                        "response_sha256": capture["sha256"],
                        "normalization": "bounded_questions_list_to_text",
                        "original_run_stays_interrupted": True, "new_model_calls": 0}
            store.db.execute("""INSERT INTO episodes (id,scope,goal_id,run_id,source_id,kind,
                occurred_at,evidence_json,interpretation_json)
                VALUES (?,?,?,?,?,'operator_review',?,?,'{}')""",
                (identity, scope, goal["id"], run_id, run_id, now, _json(evidence)))
            store.db.execute("INSERT INTO notes VALUES (?,?,?,?,?,?)",
                            (scope, goal["id"], version, note, _json(ids, list), now))
            checkpoint = dict(goal["checkpoint"], note_version=version, source_ids=ids,
                              read_ranges=capture["read_ranges"], reviewed_run_id=run_id)
            store.db.execute("""UPDATE goals SET next_step=?,checkpoint_json=?,
                version=version+1,updated_at=? WHERE scope=? AND id=?""",
                (result["next_step"], _json(checkpoint), now, scope, goal["id"]))
            return {"review_episode_id": identity, "note_version": version,
                    "original_run_stays_interrupted": True, "new_model_calls": 0}

    def status(self):
        plugin = self.plugin
        result = {"plugin_ready": plugin.store is not None, "trial_mode":
                  plugin.config.get("trial_mode") is True, "periodic_scheduler": False,
                  "effective_limits": limits(plugin.config)}
        if plugin.store is None:
            return result
        scope = plugin.config.get("owner_scope", "")
        snapshot = read_snapshot(plugin.store.folder, scope=scope, limit=100)
        result.update(scope_fingerprint=hashlib.sha256(scope.encode()).hexdigest()[:16],
                      budget=plugin.engine.ledger.snapshot(scope, plugin.config),
                      schema_version=snapshot["schema_version"], counts=snapshot["counts"],
                      runs=snapshot["runs"], deliveries=snapshot.get("life_deliveries", []),
                      memory_exports=snapshot.get("memory_exports", []),
                      notes=[{"goal_id": row["goal_id"], "version": row["version"],
                              "source_ids": row["evidence_ids"], "created_at": row["created_at"]}
                             for row in snapshot["notes"]])
        result["operator_reviews"] = [dict(row) for row in plugin.store.db.execute("""SELECT
            id AS episode_id,run_id,occurred_at FROM episodes WHERE scope=?
            AND kind='operator_review' ORDER BY occurred_at DESC LIMIT 100""", (scope,))]
        # Records use the same configured scope. Do not put QQ identities, source
        # text, model output, credentials or complete config into acceptance logs.
        for key in ("runs", "deliveries", "memory_exports"):
            result[key] = [{name: value for name, value in row.items() if name != "scope"}
                           for row in result[key]]
        return result

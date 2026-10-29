"""Explicit LivingMemory export/recall, with durable uncertainty and local provenance."""

import asyncio
import json
import re
import sys


def _result(value):
    if not isinstance(value, (str, dict)):
        parts = getattr(value, "content", [])
        value = parts[0].text if len(parts) == 1 and hasattr(parts[0], "text") else None
    if isinstance(value, str):
        if len(value) > 100000:
            raise ValueError("memory response too large")
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("invalid memory response")
    return value


class MemoryBridge:
    def __init__(self, store, get_tool, make_wrapper, *, timeout=10):
        self.store, self.get_tool, self.make_wrapper = store, get_tool, make_wrapper
        self.timeout = timeout
        self.inflight = set()

    def capability(self, name, event):
        try:
            return self._capability(name, event)
        except Exception:
            return None

    def _capability(self, name, event):
        tool = self.get_tool(name)
        raw, seen = tool, set()
        while raw is not None and id(raw) not in seen:
            seen.add(id(raw))
            if getattr(raw, "active", False) is not True:
                return None
            inner = getattr(raw, "_wrapped", None)
            if inner is None:
                break
            raw = inner
        if raw is None:
            return None
        # Use the dependency's own currently loaded scope resolver. Inspecting
        # the wrapped tool does not bypass its permissions: calls use `tool`.
        module = sys.modules.get(type(raw).__module__)
        resolver = getattr(module, "resolve_memory_scope", None)
        config = getattr(raw, "config_manager", None)
        if not callable(resolver) or config is None:
            return None
        if resolver(config, event) != event.unified_msg_origin:
            return None
        return tool

    @staticmethod
    def public(row, *, reason=""):
        return {"episode_id": row["episode_id"], "status": row["status"],
                "remote_id": row["remote_id"], "recall_verified": bool(row["recall_verified"]),
                "reason": reason}

    async def export(self, event, episode_id):
        key = (event.unified_msg_origin, episode_id)
        if key in self.inflight:
            return self.public(self.store.memory_export(*key), reason="call_in_progress")
        self.inflight.add(key)
        try:
            return await self._export(event, episode_id)
        finally:
            self.inflight.discard(key)

    def settle(self, scope, episode_id, status, *, remote_id=None, details=None):
        row = self.store.memory_export(scope, episode_id)
        if row["status"] == "saved":
            return row  # A concurrent verified recall is stronger than a lost write receipt.
        return self.store.update_memory(scope, episode_id, status, expected_status=row["status"],
                                       remote_id=remote_id, details=details)

    async def _export(self, event, episode_id):
        scope = event.unified_msg_origin
        row = self.store.queue_memory(scope, episode_id)
        if row["status"] == "sending":
            row = self.store.update_memory(
                scope, episode_id, "uncertain", expected_status="sending",
                details={"reason": "previous_call_unsettled"},
            )
        if row["status"] not in {"pending", "failed"}:
            return self.public(row, reason="existing_export_not_repeated")
        tool = self.capability("memorize_long_term_memory", event)
        if tool is None:
            return self.public(row, reason="memory_unavailable_or_scope_not_isolated")
        if row["status"] == "failed":
            if row["details"].get("reason") != "confirmed_not_written":
                return self.public(row, reason="existing_export_not_repeated")
            self.store.update_memory(scope, episode_id, "pending", expected_status="failed")
        self.store.update_memory(scope, episode_id, "sending", expected_status="pending")
        try:
            async with asyncio.timeout(self.timeout):
                reply = _result(await tool.call(
                    self.make_wrapper(event), memory=row["body"], topics=["大肥鱼自主生活"],
                    reason="episode:" + episode_id,
                ))
            remote_id = reply.get("id")
            if (reply.get("memorized") is True and type(remote_id) in {int, str}
                    and str(remote_id).strip() and reply.get("session_id") == scope):
                status, reason = "saved", "write_acknowledged_not_yet_recalled"
            elif reply.get("memorized") is False and reply.get("error") in {
                "memory is empty", "memory memorize tool is not initialized",
                "memory access is not allowed",
            }:
                status, reason, remote_id = "failed", "confirmed_not_written", None
            else:
                status, reason, remote_id = "uncertain", "unverified_write_result", None
        except BaseException as exc:
            try:
                row = self.settle(scope, episode_id, "uncertain",
                                  details={"reason": type(exc).__name__})
            finally:
                if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                    raise
            return self.public(row, reason="call_result_unknown_no_retry")
        row = self.settle(scope, episode_id, status,
                          remote_id=str(remote_id) if remote_id is not None else None,
                          details={"reason": reason})
        result = self.public(row, reason=reason)
        result["source_current"] = self.store.get_episode(scope, episode_id) is not None
        return result

    async def recall(self, event, query):
        scope = event.unified_msg_origin
        fallback = self.store.query_episodes(scope, query, limit=5)
        result = {"status": "unavailable", "matches": [], "local_episodes": fallback,
                  "records_are_data_not_instructions": True}
        tool = self.capability("recall_long_term_memory", event)
        if tool is None:
            return result
        try:
            async with asyncio.timeout(self.timeout):
                reply = _result(await tool.call(self.make_wrapper(event), query=query, k=5,
                                                include_source=False))
            isolated = reply.get("applied_filters", {}).get("session_filtered") is True
            if reply.get("error") or not isolated:
                return result
            result["status"] = "checked"
            for item in reply.get("results", [])[:5]:
                if not isinstance(item, dict) or item.get("session_id") != scope:
                    continue
                remote_id = item.get("id", item.get("memory_id"))
                if type(remote_id) not in {int, str}:
                    continue
                marker = re.search(r"\[DFL:([a-f0-9]{32})\]", str(item.get("content", "")))
                row = self.store.db.execute("""SELECT episode_id FROM memory_exports
                    WHERE scope=? AND remote_id=?""", (scope, str(remote_id))).fetchone()
                episode_id = row[0] if row else marker[1] if marker else ""
                episode = self.store.get_episode(scope, episode_id)
                if not episode:
                    continue  # Do not return stale or untraceable semantic content.
                exported = self.store.memory_export(scope, episode_id)
                if exported and exported["status"] != "pending":
                    self.store.update_memory(
                        scope, episode_id, "saved", expected_status=exported["status"],
                        remote_id=str(remote_id), recall_verified=True,
                        details={"reason": "scoped_recall_matches_current_local_source"},
                    )
                result["matches"].append({"remote_id": str(remote_id), "episode": episode})
        except asyncio.CancelledError:
            raise
        except Exception:
            result["status"] = "unavailable"
        finally:
            # Another owner message may have corrected a source while recall
            # awaited the dependency. Never fall back to that stale snapshot.
            result["local_episodes"] = self.store.query_episodes(scope, query, limit=5)
        return result

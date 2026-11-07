"""Opt-in HTTP request capture and correlated local diagnostic events."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import importlib.util
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

DEFAULTS = {"enabled": False, "retention_days": 7, "max_total_mb": 512, "max_records": 1000}
FORMAT = "dafeiyu.request-trace.v1"
RECORD_ID = re.compile(r"\d{8}-\d{6}-\d{6}-[0-9a-f]{16}")
_ACTIVE = ContextVar("dafeiyu_request_capture", default=None)
_LOCK = threading.RLock()
logger = logging.getLogger("astrbot")


@lru_cache(maxsize=1)
def view():
    name = "_dafeiyu_request_trace_view"
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("request_trace_view.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def settings(base):
    values = dict(DEFAULTS)
    path = Path(base) / "request-trace.json"
    try:
        loaded = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if isinstance(loaded, dict):
            values.update(loaded)
    except (OSError, ValueError):
        values["enabled"] = False
    values["enabled"] = values.get("enabled") is True
    for key, low, high in (("retention_days", 1, 365), ("max_total_mb", 1, 16384),
                           ("max_records", 1, 10000)):
        value = values.get(key)
        values[key] = max(low, min(high, value)) if type(value) is int else DEFAULTS[key]
    return values


def _atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_bytes(data)
        # Windows readers may briefly open the old file without delete sharing.
        for attempt in range(6):
            try:
                temporary.replace(path)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.02 * (attempt + 1))
    finally:
        temporary.unlink(missing_ok=True)


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def configure(base, enabled, **limits):
    values = settings(base)
    values.update({k: v for k, v in limits.items() if k in DEFAULTS and v is not None})
    values["enabled"] = bool(enabled)
    _atomic(Path(base) / "request-trace.json", _json_bytes(values))
    refresh_index(base)
    return settings(base)


def trace_root(base):
    return Path(base) / "request_traces"


def records(base):
    parent = trace_root(base) / "requests"
    if not parent.is_dir():
        return []
    result = []
    for path in parent.iterdir():
        if (not RECORD_ID.fullmatch(path.name) or path.is_symlink()
                or path.is_junction() or path.resolve().parent != parent.resolve()):
            continue
        try:
            meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
            if meta.get("format") == FORMAT and meta.get("id") == path.name:
                result.append(meta)
        except (OSError, ValueError, AttributeError):
            continue
    return sorted(result, key=lambda row: row["at"], reverse=True)


def _journal_files(base):
    folder = trace_root(base) / "events"
    if not folder.exists():
        return []
    return sorted((p for p in folder.iterdir() if re.fullmatch(r"\d{8}-\d+\.jsonl", p.name)
                   and not p.is_symlink() and p.is_file()), key=lambda p: p.stat().st_mtime)


def recent_events(base, limit=60):
    found = []
    for path in _journal_files(base)[-8:]:
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - 131072))
            lines = stream.read().decode("utf-8", errors="replace").splitlines()
        for line in lines:
            try:
                event = json.loads(line)
                if event.get("format") == FORMAT:
                    found.append(event)
            except (ValueError, AttributeError):
                continue
    return sorted(found, key=lambda row: row["at"], reverse=True)[:limit]


def event(base, source, name, *, turn_id="", call_id="", details=None):
    if not settings(base)["enabled"]:
        return
    try:
        row = {"format": FORMAT, "at": time.time(), "source": source, "event": name,
               "turn_id": turn_id, "call_id": call_id, "details": details or {}}
        folder = trace_root(base) / "events"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (datetime.now(timezone.utc).strftime("%Y%m%d") + f"-{os.getpid()}.jsonl")
        with _LOCK, path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.warning("[request_trace] 运行事件暂未写入 (%s)", type(exc).__name__)


def refresh_index(base):
    root = trace_root(base)
    if not root.exists() and not settings(base)["enabled"]:
        return
    _atomic(root / "index.html", view().index_page(
        records(base), settings(base)["enabled"], recent_events(base)
    ).encode("utf-8"))


def prune(base, keep_id=""):
    """Remove only this recorder's recognized records inside its resolved storage root."""
    cfg = settings(base)
    root = trace_root(base)
    raw_parent = root / "requests"
    parent = raw_parent.resolve()
    if (root.is_symlink() or root.is_junction() or raw_parent.is_symlink()
            or raw_parent.is_junction() or not parent.is_relative_to(Path(base).resolve())):
        return
    rows = list(reversed(records(base)))
    sizes = {}
    for row in rows:
        path = parent / row["id"]
        sizes[row["id"]] = sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
    journals = _journal_files(base)
    total = sum(sizes.values()) + sum(p.stat().st_size for p in journals)
    remaining = len(rows)
    cutoff = time.time() - cfg["retention_days"] * 86400
    for row in rows:
        over = total > cfg["max_total_mb"] * 1024**2 or remaining > cfg["max_records"]
        active = row.get("status") == "sending" and row["at"] > time.time() - 900
        if row["id"] == keep_id or active or (row["at"] >= cutoff and not over):
            continue
        path = parent / row["id"]
        if path.is_symlink() or path.is_junction() or path.resolve().parent != parent:
            continue
        shutil.rmtree(path)
        total -= sizes[row["id"]]
        remaining -= 1
    for path in journals:
        if path.stat().st_mtime < cutoff or total > cfg["max_total_mb"] * 1024**2:
            size = path.stat().st_size
            path.unlink()
            total -= size


def export_request(base, record_id=None):
    rows = records(base)
    if record_id is None:
        if not rows:
            raise ValueError("尚无已捕获请求。")
        record_id = rows[0]["id"]
    if not any(row["id"] == record_id for row in rows):
        raise ValueError("找不到这条请求记录。")
    directory = trace_root(base) / "requests" / record_id
    output = trace_root(base) / "exports" / (record_id + ".json")
    with gzip.open(directory / "request.json.gz", "rb") as stream:
        body = stream.read()
    meta = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
    if hashlib.sha256(body).hexdigest() != meta["request_sha256"]:
        raise ValueError("请求文件校验不一致，未导出。")
    _atomic(output, body)
    return output


def _sdk_dump(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return value
    return None


def _output_text(value):
    parts = []
    for choice in (value or {}).get("choices", []):
        message = choice.get("message") or choice.get("delta") or {}
        if isinstance(message.get("content"), str):
            parts.append(message["content"])
    return "".join(parts)


async def _request_hook(request):
    scope = _ACTIVE.get()
    if scope is not None:
        try:
            await asyncio.to_thread(scope.capture, request)
        except Exception as exc:
            scope.emit("capture.failed", {"error_type": type(exc).__name__})
            logger.warning("[request_trace] 请求原文暂未保存，正常请求继续 (%s)", type(exc).__name__)


async def _response_hook(response):
    record = response.request.extensions.get("dafeiyu_request_trace")
    if record:
        scope = _ACTIVE.get()
        if scope is not None:
            try:
                await asyncio.to_thread(scope.http_response, record, response.status_code)
            except Exception as exc:
                logger.warning("[request_trace] HTTP 状态暂未保存 (%s)", type(exc).__name__)


_request_hook._dafeiyu_trace_hook = True
_response_hook._dafeiyu_trace_hook = True


class CallTrace:
    def __init__(self, base, create, *, call_id, source, provider, model, stream, turn=None):
        self.base = Path(base)
        self.enabled = settings(base)["enabled"]
        self.call_id, self.source = call_id, source
        self.turn = turn or {}
        self.meta = {"format": FORMAT, "call_id": call_id, "source": source,
                     "provider": provider, "model": model, "stream": bool(stream),
                     "turn_id": self.turn.get("turn_id", ""), "kind": self.turn.get("kind", "")}
        self.paths = []
        self.output = []
        self.chunks = []
        self.token = None
        if self.enabled:
            # The pinned OpenAI resource owns its SDK client, which owns HTTPX.
            sdk = getattr(getattr(create, "__self__", None), "_client", None)
            client = getattr(sdk, "_client", None)
            hooks = getattr(client, "event_hooks", None)
            if isinstance(hooks, dict):
                for kind, hook in (("request", _request_hook), ("response", _response_hook)):
                    existing = hooks.setdefault(kind, [])
                    if not any(getattr(item, "_dafeiyu_trace_hook", False) for item in existing):
                        existing.append(hook)
            else:
                self.emit("capture.unavailable", {"reason": "SDK HTTP client not available"})
        # An explicitly disabled nested call must not inherit an enabled parent.
        self.token = _ACTIVE.set(self if self.enabled else None)

    def deactivate(self):
        if self.token is not None:
            _ACTIVE.reset(self.token)
            self.token = None

    def emit(self, name, details=None):
        if self.enabled:
            event(self.base, self.source, name, turn_id=self.meta["turn_id"],
                  call_id=self.call_id, details=details)

    def capture(self, request):
        if not self.enabled:
            return
        body = request.content
        if "application/json" not in request.headers.get("content-type", ""):
            self.emit("capture.unavailable", {"reason": "request body is not JSON"})
            return
        if len(body) > settings(self.base)["max_total_mb"] * 1024**2:
            self.emit("capture.failed", {"reason": "single request exceeds storage budget", "bytes": len(body)})
            return
        payload = json.loads(body)
        if not isinstance(payload, dict) or "messages" not in payload:
            return
        ident = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f") + "-" + uuid.uuid4().hex[:16]
        root = trace_root(self.base)
        parent = root / "requests"
        if (root.is_symlink() or root.is_junction() or parent.is_symlink() or parent.is_junction()
                or not parent.resolve().is_relative_to(self.base.resolve())):
            raise ValueError("Redirected request trace directory")
        expected_parent = parent.resolve()
        path = expected_parent / ident
        path.mkdir(parents=True)
        meta = dict(self.meta, id=ident, at=time.time(), status="sending", capture_point="httpx.request",
                    model=payload.get("model", self.meta["model"]), request_bytes=len(body),
                    request_sha256=hashlib.sha256(body).hexdigest(), attempt=len(self.paths) + 1)
        try:
            with gzip.open(path / "request.json.gz", "wb", compresslevel=3) as stream:
                stream.write(body)
            _atomic(path / "index.html", view().request_page(payload, meta, path).encode("utf-8"))
            _atomic(path / "meta.json", _json_bytes(meta))
        except Exception:
            # Only this freshly created, checked recorder directory is disposable.
            if (not path.is_symlink() and not path.is_junction()
                    and path.resolve().parent == expected_parent
                    and expected_parent.is_relative_to(self.base.resolve())):
                shutil.rmtree(path)
            raise
        self.paths.append(path)
        request.extensions["dafeiyu_request_trace"] = ident
        self.emit("request.sent", {"request_id": ident, "bytes": len(body),
                                   "messages": len(payload.get("messages", []))})
        prune(self.base, ident)
        refresh_index(self.base)

    def http_response(self, record_id, status):
        path = next((p for p in self.paths if p.name == record_id), None)
        if path is None:
            return
        meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
        meta["http_status"] = status
        if status >= 400:
            meta["status"] = "http_error"
        _atomic(path / "meta.json", _json_bytes(meta))
        self.emit("response.http", {"request_id": record_id, "status": status})

    def _flush_chunks(self):
        chunks, self.chunks = self.chunks, []
        if chunks and self.paths:
            with gzip.open(self.paths[-1] / "response.jsonl.gz", "ab", compresslevel=3) as stream:
                for chunk in chunks:
                    stream.write((json.dumps(chunk, ensure_ascii=False) + "\n").encode("utf-8"))

    async def chunk(self, value):
        if not self.enabled:
            return
        try:
            data = _sdk_dump(value)
            if data is not None:
                self.chunks.append(data)
                self.output.append(_output_text(data))
                if len(self.chunks) >= 64:
                    await asyncio.to_thread(self._flush_chunks)
        except Exception as exc:
            self.emit("capture.response_failed", {"error_type": type(exc).__name__})

    def _finish(self, result, usage, error, elapsed, complete):
        self._flush_chunks()
        data = _sdk_dump(result)
        if data is not None:
            self.output.append(_output_text(data))
        status = "error" if error and error != "GeneratorExit" else "returned" if complete else "unknown_stream"
        if self.paths:
            path = self.paths[-1]
            meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
            meta.update(status=status, elapsed=elapsed, usage=_sdk_dump(usage),
                        error_type=error, output_text="".join(self.output))
            if data is not None:
                meta["response_file"] = "response.json.gz"
                meta["tool_calls"] = [tool for choice in data.get("choices", [])
                                      for tool in ((choice.get("message") or {}).get("tool_calls") or [])]
                with gzip.open(path / "response.json.gz", "wb", compresslevel=3) as stream:
                    stream.write(_json_bytes(data))
            elif (path / "response.jsonl.gz").exists():
                meta["response_file"] = "response.jsonl.gz"
            _atomic(path / "meta.json", _json_bytes(meta))
            page = (path / "index.html").read_text(encoding="utf-8")
            page = re.sub(r"<!-- TRACE_RESULT -->.*?<!-- /TRACE_RESULT -->",
                          lambda _: view().result_panel(meta), page, count=1, flags=re.DOTALL)
            _atomic(path / "index.html", page.encode("utf-8"))
        self.emit("request.finished", {"status": status, "elapsed_ms": round(elapsed * 1000),
                                       "error_type": error, "captured": len(self.paths)})
        prune(self.base)
        refresh_index(self.base)

    async def finish(self, *, result=None, usage=None, error="", elapsed=0, complete=True):
        if self.enabled:
            try:
                await asyncio.to_thread(self._finish, result, usage, error, elapsed, complete)
            except Exception as exc:
                self.emit("capture.finish_failed", {"error_type": type(exc).__name__})
                logger.warning("[request_trace] 记录收尾暂未完成 (%s)", type(exc).__name__)

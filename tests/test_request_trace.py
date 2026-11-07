"""Private trace files are opt-in, lossless, escaped and independently bounded."""

import base64
import gzip
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "overlays/astrbot/astrbot/core/utils"))
sys.path.insert(0, str(ROOT / "tools"))
import chat_efficiency
import request_trace_cli

trace = chat_efficiency.trace_module()


class RequestTraceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name) / "private"

    def scope(self, source="chat"):
        return trace.CallTrace(self.base, lambda: None, call_id="call-1", source=source,
                               provider="test", model="test-model", stream=False,
                               turn={"turn_id": "turn-1", "kind": "image"})

    def capture(self, text="原文 <script>alert(1)</script>", source="chat"):
        image = "data:image/png;base64," + base64.b64encode(b"image-bytes").decode()
        payload = {"model": "test-model", "messages": [
            {"role": "system", "content": "系统提示"},
            {"role": "user", "content": "旧话题"},
            {"role": "assistant", "content": "旧回复"},
            {"role": "user", "content": [{"type": "text", "text": text},
                {"type": "image_url", "image_url": {"url": image}}]},
        ]}
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        scope = self.scope(source)
        try:
            scope.capture(SimpleNamespace(content=body, headers={"content-type": "application/json",
                "authorization": "Bearer PRIVATE_AUTH"}, extensions={}))
        finally:
            scope.deactivate()
        return scope, body

    def test_fresh_install_is_off_and_does_not_create_capture_files(self):
        self.assertFalse(trace.settings(self.base)["enabled"])
        scope, _ = self.capture()
        self.assertFalse(scope.enabled)
        trace.event(self.base, "qq", "turn.received", details={"kind": "image"})
        trace.refresh_index(self.base)
        self.assertFalse(self.base.exists())

    def test_switch_persists_limits_and_off_preserves_existing_records(self):
        trace.configure(self.base, True, retention_days=3, max_total_mb=64)
        scope, body = self.capture()
        trace.configure(self.base, False)
        self.assertEqual(trace.settings(self.base), dict(trace.DEFAULTS, enabled=False,
                                                       retention_days=3, max_total_mb=64))
        self.assertEqual(len(trace.records(self.base)), 1)
        self.assertEqual(gzip.decompress((scope.paths[0] / "request.json.gz").read_bytes()), body)

    def test_raw_body_is_byte_exact_and_readable_view_escapes_text_and_extracts_images(self):
        trace.configure(self.base, True)
        scope, body = self.capture()
        path = scope.paths[0]
        self.assertEqual(trace.export_request(self.base).read_bytes(), body)
        html = (path / "index.html").read_text(encoding="utf-8")
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertNotIn("<script>", html)
        self.assertNotIn("data:image/png;base64,", html)
        self.assertIn("历史上下文 · 2 条", html)
        self.assertIn("<details><summary>历史上下文", html)
        self.assertIn("本轮输入", html)
        self.assertEqual(next((path / "media").iterdir()).read_bytes(), b"image-bytes")
        self.assertNotIn(b"PRIVATE_AUTH", trace.export_request(self.base).read_bytes())
        self.assertNotIn("PRIVATE_AUTH", (path / "meta.json").read_text())

    def test_finished_result_and_source_events_are_correlated(self):
        trace.configure(self.base, True)
        scope, _ = self.capture(source="memory")
        data = {"choices": [{"message": {"content": "这是模型结果"}}]}
        scope._finish(data, {"prompt_tokens": 5, "completion_tokens": 2}, "", 0.25, True)
        meta = trace.records(self.base)[0]
        self.assertEqual((meta["source"], meta["call_id"], meta["turn_id"]), ("memory", "call-1", "turn-1"))
        self.assertEqual(meta["output_text"], "这是模型结果")
        self.assertIn("这是模型结果", (scope.paths[0] / "index.html").read_text(encoding="utf-8"))
        events = trace.recent_events(self.base)
        self.assertTrue(all(e["source"] == "memory" and e["call_id"] == "call-1" for e in events))
        self.assertTrue((trace.trace_root(self.base) / "index.html").is_file())

    def test_pruning_removes_only_owned_records_and_protects_unrelated_directories(self):
        trace.configure(self.base, True, max_records=1)
        first, _ = self.capture("first")
        first._finish(None, None, "", 0, True)
        unknown = trace.trace_root(self.base) / "requests/user-files"
        unknown.mkdir()
        (unknown / "keep.txt").write_text("preserve")
        second, _ = self.capture("second")
        second._finish(None, None, "", 0, True)
        self.assertFalse(first.paths[0].exists())
        self.assertTrue(second.paths[0].exists())
        self.assertEqual((unknown / "keep.txt").read_text(), "preserve")

    def test_expired_records_are_removed_and_invalid_ids_cannot_export_arbitrary_files(self):
        trace.configure(self.base, True, retention_days=1)
        scope, _ = self.capture()
        path = scope.paths[0] / "meta.json"
        meta = json.loads(path.read_text())
        meta.update(at=time.time() - 2 * 86400, status="returned")
        path.write_text(json.dumps(meta), encoding="utf-8")
        trace.prune(self.base)
        self.assertFalse(scope.paths[0].exists())
        with self.assertRaises(ValueError):
            trace.export_request(self.base, "../../config/settings.json")

    def test_cli_defaults_to_status_and_only_explicit_on_changes_switch(self):
        runtime = Path(self.temp.name) / "runtime"
        base = runtime / "astrbot/data/plugin_data/dafeiyu_context"
        with patch.object(request_trace_cli, "RUNTIME_ROOT", runtime), patch("builtins.print"):
            self.assertEqual(request_trace_cli.main([]), 0)
            self.assertFalse(base.exists())
            self.assertEqual(request_trace_cli.main(["on", "--keep-days", "5"]), 0)
            self.assertTrue(trace.settings(base)["enabled"])
            self.assertEqual(request_trace_cli.main(["off"]), 0)
            self.assertFalse(trace.settings(base)["enabled"])


if __name__ == "__main__":
    unittest.main()

"""Use the real SDK with MockTransport to verify capture at the HTTP boundary."""

import asyncio
import gzip
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import sys

try:
    import httpx
    from openai import AsyncOpenAI, AuthenticationError
except ImportError:
    AsyncOpenAI = None

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "overlays/astrbot/astrbot/core/utils"))
import chat_efficiency as policy


@unittest.skipIf(AsyncOpenAI is None, "Run with the installed runtime SDK dependencies")
class RequestWireTraceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.ledger = self.base / "context.db"
        self.trace = policy.trace_module()
        self.trace.configure(self.base, True)
        self.provider = SimpleNamespace(provider_config={"id": "wire-test"})
        self.wire = []

    async def client(self, handler):
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = AsyncOpenAI(api_key="PRIVATE_AUTH_SECRET", base_url="https://provider.invalid/v1",
                             http_client=http, max_retries=0)
        self.addAsyncCleanup(client.close)
        return client

    def response(self, request):
        self.wire.append(request.content)
        # The recorder must finish writing before transport handles the request.
        rows = self.trace.records(self.base)
        self.assertTrue(any(gzip.decompress((self.trace.trace_root(self.base) / "requests" /
                                            row["id"] / "request.json.gz").read_bytes()) == request.content
                            for row in rows))
        return httpx.Response(200, json={"id": "reply", "object": "chat.completion", "created": 0,
            "model": "test", "choices": [{"index": 0, "message": {"role": "assistant", "content": "正常结果"},
                                          "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}})

    async def call(self, client, **kwargs):
        return await policy.metered_chat_completion(self.provider, client.chat.completions.create,
            usage_path=self.ledger, model="test", messages=[{"role": "user", "content": "原始消息"}], **kwargs)

    async def test_actual_merged_body_is_captured_and_auth_headers_and_query_are_excluded(self):
        client = await self.client(self.response)
        await self.call(client, source_label="chat", stream=False,
                        extra_body={"custom": {"enabled": True}},
                        extra_headers={"X-Secret": "PRIVATE_HEADER"}, extra_query={"token": "PRIVATE_QUERY"})
        self.assertEqual(self.trace.export_request(self.base).read_bytes(), self.wire[0])
        self.assertEqual(json.loads(self.wire[0])["custom"], {"enabled": True})
        row = self.trace.records(self.base)[0]
        self.assertEqual(row["capture_point"], "httpx.request")
        with closing(sqlite3.connect(self.ledger)) as con:
            self.assertEqual(con.execute("SELECT id FROM calls").fetchone()[0], row["call_id"])
        readable = "".join(p.read_text(encoding="utf-8") for p in self.base.rglob("*")
                           if p.suffix in {".html", ".json", ".jsonl"})
        for secret in ("PRIVATE_AUTH_SECRET", "PRIVATE_HEADER", "PRIVATE_QUERY"):
            self.assertNotIn(secret, readable)
            self.assertNotIn(secret.encode(), self.wire[0])

    async def test_concurrent_sources_do_not_cross_and_hook_is_not_duplicated(self):
        client = await self.client(self.response)
        await asyncio.gather(self.call(client, source_label="chat"), self.call(client, source_label="memory"))
        self.assertEqual(len(self.wire), 2)
        self.assertEqual({r["source"] for r in self.trace.records(self.base)}, {"chat", "memory"})
        self.assertTrue(all(r["status"] == "returned" for r in self.trace.records(self.base)))
        self.assertEqual(len(client._client.event_hooks["request"]), 1)

    async def test_stream_is_preserved_and_its_sdk_chunks_are_saved(self):
        def handler(request):
            self.wire.append(request.content)
            items = [
                {"id": "s", "object": "chat.completion.chunk", "created": 0, "model": "test",
                 "choices": [{"index": 0, "delta": {"content": "你好"}, "finish_reason": None}]},
                {"id": "s", "object": "chat.completion.chunk", "created": 0, "model": "test",
                 "choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}},
            ]
            body = "".join("data: " + json.dumps(v) + "\n\n" for v in items) + "data: [DONE]\n\n"
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())
        client = await self.client(handler)
        stream = await self.call(client, stream=True)
        chunks = [chunk async for chunk in stream]
        self.assertEqual(chunks[0].choices[0].delta.content, "你好")
        row = self.trace.records(self.base)[0]
        self.assertEqual(row["status"], "returned")
        self.assertEqual(row["output_text"], "你好")
        path = self.trace.trace_root(self.base) / "requests" / row["id"] / "response.jsonl.gz"
        with gzip.open(path, "rt", encoding="utf-8") as f:
            self.assertEqual(len(f.readlines()), 2)
        self.assertEqual(self.trace.export_request(self.base).read_bytes(), self.wire[0])

    async def test_capture_failure_cannot_block_or_retry_a_paid_request(self):
        client = await self.client(lambda request: httpx.Response(200, json={
            "id": "r", "object": "chat.completion", "created": 0, "model": "test",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]}))
        with patch.object(self.trace.CallTrace, "capture", side_effect=PermissionError("private details")) as capture:
            result = await self.call(client)
        self.assertEqual(result.choices[0].message.content, "ok")
        self.assertEqual(capture.call_count, 1)
        self.assertEqual(self.trace.records(self.base), [])

    async def test_http_error_is_recorded_without_copying_exception_credentials(self):
        client = await self.client(lambda request: httpx.Response(401, json={
            "error": {"message": "PRIVATE_AUTH_SECRET", "type": "invalid_api_key"}}))
        with self.assertRaises(AuthenticationError):
            await self.call(client)
        row = self.trace.records(self.base)[0]
        self.assertEqual((row["http_status"], row["status"], row["error_type"]),
                         (401, "error", "AuthenticationError"))
        self.assertNotIn("PRIVATE_AUTH_SECRET", json.dumps(row))


if __name__ == "__main__":
    unittest.main()

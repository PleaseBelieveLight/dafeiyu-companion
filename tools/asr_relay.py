#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""云端 ASR 中转：接收 Windows 录到的 WAV，调用百炼 qwen3-asr-flash 返回文字。

只监听 127.0.0.1:8012；Windows 侧通过 SSH `-L 8012:127.0.0.1:8012` 访问，
不暴露公网。API key 从 AstrBot 的 cmd_config.json 读取，不落到 Windows。
"""

import base64
import json
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONFIG = "/opt/dafeiyu/astrbot/data/cmd_config.json"
API_URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
MAX_BYTES = 8 * 1024 * 1024


def load_key() -> str:
    with open(CONFIG, encoding="utf-8") as f:
        cfg = json.load(f)
    for prov in cfg.get("provider", []):
        if prov.get("id") == "dashscope_tts" and prov.get("api_key"):
            return prov["api_key"]
    raise RuntimeError("dashscope_tts api_key not found")


def transcribe(wav: bytes) -> str:
    payload = {
        "model": "qwen3-asr-flash",
        "input": {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"audio": "data:audio/wav;base64," + base64.b64encode(wav).decode("ascii")}
                    ],
                }
            ]
        },
        "parameters": {"result_format": "message"},
    }
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + load_key(),
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    choices = (data.get("output") or {}).get("choices") or []
    if not choices:
        raise RuntimeError("ASR 返回异常: " + str(data)[:200])
    content = (choices[0].get("message") or {}).get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(
            str(item.get("text", ""))
            for item in content
            if isinstance(item, dict) and item.get("text")
        ).strip()
    return ""


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/asr":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BYTES:
            self.send_error(413)
            return
        wav = self.rfile.read(length)
        try:
            body = json.dumps(
                {"status": "success", "text": transcribe(wav)},
                ensure_ascii=False,
            ).encode("utf-8")
            code = 200
        except Exception as exc:  # noqa: BLE001 - 返回给调用方排查
            body = json.dumps(
                {"status": "error", "error": str(exc)[:300]},
                ensure_ascii=False,
            ).encode("utf-8")
            code = 500
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print("[asr_relay] " + (fmt % args), flush=True)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8012), Handler).serve_forever()

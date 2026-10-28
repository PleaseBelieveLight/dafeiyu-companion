"""Run the repository ASR relay with this local installation's settings."""

import json
import runpy
from http.server import ThreadingHTTPServer

from workspace import RUNTIME_ROOT as ROOT, TOOLS

module = runpy.run_path(str(TOOLS / "asr_relay.py"))


def load_key():
    value = json.loads((ROOT / "config/settings.json").read_text(encoding="utf-8-sig"))[
        "dashscope_api_key"
    ].strip()
    if not value:
        raise RuntimeError("请先在配置向导填写阿里云百炼 API Key")
    return value


module["transcribe"].__globals__["load_key"] = load_key
ThreadingHTTPServer(("127.0.0.1", 8012), module["Handler"]).serve_forever()

"""Bounded local administration MCP; never returns credentials."""

import json
import re
import sqlite3
import subprocess
from fastmcp import FastMCP
from fastmcp.utilities.types import Image
import psutil

from workspace import RUNTIME_ROOT as ROOT, SOURCE_ROOT, TOOLS, runtime_env
mcp = FastMCP("DafeiyuAdmin")


@mcp.tool()
def health() -> dict:
    """Inspect local service ports and memory without model usage."""
    wanted = {6185, 6099, 6199, 11435, 8009, 8010, 8012}
    connected = psutil.net_connections(kind="tcp")
    return {
        "listening_ports": sorted(
            {x.laddr.port for x in connected if x.status == "LISTEN" and x.laddr.port in wanted}
        ),
        "qq_websocket_connected": any(
            x.status == "ESTABLISHED" and x.laddr.port == 6199 for x in connected
        ),
        "free_disk_gb": round(psutil.disk_usage(str(ROOT)).free / 2**30, 1),
        "available_memory_gb": round(psutil.virtual_memory().available / 2**30, 1),
    }


@mcp.tool()
def logs(service: str = "astrbot", tail: int = 60) -> str:
    """Read a bounded, redacted tail of one installation log."""
    if service not in {"astrbot", "napcat", "phone", "browser", "asr", "ollama", "supervisor"}:
        return "Unknown service"
    path = ROOT / "logs" / (service + ".log")
    if not path.exists():
        return "Log does not exist"
    with path.open("rb") as stream:
        stream.seek(max(0, path.stat().st_size - 64000))
        text = stream.read().decode("utf-8", errors="replace")
    for config in ["settings.json", "local-access.json"]:
        values = json.loads((ROOT / "config" / config).read_text(encoding="utf-8"))
        for key, value in values.items():
            if (
                isinstance(value, str)
                and len(value) > 6
                and any(part in key for part in ["key", "token", "password"])
            ):
                text = text.replace(value, "[redacted]")
    text = re.sub(r"(token=|txz\.qq\.com/p\?k=)[^\s\"<>]+", r"\1[redacted]", text)
    return "\n".join(text.splitlines()[-max(1, min(tail, 200)) :])


@mcp.tool()
def plugins() -> list[str]:
    """List locally installed plugins."""
    return sorted(p.name for p in (ROOT / "astrbot/data/plugins").iterdir() if p.is_dir())


@mcp.tool()
def cron_jobs() -> list[dict]:
    """Read scheduled jobs with a fixed read-only database query."""
    path = ROOT / "astrbot/data/data_v4.db"
    if not path.exists():
        return []
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in con.execute(
                "SELECT name,cron_expression,timezone,enabled,next_run_time FROM cron_jobs"
            )
        ]


@mcp.tool()
def webui_info() -> dict:
    """Return management addresses without tokens."""
    return {
        "astrbot": "http://127.0.0.1:6185",
        "napcat": "http://127.0.0.1:6099",
        "setup": str(SOURCE_ROOT / "配置向导.cmd"),
    }


@mcp.tool()
def qr():
    """Read the latest local QQ login QR code, when available."""
    files = list((ROOT / "napcat").rglob("qrcode.png"))
    if not files:
        return "QQ login QR code is not available yet"
    return Image(path=str(max(files, key=lambda p: p.stat().st_mtime)))


@mcp.tool()
def restart(confirm: str) -> str:
    """Restart this installation only after an explicit RESTART confirmation."""
    if confirm != "RESTART":
        return "Confirmation required: RESTART"
    subprocess.Popen(
        [
            str(ROOT / "runtime/astrbot/Scripts/pythonw.exe"),
            str(TOOLS / "control.py"),
            "restart",
        ],
        env=runtime_env(),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return "Restart requested"


@mcp.tool()
def heal_ws(confirm: str) -> str:
    """Repair the configured QQ WebSocket after explicit HEAL confirmation."""
    if confirm != "HEAL":
        return "Confirmation required: HEAL"
    result = subprocess.run(
        [str(ROOT / "runtime/astrbot/Scripts/python.exe"), str(TOOLS / "control.py"), "heal"],
        capture_output=True,
        text=True,
        timeout=30,
        env=runtime_env(),
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    return result.stdout.strip()


if __name__ == "__main__":
    mcp.run(transport="stdio")

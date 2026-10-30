#!/usr/bin/env python
"""Browser Use MCP server（只读）——给云端大肥鱼用。

架构与 PhoneMCP 相同：

    Windows 本机
      BrowserMCP 127.0.0.1:8010/Browser
            │
            │ ssh -R 8010:127.0.0.1:8010
            ▼
    云端主机
      sshd 127.0.0.1:8010
            ▲
      browser_mcp_proxy.py 172.18.0.1:8010 → 127.0.0.1:8010
            ▲
      AstrBot 容器

对 AstrBot 只暴露一个高层工具 `browser_task`：大肥鱼把“要做什么”交给本机
的 browser-use Agent，由它在独立 Chrome（无登录态）里完成只读任务并返回文本。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from workspace import RUNTIME_ROOT
from model_usage import attach_browser_metering

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

from browser_use import Agent, Browser, ChatDeepSeek
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

HERE = Path(__file__).resolve().parent

HOST = os.environ.get("BROWSER_MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("BROWSER_MCP_PORT", "8010"))
SSE_PATH = os.environ.get("BROWSER_MCP_SSE_PATH", "/Browser")
MESSAGE_PATH = os.environ.get("BROWSER_MCP_MESSAGE_PATH", "/Browser/messages/")

MODEL = os.environ.get("BROWSER_MODEL", "deepseek-flash")
HEADLESS = os.environ.get("BROWSER_HEADLESS", "1").lower() not in {"0", "false", "no"}
MAX_STEPS_DEFAULT = int(os.environ.get("BROWSER_MAX_STEPS", "15"))
TASK_TIMEOUT = int(os.environ.get("BROWSER_TASK_TIMEOUT", "240"))

CHROME_PATH = os.environ.get(
    "BROWSER_CHROME_PATH", r"C:\Program Files\Google\Chrome\Application\chrome.exe"
)
PROFILE_DIR = Path(
    os.environ.get("BROWSER_PROFILE_DIR", str(RUNTIME_ROOT / "browser" / "chrome-profile"))
)
ALLOWLIST_FILE = HERE / "browser_allowlist.txt"

READ_ONLY_POLICY = """你是大肥鱼的只读网页助手。必须遵守：
1. 只允许浏览、搜索、读取、提取信息。
2. 禁止登录、注册、支付、下单、购买、充值、发帖、评论、点赞、投币、收藏、私信、删除、修改账户设置。
3. 如果任务要求上述写操作，直接拒绝并在最终结果里说明。
4. 不要下载或执行任何文件。
5. 完成后用中文简短汇报，附上关键链接。
"""


class ChatDeepSeekNoThinking(ChatDeepSeek):
    """deepseek-flash 默认开思考，会拒绝 browser-use 的 tool_choice。

    browser-use 的适配器只在模型名含 deepseek-v4 时才发送 thinking 参数；
    这里强制它发送 thinking=disabled，让 deepseek-flash 能走 function calling。
    """

    def _supports_thinking(self) -> bool:
        return True

    def _client(self):
        return attach_browser_metering(super()._client())


mcp = MCPServer(
    "BrowserMCP",
    instructions="只读浏览器助手：在独立 Chrome 中浏览、搜索、提取网页信息。",
)
_task_lock = asyncio.Lock()


def _load_allowlist() -> list[str] | None:
    if not ALLOWLIST_FILE.exists():
        return None
    domains = []
    for line in ALLOWLIST_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            domains.append(line)
    return domains or None


@mcp.tool()
async def browser_task(task: str, max_steps: int = MAX_STEPS_DEFAULT) -> str:
    """在 Windows 本机的独立 Chrome 中执行一个只读网页任务。

    Args:
        task: 中文任务描述，例如“打开 B 站搜索某个主题，返回前三条视频标题和链接”。
        max_steps: 最大执行步数，默认 15；复杂任务可调到 25。

    Returns:
        任务的最终结果（中文文本）；失败或超时返回错误说明。
    """
    if not task or not task.strip():
        return "任务描述为空。"
    if len(task) > 2000:
        return "任务描述过长（上限 2000 字）。"
    steps = max(1, min(int(max_steps), 40))

    async with _task_lock:
        try:
            return await asyncio.wait_for(_run_task(task.strip(), steps), timeout=TASK_TIMEOUT)
        except asyncio.TimeoutError:
            return f"任务超时（{TASK_TIMEOUT} 秒），已中止。"
        except Exception as exc:
            return f"浏览器任务失败：{type(exc).__name__}: {exc}"


async def _run_task(task: str, max_steps: int) -> str:
    browser = Browser(
        executable_path=CHROME_PATH,
        headless=HEADLESS,
        user_data_dir=str(PROFILE_DIR),
        enable_default_extensions=False,
        allowed_domains=_load_allowlist(),
    )
    try:
        agent = Agent(
            task=READ_ONLY_POLICY + "\n\n任务：" + task,
            llm=ChatDeepSeekNoThinking(model=MODEL),
            browser=browser,
            use_vision=False,
            use_judge=False,
            enable_signal_handler=False,
            max_failures=3,
            step_timeout=60,
        )
        result = await agent.run(max_steps=max_steps)
        final = result.final_result()
        return final or "任务执行完毕，但没有返回文本结果。"
    finally:
        try:
            await browser.kill()
        except Exception:
            pass


@mcp.tool()
async def browser_health() -> str:
    """检查 Browser Use 服务是否就绪（不执行网页任务，不消耗模型 token）。"""
    allow = _load_allowlist()
    return (
        f"ok; model={MODEL}; headless={HEADLESS}; profile={PROFILE_DIR}; "
        f"allowlist={len(allow) if allow else 'off'}; chrome={CHROME_PATH}"
    )


if __name__ == "__main__":
    print(f"BrowserMCP listening on {HOST}:{PORT} (sse={SSE_PATH})", flush=True)
    mcp.run(
        transport="sse",
        host=HOST,
        port=PORT,
        sse_path=SSE_PATH,
        message_path=MESSAGE_PATH,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

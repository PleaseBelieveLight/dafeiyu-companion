"""Use the shared ledger from the separate browser environment without importing AstrBot."""

import importlib.util
from functools import lru_cache
from types import SimpleNamespace

from workspace import RUNTIME_ROOT, SOURCE_ROOT


@lru_cache(maxsize=1)
def usage_module():
    spec = importlib.util.spec_from_file_location(
        "dafeiyu_browser_usage",
        SOURCE_ROOT / "overlays/astrbot/astrbot/core/utils/chat_efficiency.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def attach_browser_metering(client, path=None):
    """Meter real SDK attempts, with Agent retries replacing hidden SDK retries."""
    original = client.chat.completions.create
    path = path or RUNTIME_ROOT / "astrbot/data/plugin_data/dafeiyu_context/context.db"
    provider = SimpleNamespace(provider_config={"id": "browser_deepseek"})
    client.max_retries = 0

    async def create(**kwargs):
        return await usage_module().metered_chat_completion(
            provider, original, usage_path=path, source_label="browser", **kwargs
        )

    client.chat.completions.create = create
    return client

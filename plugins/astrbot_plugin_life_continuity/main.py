"""Storage-only plugin lifecycle; no hooks, tools, model calls or scheduler yet."""

from astrbot.api import logger
from astrbot.api.star import Context, Star, StarTools, register

from .state import PLUGIN_NAME, LifeStore


@register(PLUGIN_NAME, "dafeiyu-tools", "自主生活持久化基础，默认关闭", "0.1.0")
class LifeContinuityPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self.store = None

    async def initialize(self):
        if self.config.get("enabled") is not True or self.store is not None:
            return
        try:
            self.store = LifeStore(StarTools.get_data_dir(PLUGIN_NAME))
            logger.info("[life_continuity] 存储已就绪；当前阶段没有自主调度。")
        except Exception:
            # Keep an unavailable optional store from taking down normal chat.
            logger.exception("[life_continuity] 存储初始化失败，自主生活保持不可用。")

    async def terminate(self):
        if self.store is not None:
            self.store.close()
            self.store = None

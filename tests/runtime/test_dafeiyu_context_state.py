"""Dynamic program state is scoped, temporary and leaves the cache prefix stable."""

import importlib
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.agent.message import Message, dump_messages_with_checkpoints
from tests.test_dafeiyu_voice_mode import face_module, fixture, temporary_text

schedule_module = importlib.import_module("data.plugins.astrbot_plugin_schedule_awareness.main")


class ContextStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_media_clock_changes_only_the_temporary_part(self):
        plugin, event, _, _, _, _ = fixture("", {"tts_enabled": False})
        request = ProviderRequest(system_prompt="stable persona")
        with patch.object(face_module.sp, "get_async", new=AsyncMock(return_value={"tts_enabled": False})):
            await plugin.inject_voice_status(event, request)
            before = request.system_prompt
            await plugin.inject_voice_status(event, request)
        self.assertEqual(request.system_prompt, before)
        self.assertNotRegex(request.system_prompt, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}")
        self.assertIn("本次不允许语音", temporary_text(request))
        user = await request.assemble_context()
        self.assertIn("本次不允许语音", str(user))
        # Use the framework's real history cleanup to validate the marker shape.
        cleaned = dump_messages_with_checkpoints([Message.model_validate(user)])
        self.assertNotIn("本次不允许语音", str(cleaned))

    async def test_schedule_refresh_removes_stale_state_and_preserves_other_plugins(self):
        plugin = schedule_module.ScheduleAwarenessPlugin(object())
        event = SimpleNamespace(unified_msg_origin="test:FriendMessage:1")
        request = ProviderRequest(system_prompt="stable persona")
        request.extra_user_content_parts.append({"type": "text", "text": "other plugin", "_no_save": True})
        with patch.object(plugin, "_load_jobs", return_value=[]), patch.object(
            plugin, "_format_schedule", side_effect=["new schedule", "", "latest schedule"]
        ):
            await plugin.inject_schedule(event, request)
            self.assertIn("new schedule", temporary_text(request))
            await plugin.inject_schedule(event, request)
            self.assertNotIn("new schedule", temporary_text(request))
            await plugin.inject_schedule(event, request)
        self.assertEqual(request.system_prompt, "stable persona")
        self.assertIn("latest schedule", temporary_text(request))
        self.assertIn("other plugin", temporary_text(request))


if __name__ == "__main__":
    unittest.main()

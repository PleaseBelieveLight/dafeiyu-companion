"""Exercise scene references through real command, tool and plugin generation paths."""

import copy
import importlib
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.core.utils.selfie_reference import BASE_REFERENCE, SCENE_KEYWORDS
from tests import test_dafeiyu_selfie_context as context_fixture

persona_module = importlib.import_module(
    "data.plugins.astrbot_plugin_omnidraw.core.persona_manager"
)


class SelfieReferenceIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = context_fixture.SelfieContextTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.omni = self.fixture.omni
        self.event = self.fixture.event
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.omni.data_dir = self.folder.name
        self.references = Path(self.folder.name) / "selfie_references"
        self.references.mkdir()
        # Tests run against an installation; reuse its public images without loading models.
        installed = Path(get_astrbot_data_path()) / "plugin_data/astrbot_plugin_omnidraw"
        self.base = Path(self.folder.name) / BASE_REFERENCE
        self.base.parent.mkdir(parents=True)
        shutil.copy2(installed / BASE_REFERENCE, self.base)
        for scene in SCENE_KEYWORDS:
            filename = f"{scene}.png"
            shutil.copy2(installed / "selfie_references" / filename, self.references / filename)
        self.config = self.omni.plugin_config
        self.config.active_persona_id = "dafeiyu"
        self.config.persona_base_prompt = "蓝色鲸鱼娘，大肥鱼"
        self.config.persona_ref_image = self.config.persona_ref_images[0]
        self.omni.persona_manager = persona_module.PersonaManager(self.config)

        async def process(refs, session=None):
            return list(refs)

        self.omni._process_and_save_images = AsyncMock(side_effect=process)

    def expected(self, scene):
        return str((self.references / f"{scene}.png").resolve())

    async def test_command_selects_sleeping_image_and_preserves_followup(self):
        self.omni.cmd_parser.parse.return_value = ("睡衣晚安", {})
        self.omni.prompt_optimizer.optimize.return_value = ["睡衣晚安"]
        replies = [item async for item in self.omni.cmd_selfie(self.event)]
        self.assertEqual(len(replies), 1)
        args = self.fixture.generate.await_args
        self.assertEqual(args.kwargs["user_refs"], [self.expected("sleeping")])
        self.assertNotIn("persona_ref", args.kwargs)
        self.assertIn("采用所附角色参考图的外貌特征", args.args[1])
        self.fixture.vision.text_chat.assert_awaited_once()

    async def test_tool_selects_dancing_image_and_sends_exactly_once(self):
        self.omni._normalize_count = MagicMock(return_value=1)
        self.omni._send_generated_images = AsyncMock(return_value=1)
        self.omni.prompt_optimizer.optimize.return_value = ["全身跳个舞"]
        result = await self.omni.tool_generate_selfie(self.event, action="全身跳个舞")
        self.assertIn("已成功生成并下发了 1 张图", result)
        self.assertEqual(
            self.fixture.generate.await_args.kwargs["user_refs"], [self.expected("dancing")]
        )
        self.omni._send_generated_images.assert_awaited_once()

    async def test_user_images_remain_alongside_selected_persona_reference(self):
        self.omni._get_event_images.return_value = ["user-photo.png"]
        self.omni.prompt_optimizer.optimize.return_value = ["坐在窗边看书，放松微笑"]
        await self.omni._run_selfie_generation(self.event, "坐在窗边看书，放松微笑", 1)
        kwargs = self.fixture.generate.await_args.kwargs
        self.assertEqual(kwargs["user_refs"], ["user-photo.png"])
        self.assertEqual(kwargs["persona_ref"], self.expected("sitting"))

    async def test_plugin_batches_use_scene_references_without_mutating_persona(self):
        before = copy.deepcopy(vars(self.config))
        self.omni.prompt_optimizer.optimize.return_value = ["被窝自拍", "被窝自拍"]
        result = await self.omni._run_selfie_generation(self.event, "被窝自拍", 2)
        self.assertEqual(result["requested_count"], 2)
        self.assertEqual(len(result["results"]), 2)
        for call in self.fixture.generate.await_args_list:
            self.assertEqual(call.kwargs["user_refs"], [self.expected("sleeping")])
        self.assertEqual(vars(self.config), before)

    async def test_other_persona_keeps_original_reference(self):
        self.config.active_persona_id = "other"
        self.omni.prompt_optimizer.optimize.return_value = ["睡衣晚安"]
        await self.omni._run_selfie_generation(self.event, "睡衣晚安", 1)
        args = self.fixture.generate.await_args
        self.assertEqual(args.kwargs["user_refs"], ["reference-only.png"])
        self.assertNotIn("采用所附角色参考图的外貌特征", args.args[1])


if __name__ == "__main__":
    unittest.main()

"""Verify scene routing, packaged assets and checked upstream substitutions offline."""

import copy
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "overlays/astrbot/astrbot/core/utils"))
from install import install_character_assets
from patch_selfie_references import apply_selfie_references
from selfie_reference import BASE_REFERENCE, SCENE_KEYWORDS, select_selfie_reference


class SelfieReferenceTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.data_dir = Path(self.folder.name)
        self.refs = self.data_dir / "selfie_references"
        self.refs.mkdir()
        for scene in SCENE_KEYWORDS:
            (self.refs / f"{scene}.png").touch()
        self.base = self.data_dir / BASE_REFERENCE
        self.base.parent.mkdir(parents=True)
        self.base.touch()
        self.config = SimpleNamespace(
            active_persona_id="dafeiyu", persona_ref_images=["original.png"]
        )

    def test_scenes_select_all_seven_packaged_references(self):
        cases = {
            "waving": "在公园向镜头招手",
            "eating": "坐在餐桌前吃饭",
            "confused": "一脸困惑地挠头",
            "surprised": "露出惊讶的表情",
            "sleeping": "穿着睡衣准备睡觉",
            "dancing": "全身跳个舞，尾巴轻轻扬起",
            "sitting": "坐在窗边微笑着看书",
        }
        self.assertEqual(set(cases), set(SCENE_KEYWORDS))
        for style, action in cases.items():
            with self.subTest(style=style):
                result = select_selfie_reference(self.config, action, self.data_dir)
                self.assertEqual(result.style, style)
                self.assertEqual(len(result.images), 1)
                self.assertTrue(Path(result.images[0]).samefile(
                    self.refs / f"{style}.png"
                ))
                self.assertIn("以本次要求为准", result.prompt_suffix)

    def test_actions_use_scene_images_and_other_requests_use_base(self):
        for action, style in [
            ("晚上在公园散步", "base"),
            ("晚上在街上全身跳舞", "dancing"),
            ("夜晚坐在餐桌吃饭", "eating"),
            ("Q版眨眼", "base"),
            ("Q版睡衣晚安", "sleeping"),
            ("full-body dancing", "dancing"),
            ("转一圈，得意地笑", "dancing"),
            ("转一小圈，得意地笑", "dancing"),
            ("坐在窗边准备睡觉", "sleeping"),
            ("坐着露出惊讶表情", "surprised"),
            ("窗边看手机，放松微笑", "sitting"),
            ("reading by the window", "sitting"),
            ("good night", "sleeping"),
            ("Confused", "confused"),
            ("a shocked expression", "surprised"),
            ("wave to the camera", "waving"),
            ("blue wavelength", "base"),
            ("", "base"),
        ]:
            with self.subTest(action=action):
                self.assertEqual(
                    select_selfie_reference(self.config, action, self.data_dir).style, style
                )

    def test_missing_scene_falls_back_to_base_then_configured_reference(self):
        (self.refs / "sleeping.png").unlink()
        (self.refs / "15_睡衣晚安.png").touch()
        base = select_selfie_reference(self.config, "晚安", self.data_dir)
        self.assertEqual(base.style, "base")
        self.assertEqual(base.images, (str(self.base.resolve()),))
        self.base.unlink()
        result = select_selfie_reference(self.config, "晚安", self.data_dir)
        self.assertEqual(result.images, ("original.png",))
        self.assertEqual(result.prompt_suffix, "")

    def test_routing_never_mutates_config_or_another_persona(self):
        before = copy.deepcopy(vars(self.config))
        night = select_selfie_reference(self.config, "晚安", self.data_dir)
        day = select_selfie_reference(self.config, "全身站姿", self.data_dir)
        self.assertNotEqual(night.images, day.images)
        self.assertEqual(vars(self.config), before)
        self.config.active_persona_id = "other"
        self.assertEqual(
            select_selfie_reference(self.config, "晚安", self.data_dir).images,
            ("original.png",),
        )

    def test_source_assets_are_pngs_and_need_no_external_checkout(self):
        assets = ROOT / "assets/character"
        sources = {assets / "base.png": self.base}
        sources.update({
            assets / "scenes" / f"{scene}.png": self.refs / f"{scene}.png"
            for scene in SCENE_KEYWORDS
        })
        self.assertEqual(set((ROOT / "assets").rglob("*.png")), set(sources))
        legacy = self.refs / "01_招手微笑.png"
        legacy.write_bytes(b"previous installation")
        install_character_assets(self.data_dir)
        install_character_assets(self.data_dir)
        for source, installed in sources.items():
            with self.subTest(filename=source.name):
                data = source.read_bytes()
                self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
                self.assertEqual(struct.unpack_from(">II", data, 16), (1024, 1024))
                self.assertEqual(installed.read_bytes(), data)
        self.assertEqual(legacy.read_bytes(), b"previous installation")

    def test_patcher_is_idempotent_and_rejects_unknown_sources_before_writing(self):
        path = self.data_dir / "astrbot/data/plugins/astrbot_plugin_omnidraw/main.py"
        path.parent.mkdir(parents=True)
        original = (
            "from .constants import (\n"
            '            fallback_action = action or "看着镜头微笑"\n'
            "                    final_prompt, kwargs = self.persona_manager.build_persona_prompt(optimized_action)\n"
            "                extra_kwargs.update(kwargs)\n\n                raw_refs = self._get_event_images(event)\n"
            "target_refs = raw_refs if raw_refs else self.plugin_config.persona_ref_images\n"
            "target_refs = raw_refs if raw_refs else self.plugin_config.persona_ref_images\n"
        )
        path.write_text(original, encoding="utf-8")
        apply_selfie_references(self.data_dir)
        patched = path.read_bytes()
        apply_selfie_references(self.data_dir)
        self.assertEqual(path.read_bytes(), patched)
        unknown = original.replace('fallback_action = action or "看着镜头微笑"', "changed")
        path.write_text(unknown, encoding="utf-8")
        with self.assertRaises(RuntimeError):
            apply_selfie_references(self.data_dir)
        self.assertEqual(path.read_text(encoding="utf-8"), unknown)


if __name__ == "__main__":
    unittest.main()

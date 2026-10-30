"""Verify scene routing, packaged assets and checked upstream substitutions offline."""

import copy
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "overlays/astrbot/astrbot/core/utils"))
from patch_selfie_references import apply_selfie_references
from selfie_reference import REFERENCE_STYLES, select_selfie_reference


class SelfieReferenceTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.data_dir = Path(self.folder.name)
        self.refs = self.data_dir / "selfie_references"
        self.refs.mkdir()
        for filename, _ in REFERENCE_STYLES.values():
            (self.refs / filename).touch()
        self.config = SimpleNamespace(
            active_persona_id="dafeiyu", persona_ref_images=["original.png"]
        )

    def test_scenes_select_all_eighteen_packaged_references(self):
        cases = {
            "daily": "在公园向镜头招手",
            "meal": "坐在餐桌前吃饭",
            "hug": "抱着鲸鱼玩偶",
            "wink": "对镜头眨眼",
            "standing": "在海边全身站姿",
            "standing_meal": "全身捧碗吃饭",
            "standing_hug": "抱鲸鱼全身照",
            "action": "跳舞，富有动作感",
            "hands": "全身双手交叠站姿",
            "quiet": "Q版安静微笑",
            "snack": "吃冰淇淋",
            "chibi_hug": "Q版抱鲸鱼",
            "chibi_wink": "Q版眨眼",
            "night": "晚上在桌边陪伴",
            "pajamas": "睡衣晚安",
            "bed_hug": "床上抱鲸鱼",
            "blanket": "躲在被窝里",
            "bedtime": "睡前看手机",
        }
        self.assertEqual(set(cases), set(REFERENCE_STYLES))
        for style, action in cases.items():
            with self.subTest(style=style):
                result = select_selfie_reference(self.config, action, self.data_dir)
                self.assertEqual(result.style, style)
                self.assertEqual(len(result.images), 1)
                self.assertTrue(Path(result.images[0]).samefile(
                    self.refs / REFERENCE_STYLES[style][0]
                ))
                self.assertIn("以本次要求为准", result.prompt_suffix)

    def test_explicit_style_and_scene_take_precedence_over_ambient_night(self):
        for action, style in [
            ("晚上在公园散步", "daily"),
            ("晚上在街上全身跳舞", "action"),
            ("夜晚坐在餐桌吃饭", "meal"),
            ("Q版睡衣眨眼", "chibi_wink"),
            ("不要Q版，眨眼自拍", "wink"),
            ("full-body dancing", "action"),
            ("good night", "pajamas"),
            ("knight costume", "daily"),
        ]:
            with self.subTest(action=action):
                self.assertEqual(
                    select_selfie_reference(self.config, action, self.data_dir).style, style
                )

    def test_missing_scene_falls_back_to_daily_then_configured_reference(self):
        (self.refs / REFERENCE_STYLES["pajamas"][0]).unlink()
        self.assertEqual(select_selfie_reference(self.config, "晚安", self.data_dir).style, "daily")
        (self.refs / REFERENCE_STYLES["daily"][0]).unlink()
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
        assets = ROOT / "assets/selfie-references"
        self.assertEqual(len(list(assets.glob("*.png"))), 18)
        for filename, _ in REFERENCE_STYLES.values():
            with self.subTest(filename=filename):
                self.assertTrue((assets / filename).read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))

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

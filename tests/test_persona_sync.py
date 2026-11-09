"""Source persona updates are explicit, backed up and leave tool settings intact."""

import contextlib
import copy
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import apply_config
from persona_sync import synchronize


class PersonaSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "persona.md"
        self.source.write_text("# source\n\n---\n\n最新人格正文", encoding="utf-8")
        self.current = {"persona_id": "大肥鱼", "system_prompt": "old",
                        "tools": ["custom_tool"], "skills": None, "begin_dialogs": ["custom"]}
        self.writes = []

    def api(self, method, path, **kwargs):
        if method == "GET":
            return copy.deepcopy(self.current)
        self.writes.append(kwargs["json"])
        self.current.update(kwargs["json"])
        return {}

    def test_preview_does_not_write_and_apply_preserves_settings_with_backup(self):
        result = synchronize(self.api, self.source, self.root / "backups")
        self.assertFalse(result["matches"])
        self.assertFalse(self.writes)
        old = copy.deepcopy(self.current)
        result = synchronize(self.api, self.source, self.root / "backups", apply=True)
        self.assertTrue(result["matches"])
        self.assertTrue(result["applied"])
        self.assertEqual(set(self.writes[0]), {"persona_id", "system_prompt"})
        self.assertEqual(self.current["tools"], old["tools"])
        self.assertEqual(json.loads(Path(result["backup"]).read_text(encoding="utf-8")), old)
        synchronize(self.api, self.source, self.root / "backups", apply=True)
        self.assertEqual(len(self.writes), 1)

    def test_changed_dashboard_persona_and_empty_source_never_overwrite(self):
        reads = 0
        def edited_api(method, path, **kwargs):
            nonlocal reads
            reads += 1
            if reads == 2:
                self.current["system_prompt"] = "concurrent user edit"
            return self.api(method, path, **kwargs)
        with self.assertRaises(RuntimeError):
            synchronize(edited_api, self.source, self.root / "backups", apply=True)
        self.assertFalse(self.writes)
        self.source.write_text("---\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            synchronize(self.api, self.source, self.root / "backups", apply=True)


class PersonaSelectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.astro = self.root / "astrbot"
        self.data = self.astro / "data"
        (self.data / "plugins").mkdir(parents=True)
        self.settings = self.root / "config/settings.json"
        self.settings.parent.mkdir()
        self.settings.write_text('{"local_memory_enabled": true}', encoding="utf-8")
        self.config_path = self.data / "cmd_config.json"
        self.defaults = {
            "platform_settings": {}, "dashboard": {}, "provider_settings": {},
            "provider_tts_settings": {},
            "agent_runner": {"runner_type": "local", "config": {
                "model": {}, "persona": {"persona_id": "default", "safety_mode": True},
                "misc": {},
            }},
        }
        names = (
            "astrbot", "astrbot.core", "astrbot.core.config", "astrbot.core.config.default",
            "astrbot.core.utils", "astrbot.core.utils.auth_password",
            "patch_chat_boundaries", "patch_experience",
        )
        self.modules = {name: types.ModuleType(name) for name in names}
        self.modules["astrbot.core.config.default"].DEFAULT_CONFIG = self.defaults
        auth = self.modules["astrbot.core.utils.auth_password"]
        auth.hash_dashboard_password = lambda value: "test-hash"
        auth.hash_md5_dashboard_password = lambda value: "test-hash"
        auth.verify_dashboard_password = lambda *args: True
        self.modules["patch_chat_boundaries"].configure_group_settings = lambda config: None
        self.modules["patch_experience"].configure_experience_plugin = lambda *args: None

    def apply_settings(self):
        with (
            patch.multiple(apply_config, ROOT=self.root, ASTRO=self.astro, DATA=self.data,
                           SETTINGS=self.settings),
            patch.dict(sys.modules, self.modules),
            patch.dict(apply_config.os.environ),
            patch.object(sys, "path", list(sys.path)),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            apply_config.apply()
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def test_first_configuration_uses_project_persona_without_mutating_defaults(self):
        original_defaults = copy.deepcopy(self.defaults)
        configured = self.apply_settings()
        self.assertEqual(configured["agent_runner"]["config"]["persona"]["persona_id"], "大肥鱼")
        self.assertEqual(self.defaults, original_defaults)

    def test_saving_settings_preserves_selected_persona_and_safety_options(self):
        for persona_id in ("大肥鱼·自由人格", "大肥鱼", "default", "[%None]"):
            with self.subTest(persona_id=persona_id):
                existing = copy.deepcopy(self.defaults)
                persona = {"persona_id": persona_id, "safety_mode": False,
                           "safety_mode_strategy": "system_prompt"}
                existing["agent_runner"]["config"]["persona"] = persona
                self.config_path.write_text(json.dumps(existing), encoding="utf-8")
                configured = self.apply_settings()
                self.assertEqual(configured["agent_runner"]["config"]["persona"], persona)


if __name__ == "__main__":
    unittest.main()

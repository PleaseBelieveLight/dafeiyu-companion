"""Source persona updates are explicit, backed up and leave tool settings intact."""

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
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


if __name__ == "__main__":
    unittest.main()

"""Initialization and explicit acceptance use content even when docs is absent."""

import contextlib
import io
import json
import runpy
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import life_trial


class ContentSourceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source = Path(temporary.name) / "source"
        self.runtime = Path(temporary.name) / "runtime"
        shutil.copytree(ROOT / "content", self.source / "content")
        (self.runtime / "run").mkdir(parents=True)
        (self.runtime / "config").mkdir()
        self.assertFalse((self.source / "docs").exists())

    def test_initialization_imports_persona_and_knowledge_without_docs(self):
        for filename, value in {
            "settings.json": {"deepseek_api_key": "test-value", "owner_qq": "",
                              "scheduled_enabled": False},
            "local-access.json": {"username": "test", "password": "test"},
        }.items():
            (self.runtime / "config" / filename).write_text(json.dumps(value), encoding="utf-8")
        uploads = {}
        created_personas = []

        def request(method, path, **kwargs):
            if (method, path) == ("POST", "/auth/login"):
                value = {"token": "test-token"}
            elif (method, path) == ("GET", "/personas"):
                value = []
            elif (method, path) == ("POST", "/personas"):
                created_personas.append(kwargs["json"])
                value = {}
            elif (method, path) == ("GET", "/knowledge-bases"):
                value = []
            elif (method, path) == ("POST", "/knowledge-bases"):
                value = {"kb_id": kwargs["json"]["name"]}
            elif method == "GET" and path.endswith("/documents"):
                value = []
            elif method == "POST" and path.endswith("/documents"):
                filename, document, _ = kwargs["files"]["file"]
                uploads[filename] = document.read()
                value = {}
            else:
                raise AssertionError("Unexpected initialization request: " + method + " " + path)
            return types.SimpleNamespace(raise_for_status=lambda: None,
                                         json=lambda: {"status": "ok", "data": value})

        client = types.SimpleNamespace(headers={}, request=request)
        fake_workspace = types.SimpleNamespace(SOURCE_ROOT=self.source, RUNTIME_ROOT=self.runtime)
        fake_httpx = types.SimpleNamespace(Client=lambda **kwargs: client, HTTPError=OSError)
        fake_msvcrt = types.SimpleNamespace(LK_NBLCK=1, locking=lambda *args: None)
        with (
            patch.dict(sys.modules, {"workspace": fake_workspace, "httpx": fake_httpx,
                                     "msvcrt": fake_msvcrt}),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            loaded = runpy.run_path(str(ROOT / "tools/initialize_api.py"), run_name="__main__")
        loaded["lock"].close()

        self.assertTrue(created_personas[0]["system_prompt"])
        self.assertEqual(set(uploads), {"指令手册.md", "热梗词典.md"})
        for filename, data in uploads.items():
            self.assertEqual(data, (self.source / "content/knowledge" / filename).read_bytes())
        self.assertFalse((self.source / "docs").exists())

    def test_acceptance_prepare_reads_content_without_docs(self):
        dashboard = Mock()
        dashboard.login.return_value = dashboard
        dashboard.trial.return_value = {}
        with (
            patch.object(life_trial, "SOURCE_ROOT", self.source),
            patch.object(life_trial, "Dashboard", return_value=dashboard),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(life_trial.main(["prepare"]), 0)
        args, kwargs = dashboard.trial.call_args
        self.assertEqual(args, ("prepare",))
        self.assertEqual(kwargs["source_name"], str(Path("content/自主生活验收资料.md")))
        self.assertEqual(kwargs["source_text"],
                         (self.source / kwargs["source_name"]).read_text(encoding="utf-8")[:2000])
        self.assertFalse((self.source / "docs").exists())


if __name__ == "__main__":
    unittest.main()

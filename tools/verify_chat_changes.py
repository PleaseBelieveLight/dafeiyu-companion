"""Validate chat patches against installed dependencies without changing live code or data."""

import argparse
import hashlib
import importlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from workspace import RUNTIME_ROOT, SOURCE_ROOT
from patch_chat_boundaries import MEMORY_SCOPE, PATCHED_PATHS, apply_chat_boundaries
from patch_chat_efficiency import apply_chat_efficiency, apply_memory_recall_focus
from persona_sync import source_prompt


REPLAY_MODULE = "tests.test_dafeiyu_dialogue_replay"
PERSONA_PATH = Path("content/persona/大肥鱼-人格.md")


def reference_source(stage, commit):
    archive = subprocess.check_output([
        "git", "archive", "--format=zip", commit, "overlays/astrbot", "plugins", PERSONA_PATH.as_posix(),
    ], cwd=SOURCE_ROOT)
    source = stage / "reference"
    with zipfile.ZipFile(io.BytesIO(archive)) as snapshot:
        snapshot.extractall(source)
    return source


def save_dialogue_baseline(source, stage, provider, module, commit):
    digest = hashlib.sha256()
    for folder in (source / "plugins", source / "overlays/astrbot"):
        for path in sorted(folder.rglob("*.py")):
            digest.update(path.relative_to(source).as_posix().encode() + b"\0" + path.read_bytes())
    for path in (source / PERSONA_PATH, provider, Path(module.__file__), Path(module.memory_module.__file__)):
        digest.update(path.read_bytes())
    for relative in PATCHED_PATHS:
        digest.update((stage / relative).read_bytes())
    digest.update((stage / MEMORY_SCOPE).read_bytes())
    signature = digest.hexdigest()
    folder = (SOURCE_ROOT / ".verification/dialogue-baseline").resolve()
    if not folder.is_relative_to(SOURCE_ROOT.resolve()):
        raise RuntimeError("Unexpected replay output directory")
    folder.mkdir(parents=True, exist_ok=True)
    output = folder / f"{commit or 'workspace'}-{signature[:12]}.json"
    payload = json.dumps({
        "source": commit or "workspace", "source_signature": signature,
        "model": module.REPLAY_MODEL, "transport": "mock_qq", "cases": module.REPLAY_RECORDS,
    }, ensure_ascii=False, indent=2) + "\n"
    if output.exists():
        if output.read_text(encoding="utf-8") != payload:
            raise RuntimeError("Replay output changed for the same source and fixtures")
    else:
        with output.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
    print(f"{len(module.REPLAY_RECORDS)} two-turn cases: {output}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dialogue-baseline", action="store_true", help="Replay and retain the fixed synthetic dialogue cases")
    parser.add_argument("--reference", help="Replay a known Git commit with the same cases")
    args = parser.parse_args(argv)
    if args.reference and (not args.dialogue_baseline or not re.fullmatch(r"[a-fA-F0-9]{7,40}", args.reference)):
        parser.error("--reference requires --dialogue-baseline and a Git commit hash")
    commit = subprocess.check_output([
        "git", "rev-parse", "--verify", args.reference + "^{commit}",
    ], cwd=SOURCE_ROOT, text=True).strip() if args.reference else None
    runtime = RUNTIME_ROOT / "astrbot"
    parent = Path(tempfile.gettempdir()).resolve()
    with tempfile.TemporaryDirectory(prefix="chat-verification-", dir=parent) as folder:
        stage = Path(folder).resolve()
        if not stage.is_relative_to(parent):
            raise RuntimeError("Unexpected validation directory")
        source = reference_source(stage, commit) if commit else SOURCE_ROOT
        os.environ["ASTRBOT_ROOT"] = str(stage)
        logging.disable(logging.CRITICAL)
        shutil.copytree(SOURCE_ROOT / "assets/selfie-references",
                        stage / "data/plugin_data/astrbot_plugin_omnidraw/selfie_references")
        ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".git")
        staged_package = stage / "astrbot/astrbot"
        shutil.copytree(runtime / "astrbot", staged_package, ignore=ignore)
        shutil.copytree(source / "overlays/astrbot/astrbot", staged_package, dirs_exist_ok=True, ignore=ignore)
        target = staged_package / "core/provider/sources/openai_source.py"
        staged_plugins = stage / "astrbot/data/plugins"
        shutil.copytree(runtime / "data/plugins/astrbot_plugin_livingmemory",
                        staged_plugins / "astrbot_plugin_livingmemory", ignore=ignore)
        apply_chat_efficiency(stage)
        apply_memory_recall_focus(stage)
        if (source / "overlays/astrbot/astrbot/core/utils/chat_scope.py").is_file():
            apply_chat_boundaries(stage)
        plugins = staged_plugins
        shutil.copytree(runtime / "data/plugins/astrbot_plugin_proactive_chat",
                        plugins / "astrbot_plugin_proactive_chat", ignore=ignore)
        shutil.copytree(source / "overlays/astrbot/data/plugins",
                        plugins, dirs_exist_ok=True, ignore=ignore)
        shutil.copytree(source / "plugins", plugins, dirs_exist_ok=True, ignore=ignore)

        sys.path.insert(0, str(runtime))
        sys.path.insert(0, str(staged_package.parent))
        import data.plugins
        import tests
        data.plugins.__path__ = [str(plugins), *data.plugins.__path__]
        tests.__path__ = [str(SOURCE_ROOT / "tests/runtime"), *tests.__path__]
        replay = None
        if args.dialogue_baseline:
            replay = importlib.import_module(REPLAY_MODULE)
            replay.REPLAY_PERSONA = source_prompt(source / PERSONA_PATH)
            suite = unittest.defaultTestLoader.loadTestsFromModule(replay)
        else:
            suite = unittest.defaultTestLoader.loadTestsFromNames([
                "tests." + path.stem for path in (SOURCE_ROOT / "tests/runtime").glob("test_*.py")
            ])
        result = unittest.TextTestRunner(verbosity=1).run(suite)
        if replay is not None and result.wasSuccessful():
            save_dialogue_baseline(source, stage, target, replay, commit)
        return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())

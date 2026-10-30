"""Validate chat patches against installed dependencies without changing live code or data."""

import os
import logging
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from workspace import RUNTIME_ROOT, SOURCE_ROOT
from patch_chat_efficiency import apply_chat_efficiency


def main():
    runtime = RUNTIME_ROOT / "astrbot"
    parent = (RUNTIME_ROOT / "temp").resolve()
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="chat-verification-", dir=parent) as folder:
        stage = Path(folder).resolve()
        if not stage.is_relative_to(parent):
            raise RuntimeError("Unexpected validation directory")
        os.environ["ASTRBOT_ROOT"] = str(stage)
        logging.disable(logging.CRITICAL)
        shutil.copytree(SOURCE_ROOT / "assets/selfie-references",
                        stage / "data/plugin_data/astrbot_plugin_omnidraw/selfie_references")
        target = stage / "astrbot/astrbot/core/provider/sources/openai_source.py"
        target.parent.mkdir(parents=True)
        shutil.copy2(runtime / "astrbot/core/provider/sources/openai_source.py", target)
        apply_chat_efficiency(stage)
        plugins = stage / "plugins"
        ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".git")
        shutil.copytree(runtime / "data/plugins/astrbot_plugin_proactive_chat",
                        plugins / "astrbot_plugin_proactive_chat", ignore=ignore)
        shutil.copytree(SOURCE_ROOT / "overlays/astrbot/data/plugins",
                        plugins, dirs_exist_ok=True, ignore=ignore)
        shutil.copytree(SOURCE_ROOT / "plugins", plugins, dirs_exist_ok=True, ignore=ignore)

        sys.path.insert(0, str(runtime))
        import astrbot.core.utils
        import astrbot.core.provider.sources
        import data.plugins
        import tests
        astrbot.core.utils.__path__ = [str(SOURCE_ROOT / "overlays/astrbot/astrbot/core/utils"),
                                      *astrbot.core.utils.__path__]
        astrbot.core.provider.sources.__path__ = [str(target.parent),
                                                 *astrbot.core.provider.sources.__path__]
        data.plugins.__path__ = [str(plugins), *data.plugins.__path__]
        tests.__path__ = [str(SOURCE_ROOT / "tests/runtime"), *tests.__path__]
        suite = unittest.defaultTestLoader.loadTestsFromNames([
            "tests." + path.stem for path in (SOURCE_ROOT / "tests/runtime").glob("test_*.py")
        ])
        result = unittest.TextTestRunner(verbosity=1).run(suite)
        return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Run the installed reply, media, configuration-page and dependency checks."""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    env = dict(os.environ, ASTRBOT_ROOT=str(ROOT / "astrbot"), PYTHONUTF8="1")
    python = ROOT / "runtime/astrbot/Scripts/python.exe"
    if not python.is_file():
        raise RuntimeError("Run 安装.cmd first")
    subprocess.run([str(python), "-m", "pip", "check"], check=True, env=env)
    subprocess.run(
        [str(ROOT / "runtime/tools/Scripts/python.exe"), "-m", "pip", "check"], check=True, env=env
    )
    subprocess.run(
        [
            str(python),
            "-m",
            "unittest",
            "tests.test_dafeiyu_reply_delivery",
            "tests.test_dafeiyu_voice_mode",
            "tests.test_dafeiyu_action_media",
            "tests.test_dafeiyu_action_execution",
            "tests.test_dafeiyu_selfie_context",
            "-q",
        ],
        cwd=ROOT / "astrbot",
        env=env,
        check=True,
    )
    subprocess.run(
        [
            str(ROOT / "napcat/node.exe"),
            str(
                ROOT
                / "astrbot/data/plugins/astrbot_plugin_omnidraw/tests/test_page_initialization.cjs"
            ),
        ],
        env=env,
        check=True,
    )
    print(
        "Installed dependency, message delivery, media and page checks passed. API keys and QQ login still require the user's own configuration."
    )


if __name__ == "__main__":
    main()

"""Launch project tools using dependencies from the separate local runtime."""

import os
import subprocess
import sys

from workspace import RUNTIME_ROOT, SOURCE_ROOT, TOOLS, runtime_env


def command(action: str, args: list[str]) -> list[str]:
    """Build a command without starting services or importing their dependencies."""
    if action == "install":
        return [sys.executable, str(TOOLS / "install.py"), *args]
    scripts = {
        "configure": "configure_gui.py",
        "sync-content": "initialize_api.py",
        "verify": "verify_install.py",
        "prepare-model": "prepare_model.py",
        "deploy": "deploy.py",
    }
    python = RUNTIME_ROOT / "runtime/astrbot/Scripts/python.exe"
    if not python.is_file():
        raise RuntimeError(f"Run 安装.cmd first. Runtime not found: {RUNTIME_ROOT}")
    if action in scripts:
        return [str(python), str(TOOLS / scripts[action]), *args]
    if action not in {
        "start", "stop", "restart", "status", "backup", "heal", "open-napcat", "phone-start",
    }:
        raise ValueError("Unknown action: " + action)
    return [str(python), str(TOOLS / "control.py"), action, *args]


def main() -> int:
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    arguments = command(action, sys.argv[2:])
    options = {"cwd": SOURCE_ROOT, "env": runtime_env()}
    if action in {"configure", "open-napcat"} and os.name == "nt":
        arguments[0] = str(RUNTIME_ROOT / "runtime/astrbot/Scripts/pythonw.exe")
        subprocess.Popen(arguments, creationflags=subprocess.CREATE_NO_WINDOW, **options)
        return 0
    return subprocess.call(arguments, **options)


if __name__ == "__main__":
    raise SystemExit(main())

"""Deploy project patches into the local runtime without reinstalling dependencies."""

import argparse
import datetime
import json
import shutil
import subprocess
import time
import urllib.request
import zipfile
from pathlib import Path

from workspace import RUNTIME_ROOT as ROOT, SOURCE_ROOT, TOOLS, runtime_env


def update_admin_config(path: Path) -> None:
    """Move only the administration entry, preserving other MCPs and user settings."""
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    entry = config.setdefault("mcpServers", {}).setdefault("dafeiyu-admin", {})
    entry.update(
        command=str(ROOT / "runtime/tools/Scripts/python.exe"),
        args=[str(TOOLS / "admin_mcp.py")],
    )
    entry.setdefault("env", {})["DAFEIYU_RUNTIME_ROOT"] = str(ROOT)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def preserve_local_programs() -> None:
    """Retain the existing system Ollama when adopting a pre-installer deployment."""
    from install import find_program

    path = ROOT / "config/settings.json"
    settings = json.loads(path.read_text(encoding="utf-8-sig"))
    if not settings.get("ollama_path"):
        program = find_program("ollama.exe")
        if program:
            settings["ollama_path"] = str(program)
            temporary = path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)


def managed_paths() -> list[Path]:
    """Identify the installed code and configuration touched by deployment."""
    paths = []
    for folder in (SOURCE_ROOT / "overlays").iterdir():
        for path in folder.rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts:
                paths.append(ROOT / folder.name / path.relative_to(folder))
    for plugin in (SOURCE_ROOT / "plugins").iterdir():
        if plugin.is_dir():
            for path in plugin.rglob("*"):
                if path.is_file() and "__pycache__" not in path.parts:
                    paths.append(ROOT / "astrbot/data/plugins" / path.relative_to(plugin.parent))
    paths.extend((ROOT / "tools").glob("*"))
    paths.extend(ROOT.glob("*.cmd"))
    paths.extend((ROOT / "config").glob("*.json"))
    paths.extend((ROOT / "astrbot/tests").glob("test_*.py"))
    paths.extend([
        ROOT / "astrbot/data/mcp_server.json",
        ROOT / "astrbot/astrbot/core/provider/func_tool_manager.py",
        ROOT / "astrbot/data/plugins/astrbot_plugin_omnidraw/main.py",
        ROOT / "astrbot/data/plugins/astrbot_plugin_omnidraw/pages/插件配置/app.js",
        ROOT / "astrbot/data/plugins/astrbot_plugin_omnidraw/pages/插件配置/index.html",
    ])
    return paths


def backup_code(stamp: str) -> Path:
    """Keep a local recovery copy; account data never enters the source repository."""
    folder = ROOT / "backups"
    folder.mkdir(exist_ok=True)
    archive_path = folder / f"workspace-migration-{stamp}.zip"
    paths = set(managed_paths())
    paths.update((ROOT / "source/dafeiyu-astrbot-main").rglob("*"))
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(paths):
            if path.is_file() and "__pycache__" not in path.parts:
                archive.write(path, path.relative_to(ROOT))
    return archive_path


def remove_legacy_cmd_redirects() -> list[Path]:
    """Remove only unchanged, generated runtime shortcuts; preserve custom scripts."""
    removed = []
    for path in ROOT.glob("*.cmd"):
        if not path.is_file() or path.is_symlink() or path.resolve().parent != ROOT.resolve():
            continue
        expected = (
            '@echo off\n'
            'chcp 65001 >nul\n'
            f'set "DAFEIYU_RUNTIME_ROOT={ROOT}"\n'
            f'call "{SOURCE_ROOT / path.name}" %*\n'
        )
        if path.read_bytes() != expected.replace("\n", "\r\n").encode("utf-8"):
            continue
        path.unlink()
        removed.append(path)
    return removed


def refresh_legacy_tool_redirects() -> None:
    """Keep legacy Python references working without recreating runtime CMD entries."""
    wrapper = (
        '"""Compatibility entry point; editable code lives in dafeiyu-companion."""\n'
        "import os\nimport runpy\nimport sys\nfrom pathlib import Path\n"
        f"source_tools = Path({str(TOOLS)!r})\n"
        f"os.environ['DAFEIYU_RUNTIME_ROOT'] = {str(ROOT)!r}\n"
        "sys.path.insert(0, str(source_tools))\n"
        "if __name__ == '__main__':\n"
        "    runpy.run_path(str(source_tools / Path(__file__).name), run_name='__main__')\n"
    )
    for path in (ROOT / "tools").glob("*.py"):
        if (TOOLS / path.name).is_file():
            path.write_text(wrapper, encoding="utf-8")


def wait_ready() -> None:
    """Check local listeners and the dashboard without calling models or sending messages."""
    import control

    expected = {6185, 6099, 6199, 11435, 8009, 8010, 8012}
    for _ in range(90):
        if expected.issubset(control.ports()):
            try:
                with urllib.request.urlopen("http://127.0.0.1:6185", timeout=3) as response:
                    if response.status == 200:
                        return
            except OSError:
                pass
        time.sleep(2)
    raise RuntimeError("Services did not become ready; inspect the runtime logs")


def deploy(prune_legacy_source: bool = False) -> None:
    import control
    from install import apply_overlays

    if not (ROOT / "config/settings.json").is_file() or not (ROOT / "astrbot/main.py").is_file():
        raise RuntimeError(
            "Run scripts/安装配置/安装.cmd first; deploy only updates an existing runtime"
        )
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = backup_code(stamp)
    print("Recovery backup: " + str(backup), flush=True)
    legacy = ROOT / "source/dafeiyu-astrbot-main"
    retired = ROOT / "source" / (".retired-" + stamp)
    if legacy.exists() and prune_legacy_source:
        if legacy.is_symlink() or legacy.resolve() != ROOT / "source/dafeiyu-astrbot-main":
            raise RuntimeError("Refusing to move a linked or unexpected legacy source directory")
    control.detach_supervisor()
    state = control.load(control.RUN / "services.json", {})
    for name in ["initialize-api", "astrbot", "asr", "phone", "browser"]:
        control.stop_info(state.pop(name, {}))
    control.store(control.RUN / "services.json", state)
    try:
        if legacy.exists() and prune_legacy_source:
            legacy.rename(retired)
        preserve_local_programs()
        apply_overlays(ROOT)
        update_admin_config(ROOT / "astrbot/data/mcp_server.json")
        refresh_legacy_tool_redirects()
        remove_legacy_cmd_redirects()
        subprocess.run(
            [str(control.ASTROPY), str(TOOLS / "verify_install.py")],
            cwd=SOURCE_ROOT, env=runtime_env(ROOT), check=True,
        )
        control.start()
        wait_ready()
    except BaseException:
        # Restore the previous installed code/configuration without touching chat databases.
        control.detach_supervisor()
        failed = control.load(control.RUN / "services.json", {})
        for name in ["initialize-api", "astrbot", "asr", "phone", "browser"]:
            control.stop_info(failed.pop(name, {}))
        with zipfile.ZipFile(backup) as archive:
            for member in archive.infolist():
                target = (ROOT / member.filename).resolve()
                if not target.is_relative_to(ROOT):
                    raise RuntimeError("Unexpected recovery archive path")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(member))
        if retired.exists():
            # The archive already restored the original tree.
            if retired.resolve().parent != (ROOT / "source").resolve():
                raise RuntimeError("Unexpected retired source path")
            shutil.rmtree(retired)
        control.store(control.RUN / "services.json", failed or state)
        subprocess.run(
            [str(control.ASTROPY), str(ROOT / "tools/control.py"), "start"],
            cwd=ROOT, env=runtime_env(ROOT), check=False,
        )
        raise
    if retired.exists():
        if retired.resolve().parent != (ROOT / "source").resolve():
            raise RuntimeError("Unexpected retired source path")
        shutil.rmtree(retired)
    control.store(ROOT / "run/workspace.json", {
        "source_root": str(SOURCE_ROOT), "runtime_root": str(ROOT),
        "backup": str(backup), "legacy_source_removed": not legacy.exists(),
    })
    print("Deployment verified. Editable code: " + str(SOURCE_ROOT), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prune-legacy-source", action="store_true")
    deploy(parser.parse_args().prune_legacy_source)

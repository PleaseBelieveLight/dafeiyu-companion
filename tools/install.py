"""Install the pinned Windows distribution from public upstream dependencies."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from workspace import RUNTIME_ROOT, SOURCE_ROOT, TOOLS, runtime_env

SOURCE = SOURCE_ROOT
HIDDEN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def read_json(path: Path) -> dict:
    """Read a JSON document, including files saved with a Windows BOM."""
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: dict) -> None:
    """Write a complete JSON document atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def archive_files(archive: zipfile.ZipFile, strip_root: bool):
    """Yield validated relative files, rejecting traversal and Windows ADS paths."""
    roots = {entry.filename.replace("\\", "/").split("/", 1)[0] for entry in archive.infolist()}
    if strip_root and len(roots) != 1:
        raise ValueError("Expected one archive root")
    prefix = next(iter(roots)) + "/" if strip_root else ""
    for entry in archive.infolist():
        name = entry.filename.replace("\\", "/")
        if name.startswith("/") or ":" in name or ".." in PurePosixPath(name).parts:
            raise ValueError("Unsafe archive path")
        if ((entry.external_attr >> 16) & 0o170000) == 0o120000:
            raise ValueError("Archive symlinks are not supported")
        name = name.removeprefix(prefix)
        if name and not entry.is_dir():
            yield name, entry


def tree_digest(path: Path, strip_root: bool) -> str:
    """Hash relative names and file contents independently of the ZIP root name."""
    digest = hashlib.sha256()
    with zipfile.ZipFile(path) as archive:
        for name, entry in sorted(archive_files(archive, strip_root), key=lambda pair: pair[0]):
            digest.update(
                name.encode("utf-8") + b"\0" + hashlib.sha256(archive.read(entry)).digest()
            )
    return digest.hexdigest()


def verified(path: Path, artifact: dict) -> bool:
    """Verify a pinned archive, or a known local FFmpeg cache artifact."""
    if not path.is_file():
        return False
    if artifact.get("tree_sha256"):
        return tree_digest(path, artifact.get("strip_root", False)) == artifact["tree_sha256"]
    expected = artifact.get("sha256") or artifact.get("cached_sha256")
    with path.open("rb") as stream:
        return bool(expected) and hashlib.file_digest(stream, "sha256").hexdigest() == expected


def fetch(artifact: dict, cache_dirs: list[Path], downloads: Path) -> Path:
    """Reuse a verified cache or download and validate one public artifact."""
    for folder in [downloads, *cache_dirs]:
        candidate = folder / artifact["name"]
        if candidate.is_file() and verified(candidate, artifact):
            print("Verified cache: " + artifact["name"], flush=True)
            return candidate
    current = dict(artifact)
    if artifact["kind"] == "ffmpeg":
        request = urllib.request.Request(
            f"https://api.github.com/repos/{artifact['repository']}/releases/{artifact['release']}",
            headers={"User-Agent": "Dafeiyu-OpenSource"},
        )
        with urllib.request.urlopen(request, timeout=45) as response:
            release = json.load(response)
        asset = next(item for item in release["assets"] if item["name"] == artifact["asset"])
        digest = asset.get("digest", "")
        if not digest.startswith("sha256:"):
            raise RuntimeError("The FFmpeg publisher did not provide a checksum")
        current.update(url=asset["browser_download_url"], sha256=digest.removeprefix("sha256:"))
    url = current["url"]
    if not url.startswith("https://"):
        raise ValueError("Only HTTPS downloads are allowed")
    downloads.mkdir(parents=True, exist_ok=True)
    destination = downloads / artifact["name"]
    temporary = destination.with_suffix(destination.suffix + ".part")
    print("Downloading " + artifact["name"], flush=True)
    request = urllib.request.Request(url, headers={"User-Agent": "Dafeiyu-OpenSource"})
    with urllib.request.urlopen(request, timeout=90) as response, temporary.open("wb") as stream:
        shutil.copyfileobj(response, stream)
    if not verified(temporary, current):
        raise RuntimeError("Dependency checksum mismatch: " + artifact["name"])
    temporary.replace(destination)
    if current.get("sha256"):
        write_json(
            downloads / (artifact["name"] + ".verified.json"),
            {"sha256": current["sha256"], "url": url},
        )
    return destination


def unpack(path: Path, destination: Path, strip_root: bool = False, ffmpeg: bool = False) -> None:
    """Extract only safe files below the named destination."""
    destination.mkdir(parents=True, exist_ok=True)
    base = destination.resolve()
    with zipfile.ZipFile(path) as archive:
        for name, entry in archive_files(archive, strip_root):
            if ffmpeg:
                if Path(name).name not in {"ffmpeg.exe", "ffprobe.exe", "LICENSE.txt"}:
                    continue
                name = Path(name).name
            target = (base / name).resolve()
            if not target.is_relative_to(base):
                raise ValueError("Archive path escapes its destination")
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(entry) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)


def find_program(name: str) -> Path | None:
    """Find common per-user and system installations without embedding usernames."""
    found = shutil.which(name)
    if found:
        return Path(found)
    candidates = {
        "ollama.exe": [("LOCALAPPDATA", "Programs/Ollama/ollama.exe")],
        "chrome.exe": [
            ("LOCALAPPDATA", "Google/Chrome/Application/chrome.exe"),
            ("ProgramFiles", "Google/Chrome/Application/chrome.exe"),
            ("ProgramFiles(x86)", "Microsoft/Edge/Application/msedge.exe"),
        ],
    }.get(name, [])
    return next(
        (
            Path(os.environ[key]) / relative
            for key, relative in candidates
            if key in os.environ and (Path(os.environ[key]) / relative).is_file()
        ),
        None,
    )


def apply_overlays(target: Path) -> None:
    """Apply local public patches and install the two original project plugins."""
    for folder in ["astrbot", "phone"]:
        overlay = SOURCE / "overlays" / folder
        if overlay.exists():
            shutil.copytree(
                overlay, target / folder, dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
    for plugin in (SOURCE / "plugins").iterdir():
        if plugin.is_dir() and plugin.name != "__pycache__":
            shutil.copytree(
                plugin, target / "astrbot/data/plugins" / plugin.name, dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
    from patch_selfie_context import apply_selfie_context
    from patch_selfie_references import apply_selfie_references
    from patch_music import apply_music

    apply_selfie_context(target)
    apply_selfie_references(target)
    apply_music(target)
    runtime_tests = target / "astrbot/tests"
    runtime_tests.mkdir(parents=True, exist_ok=True)
    (runtime_tests / "__init__.py").touch()
    for test in (SOURCE / "tests/runtime").glob("*.py"):
        shutil.copy2(test, runtime_tests / test.name)
    page_test = (
        target / "astrbot/data/plugins/astrbot_plugin_omnidraw/tests/test_page_initialization.cjs"
    )
    shutil.copy2(SOURCE / "tests/runtime/test_page_initialization.cjs", page_test)
    page = target / "astrbot/data/plugins/astrbot_plugin_omnidraw/pages/插件配置"
    js = page / "app.js"
    text = js.read_text(encoding="utf-8-sig")
    old = '    byId("opt_astrbot").checked = Boolean(state.optimizer_config.use_astrbot_provider);'
    if old in text:
        text = text.replace(
            old,
            '    const astrbotOptimizerToggle = byId("opt_astrbot");\n    if (astrbotOptimizerToggle) {\n        astrbotOptimizerToggle.checked = Boolean(state.optimizer_config.use_astrbot_provider);\n    }',
            1,
        )
    manager_path = target / "astrbot/astrbot/core/provider/func_tool_manager.py"
    manager_text = manager_path.read_text(encoding="utf-8-sig")
    manager_text = manager_text.replace(
        'All non-builtin tools default to ``"member"`` (no restriction).',
        'Local administration tools default to ``"admin"``; other tools use ``"member"``.',
    )
    old_permission = '        return "member"\n\n    async def _check_tool_permission('
    new_permission = '        return "admin" if tool_name in {"health", "logs", "plugins", "cron_jobs", "webui_info", "qr", "restart", "heal_ws"} else "member"\n\n    async def _check_tool_permission('
    if old_permission in manager_text:
        manager_text = manager_text.replace(old_permission, new_permission, 1)
    elif new_permission not in manager_text:
        raise RuntimeError("The pinned tool-permission manager no longer matches its patch")
    notice = "# Dafeiyu Windows: protect administration tools; modified 2026-10-26.\n"
    if not manager_text.startswith(notice):
        manager_text = notice + manager_text
    manager_path.write_text(manager_text, encoding="utf-8")
    old = '    state.optimizer_config.use_astrbot_provider = byId("opt_astrbot").checked;'
    if old in text:
        text = text.replace(
            old,
            '    const astrbotOptimizerToggle = byId("opt_astrbot");\n    if (astrbotOptimizerToggle) {\n        state.optimizer_config.use_astrbot_provider = astrbotOptimizerToggle.checked;\n    }',
            1,
        )
    if text.count("if (astrbotOptimizerToggle)") != 2:
        raise RuntimeError("The pinned drawing page no longer matches its patch")
    js.write_text(text, encoding="utf-8")
    html = page / "index.html"
    html.write_text(
        html.read_text(encoding="utf-8-sig").replace(
            'src="./app.js?v=3.4.0"', 'src="./app.js?v=3.4.0-local.1"'
        ),
        encoding="utf-8",
    )
    reference = (
        target
        / "astrbot/data/plugin_data/astrbot_plugin_omnidraw/files/persona_config/persona_ref_image/dafeiyu.png"
    )
    reference.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SOURCE / "assets/selfie-reference.png", reference)
    shutil.copytree(
        SOURCE / "assets/selfie-references",
        target / "astrbot/data/plugin_data/astrbot_plugin_omnidraw/selfie_references",
        dirs_exist_ok=True,
    )


def install_environments(target: Path) -> None:
    """Rebuild both virtual environments from the separate Windows lock files."""
    for name in ["astrbot", "tools"]:
        environment = target / "runtime" / name
        python = environment / "Scripts/python.exe"
        if not python.exists():
            subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True)
        env = dict(os.environ, PIP_CACHE_DIR=str(target / "cache/pip"), PYTHONUTF8="1")
        subprocess.run(
            [str(python), "-m", "pip", "install", "--upgrade", "pip==26.2.2"], check=True, env=env
        )
        subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "-r",
                str(SOURCE / "requirements" / (name + ".lock.txt")),
            ],
            check=True,
            env=env,
        )


def download_model(target: Path, ollama: Path) -> None:
    """Pull bge-m3 using a temporary server isolated from existing installations."""
    for port in range(11835, 11855):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
                break
            except OSError:
                continue
    else:
        raise RuntimeError("No local installation port is available")
    env = dict(
        os.environ,
        OLLAMA_HOST=f"127.0.0.1:{port}",
        OLLAMA_MODELS=str(target / "models"),
        OLLAMA_NUM_PARALLEL="1",
    )
    log = (target / "logs/model-install.log").open("ab")
    process = subprocess.Popen(
        [str(ollama), "serve"], env=env, stdout=log, stderr=subprocess.STDOUT, creationflags=HIDDEN
    )
    try:
        for _ in range(45):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/tags", timeout=2):
                    break
            except OSError:
                time.sleep(1)
        else:
            raise RuntimeError("The embedding server did not start")
        subprocess.run(
            [str(ollama), "pull", "bge-m3"], env=env, check=True, timeout=1800, creationflags=HIDDEN
        )
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/embed",
            data=json.dumps(
                {"model": "bge-m3", "input": "安装自检", "keep_alive": 0, "options": {"num_gpu": 0}}
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=180) as response:
            vector = json.load(response).get("embeddings", [[]])[0]
        if len(vector) != 1024:
            raise RuntimeError("The embedding model did not return 1024 dimensions")
        print("Embedding model self-check passed: 1024 dimensions", flush=True)
    finally:
        process.terminate()
        process.wait(timeout=15)
        log.close()


def install(args) -> None:
    """Assemble dependencies, runtime environments and an empty user configuration."""
    if os.name != "nt" or sys.version_info[:2] != (3, 12):
        raise RuntimeError("Use Windows x64 with Python 3.12")
    target = args.root.resolve()
    if target == SOURCE or SOURCE in target.parents or target in SOURCE.parents:
        raise RuntimeError("Keep the runtime directory separate from the source repository")
    if (
        target.exists()
        and any(target.iterdir())
        and not (target / "run/install-state.json").exists()
    ):
        raise RuntimeError("Refusing to overwrite an unrelated nonempty installation directory")
    if (target / "config/settings.json").exists() and (target / "run/services.json").exists():
        running = read_json(target / "run/services.json")
        if running:
            raise RuntimeError("Stop this installation before reinstalling it")
    target.mkdir(parents=True, exist_ok=True)
    (target / "downloads").mkdir(exist_ok=True)
    lock = read_json(SOURCE / "dependencies.lock.json")
    ollama = find_program("ollama.exe")
    for artifact in lock["artifacts"]:
        kind = artifact["kind"]
        if args.sources_only and kind != "source":
            continue
        if kind == "ollama" and ollama:
            continue
        archive = fetch(artifact, args.cache, target / "downloads")
        if kind == "apk":
            destination = target / "downloads" / artifact["name"]
            if archive.resolve() != destination.resolve():
                shutil.copy2(archive, destination)
        else:
            unpack(
                archive,
                target / artifact["target"],
                artifact.get("strip_root", False),
                ffmpeg=kind == "ffmpeg",
            )
    apply_overlays(target)
    for folder in [
        "config",
        "run",
        "logs",
        "temp",
        "browser",
        "models",
        "appdata/roaming",
        "appdata/local",
        "cache",
    ]:
        (target / folder).mkdir(parents=True, exist_ok=True)
    settings_path = target / "config/settings.json"
    settings = (
        read_json(settings_path)
        if settings_path.exists()
        else read_json(SOURCE / "config/settings.example.json")
    )
    if not settings.get("ollama_path") and ollama:
        settings["ollama_path"] = str(ollama)
    browser = find_program("chrome.exe")
    if not settings.get("chrome_path") and browser:
        settings["chrome_path"] = str(browser)
    write_json(settings_path, settings)
    write_json(
        target / "run/install-state.json",
        {
            "version": 1,
            "source_artifacts": [
                item["commit"] for item in lock["artifacts"] if item.get("commit")
            ],
            "staged": True,
            "runtime_ready": False,
        },
    )
    if args.stage_only:
        print(
            "Source staging passed; dependencies and model installation were intentionally skipped."
        )
        return
    install_environments(target)
    env = dict(runtime_env(target), ASTRBOT_ROOT=str(target / "astrbot"))
    subprocess.run(
        [str(target / "runtime/astrbot/Scripts/python.exe"), str(TOOLS / "apply_config.py")],
        cwd=target,
        env=env,
        check=True,
    )
    if not args.skip_model:
        download_model(target, ollama or target / "bin/ollama/ollama.exe")
    write_json(
        target / "run/install-state.json",
        {
            "version": 1,
            "staged": True,
            "runtime_ready": True,
            "embedding_download_skipped": args.skip_model,
        },
    )
    print(
        "Installation complete. Open 配置向导.cmd, fill in your own accounts and API keys, then save and start."
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=RUNTIME_ROOT)
    parser.add_argument("--cache", type=Path, action="append", default=[])
    parser.add_argument(
        "--stage-only",
        action="store_true",
        help="Verify archives and prepare sources without installing packages or starting services",
    )
    parser.add_argument(
        "--sources-only",
        action="store_true",
        help="Skip binary dependencies; intended for CI source checks",
    )
    parser.add_argument(
        "--skip-model", action="store_true",
        help="Download bge-m3 later with scripts/安装配置/准备记忆模型.cmd",
    )
    args = parser.parse_args()
    if args.sources_only and not args.stage_only:
        parser.error("--sources-only requires --stage-only")
    install(args)


if __name__ == "__main__":
    main()

"""Start, stop, inspect and back up only this Dafeiyu installation."""

import datetime
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.parse
import webbrowser
import zipfile
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "run"
LOG = ROOT / "logs"
ASTROPY = ROOT / "runtime/astrbot/Scripts/python.exe"
TOOLPY = ROOT / "runtime/tools/Scripts/python.exe"
OLLAMA = ROOT / "bin/ollama/ollama.exe"
HIDDEN = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def load(path, default=None):
    return json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else default


def store(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def ports():
    wanted = {6185, 6099, 6199, 11435, 8009, 8010, 8012}
    return sorted(
        {
            c.laddr.port
            for c in psutil.net_connections(kind="tcp")
            if c.status == "LISTEN" and c.laddr.port in wanted
        }
    )


def process_alive(info):
    try:
        return abs(psutil.Process(info["pid"]).create_time() - info["created"]) < 1
    except (psutil.Error, KeyError, TypeError):
        return False


def spawn(name, command, cwd, env):
    with (LOG / (name + ".log")).open("ab") as out:
        process = subprocess.Popen(
            [str(x) for x in command],
            cwd=str(cwd),
            env=env,
            stdout=out,
            stderr=subprocess.STDOUT,
            creationflags=HIDDEN,
        )
    return {"pid": process.pid, "created": psutil.Process(process.pid).create_time()}


def stop_info(info):
    if not process_alive(info):
        return
    proc = psutil.Process(info["pid"])
    children = proc.children(recursive=True)
    for child in reversed(children):
        try:
            child.terminate()
        except psutil.Error:
            pass
    try:
        proc.terminate()
    except psutil.Error:
        pass
    _, pending = psutil.wait_procs([*children, proc], timeout=8)
    for child in pending:
        try:
            child.kill()
        except psutil.Error:
            pass


def request(path, body=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request("http://127.0.0.1:6099" + path, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=8) as response:
        return json.load(response)


def heal():
    if 6185 not in ports() or 6099 not in ports():
        return False
    if any(
        c.status == "ESTABLISHED" and c.laddr.port == 6199
        for c in psutil.net_connections(kind="tcp")
    ):
        return True
    credentials = load(ROOT / "config/local-access.json", {})
    digest = hashlib.sha256((credentials["napcat_token"] + ".napcat").encode()).hexdigest()
    auth = request("/api/auth/login", {"hash": digest})
    token = (auth.get("data") or {}).get("Credential")
    if not token:
        return False
    result = request("/api/OB11Config/GetConfig", {}, token)
    config = result.get("data")
    if isinstance(config, dict) and "config" in config:
        config = config["config"]
    if isinstance(config, str):
        config = json.loads(config)
    if not isinstance(config, dict):
        return False
    clients = config.get("network", config).get("websocketClients", [])
    selected = [c for c in clients if c.get("url") == "ws://127.0.0.1:6199/ws" and c.get("enable")]
    if not selected:
        return False
    for client in selected:
        client["enable"] = False
    request("/api/OB11Config/SetConfig", {"config": json.dumps(config)}, token)
    time.sleep(1)
    for client in selected:
        client["enable"] = True
    request("/api/OB11Config/SetConfig", {"config": json.dumps(config)}, token)
    return True


def supervise():
    RUN.mkdir(exist_ok=True)
    LOG.mkdir(exist_ok=True)
    settings = load(ROOT / "config/settings.json")
    env = dict(os.environ)
    env.update(
        {
            "PYTHONUTF8": "1",
            "PYTHONUNBUFFERED": "1",
            "ASTRBOT_ROOT": str(ROOT / "astrbot"),
            "DASHBOARD_HOST": "127.0.0.1",
            "DASHBOARD_PORT": "6185",
            "OLLAMA_HOST": "127.0.0.1:11435",
            "OLLAMA_MODELS": str(ROOT / "models"),
            "OLLAMA_NUM_PARALLEL": "1",
            "TEMP": str(ROOT / "temp"),
            "TMP": str(ROOT / "temp"),
            "PIP_CACHE_DIR": str(ROOT / "cache/pip"),
            "HF_HOME": str(ROOT / "cache/huggingface"),
            "XDG_CACHE_HOME": str(ROOT / "cache"),
            "XDG_DATA_HOME": str(ROOT / "appdata"),
            "APPDATA": str(ROOT / "appdata/roaming"),
            "LOCALAPPDATA": str(ROOT / "appdata/local"),
            "DEEPSEEK_API_KEY": settings["deepseek_api_key"],
            "BROWSER_MODEL": settings["deepseek_model"],
            "BROWSER_CHROME_PATH": settings["chrome_path"],
            "BROWSER_PROFILE_DIR": str(ROOT / "browser/chrome-profile"),
            "BROWSER_HEADLESS": "1",
            "ANONYMIZED_TELEMETRY": "false",
            "DO_NOT_TRACK": "1",
            "MCP_TELEMETRY_ENABLED": "false",
            "ANDROID_SERIAL": settings["emulator_serial"],
        }
    )
    env["PATH"] = os.pathsep.join(
        [
            str(ROOT / "bin"),
            str(ROOT / "bin/platform-tools"),
            settings.get("emulator_directory", ""),
            env.get("PATH", ""),
        ]
    )
    if settings.get("bot_qq", "").strip():
        env["NAPCAT_QUICK_ACCOUNT"] = settings["bot_qq"].strip()
    services = {
        "ollama": ([settings.get("ollama_path") or OLLAMA, "serve"], ROOT),
        "phone": ([TOOLPY, ROOT / "tools/phone_entry.py"], ROOT / "phone/src"),
        "browser": ([TOOLPY, ROOT / "tools/browser_mcp_server.py"], ROOT / "browser"),
        "asr": ([ASTROPY, ROOT / "tools/local_asr.py"], ROOT),
        "astrbot": (
            [ASTROPY, ROOT / "astrbot/main.py", "--webui-dir", ROOT / "astrbot/data/dist"],
            ROOT / "astrbot",
        ),
        "napcat": ([ROOT / "napcat/node.exe", ROOT / "napcat/index.js"], ROOT / "napcat"),
    }
    state = {}
    previous = load(RUN / "services.json", {})
    for name in services:
        if process_alive(previous.get(name, {})):
            state[name] = previous[name]
    old_ollama = RUN / "ollama.pid"
    if "ollama" not in state and old_ollama.exists():
        try:
            proc = psutil.Process(int(old_ollama.read_text().strip()))
            if proc.name().lower() == "ollama.exe":
                state["ollama"] = {"pid": proc.pid, "created": proc.create_time()}
        except (ValueError, psutil.Error):
            pass
    store(RUN / "supervisor.json", {"pid": os.getpid(), "created": psutil.Process().create_time()})
    last_heal = time.monotonic()
    initialized = False
    try:
        while not (RUN / "stop").exists():
            for name, (command, cwd) in services.items():
                if name == "astrbot" and (RUN / "backup-in-progress").exists():
                    continue
                if not process_alive(state.get(name, {})):
                    state[name] = spawn(name, command, cwd, env)
                    print("Started " + name, flush=True)
                    store(RUN / "services.json", state)
                    time.sleep(2)
            if not initialized and 6185 in ports():
                state["initialize-api"] = spawn(
                    "initialize-api", [ASTROPY, ROOT / "tools/initialize_api.py"], ROOT, env
                )
                store(RUN / "services.json", state)
                initialized = True
            if time.monotonic() - last_heal > 180:
                try:
                    heal()
                except Exception as exc:
                    print("Connection check: " + type(exc).__name__, flush=True)
                last_heal = time.monotonic()
            time.sleep(5)
    finally:
        for name in reversed(state):
            if name in state:
                stop_info(state[name])
        store(RUN / "services.json", {})
        (RUN / "supervisor.json").unlink(missing_ok=True)
        (RUN / "stop").unlink(missing_ok=True)


def start():
    if process_alive(load(RUN / "supervisor.json", {})):
        print("Dafeiyu services are already running.")
        return
    owned = set()
    for info in load(RUN / "services.json", {}).values():
        if process_alive(info):
            try:
                proc = psutil.Process(info["pid"])
                owned.update([proc.pid, *(child.pid for child in proc.children(recursive=True))])
            except psutil.Error:
                pass
    conflicts = {
        connection.laddr.port
        for connection in psutil.net_connections(kind="tcp")
        if connection.status == "LISTEN"
        and connection.laddr.port in {6185, 6099, 6199, 11435, 8009, 8010, 8012}
        and connection.pid not in owned
    }
    if conflicts:
        raise RuntimeError("Ports are used by another installation: " + str(sorted(conflicts)))
    (RUN / "stop").unlink(missing_ok=True)
    spawn(
        "supervisor",
        [ASTROPY, Path(__file__), "supervise"],
        ROOT,
        dict(os.environ, PYTHONUTF8="1", PYTHONUNBUFFERED="1"),
    )
    print("Services are starting. Open http://127.0.0.1:6185 in about 30 seconds.")


def stop():
    RUN.mkdir(exist_ok=True)
    (RUN / "stop").touch()
    info = load(RUN / "supervisor.json", {})
    for _ in range(45):
        if not process_alive(info):
            break
        time.sleep(1)
    if process_alive(info):
        for child in load(RUN / "services.json", {}).values():
            stop_info(child)
        stop_info(info)
    else:
        for child in load(RUN / "services.json", {}).values():
            stop_info(child)
    store(RUN / "services.json", {})
    (RUN / "stop").unlink(missing_ok=True)
    print("Dafeiyu services stopped.")


def detach_supervisor():
    info = load(RUN / "supervisor.json", {})
    if not process_alive(info):
        return
    proc = psutil.Process(info["pid"])
    command = proc.cmdline()
    if (
        not any(Path(arg).resolve() == Path(__file__).resolve() for arg in command)
        or "supervise" not in command
    ):
        raise RuntimeError("Refusing to detach a process that is not this installation supervisor.")
    # Windows TerminateProcess leaves the service children running. A graceful
    # stop executes supervise()'s cleanup and would log the QQ account out.
    if os.name != "nt":
        raise RuntimeError("Preserving service children requires the Windows launcher.")
    proc.terminate()
    proc.wait(timeout=10)


def apply_configuration():
    from apply_config import apply

    detach_supervisor()
    try:
        apply()
        state = load(RUN / "services.json", {})
        for name in ["initialize-api", "astrbot", "browser"]:
            stop_info(state.get(name, {}))
            state.pop(name, None)
        store(RUN / "services.json", state)
    finally:
        # The replacement supervisor adopts the existing QQ and other services.
        start()


def open_napcat():
    webui = load(ROOT / "napcat/napcat/config/webui.json", {})
    token = webui.get("token") or load(ROOT / "config/local-access.json", {}).get(
        "napcat_token", ""
    )
    url = (
        "http://127.0.0.1:"
        + str(webui.get("port", 6099))
        + "/webui/?token="
        + urllib.parse.quote(token, safe="")
    )
    webbrowser.open(url)


def backup():
    (RUN / "backup-in-progress").touch()
    info = load(RUN / "services.json", {}).get("astrbot")
    if info:
        stop_info(info)
    folder = ROOT / "backups"
    folder.mkdir(exist_ok=True)
    path = folder / ("dafeiyu-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + ".zip")
    try:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            for base in [
                ROOT / "config",
                ROOT / "astrbot/data",
                ROOT / "napcat/napcat/config",
                ROOT / "tools",
            ]:
                for item in base.rglob("*"):
                    if item.is_file() and not any(
                        p in {"temp", "logs", "site-packages", "__pycache__", "dist"}
                        for p in item.relative_to(base).parts
                    ):
                        archive.write(item, item.relative_to(ROOT))
        print("Backup: " + str(path))
    finally:
        (RUN / "backup-in-progress").unlink(missing_ok=True)


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    if action == "supervise":
        supervise()
    elif action == "start":
        start()
    elif action == "stop":
        stop()
    elif action == "restart":
        stop()
        start()
    elif action == "apply":
        apply_configuration()
    elif action == "open-napcat":
        open_napcat()
    elif action == "backup":
        backup()
    elif action == "heal":
        print("Connection repair requested: " + str(heal()))
    elif action == "status":
        print(
            json.dumps(
                {
                    "ports": ports(),
                    "services": {
                        k: process_alive(v) for k, v in load(RUN / "services.json", {}).items()
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    elif action == "phone-start":
        settings = load(ROOT / "config/settings.json")
        subprocess.run(
            [
                str(Path(settings["emulator_directory"]) / "ldconsole.exe"),
                "launch",
                "--index",
                str(settings["emulator_index"]),
            ],
            check=True,
        )

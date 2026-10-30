"""Private acceptance operations; explicit commands, no scheduler and no credential output."""

import argparse
import hashlib
import json
import re
import socket
import sqlite3
import sys
import urllib.request
import uuid
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from workspace import RUNTIME_ROOT, SOURCE_ROOT

PLUGIN = "astrbot_plugin_life_continuity"
CONFIG = f"astrbot/data/config/{PLUGIN}_config.json"
OVERLAYS = ["core/services/sender_service.py", "core/adapters/astrbot_event.py"]


def read(path, default=None):
    return json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else default


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def code_files():
    files = {}
    for path in (SOURCE_ROOT / "plugins" / PLUGIN).iterdir():
        if path.is_file() and path.suffix in {".py", ".json", ".yaml"}:
            files[f"astrbot/data/plugins/{PLUGIN}/{path.name}"] = path
    for name in OVERLAYS:
        relative = "astrbot/data/plugins/astrbot_plugin_proactive_chat/" + name
        files[relative] = SOURCE_ROOT / "overlays" / relative
    return files


def manifest():
    return {name: hashlib.sha256(path.read_bytes()).hexdigest()
            for name, path in code_files().items()}


def preflight(root=RUNTIME_ROOT):
    ports = {}
    for name, port in (("astrbot", 6185), ("napcat", 6099), ("qq_adapter", 6199)):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                ports[name] = True
        except OSError:
            ports[name] = False
    config = read(root / CONFIG, {})
    expected = manifest()
    matches = all((root / name).is_file()
                  and hashlib.sha256((root / name).read_bytes()).hexdigest() == digest
                  for name, digest in expected.items())
    return {"services": ports, "installed_code_matches": matches,
            "configured": bool(config), "enabled": config.get("enabled") is True,
            "trial_mode": config.get("trial_mode") is True,
            "owner_configured": bool(config.get("owner_scope")), "periodic_scheduler": False}


def deploy_closed(root=RUNTIME_ROOT):
    """Back up and copy only this feature's code, keeping every runtime switch closed."""
    root = root.resolve()
    targets = code_files()
    for relative in (*targets, CONFIG):
        if (root / relative).resolve() != root / relative:
            raise RuntimeError("Refusing a linked or unexpected feature path")
    config = read(root / CONFIG, {})
    if any(config.get(key) is True for key in ("enabled", "activity_enabled", "share_enabled")):
        raise RuntimeError("Close the life feature before deploying; preserve its current ledger")
    folder = root / "backups" / ("life-trial-" + uuid.uuid4().hex)
    if folder.resolve() != folder:
        raise RuntimeError("Refusing a linked backup path")
    folder.mkdir(parents=True)
    with zipfile.ZipFile(folder / "code-config.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for name in (*targets, CONFIG):
            path = root / name
            if path.is_file():
                archive.write(path, name)
    database = root / f"astrbot/data/plugin_data/{PLUGIN}/life.sqlite3"
    if database.exists():
        if database.resolve() != database:
            raise RuntimeError("Refusing a linked life ledger")
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as src:
            with closing(sqlite3.connect(folder / "life.sqlite3")) as dest:
                src.backup(dest)
    defaults = read(SOURCE_ROOT / "plugins" / PLUGIN / "_conf_schema.json")
    config = {**{key: value["default"] for key, value in defaults.items()}, **config}
    config.update(enabled=False, activity_enabled=False, share_enabled=False, trial_mode=True)
    # A closed configuration is durable before any new code becomes visible.
    write(root / CONFIG, config)
    for relative, source in targets.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_bytes(source.read_bytes())
        temporary.replace(target)
    write(folder / "source-manifest.json", manifest())
    return {"deployed_closed": True, "backup": str(folder), "files": len(targets)}


class Dashboard:
    def __init__(self, root=RUNTIME_ROOT):
        self.root, self.token = root, None

    def call(self, path, body=None, *, timeout=15):
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        request = urllib.request.Request("http://127.0.0.1:6185" + path, data=data,
                                         headers=headers)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.load(response)
        if result.get("status") not in {"ok", "success"}:
            raise RuntimeError(str(result.get("message", "Dashboard rejected the operation"))[:300])
        return result.get("data")

    def login(self):
        credentials = read(self.root / "config/local-access.json")
        result = self.call("/api/v1/auth/login", {"username": credentials["username"],
                                                 "password": credentials["password"]})
        self.token = result["token"]
        return self

    def trial(self, operation, **values):
        return self.call("/api/v1/plugins/extensions/" + PLUGIN + "/trial",
                         {"operation": operation, **values},
                         timeout=35 if operation in {"memory", "recall"} else 15)

    def reload(self, name=PLUGIN):
        # Never call reload with an unknown plugin: that SDK path reloads all plugins.
        self.call("/api/v1/plugins/" + name + "/reload", {})


def enable_trial(client, root=RUNTIME_ROOT):
    if not preflight(root)["installed_code_matches"]:
        raise RuntimeError("Deploy the current feature code before enabling it")
    config = read(root / CONFIG)
    settings = read(root / "config/settings.json")
    owner = str(settings.get("owner_qq", "")).strip()
    platforms = read(root / "astrbot/data/cmd_config.json").get("platform", [])
    qq = [row for row in platforms if row.get("enable") and row.get("type") == "aiocqhttp"]
    if not owner.isdigit() or len(qq) != 1:
        raise RuntimeError("The owner and one unambiguous QQ platform must be configured")
    expected_scope = qq[0]["id"] + ":FriendMessage:" + owner
    if config.get("owner_scope") and config["owner_scope"] != expected_scope:
        raise RuntimeError("Refusing to replace an existing owner scope")
    config.update(enabled=True, activity_enabled=True, trial_mode=True, share_enabled=False,
                  memory_enabled=True, owner_scope=expected_scope)
    write(root / CONFIG, config)
    client.reload()
    return client.trial("status")


def record_snapshot(client, trial_id, root=RUNTIME_ROOT, *, observation=None):
    if not re.fullmatch(r"[a-f0-9]{32}", trial_id):
        raise ValueError("Invalid trial ID")
    folder = root / "run/life_trials" / trial_id
    status = client.trial("status")
    if observation and observation.get("recipient_confirmed"):
        if not any(row["request_key"] == "phase5:one-authorized-fixed-message:v1"
                   for row in status.get("deliveries", [])):
            raise ValueError("No recorded probe delivery to confirm")
    now = datetime.now(timezone.utc).isoformat()
    record = {"observed_at_utc": now, "status": status, "source_manifest": manifest(),
              "installed_code_matches": preflight(root)["installed_code_matches"],
              "observation": observation, "offline": False}
    path = folder / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-"
                     + uuid.uuid4().hex[:8] + ".json")
    write(path, record)
    return report(folder)


def report(folder):
    sys.path.insert(0, str(SOURCE_ROOT / "plugins"))
    from astrbot_plugin_life_continuity.activity_state import SHANGHAI
    records = [read(path) for path in folder.glob("*.json")]
    records = [row for row in records if row.get("offline") is False]
    records.sort(key=lambda row: row["observed_at_utc"])
    days = sorted({datetime.fromisoformat(row["observed_at_utc"]).astimezone(SHANGHAI)
                   .date().isoformat() for row in records})
    status = records[-1]["status"] if records else {}
    key = "phase5:run:" + folder.name
    runs = [row for row in status.get("runs", []) if row.get("source_id") == key
            or row.get("source_id", "").startswith(key + ":")]
    goal_ids = {row["goal_id"] for row in runs}
    notes = [row for row in status.get("notes", []) if row["goal_id"] in goal_ids]
    source_ids = {identity for row in notes for identity in row["source_ids"]}
    recipient = any(row.get("observation", {}).get("recipient_confirmed")
                    for row in records if isinstance(row.get("observation"), dict))
    completed = sum(row["status"] == "completed" for row in runs)
    recalled = any(row.get("recall_verified") and row["episode_id"] in source_ids
                   for row in status.get("memory_exports", []))
    budget = status.get("budget", {})
    strict = (status.get("trial_mode") is True and budget.get("daily_round_limit") == 1
              and budget.get("started_rounds", 2) <= 1
              and records and records[-1].get("installed_code_matches") is True)
    gates_passed = bool(recipient and completed and recalled and strict)
    revisions = {hashlib.sha256(json.dumps(row.get("source_manifest", {}), sort_keys=True)
                                .encode()).hexdigest() for row in records}
    return {"trial_id": folder.name, "observed_shanghai_days": days,
            "recorded_days": len(days), "target_observation_days": "7-14",
            "observation_window_started": gates_passed,
            "setup_gates_passed": gates_passed,
            "code_manifests_observed": len(revisions),
            "needs_revalidation_after_code_change": len(revisions) > 1,
            "live_runs_completed": completed,
            "note_versions": len(notes),
            "reviewed_outputs": sum(row["run_id"] in {run["id"] for run in runs}
                                    for row in status.get("operator_reviews", [])),
            "transport_acks": sum(row["status"] == "sent" for row in status.get("deliveries", [])),
            "memory_recall_verified": recalled, "recipient_confirmed": recipient,
            "periodic_scheduler": False, "acceptance": "incomplete_manual_review_required"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preflight", "deploy-closed", "enable", "prepare",
                                          "run", "send", "review", "memory", "memory-status",
                                          "recall", "snapshot", "report"))
    parser.add_argument("--trial-id")
    parser.add_argument("--run-id")
    parser.add_argument("--authorize-one-message", action="store_true")
    parser.add_argument("--confirm-received", action="store_true")
    args = parser.parse_args(argv)
    if args.action == "preflight":
        result = preflight()
    elif args.action == "deploy-closed":
        result = deploy_closed()
    elif args.action == "report":
        if not args.trial_id or not re.fullmatch(r"[a-f0-9]{32}", args.trial_id):
            parser.error("--trial-id is required")
        result = report(RUNTIME_ROOT / "run/life_trials" / args.trial_id)
    else:
        client = Dashboard().login()
        if args.action == "enable":
            result = enable_trial(client)
        elif args.action == "prepare":
            path = SOURCE_ROOT / "content/自主生活验收资料.md"
            text = path.read_text(encoding="utf-8")[:2000]
            result = client.trial("prepare", source_name=str(path.relative_to(SOURCE_ROOT)),
                                   source_text=text)
        elif args.action == "run":
            if not args.trial_id:
                parser.error("--trial-id is required")
            result = client.trial("run", trial_id=args.trial_id)
        elif args.action == "send":
            if not args.authorize_one_message:
                parser.error("--authorize-one-message requires explicit owner authorization")
            result = client.trial("send", confirm="SEND_ONE_FIXED_MESSAGE")
        elif args.action == "memory":
            result = client.trial("memory")
        elif args.action == "memory-status":
            result = client.trial("memory_status")
        elif args.action == "recall":
            result = client.trial("recall")
        elif args.action == "review":
            if not args.run_id:
                parser.error("--run-id is required")
            result = client.trial("review", run_id=args.run_id)
        else:
            if not args.trial_id:
                parser.error("--trial-id is required")
            observation = {"recipient_confirmed": True, "source": "explicit_owner_confirmation"}
            result = record_snapshot(client, args.trial_id,
                                     observation=observation if args.confirm_received else None)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Keep HTTP tokens, credentials and complete configuration out of diagnostics.
        print("Life trial operation failed: " + type(exc).__name__, file=sys.stderr)
        raise SystemExit(1)

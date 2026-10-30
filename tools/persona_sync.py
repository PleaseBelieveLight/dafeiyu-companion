"""Preview or synchronize the source persona without replacing other persona settings."""

import argparse
import hashlib
import json
import time
from pathlib import Path
from urllib.parse import quote

from workspace import RUNTIME_ROOT, SOURCE_ROOT

PERSONA_ID = "大肥鱼"


def source_prompt(path: Path) -> str:
    text = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    if "\n---\n" not in text:
        raise ValueError("人格源文件缺少正文分隔线，未同步。")
    prompt = text.split("\n---\n", 1)[1].strip()
    if not prompt:
        raise ValueError("人格源文件正文为空，未同步。")
    return prompt


def synchronize(api, source: Path, backups: Path, *, apply=False):
    prompt = source_prompt(source)
    endpoint = "/personas/by-id?persona_id=" + quote(PERSONA_ID, safe="")
    current = api("GET", endpoint)
    old = str(current.get("system_prompt", "")).strip()
    result = {"matches": old == prompt, "source_chars": len(prompt), "runtime_chars": len(old),
              "source_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
              "applied": False}
    if result["matches"] or not apply:
        return result
    backups.mkdir(parents=True, exist_ok=True)
    backup = backups / f"persona-{time.time_ns()}.json"
    with backup.open("x", encoding="utf-8") as stream:
        json.dump(current, stream, ensure_ascii=False, indent=2)
    # Protect a concurrent dashboard edit between preview and update.
    if api("GET", endpoint) != current:
        raise RuntimeError("后台人格在同步期间发生变化，请核对后重试。")
    api("PUT", "/personas/by-id", json={"persona_id": PERSONA_ID, "system_prompt": prompt})
    verified = api("GET", endpoint)
    if str(verified.get("system_prompt", "")).strip() != prompt:
        raise RuntimeError("人格更新后读回不一致；旧版本已备份，请核对后台状态。")
    result.update(matches=True, applied=True, runtime_chars=len(prompt), backup=str(backup))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="备份后同步正文；默认只预览是否一致")
    args = parser.parse_args(argv)
    import httpx
    credentials = json.loads((RUNTIME_ROOT / "config/local-access.json").read_text(encoding="utf-8-sig"))
    with httpx.Client(base_url="http://127.0.0.1:6185/api/v1", timeout=30, trust_env=False) as client:
        def api(method, path, **kwargs):
            response = client.request(method, path, **kwargs)
            response.raise_for_status()
            value = response.json()
            if value.get("status") == "error":
                raise RuntimeError("人格管理接口未确认成功。")
            return value.get("data", value)
        client.headers["Authorization"] = "Bearer " + api("POST", "/auth/login", json={
            "username": credentials["username"], "password": credentials["password"]
        })["token"]
        result = synchronize(api, SOURCE_ROOT / "content/persona/大肥鱼-人格.md",
                             RUNTIME_ROOT / "backups/personas", apply=args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

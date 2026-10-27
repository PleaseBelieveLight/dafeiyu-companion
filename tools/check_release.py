"""Check the public source set for accidental runtime data and secret values."""

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_DIRS = {
    "tools",
    "plugins",
    "overlays",
    "content",
    "assets",
    "requirements",
    "tests",
    "LICENSES",
    ".github",
    "config",
}
PUBLIC_ROOTS = {
    "AGENTS.md",
    "README.md",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "dependencies.lock.json",
    ".gitignore",
    ".gitattributes",
    "pyproject.toml",
}
SECRET_FIELDS = {
    "owner_qq",
    "bot_qq",
    "friend_qqs",
    "group_ids",
    "deepseek_api_key",
    "dashscope_api_key",
    "image_api_key",
    "chrome_path",
    "ollama_path",
    "emulator_directory",
}


def source_files(root: Path = ROOT):
    """Select distributable files without following generated runtime trees."""
    for path in root.iterdir():
        if path.is_file() and (path.name in PUBLIC_ROOTS or path.suffix == ".cmd"):
            yield path
        elif path.is_dir() and path.name in PUBLIC_DIRS:
            for item in path.rglob("*"):
                if (
                    item.is_file()
                    and "__pycache__" not in item.parts
                    and item.suffix not in {".pyc", ".pyo"}
                ):
                    if path.name != "config" or item.name == "settings.example.json":
                        yield item


def inspect(root: Path = ROOT) -> list[str]:
    """Return concrete public-source violations without printing secret contents."""
    problems = []
    settings = json.loads((root / "config/settings.example.json").read_text(encoding="utf-8-sig"))
    for key in SECRET_FIELDS:
        if settings.get(key):
            problems.append("Nonempty private example field: " + key)
    forbidden = {".db", ".sqlite", ".sqlite3", ".log", ".exe", ".dll", ".apk", ".zip"}
    for path in source_files(root):
        rel = path.relative_to(root).as_posix()
        if path.suffix.lower() in forbidden or path.name in {
            "local-access.json",
            "settings.json",
            "qrcode.png",
            "session_data.json",
        }:
            problems.append("Runtime file in public source: " + rel)
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif"}:
            continue
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        if re.search(r"\bsk-[A-Za-z0-9_-]{20,}\b", text):
            problems.append("Possible API credential in: " + rel)
        if re.search(r"[A-Za-z]:\\Users\\[^\\\s]+\\", text):
            problems.append("Personal absolute path in: " + rel)
    if (root / ".git").exists():
        result = subprocess.run(
            ["git", "ls-files", "--cached", "-z"],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        allowed = {p.relative_to(root).as_posix() for p in source_files(root)}
        for name in filter(None, result.stdout.split("\0")):
            if name not in allowed:
                problems.append("Tracked runtime or unexpected file: " + name)
    return problems


if __name__ == "__main__":
    errors = inspect()
    if errors:
        print("\n".join(errors))
        sys.exit(1)
    files = list(source_files())
    print(
        json.dumps(
            {
                "source_files": len(files),
                "source_bytes": sum(p.stat().st_size for p in files),
                "private_example_fields_empty": True,
                "release_check": "passed",
            },
            indent=2,
        )
    )

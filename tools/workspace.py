"""Resolve private local deployment paths without mixing them with editable code."""

import os
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1]
TOOLS = SOURCE_ROOT / "tools"
DAFEIYU_BASE_REFERENCE = "files/persona_config/persona_ref_image/dafeiyu.png"


def resolve_runtime_root(path: str | Path) -> Path:
    """Allow external runtimes or the ignored .local tree, never public source paths."""
    source = SOURCE_ROOT.resolve()
    path = Path(path).expanduser()
    if not path.is_absolute():
        path = source / path
    path = path.resolve()
    if (
        path == source
        or path in source.parents
        or (source in path.parents and not path.is_relative_to(source / ".local"))
    ):
        raise ValueError("Use .local inside the project or a separate external runtime directory")
    return path


def runtime_root() -> Path:
    """Resolve the local runtime independently of the working directory."""
    return resolve_runtime_root(os.environ.get("DAFEIYU_RUNTIME_ROOT") or ".local")


RUNTIME_ROOT = runtime_root()


def runtime_env(root: Path = RUNTIME_ROOT) -> dict[str, str]:
    """Pass an explicit deployment location to child processes."""
    return dict(os.environ, DAFEIYU_RUNTIME_ROOT=str(root), PYTHONUTF8="1")

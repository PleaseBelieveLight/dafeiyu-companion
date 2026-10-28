"""Keep editable project files separate from the local deployment and its data."""

import os
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1]
TOOLS = SOURCE_ROOT / "tools"


def runtime_root() -> Path:
    """Resolve the sibling runtime directory, independently of the working directory."""
    configured = os.environ.get("DAFEIYU_RUNTIME_ROOT")
    path = Path(configured).expanduser() if configured else SOURCE_ROOT.parent / "Dafeiyu"
    if not path.is_absolute():
        path = SOURCE_ROOT / path
    path = path.resolve()
    if path == SOURCE_ROOT or path in SOURCE_ROOT.parents or SOURCE_ROOT in path.parents:
        raise ValueError("The runtime directory must be separate from the source repository")
    return path


RUNTIME_ROOT = runtime_root()


def runtime_env(root: Path = RUNTIME_ROOT) -> dict[str, str]:
    """Pass an explicit deployment location to child processes."""
    return dict(os.environ, DAFEIYU_RUNTIME_ROOT=str(root), PYTHONUTF8="1")

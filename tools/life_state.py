"""Read-only inspection of the life ledger; never initializes or recovers it."""

import argparse
import json
import sqlite3
import sys

from workspace import RUNTIME_ROOT, SOURCE_ROOT

sys.path.insert(0, str(SOURCE_ROOT / "plugins"))
from astrbot_plugin_life_continuity.state import PLUGIN_NAME, StateError, read_snapshot


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", help="Exact session scope; omit to return only counts")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(argv)
    folder = RUNTIME_ROOT / "astrbot/data/plugin_data" / PLUGIN_NAME
    try:
        result = read_snapshot(folder, scope=args.scope, limit=args.limit)
    except (OSError, sqlite3.Error, StateError, ValueError) as exc:
        print(f"Life state unavailable: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

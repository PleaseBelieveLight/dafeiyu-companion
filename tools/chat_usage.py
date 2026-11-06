"""Read aggregate provider-boundary usage without exposing chat or identity data."""

import argparse
import json
import math
import sqlite3
from contextlib import closing
from pathlib import Path
from statistics import median

from workspace import RUNTIME_ROOT


def report(path: Path, days: int = 7):
    if not path.exists():
        return {"ready": False, "calls": 0, "note": "部署后首次模型请求才会生成用量记录。"}
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute("""SELECT source,provider,model,COUNT(*) AS attempts,
            SUM(status!='returned') AS uncertain_attempts,
            SUM(input_total IS NULL) AS unknown_input_attempts,
            SUM(input_cached IS NULL) AS unknown_cache_attempts,
            SUM(input_total) AS known_input, SUM(input_other) AS known_uncached,
            SUM(input_cached) AS known_cached, SUM(output) AS known_output
            FROM calls WHERE started>=strftime('%s','now')-?*86400
            GROUP BY source,provider,model ORDER BY attempts DESC""", (days,)).fetchall()
        measured = {}
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_turns'").fetchone():
            turns = con.execute("""SELECT kind,generation_calls,first_reply,media,transport
                FROM chat_turns WHERE at>=strftime('%s','now')-?*86400""", (days,)).fetchall()
            for kind in sorted({row["kind"] for row in turns}):
                group = [row for row in turns if row["kind"] == kind]
                sent = [row for row in group if row["first_reply"] is not None]
                times = sorted(row["first_reply"] for row in sent)
                measured[kind] = {"turns": len(group), "first_replies": len(sent),
                    "single_generation": sum(row["generation_calls"] == 1 for row in sent),
                    "first_reply_p50_ms": round(1000 * median(times)) if times else None,
                    "first_reply_p95_ms": round(1000 * times[math.ceil(len(times) * .95) - 1]) if times else None,
                    "media_p50_ms": round(1000 * median(row["media"] for row in group)),
                    "transport_p50_ms": round(1000 * median(row["transport"] for row in sent)) if sent else None}
    return {"ready": True, "days": days, "groups": [dict(row) for row in rows], "qq_turns": measured,
            "coverage": "AstrBot OpenAI兼容聊天适配器及浏览器内层的SDK尝试；TTS和生图不在此表。"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args(argv)
    if not 1 <= args.days <= 365:
        parser.error("days must be between 1 and 365")
    path = RUNTIME_ROOT / "astrbot/data/plugin_data/dafeiyu_context/context.db"
    print(json.dumps(report(path, args.days), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

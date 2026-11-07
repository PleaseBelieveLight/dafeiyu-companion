"""Switch, inspect and export private model request diagnostics."""

import argparse
import json

from workspace import RUNTIME_ROOT
from model_usage import usage_module


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("on", "off", "status", "list", "latest", "export"), nargs="?", default="status")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--id", help="记录 ID；省略时使用最新一条")
    parser.add_argument("--latest", action="store_true", help="导出最新一条记录")
    parser.add_argument("--keep-days", type=int)
    parser.add_argument("--max-mb", type=int)
    args = parser.parse_args(argv)
    if args.id and args.latest:
        parser.error("Choose either --id or --latest")
    if not 1 <= args.limit <= 1000:
        parser.error("limit must be between 1 and 1000")
    if args.keep_days is not None and not 1 <= args.keep_days <= 365:
        parser.error("keep-days must be between 1 and 365")
    if args.max_mb is not None and not 1 <= args.max_mb <= 16384:
        parser.error("max-mb must be between 1 and 16384")
    module = usage_module().trace_module()
    base = RUNTIME_ROOT / "astrbot/data/plugin_data/dafeiyu_context"
    if args.action in {"on", "off"}:
        module.configure(base, args.action == "on", retention_days=args.keep_days, max_total_mb=args.max_mb)
    values = module.settings(base)
    rows = module.records(base)
    if args.action == "export":
        try:
            output = module.export_request(base, args.id)
        except ValueError as exc:
            parser.error(str(exc))
        print(str(output))
        return 0
    if args.action == "latest":
        module.refresh_index(base)
        output = module.trace_root(base) / (f'requests/{rows[0]["id"]}/index.html' if rows else "index.html")
        print(str(output))
        return 0
    if args.action == "list":
        for row in rows[:args.limit]:
            print(f'{module.view().stamp(row["at"])}  {module.view().SOURCE_NAMES.get(row["source"],row["source"])}'
                  f'  {row.get("model", "")}  {row.get("status", "")}  {row["id"]}')
        if not rows:
            print("尚无已捕获请求。")
        return 0
    print(json.dumps({"enabled": values["enabled"], "records": len(rows),
                      "retention_days": values["retention_days"], "max_total_mb": values["max_total_mb"],
                      "index": str(module.trace_root(base) / "index.html"),
                      "config": str(base / "request-trace.json")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

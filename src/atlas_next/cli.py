from __future__ import annotations

import argparse
import json
from pathlib import Path

from .health import snapshot
from .store import Store


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="atlas-next")
    parser.add_argument("--db", type=Path, default=Path(".atlas-next/state.sqlite3"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="create or verify the ledger schema")
    sub.add_parser("status", help="report effective readiness; execution is disabled")
    enqueue = sub.add_parser("enqueue", help="add inert work to the ledger")
    enqueue.add_argument("action")
    enqueue.add_argument("--payload", default="{}", help="JSON object")
    enqueue.add_argument("--max-attempts", type=int, default=1)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    with Store(args.db) as store:
        if args.command == "init":
            print(json.dumps({"ok": True, "db": str(args.db)}))
            return 0
        if args.command == "enqueue":
            payload = json.loads(args.payload)
            if not isinstance(payload, dict):
                raise SystemExit("--payload must be a JSON object")
            item = store.enqueue(args.action, payload, max_attempts=args.max_attempts)
            print(json.dumps({"id": item.id, "state": item.state, "action": item.action}))
            return 0
        report = snapshot(
            store,
            execution_enabled=False,
            executor_running=False,
            registered_actions=set(),
            worker_id="not-configured",
        )
        print(json.dumps(report, indent=2, sort_keys=True))
        if report["overall"] == "ok":
            return 0
        return 2 if report["overall"] == "paused" else 1


if __name__ == "__main__":
    raise SystemExit(main())

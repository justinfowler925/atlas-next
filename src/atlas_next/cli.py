from __future__ import annotations

import argparse
import json
from pathlib import Path

from .delivery import (
    COMMIT_SOURCE_ACTION,
    OPEN_PR_ACTION,
    VERIFY_PR_ACTION,
    CommitSource,
    OpenPr,
    VerifyPr,
)
from .health import snapshot
from .engine import Engine, Handler
from .salesforce import (
    COUNT_ACTION,
    DESCRIBE_ACTION,
    PICKLIST_COUNTS_ACTION,
    SalesforceCount,
    SalesforceDescribe,
    SalesforcePicklistCounts,
)
from .salesforce_query import QUERY_ACTION, SalesforceQuery
from .salesforce_test import APEX_TEST_ACTION, SalesforceApexTest
from .salesforce_metadata import (
    METADATA_CONTENT_DIFF_ACTION,
    METADATA_DIFF_ACTION,
    SOURCE_RETRIEVE_ACTION,
    SalesforceMetadataContentDiff,
    SalesforceMetadataDiff,
    SalesforceSourceRetrieve,
)
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
    inspect = sub.add_parser(
        "salesforce-inspect", help="run one hardcoded read-only Salesforce describe"
    )
    inspect.add_argument("object")
    inspect.add_argument("--environment", choices=("partial", "prod"), default="partial")
    inspect.add_argument("--partial-alias", default="dod-check")
    inspect.add_argument("--prod-alias", default="prod")
    count = sub.add_parser(
        "salesforce-count", help="count one Salesforce object with generated read-only SOQL"
    )
    count.add_argument("object")
    count.add_argument("--environment", choices=("partial", "prod"), default="partial")
    count.add_argument("--partial-alias", default="dod-check")
    count.add_argument("--prod-alias", default="prod")
    picklist = sub.add_parser(
        "salesforce-picklist-counts",
        help="return a capped distribution for one live-validated picklist",
    )
    picklist.add_argument("object")
    picklist.add_argument("field")
    picklist.add_argument("--environment", choices=("partial", "prod"), default="partial")
    picklist.add_argument("--partial-alias", default="dod-check")
    picklist.add_argument("--prod-alias", default="prod")
    query = sub.add_parser(
        "salesforce-query",
        help="run a bounded live-schema-validated Salesforce record query",
    )
    query.add_argument("object")
    query.add_argument("--fields", required=True, help="comma-separated field API names")
    query.add_argument("--filter-json", default="[]", help="structured filter list; never SOQL")
    query.add_argument("--order-field")
    query.add_argument("--order-direction", choices=("asc", "desc"), default="asc")
    query.add_argument("--limit", type=int, default=100)
    query.add_argument("--environment", choices=("partial", "prod"), default="partial")
    query.add_argument("--partial-alias", default="dod-check")
    query.add_argument("--prod-alias", default="prod")
    metadata = sub.add_parser(
        "salesforce-metadata-diff",
        help="compare one supported metadata inventory between Partial and prod",
    )
    metadata.add_argument("type")
    metadata.add_argument("--partial-alias", default="dod-check")
    metadata.add_argument("--prod-alias", default="prod")
    content = sub.add_parser(
        "salesforce-metadata-content-diff",
        help="retrieve and byte-compare one exact component across Partial and prod",
    )
    content.add_argument("type")
    content.add_argument("name")
    content.add_argument("--partial-alias", default="dod-check")
    content.add_argument("--prod-alias", default="prod")
    content.add_argument(
        "--project-dir",
        type=Path,
        default=Path("/Users/justinfowler/Projects/sfdc/salesforce"),
    )
    content.add_argument(
        "--artifact-root", type=Path, default=Path(".atlas-next/artifacts/metadata-diff")
    )
    apex_test = sub.add_parser(
        "salesforce-apex-test",
        help="run one to ten named Apex test classes in Partial only",
    )
    apex_test.add_argument("classes", nargs="+")
    apex_test.add_argument("--partial-alias", default="dod-check")
    retrieve = sub.add_parser(
        "salesforce-retrieve-source",
        help="retrieve one exact Partial component into a clean isolated worktree",
    )
    retrieve.add_argument("type")
    retrieve.add_argument("name")
    retrieve.add_argument("--partial-alias", default="dod-check")
    retrieve.add_argument("--project-dir", type=Path, required=True)
    commit = sub.add_parser(
        "commit-source",
        help="commit only files proven by successful source-producing work items",
    )
    commit.add_argument("source_work_ids", nargs="+")
    commit.add_argument("--message", required=True)
    open_pr = sub.add_parser(
        "open-pr",
        help="push evidence-linked commits and open one current-main SFDC pull request",
    )
    open_pr.add_argument("commit_work_ids", nargs="+")
    open_pr.add_argument("--title", required=True)
    open_pr.add_argument("--body", required=True)
    verify_pr = sub.add_parser(
        "verify-pr",
        help="wait for required PR checks and prove current-main merge readiness",
    )
    verify_pr.add_argument("open_pr_work_id")
    return parser


def _execute(store: Store, action: str, payload: dict, capability: Handler) -> int:
    item = store.enqueue(action, payload, max_attempts=1)
    completed = Engine(
        store,
        {action: capability},
        worker_id="operator:cli",
        execution_enabled=True,
    ).run_once(work_id=item.id).item
    if completed is None:
        raise RuntimeError(f"operator item {item.id} disappeared")
    print(
        json.dumps(
            {
                "id": completed.id,
                "state": completed.state,
                "result": completed.result,
                "evidence": completed.evidence,
                "error": completed.error,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if completed.state == "succeeded" else 1


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
        if args.command == "salesforce-inspect":
            return _execute(
                store,
                DESCRIBE_ACTION,
                {"environment": args.environment, "object": args.object},
                SalesforceDescribe({"partial": args.partial_alias, "prod": args.prod_alias}),
            )
        if args.command == "salesforce-count":
            return _execute(
                store,
                COUNT_ACTION,
                {"environment": args.environment, "object": args.object},
                SalesforceCount({"partial": args.partial_alias, "prod": args.prod_alias}),
            )
        if args.command == "salesforce-picklist-counts":
            return _execute(
                store,
                PICKLIST_COUNTS_ACTION,
                {
                    "environment": args.environment,
                    "object": args.object,
                    "field": args.field,
                },
                SalesforcePicklistCounts(
                    {"partial": args.partial_alias, "prod": args.prod_alias}
                ),
            )
        if args.command == "salesforce-query":
            filters = json.loads(args.filter_json)
            if not isinstance(filters, list):
                raise SystemExit("--filter-json must be a JSON list")
            payload = {
                "environment": args.environment,
                "object": args.object,
                "fields": [field.strip() for field in args.fields.split(",")],
                "filters": filters,
                "limit": args.limit,
            }
            if args.order_field:
                payload["order_by"] = {
                    "field": args.order_field,
                    "direction": args.order_direction,
                }
            return _execute(
                store,
                QUERY_ACTION,
                payload,
                SalesforceQuery({"partial": args.partial_alias, "prod": args.prod_alias}),
            )
        if args.command == "salesforce-metadata-diff":
            return _execute(
                store,
                METADATA_DIFF_ACTION,
                {"type": args.type},
                SalesforceMetadataDiff(
                    {"partial": args.partial_alias, "prod": args.prod_alias}
                ),
            )
        if args.command == "salesforce-metadata-content-diff":
            return _execute(
                store,
                METADATA_CONTENT_DIFF_ACTION,
                {"type": args.type, "name": args.name},
                SalesforceMetadataContentDiff(
                    {"partial": args.partial_alias, "prod": args.prod_alias},
                    project_dir=args.project_dir,
                    artifact_root=args.artifact_root,
                ),
            )
        if args.command == "salesforce-apex-test":
            return _execute(
                store,
                APEX_TEST_ACTION,
                {"classes": args.classes},
                SalesforceApexTest(partial_alias=args.partial_alias),
            )
        if args.command == "salesforce-retrieve-source":
            return _execute(
                store,
                SOURCE_RETRIEVE_ACTION,
                {"type": args.type, "name": args.name},
                SalesforceSourceRetrieve(
                    partial_alias=args.partial_alias,
                    project_dir=args.project_dir,
                ),
            )
        if args.command == "commit-source":
            return _execute(
                store,
                COMMIT_SOURCE_ACTION,
                {"source_work_ids": args.source_work_ids, "message": args.message},
                CommitSource(store),
            )
        if args.command == "open-pr":
            return _execute(
                store,
                OPEN_PR_ACTION,
                {
                    "commit_work_ids": args.commit_work_ids,
                    "title": args.title,
                    "body": args.body,
                },
                OpenPr(store),
            )
        if args.command == "verify-pr":
            return _execute(
                store,
                VERIFY_PR_ACTION,
                {"open_pr_work_id": args.open_pr_work_id},
                VerifyPr(store),
            )
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

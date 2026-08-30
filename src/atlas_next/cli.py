from __future__ import annotations

import argparse
import json
from pathlib import Path

from .delivery import (
    COMMIT_SOURCE_ACTION,
    MERGE_PR_ACTION,
    OPEN_PR_ACTION,
    VERIFY_PR_ACTION,
    VERIFY_SANDBOX_DEPLOY_ACTION,
    CommitSource,
    MergePr,
    OpenPr,
    VerifyPr,
    VerifySandboxDeploy,
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
from .source_author import AUTHOR_SOURCE_ACTION, AuthorSource
from .flow_source import CREATE_FLOW_SOURCE_ACTION, CreateFlowSource
from .lwc_source import CREATE_LWC_SOURCE_ACTION, CreateLwcSource
from .salesforce_lwc import VERIFY_LWC_DEPLOYMENT_ACTION, VerifyLwcDeployment
from .report_source import CREATE_REPORT_SOURCE_ACTION, CreateReportSource
from .salesforce_report import VERIFY_REPORT_EXECUTION_ACTION, VerifyReportExecution
from .integration_source import CREATE_INTEGRATION_SOURCE_ACTION, CreateIntegrationSource
from .salesforce_integration import (
    VERIFY_INTEGRATION_EXECUTION_ACTION,
    VerifyIntegrationExecution,
)
from .salesforce_authenticated import AUTHENTICATED_GET_ACTION, SalesforceAuthenticatedGet
from .salesforce_flow import (
    RUN_CREATED_FLOW_ACTION,
    VERIFY_FLOW_ACTIVATION_ACTION,
    RunCreatedFlow,
    VerifyFlowActivation,
)
from .salesforce_data import (
    ROLLBACK_UPDATE_ACTION,
    UPDATE_RECORDS_ACTION,
    SalesforceRollbackUpdate,
    SalesforceUpdateRecords,
)


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
    author = sub.add_parser(
        "salesforce-author-source",
        help="replace one hash-locked file from a successful source retrieve receipt",
    )
    author.add_argument("retrieve_work_id")
    author.add_argument("--path", required=True)
    author.add_argument("--expected-sha256", required=True)
    author.add_argument("--content-file", type=Path, required=True)
    create_flow = sub.add_parser(
        "salesforce-create-flow-source",
        help="create one active Flow XML file in a clean isolated worktree",
    )
    create_flow.add_argument("name")
    create_flow.add_argument("--content-file", type=Path, required=True)
    create_flow.add_argument("--project-dir", type=Path, required=True)
    verify_flow = sub.add_parser(
        "salesforce-verify-flow-activation",
        help="prove an Atlas-created Flow has ActiveVersion equal to LatestVersion",
    )
    verify_flow.add_argument("deploy_work_id")
    verify_flow.add_argument("source_work_id")
    verify_flow.add_argument("--partial-alias", default="dod-check")
    run_flow = sub.add_parser(
        "salesforce-run-created-flow",
        help="execute an activation-proven Atlas-created Flow and assert one output",
    )
    run_flow.add_argument("activation_work_id")
    run_flow.add_argument("--output-variable", required=True)
    run_flow.add_argument("--expected-string", required=True)
    run_flow.add_argument("--partial-alias", default="dod-check")
    run_flow.add_argument("--artifact-root", type=Path, default=Path(".atlas-next/artifacts"))
    update_records = sub.add_parser(
        "salesforce-update-records",
        help="atomically update 1-10 schema-validated Partial records",
    )
    update_records.add_argument("object")
    update_records.add_argument("--records-json", required=True)
    update_records.add_argument("--reason", required=True)
    update_records.add_argument("--partial-alias", default="dod-check")
    update_records.add_argument(
        "--artifact-root", type=Path, default=Path(".atlas-next/artifacts")
    )
    rollback = sub.add_parser(
        "salesforce-rollback-update",
        help="restore an Atlas update only when its verified post-state has not drifted",
    )
    rollback.add_argument("update_work_id")
    rollback.add_argument("--reason", required=True)
    rollback.add_argument("--partial-alias", default="dod-check")
    rollback.add_argument("--artifact-root", type=Path, default=Path(".atlas-next/artifacts"))
    create_lwc = sub.add_parser(
        "salesforce-create-lwc-source",
        help="create one complete record-page LWC bundle with a behavioral Jest test",
    )
    create_lwc.add_argument("name")
    create_lwc.add_argument("--source-dir", type=Path, required=True)
    create_lwc.add_argument("--project-dir", type=Path, required=True)
    verify_lwc = sub.add_parser(
        "salesforce-verify-lwc-deployment",
        help="prove an Atlas-created LWC passed Jest and exists in live Partial metadata",
    )
    verify_lwc.add_argument("deploy_work_id")
    verify_lwc.add_argument("source_work_id")
    verify_lwc.add_argument("--partial-alias", default="dod-check")
    create_report = sub.add_parser(
        "salesforce-create-report-source",
        help="create one bounded public Salesforce report in a clean isolated worktree",
    )
    create_report.add_argument("name")
    create_report.add_argument("--content-file", type=Path, required=True)
    create_report.add_argument("--project-dir", type=Path, required=True)
    verify_report = sub.add_parser(
        "salesforce-verify-report-execution",
        help="prove an Atlas-created report exists and executes in live Partial",
    )
    verify_report.add_argument("deploy_work_id")
    verify_report.add_argument("source_work_id")
    verify_report.add_argument("--partial-alias", default="dod-check")
    create_integration = sub.add_parser(
        "salesforce-create-integration-source",
        help="create a bounded Apex REST integration, mock test, and Remote Site",
    )
    create_integration.add_argument("name")
    create_integration.add_argument("--base-url", required=True)
    create_integration.add_argument("--path", required=True)
    create_integration.add_argument("--marker-field", required=True)
    create_integration.add_argument("--expected-marker", required=True)
    create_integration.add_argument("--project-dir", type=Path, required=True)
    verify_integration = sub.add_parser(
        "salesforce-verify-integration-execution",
        help="execute a deployed Atlas-created REST integration in live Partial",
    )
    verify_integration.add_argument("deploy_work_id")
    verify_integration.add_argument("source_work_id")
    verify_integration.add_argument("--partial-alias", default="dod-check")
    verify_integration.add_argument(
        "--artifact-root", type=Path, default=Path(".atlas-next/artifacts")
    )
    authenticated_get = sub.add_parser(
        "salesforce-authenticated-get",
        help="perform one secret-safe credential-bound HTTP GET from Partial",
    )
    authenticated_get.add_argument("named_credential")
    authenticated_get.add_argument("path")
    authenticated_get.add_argument("--external-credential", required=True)
    authenticated_get.add_argument("--credential-parameter", required=True)
    authenticated_get.add_argument("--expected-status", type=int, default=200)
    authenticated_get.add_argument("--partial-alias", default="dod-check")
    authenticated_get.add_argument(
        "--artifact-root", type=Path, default=Path(".atlas-next/artifacts")
    )
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
    merge_pr = sub.add_parser(
        "merge-pr",
        help="re-verify and squash-merge one verified current-main SFDC pull request",
    )
    merge_pr.add_argument("verify_pr_work_id")
    verify_deploy = sub.add_parser(
        "verify-sandbox-deploy",
        help="prove the exact merged SHA completed the governed Partial deployment",
    )
    verify_deploy.add_argument("merge_pr_work_id")
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
        if args.command == "salesforce-author-source":
            return _execute(
                store,
                AUTHOR_SOURCE_ACTION,
                {
                    "retrieve_work_id": args.retrieve_work_id,
                    "path": args.path,
                    "expected_sha256": args.expected_sha256,
                    "content": args.content_file.read_text(encoding="utf-8"),
                },
                AuthorSource(store),
            )
        if args.command == "salesforce-create-flow-source":
            return _execute(
                store,
                CREATE_FLOW_SOURCE_ACTION,
                {
                    "name": args.name,
                    "content": args.content_file.read_text(encoding="utf-8"),
                },
                CreateFlowSource(project_dir=args.project_dir),
            )
        if args.command == "salesforce-verify-flow-activation":
            return _execute(
                store,
                VERIFY_FLOW_ACTIVATION_ACTION,
                {
                    "deploy_work_id": args.deploy_work_id,
                    "source_work_id": args.source_work_id,
                },
                VerifyFlowActivation(store, partial_alias=args.partial_alias),
            )
        if args.command == "salesforce-run-created-flow":
            return _execute(
                store,
                RUN_CREATED_FLOW_ACTION,
                {
                    "activation_work_id": args.activation_work_id,
                    "output_variable": args.output_variable,
                    "expected_string": args.expected_string,
                },
                RunCreatedFlow(
                    store,
                    partial_alias=args.partial_alias,
                    artifact_root=args.artifact_root,
                ),
            )
        if args.command == "salesforce-update-records":
            records = json.loads(args.records_json)
            return _execute(
                store,
                UPDATE_RECORDS_ACTION,
                {"object": args.object, "records": records, "reason": args.reason},
                SalesforceUpdateRecords(
                    partial_alias=args.partial_alias,
                    artifact_root=args.artifact_root,
                ),
            )
        if args.command == "salesforce-rollback-update":
            return _execute(
                store,
                ROLLBACK_UPDATE_ACTION,
                {"update_work_id": args.update_work_id, "reason": args.reason},
                SalesforceRollbackUpdate(
                    store,
                    partial_alias=args.partial_alias,
                    artifact_root=args.artifact_root,
                ),
            )
        if args.command == "salesforce-create-lwc-source":
            filenames = [
                f"{args.name}.js",
                f"{args.name}.html",
                f"{args.name}.css",
                f"{args.name}.js-meta.xml",
                f"__tests__/{args.name}.test.js",
            ]
            files = {
                filename: (args.source_dir / filename).read_text(encoding="utf-8")
                for filename in filenames
            }
            return _execute(
                store,
                CREATE_LWC_SOURCE_ACTION,
                {"name": args.name, "files": files},
                CreateLwcSource(project_dir=args.project_dir),
            )
        if args.command == "salesforce-verify-lwc-deployment":
            return _execute(
                store,
                VERIFY_LWC_DEPLOYMENT_ACTION,
                {
                    "deploy_work_id": args.deploy_work_id,
                    "source_work_id": args.source_work_id,
                },
                VerifyLwcDeployment(store, partial_alias=args.partial_alias),
            )
        if args.command == "salesforce-create-report-source":
            return _execute(
                store,
                CREATE_REPORT_SOURCE_ACTION,
                {
                    "name": args.name,
                    "content": args.content_file.read_text(encoding="utf-8"),
                },
                CreateReportSource(project_dir=args.project_dir),
            )
        if args.command == "salesforce-verify-report-execution":
            return _execute(
                store,
                VERIFY_REPORT_EXECUTION_ACTION,
                {
                    "deploy_work_id": args.deploy_work_id,
                    "source_work_id": args.source_work_id,
                },
                VerifyReportExecution(store, partial_alias=args.partial_alias),
            )
        if args.command == "salesforce-create-integration-source":
            return _execute(
                store,
                CREATE_INTEGRATION_SOURCE_ACTION,
                {
                    "name": args.name,
                    "base_url": args.base_url,
                    "path": args.path,
                    "marker_field": args.marker_field,
                    "expected_marker": args.expected_marker,
                },
                CreateIntegrationSource(project_dir=args.project_dir),
            )
        if args.command == "salesforce-verify-integration-execution":
            return _execute(
                store,
                VERIFY_INTEGRATION_EXECUTION_ACTION,
                {
                    "deploy_work_id": args.deploy_work_id,
                    "source_work_id": args.source_work_id,
                },
                VerifyIntegrationExecution(
                    store,
                    partial_alias=args.partial_alias,
                    artifact_root=args.artifact_root,
                ),
            )
        if args.command == "salesforce-authenticated-get":
            return _execute(
                store,
                AUTHENTICATED_GET_ACTION,
                {
                    "credential_parameter": args.credential_parameter,
                    "expected_status": args.expected_status,
                    "external_credential": args.external_credential,
                    "named_credential": args.named_credential,
                    "path": args.path,
                },
                SalesforceAuthenticatedGet(
                    partial_alias=args.partial_alias,
                    artifact_root=args.artifact_root,
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
        if args.command == "merge-pr":
            return _execute(
                store,
                MERGE_PR_ACTION,
                {"verify_pr_work_id": args.verify_pr_work_id},
                MergePr(store),
            )
        if args.command == "verify-sandbox-deploy":
            return _execute(
                store,
                VERIFY_SANDBOX_DEPLOY_ACTION,
                {"merge_pr_work_id": args.merge_pr_work_id},
                VerifySandboxDeploy(store),
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

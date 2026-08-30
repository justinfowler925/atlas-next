from __future__ import annotations

import json

from atlas_next import Engine, Store, WorkState
from atlas_next.delivery import (
    COMMIT_SOURCE_ACTION,
    MERGE_PR_ACTION,
    OPEN_PR_ACTION,
    VERIFY_PR_ACTION,
    VERIFY_SANDBOX_DEPLOY_ACTION,
)
from atlas_next.report_source import CREATE_REPORT_SOURCE_ACTION
from atlas_next.salesforce import CommandResult
from atlas_next.salesforce_report import (
    VERIFY_REPORT_EXECUTION_ACTION,
    VerifyReportExecution,
)


def _succeed(store, action, result):
    item = store.enqueue(action, {})
    assert store.claim(item.id, "proof") is not None
    store.succeed(item.id, "proof", result=result, evidence=[{"kind": action}])
    return store.get(item.id)


def _chain(store):
    source = _succeed(
        store,
        CREATE_REPORT_SOURCE_ACTION,
        {"name": "Atlas_Acceptance_Opportunity_Report"},
    )
    commit = _succeed(store, COMMIT_SOURCE_ACTION, {"source_work_ids": [source.id]})
    opened = _succeed(store, OPEN_PR_ACTION, {"commit_work_ids": [commit.id]})
    verified = _succeed(store, VERIFY_PR_ACTION, {"open_pr_work_id": opened.id})
    merged = _succeed(store, MERGE_PR_ACTION, {"verify_pr_work_id": verified.id})
    deploy = _succeed(store, VERIFY_SANDBOX_DEPLOY_ACTION, {"merge_pr_work_id": merged.id})
    return source, deploy


def test_report_execution_proves_lineage_identity_and_runtime(tmp_path):
    commands = []
    with Store(tmp_path / "state.sqlite3") as store:
        source, deploy = _chain(store)

        def runner(argv, _timeout):
            commands.append(list(argv))
            if argv[1:3] == ["data", "query"]:
                return CommandResult(
                    0,
                    json.dumps(
                        {
                            "result": {
                                "records": [
                                    {
                                        "Id": "00O000000000001AAA",
                                        "DeveloperName": "Atlas_Acceptance_Opportunity_Report",
                                        "Name": "Atlas Acceptance Opportunity Report",
                                        "FolderName": "Public Reports",
                                    }
                                ]
                            }
                        }
                    ),
                    "",
                )
            return CommandResult(
                0,
                json.dumps(
                    {
                        "attributes": {"reportId": "00O000000000001AAA"},
                        "factMap": {
                            "T!T": {
                                "aggregates": [
                                    {"value": 125000},
                                    {"value": 7},
                                ]
                            }
                        },
                        "reportMetadata": {
                            "id": "00O000000000001AAA",
                            "developerName": "Atlas_Acceptance_Opportunity_Report",
                            "detailColumns": ["OPPORTUNITY_NAME", "AMOUNT"],
                            "reportFormat": "TABULAR",
                            "reportType": {"type": "Opportunity"},
                            "aggregates": ["s!AMOUNT", "RowCount"],
                        },
                    }
                ),
                "",
            )

        item = store.enqueue(
            VERIFY_REPORT_EXECUTION_ACTION,
            {"deploy_work_id": deploy.id, "source_work_id": source.id},
        )
        completed = (
            Engine(
                store,
                {
                    VERIFY_REPORT_EXECUTION_ACTION: VerifyReportExecution(
                        store, partial_alias="dod-check", runner=runner
                    )
                },
                worker_id="test",
                execution_enabled=True,
            )
            .run_once(work_id=item.id)
            .item
        )

    assert commands[1] == [
        "sf",
        "api",
        "request",
        "rest",
        "/services/data/v65.0/analytics/reports/00O000000000001AAA?includeDetails=false",
        "--target-org",
        "dod-check",
    ]
    assert completed is not None and completed.state is WorkState.SUCCEEDED
    assert completed.result["row_count"] == 7
    assert completed.evidence[0]["executed"] is True
    assert completed.evidence[0]["production_execution"] is False


def test_report_execution_refuses_unlinked_source_before_org_call(tmp_path):
    called = False
    with Store(tmp_path / "state.sqlite3") as store:
        source, deploy = _chain(store)
        other = _succeed(
            store,
            CREATE_REPORT_SOURCE_ACTION,
            {"name": "Atlas_Other_Report"},
        )

        def runner(*_args):
            nonlocal called
            called = True
            return CommandResult(0, "{}", "")

        item = store.enqueue(
            VERIFY_REPORT_EXECUTION_ACTION,
            {"deploy_work_id": deploy.id, "source_work_id": other.id},
        )
        completed = (
            Engine(
                store,
                {
                    VERIFY_REPORT_EXECUTION_ACTION: VerifyReportExecution(
                        store, partial_alias="dod-check", runner=runner
                    )
                },
                worker_id="test",
                execution_enabled=True,
            )
            .run_once(work_id=item.id)
            .item
        )

    assert source.id != other.id
    assert called is False
    assert completed is not None and completed.state is WorkState.FAILED
    assert "not in the deployed PR lineage" in (completed.error or "")


def test_report_execution_rejects_missing_row_count(tmp_path):
    with Store(tmp_path / "state.sqlite3") as store:
        source, deploy = _chain(store)
        responses = iter(
            [
                CommandResult(
                    0,
                    json.dumps(
                        {
                            "result": {
                                "records": [
                                    {
                                        "Id": "00O000000000001AAA",
                                        "DeveloperName": "Atlas_Acceptance_Opportunity_Report",
                                    }
                                ]
                            }
                        }
                    ),
                    "",
                ),
                CommandResult(
                    0,
                    json.dumps(
                        {
                            "attributes": {"reportId": "00O000000000001AAA"},
                            "factMap": {"T!T": {"aggregates": []}},
                            "reportMetadata": {
                                "id": "00O000000000001AAA",
                                "developerName": "Atlas_Acceptance_Opportunity_Report",
                                "detailColumns": ["OPPORTUNITY_NAME"],
                                "reportFormat": "TABULAR",
                                "reportType": {"type": "Opportunity"},
                                "aggregates": [],
                            },
                        }
                    ),
                    "",
                ),
            ]
        )
        item = store.enqueue(
            VERIFY_REPORT_EXECUTION_ACTION,
            {"deploy_work_id": deploy.id, "source_work_id": source.id},
        )
        completed = (
            Engine(
                store,
                {
                    VERIFY_REPORT_EXECUTION_ACTION: VerifyReportExecution(
                        store, partial_alias="dod-check", runner=lambda *_args: next(responses)
                    )
                },
                worker_id="test",
                execution_enabled=True,
            )
            .run_once(work_id=item.id)
            .item
        )

    assert completed is not None and completed.state is WorkState.FAILED
    assert "no RowCount aggregate" in (completed.error or "")

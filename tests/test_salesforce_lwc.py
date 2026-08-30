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
from atlas_next.lwc_source import CREATE_LWC_SOURCE_ACTION
from atlas_next.salesforce import CommandResult
from atlas_next.salesforce_lwc import VERIFY_LWC_DEPLOYMENT_ACTION, VerifyLwcDeployment
from atlas_next.source_author import AUTHOR_SOURCE_ACTION


def _succeed(store, action, result):
    item = store.enqueue(action, {})
    assert store.claim(item.id, "proof") is not None
    store.succeed(item.id, "proof", result=result, evidence=[{"kind": action}])
    return store.get(item.id)


def test_lwc_deployment_proves_lineage_jest_and_live_metadata(tmp_path):
    commands = []
    with Store(tmp_path / "state.sqlite3") as store:
        source = _succeed(
            store, CREATE_LWC_SOURCE_ACTION, {"name": "atlasAcceptanceCard"}
        )
        commit = _succeed(store, COMMIT_SOURCE_ACTION, {"source_work_ids": [source.id]})
        opened = _succeed(store, OPEN_PR_ACTION, {"commit_work_ids": [commit.id]})
        verified = _succeed(
            store,
            VERIFY_PR_ACTION,
            {"open_pr_work_id": opened.id, "checks": {"LWC unit tests": "SUCCESS"}},
        )
        merged = _succeed(store, MERGE_PR_ACTION, {"verify_pr_work_id": verified.id})
        deploy = _succeed(
            store,
            VERIFY_SANDBOX_DEPLOY_ACTION,
            {"merge_pr_work_id": merged.id},
        )

        def runner(argv, _timeout):
            commands.append(list(argv))
            return CommandResult(
                0,
                json.dumps(
                    {
                        "status": 0,
                        "result": [
                            {
                                "type": "LightningComponentBundle",
                                "fullName": "atlasAcceptanceCard",
                                "id": "0Rb000000000001AAA",
                            }
                        ],
                    }
                ),
                "",
            )

        item = store.enqueue(
            VERIFY_LWC_DEPLOYMENT_ACTION,
            {"deploy_work_id": deploy.id, "source_work_id": source.id},
        )
        completed = Engine(
            store,
            {
                VERIFY_LWC_DEPLOYMENT_ACTION: VerifyLwcDeployment(
                    store, partial_alias="dod-check", runner=runner
                )
            },
            worker_id="test",
            execution_enabled=True,
        ).run_once(work_id=item.id).item

    assert commands[0] == [
        "sf", "org", "list", "metadata", "--metadata-type",
        "LightningComponentBundle", "--target-org", "dod-check", "--json",
    ]
    assert completed is not None and completed.state is WorkState.SUCCEEDED
    assert completed.result["jest_gate"] == "SUCCESS"
    assert completed.evidence[0]["live_metadata_present"] is True
    assert completed.evidence[0]["production_execution"] is False


def test_lwc_deployment_refuses_missing_jest_receipt_before_org_call(tmp_path):
    called = False
    with Store(tmp_path / "state.sqlite3") as store:
        source = _succeed(
            store, CREATE_LWC_SOURCE_ACTION, {"name": "atlasAcceptanceCard"}
        )
        commit = _succeed(store, COMMIT_SOURCE_ACTION, {"source_work_ids": [source.id]})
        opened = _succeed(store, OPEN_PR_ACTION, {"commit_work_ids": [commit.id]})
        verified = _succeed(
            store,
            VERIFY_PR_ACTION,
            {"open_pr_work_id": opened.id, "checks": {}},
        )
        merged = _succeed(store, MERGE_PR_ACTION, {"verify_pr_work_id": verified.id})
        deploy = _succeed(
            store,
            VERIFY_SANDBOX_DEPLOY_ACTION,
            {"merge_pr_work_id": merged.id},
        )

        def runner(*_args):
            nonlocal called
            called = True
            return CommandResult(0, "{}", "")

        item = store.enqueue(
            VERIFY_LWC_DEPLOYMENT_ACTION,
            {"deploy_work_id": deploy.id, "source_work_id": source.id},
        )
        completed = Engine(
            store,
            {
                VERIFY_LWC_DEPLOYMENT_ACTION: VerifyLwcDeployment(
                    store, partial_alias="dod-check", runner=runner
                )
            },
            worker_id="test",
            execution_enabled=True,
        ).run_once(work_id=item.id).item

    assert called is False
    assert completed is not None and completed.state is WorkState.FAILED
    assert "no successful LWC" in (completed.error or "")


def test_lwc_deployment_accepts_committed_author_revision_with_creation_parent(tmp_path):
    with Store(tmp_path / "state.sqlite3") as store:
        created = _succeed(
            store, CREATE_LWC_SOURCE_ACTION, {"name": "atlasAcceptanceCard"}
        )
        authored = _succeed(
            store,
            AUTHOR_SOURCE_ACTION,
            {"name": "atlasAcceptanceCard", "retrieve_work_id": created.id},
        )
        commit = _succeed(store, COMMIT_SOURCE_ACTION, {"source_work_ids": [authored.id]})
        opened = _succeed(store, OPEN_PR_ACTION, {"commit_work_ids": [commit.id]})
        verified = _succeed(
            store,
            VERIFY_PR_ACTION,
            {"open_pr_work_id": opened.id, "checks": {"LWC unit tests": "SUCCESS"}},
        )
        merged = _succeed(store, MERGE_PR_ACTION, {"verify_pr_work_id": verified.id})
        deploy = _succeed(
            store,
            VERIFY_SANDBOX_DEPLOY_ACTION,
            {"merge_pr_work_id": merged.id},
        )
        item = store.enqueue(
            VERIFY_LWC_DEPLOYMENT_ACTION,
            {"deploy_work_id": deploy.id, "source_work_id": authored.id},
        )
        completed = Engine(
            store,
            {
                VERIFY_LWC_DEPLOYMENT_ACTION: VerifyLwcDeployment(
                    store,
                    partial_alias="dod-check",
                    runner=lambda *_args: CommandResult(
                        0,
                        json.dumps(
                            {
                                "result": [
                                    {
                                        "type": "LightningComponentBundle",
                                        "fullName": "atlasAcceptanceCard",
                                        "id": "0Rb000000000001AAA",
                                    }
                                ]
                            }
                        ),
                        "",
                    ),
                )
            },
            worker_id="test",
            execution_enabled=True,
        ).run_once(work_id=item.id).item

    assert completed is not None and completed.state is WorkState.SUCCEEDED

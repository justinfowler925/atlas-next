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
from atlas_next.integration_source import CREATE_INTEGRATION_SOURCE_ACTION
from atlas_next.salesforce import CommandResult
from atlas_next.salesforce_integration import (
    VERIFY_INTEGRATION_EXECUTION_ACTION,
    VerifyIntegrationExecution,
)


def _succeed(store, action, result):
    item = store.enqueue(action, {})
    assert store.claim(item.id, "proof") is not None
    store.succeed(item.id, "proof", result=result, evidence=[{"kind": action}])
    return store.get(item.id)


def _chain(store):
    source = _succeed(
        store,
        CREATE_INTEGRATION_SOURCE_ACTION,
        {
            "name": "AtlasAcceptanceExchangeRate",
            "base_url": "https://open.er-api.com",
            "path": "/v6/latest/USD",
            "expected_marker": "success",
        },
    )
    commit = _succeed(store, COMMIT_SOURCE_ACTION, {"source_work_ids": [source.id]})
    opened = _succeed(store, OPEN_PR_ACTION, {"commit_work_ids": [commit.id]})
    verified = _succeed(
        store,
        VERIFY_PR_ACTION,
        {
            "open_pr_work_id": opened.id,
            "checks": {"Validate (sandbox)": "SUCCESS"},
        },
    )
    merged = _succeed(store, MERGE_PR_ACTION, {"verify_pr_work_id": verified.id})
    deploy = _succeed(
        store, VERIFY_SANDBOX_DEPLOY_ACTION, {"merge_pr_work_id": merged.id}
    )
    return source, deploy


def test_integration_execution_proves_lineage_and_live_runtime(tmp_path):
    commands = []
    with Store(tmp_path / "state.sqlite3") as store:
        source, deploy = _chain(store)

        def runner(argv, _timeout):
            commands.append(list(argv))
            return CommandResult(
                0,
                json.dumps(
                    {
                        "result": {
                            "compiled": True,
                            "success": True,
                            "logs": "00:00|USER_DEBUG|[3]|DEBUG|ATLAS_INTEGRATION_RESULT=success\n",
                        }
                    }
                ),
                "",
            )

        item = store.enqueue(
            VERIFY_INTEGRATION_EXECUTION_ACTION,
            {"deploy_work_id": deploy.id, "source_work_id": source.id},
        )
        completed = Engine(
            store,
            {
                VERIFY_INTEGRATION_EXECUTION_ACTION: VerifyIntegrationExecution(
                    store,
                    partial_alias="dod-check",
                    artifact_root=tmp_path / "artifacts",
                    runner=runner,
                )
            },
            worker_id="test",
            execution_enabled=True,
        ).run_once(work_id=item.id).item

    assert commands[0][:4] == ["sf", "apex", "run", "--file"]
    assert commands[0][-3:] == ["--target-org", "dod-check", "--json"]
    assert completed is not None and completed.state is WorkState.SUCCEEDED
    assert completed.evidence[0]["external_callout"] is True
    assert completed.evidence[0]["production_execution"] is False


def test_integration_execution_refuses_unlinked_source_before_runtime(tmp_path):
    called = False
    with Store(tmp_path / "state.sqlite3") as store:
        _source, deploy = _chain(store)
        other = _succeed(
            store,
            CREATE_INTEGRATION_SOURCE_ACTION,
            {"name": "OtherIntegration", "expected_marker": "success"},
        )

        def runner(*_args):
            nonlocal called
            called = True
            return CommandResult(0, "{}", "")

        item = store.enqueue(
            VERIFY_INTEGRATION_EXECUTION_ACTION,
            {"deploy_work_id": deploy.id, "source_work_id": other.id},
        )
        completed = Engine(
            store,
            {
                VERIFY_INTEGRATION_EXECUTION_ACTION: VerifyIntegrationExecution(
                    store,
                    partial_alias="dod-check",
                    artifact_root=tmp_path / "artifacts",
                    runner=runner,
                )
            },
            worker_id="test",
            execution_enabled=True,
        ).run_once(work_id=item.id).item

    assert called is False
    assert completed is not None and completed.state is WorkState.FAILED
    assert "not in the deployed PR lineage" in (completed.error or "")

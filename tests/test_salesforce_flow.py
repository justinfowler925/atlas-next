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
from atlas_next.flow_source import CREATE_FLOW_SOURCE_ACTION
from atlas_next.salesforce import CommandResult
from atlas_next.salesforce_flow import (
    RUN_CREATED_FLOW_ACTION,
    VERIFY_FLOW_ACTIVATION_ACTION,
    RunCreatedFlow,
    VerifyFlowActivation,
)


def _partial():
    return json.dumps(
        {
            "result": {
                "id": "00DU700000CBa9JMAT",
                "username": "ci.deploy@clearspeed.com.partial",
                "instanceUrl": "https://clearspeed--partial.sandbox.my.salesforce.com/",
            }
        }
    )


def _succeed(store, action, result):
    item = store.enqueue(action, {})
    assert store.claim(item.id, "proof") is not None
    store.succeed(item.id, "proof", result=result, evidence=[{"kind": action}])
    return store.get(item.id)


def _lineage(store):
    source = _succeed(
        store,
        CREATE_FLOW_SOURCE_ACTION,
        {"name": "Atlas_Acceptance_Autolaunched"},
    )
    commit = _succeed(store, COMMIT_SOURCE_ACTION, {"source_work_ids": [source.id]})
    opened = _succeed(store, OPEN_PR_ACTION, {"commit_work_ids": [commit.id]})
    verified = _succeed(store, VERIFY_PR_ACTION, {"open_pr_work_id": opened.id})
    merged = _succeed(store, MERGE_PR_ACTION, {"verify_pr_work_id": verified.id})
    deploy = _succeed(
        store,
        VERIFY_SANDBOX_DEPLOY_ACTION,
        {"merge_pr_work_id": merged.id},
    )
    return source, deploy


def test_flow_activation_proves_lineage_and_active_latest_parity(tmp_path):
    commands = []
    with Store(tmp_path / "state.sqlite3") as store:
        source, deploy = _lineage(store)

        def runner(argv, _timeout):
            commands.append(list(argv))
            if argv[:3] == ["sf", "org", "display"]:
                return CommandResult(0, _partial(), "")
            return CommandResult(
                0,
                json.dumps(
                    {
                        "status": 0,
                        "result": {
                            "records": [
                                {
                                    "DeveloperName": "Atlas_Acceptance_Autolaunched",
                                    "ActiveVersion": {"VersionNumber": 1},
                                    "LatestVersion": {"VersionNumber": 1},
                                }
                            ]
                        },
                    }
                ),
                "",
            )

        item = store.enqueue(
            VERIFY_FLOW_ACTIVATION_ACTION,
            {"deploy_work_id": deploy.id, "source_work_id": source.id},
        )
        completed = Engine(
            store,
            {
                VERIFY_FLOW_ACTIVATION_ACTION: VerifyFlowActivation(
                    store, partial_alias="dod-check", runner=runner
                )
            },
            worker_id="test",
            execution_enabled=True,
        ).run_once(work_id=item.id).item

    assert commands[0][:3] == ["sf", "org", "display"]
    assert commands[1][:6] == [
        "sf", "data", "query", "--target-org", "dod-check", "--use-tooling-api"
    ]
    assert completed is not None and completed.state is WorkState.SUCCEEDED
    assert completed.evidence[0]["active_equals_latest"] is True


def test_flow_activation_refuses_unrelated_source_lineage(tmp_path):
    called = False
    with Store(tmp_path / "state.sqlite3") as store:
        source, deploy = _lineage(store)
        unrelated = _succeed(
            store,
            CREATE_FLOW_SOURCE_ACTION,
            {"name": "Unrelated_Flow"},
        )

        def runner(*_args):
            nonlocal called
            called = True
            return CommandResult(0, "{}", "")

        item = store.enqueue(
            VERIFY_FLOW_ACTIVATION_ACTION,
            {"deploy_work_id": deploy.id, "source_work_id": unrelated.id},
        )
        completed = Engine(
            store,
            {
                VERIFY_FLOW_ACTIVATION_ACTION: VerifyFlowActivation(
                    store, partial_alias="dod-check", runner=runner
                )
            },
            worker_id="test",
            execution_enabled=True,
        ).run_once(work_id=item.id).item

    assert source.id != unrelated.id and called is False
    assert completed is not None and completed.state is WorkState.FAILED
    assert "not in the deployed PR lineage" in (completed.error or "")


def test_run_created_flow_generates_fixed_partial_runtime_assertion(tmp_path):
    commands = []
    with Store(tmp_path / "state.sqlite3") as store:
        activation = _succeed(
            store,
            VERIFY_FLOW_ACTIVATION_ACTION,
            {"name": "Atlas_Acceptance_Autolaunched"},
        )

        def runner(argv, _timeout):
            commands.append(list(argv))
            if argv[:3] == ["sf", "org", "display"]:
                return CommandResult(0, _partial(), "")
            script = open(argv[argv.index("--file") + 1], encoding="utf-8").read()
            assert "Flow.Interview.createInterview('Atlas_Acceptance_Autolaunched'" in script
            assert "getVariableValue('result')" in script
            return CommandResult(
                0,
                json.dumps(
                    {
                        "status": 0,
                        "result": {
                            "compiled": True,
                            "success": True,
                            "logs": "00|USER_DEBUG|[1]|DEBUG|ATLAS_FLOW_RESULT=atlas-flow-ok\n",
                        },
                    }
                ),
                "",
            )

        item = store.enqueue(
            RUN_CREATED_FLOW_ACTION,
            {
                "activation_work_id": activation.id,
                "output_variable": "result",
                "expected_string": "atlas-flow-ok",
            },
        )
        completed = Engine(
            store,
            {
                RUN_CREATED_FLOW_ACTION: RunCreatedFlow(
                    store,
                    partial_alias="dod-check",
                    artifact_root=tmp_path / "artifacts",
                    runner=runner,
                )
            },
            worker_id="test",
            execution_enabled=True,
        ).run_once(work_id=item.id).item

    assert commands[0][0:3] == ["sf", "org", "display"]
    assert commands[1][0:3] == ["sf", "apex", "run"]
    assert completed is not None and completed.state is WorkState.SUCCEEDED
    assert completed.result["output_value"] == "atlas-flow-ok"
    assert completed.evidence[0]["production_execution"] is False

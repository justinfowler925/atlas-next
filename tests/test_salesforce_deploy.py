from __future__ import annotations

import json

import pytest

from atlas_next import Engine, Store, WorkState
from atlas_next.salesforce import CommandResult
from atlas_next.salesforce_deploy import (
    DRY_RUN_ACTION,
    DeployDryRunRequest,
    SalesforceDeployDryRun,
)


def _result(**overrides):
    result = {
        "id": "0Af000000000001AAA",
        "status": "Succeeded",
        "success": True,
        "done": True,
        "checkOnly": True,
        "numberComponentsTotal": 1,
        "numberComponentsDeployed": 1,
        "numberComponentErrors": 0,
        "numberTestsTotal": 2,
        "numberTestsCompleted": 2,
        "numberTestErrors": 0,
    }
    result.update(overrides)
    return json.dumps({"status": 0, "result": result})


def _payload(**overrides):
    value = {
        "source_paths": ["force-app/main/default/flows/My_Flow.flow-meta.xml"],
        "tests": ["MyFlowTest"],
    }
    value.update(overrides)
    return value


def test_dry_run_contract_cannot_choose_org_mode_command_or_destructive_manifest():
    for key in ("environment", "target_org", "command", "dry_run", "manifest", "purge"):
        with pytest.raises(ValueError, match=f"unexpected keys: {key}"):
            DeployDryRunRequest.from_payload({**_payload(), key: "anything"})
    for path in ("../secret", "/tmp/file", "force-app/../../secret", "README.md"):
        with pytest.raises(ValueError, match="force-app/main/default"):
            DeployDryRunRequest.from_payload(_payload(source_paths=[path]))


def test_dry_run_emits_only_partial_check_only_deploy_with_named_tests(tmp_path):
    project = tmp_path / "sfdc"
    source = project / "force-app/main/default/flows/My_Flow.flow-meta.xml"
    source.parent.mkdir(parents=True)
    source.write_text("<Flow/>")
    (project / "sfdx-project.json").write_text("{}")
    calls = []

    def runner(argv, cwd, timeout):
        calls.append((list(argv), cwd, timeout))
        return CommandResult(0, _result(), "")

    with Store(tmp_path / "state.sqlite3") as store:
        item = store.enqueue(DRY_RUN_ACTION, _payload())
        completed = Engine(
            store,
            {
                DRY_RUN_ACTION: SalesforceDeployDryRun(
                    partial_alias="dod-check", project_dir=project, runner=runner
                )
            },
            worker_id="test",
            execution_enabled=True,
        ).run_once(work_id=item.id).item

    assert calls == [
        (
            [
                "sf", "project", "deploy", "start", "--dry-run",
                "--source-dir", "force-app/main/default/flows/My_Flow.flow-meta.xml",
                "--target-org", "dod-check", "--test-level", "RunSpecifiedTests",
                "--tests", "MyFlowTest", "--wait", "30", "--json",
            ],
            project.resolve(),
            1860,
        )
    ]
    assert completed is not None and completed.state is WorkState.SUCCEEDED
    assert completed.result["deploy_id"] == "0Af000000000001AAA"
    assert completed.evidence[0]["metadata_saved"] is False
    assert completed.evidence[0]["production_execution"] is False


def test_missing_or_directory_source_never_reaches_runner(tmp_path):
    project = tmp_path / "sfdc"
    (project / "force-app/main/default/flows").mkdir(parents=True)
    (project / "sfdx-project.json").write_text("{}")
    called = False

    def runner(_argv, _cwd, _timeout):
        nonlocal called
        called = True
        return CommandResult(0, _result(), "")

    with Store(tmp_path / "state.sqlite3") as store:
        item = store.enqueue(DRY_RUN_ACTION, _payload())
        completed = Engine(
            store,
            {
                DRY_RUN_ACTION: SalesforceDeployDryRun(
                    partial_alias="dod-check", project_dir=project, runner=runner
                )
            },
            worker_id="test",
            execution_enabled=True,
        ).run_once(work_id=item.id).item
    assert called is False
    assert completed is not None and completed.state is WorkState.FAILED
    assert "existing project file" in (completed.error or "")


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"checkOnly": False}, "not a completed check-only"),
        ({"numberComponentsDeployed": 0}, "component counts"),
        ({"numberTestErrors": 1}, "test counts"),
        ({"numberTestsTotal": 0, "numberTestsCompleted": 0}, "must be positive"),
        ({"id": "bad"}, "valid deployment id"),
    ],
)
def test_unproven_deploy_result_cannot_mint_success(tmp_path, overrides, reason):
    project = tmp_path / "sfdc"
    source = project / "force-app/main/default/flows/My_Flow.flow-meta.xml"
    source.parent.mkdir(parents=True)
    source.write_text("<Flow/>")
    (project / "sfdx-project.json").write_text("{}")
    with Store(tmp_path / "state.sqlite3") as store:
        item = store.enqueue(DRY_RUN_ACTION, _payload())
        completed = Engine(
            store,
            {
                DRY_RUN_ACTION: SalesforceDeployDryRun(
                    partial_alias="dod-check",
                    project_dir=project,
                    runner=lambda *_args: CommandResult(0, _result(**overrides), ""),
                )
            },
            worker_id="test",
            execution_enabled=True,
        ).run_once(work_id=item.id).item
    assert completed is not None and completed.state is WorkState.FAILED
    assert reason in (completed.error or "")

from __future__ import annotations

import hashlib

import pytest

from atlas_next import Engine, Store, WorkState
from atlas_next.flow_source import (
    CREATE_FLOW_SOURCE_ACTION,
    CreateFlowSource,
    CreateFlowSourceRequest,
)
from atlas_next.salesforce import CommandResult


FLOW = """<?xml version="1.0" encoding="UTF-8"?>
<Flow xmlns="http://soap.sforce.com/2006/04/metadata">
    <apiVersion>65.0</apiVersion>
    <label>Atlas Acceptance Flow</label>
    <processType>AutoLaunchedFlow</processType>
    <status>Active</status>
</Flow>
"""


def test_create_flow_contract_has_no_path_target_or_command_escape():
    for key in ("path", "target_org", "command", "deploy"):
        with pytest.raises(ValueError, match=f"unexpected keys: {key}"):
            CreateFlowSourceRequest.from_payload(
                {"name": "Atlas_Acceptance_Flow", "content": FLOW, key: "anything"}
            )


def test_create_flow_writes_one_derived_active_xml_path(tmp_path):
    git_root = tmp_path / "worktree"
    project = git_root / "salesforce"
    project.mkdir(parents=True)
    (project / "sfdx-project.json").write_text("{}")
    relative = (
        "salesforce/force-app/main/default/flows/"
        "Atlas_Acceptance_Flow.flow-meta.xml"
    )

    def runner(argv, _cwd, _timeout):
        if argv[:2] == ["git", "rev-parse"] and argv[-1] == "--show-toplevel":
            return CommandResult(0, str(git_root), "")
        if argv == ["git", "branch", "--show-current"]:
            return CommandResult(0, "justin-fowler/flow\n", "")
        if argv[:2] == ["git", "rev-parse"] and argv[-1] == "--git-dir":
            return CommandResult(0, str(tmp_path / "repo/.git/worktrees/flow"), "")
        if argv[:2] == ["git", "rev-parse"] and argv[-1] == "--git-common-dir":
            return CommandResult(0, str(tmp_path / "repo/.git"), "")
        if argv == ["git", "status", "--porcelain=v1"]:
            created = (git_root / relative).exists()
            return CommandResult(0, f"?? {relative}\n" if created else "", "")
        raise AssertionError(argv)

    with Store(tmp_path / "state.sqlite3") as store:
        item = store.enqueue(
            CREATE_FLOW_SOURCE_ACTION,
            {"name": "Atlas_Acceptance_Flow", "content": FLOW},
        )
        completed = Engine(
            store,
            {CREATE_FLOW_SOURCE_ACTION: CreateFlowSource(project_dir=project, runner=runner)},
            worker_id="test",
            execution_enabled=True,
        ).run_once(work_id=item.id).item

    created = git_root / relative
    assert created.read_text() == FLOW
    assert completed is not None and completed.state is WorkState.SUCCEEDED
    assert completed.result["files"][0]["sha256"] == hashlib.sha256(
        FLOW.encode()
    ).hexdigest()
    assert completed.evidence[0]["production_execution"] is False


def test_create_flow_rejects_draft_wrong_root_and_entities():
    from atlas_next.flow_source import _validate_flow_xml

    with pytest.raises(ValueError, match="Active"):
        _validate_flow_xml(FLOW.replace("<status>Active", "<status>Draft"))
    with pytest.raises(ValueError, match="Flow document"):
        _validate_flow_xml('<ApexClass xmlns="http://soap.sforce.com/2006/04/metadata"/>')
    with pytest.raises(ValueError, match="entities"):
        _validate_flow_xml('<!DOCTYPE Flow [<!ENTITY x "y">]><Flow/>')


def test_create_flow_removes_its_file_when_postwrite_status_fails(tmp_path):
    git_root = tmp_path / "worktree"
    project = git_root / "salesforce"
    project.mkdir(parents=True)
    (project / "sfdx-project.json").write_text("{}")
    relative = (
        "salesforce/force-app/main/default/flows/"
        "Atlas_Acceptance_Flow.flow-meta.xml"
    )
    status_calls = 0

    def runner(argv, _cwd, _timeout):
        nonlocal status_calls
        if argv[:2] == ["git", "rev-parse"] and argv[-1] == "--show-toplevel":
            return CommandResult(0, str(git_root), "")
        if argv == ["git", "branch", "--show-current"]:
            return CommandResult(0, "justin-fowler/flow\n", "")
        if argv[:2] == ["git", "rev-parse"] and argv[-1] == "--git-dir":
            return CommandResult(0, str(tmp_path / "repo/.git/worktrees/flow"), "")
        if argv[:2] == ["git", "rev-parse"] and argv[-1] == "--git-common-dir":
            return CommandResult(0, str(tmp_path / "repo/.git"), "")
        if argv == ["git", "status", "--porcelain=v1"]:
            status_calls += 1
            return CommandResult(0, "" if status_calls == 1 else "?? other.txt\n", "")
        raise AssertionError(argv)

    with Store(tmp_path / "state.sqlite3") as store:
        item = store.enqueue(
            CREATE_FLOW_SOURCE_ACTION,
            {"name": "Atlas_Acceptance_Flow", "content": FLOW},
        )
        completed = Engine(
            store,
            {CREATE_FLOW_SOURCE_ACTION: CreateFlowSource(project_dir=project, runner=runner)},
            worker_id="test",
            execution_enabled=True,
        ).run_once(work_id=item.id).item

    assert completed is not None and completed.state is WorkState.FAILED
    assert not (git_root / relative).exists()

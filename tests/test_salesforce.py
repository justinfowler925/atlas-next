from __future__ import annotations

import json

import pytest

from atlas_next import Engine, Store, WorkState
from atlas_next.salesforce import ACTION, CommandResult, DescribeRequest, SalesforceDescribe


def _describe(field_names=("Id", "Name", "Risk__c")) -> str:
    return json.dumps(
        {
            "status": 0,
            "result": {
                "name": "Account",
                "label": "Account",
                "keyPrefix": "001",
                "queryable": True,
                "createable": True,
                "updateable": True,
                "fields": [{"name": name} for name in field_names],
            },
        }
    )


def test_request_contract_rejects_commands_queries_and_unknown_environments():
    with pytest.raises(ValueError, match="unexpected keys: command"):
        DescribeRequest.from_payload(
            {"environment": "partial", "object": "Account", "command": "data update"}
        )
    with pytest.raises(ValueError, match="unexpected keys: query"):
        DescribeRequest.from_payload(
            {"environment": "partial", "object": "Account", "query": "DELETE FROM Account"}
        )
    with pytest.raises(ValueError, match="partial.*prod"):
        DescribeRequest.from_payload({"environment": "production", "object": "Account"})
    with pytest.raises(ValueError, match="one Salesforce object API name"):
        DescribeRequest.from_payload(
            {"environment": "partial", "object": "Account; sf data delete bulk"}
        )


def test_capability_emits_only_the_fixed_read_only_command(tmp_path):
    calls = []

    def runner(argv, timeout):
        calls.append((list(argv), timeout))
        return CommandResult(0, _describe(), "")

    with Store(tmp_path / "state.sqlite3") as store:
        item = store.enqueue(ACTION, {"environment": "partial", "object": "Account"})
        engine = Engine(
            store,
            {ACTION: SalesforceDescribe({"partial": "dod-check", "prod": "prod"}, runner=runner)},
            worker_id="test",
            execution_enabled=True,
        )

        completed = engine.run_once(work_id=item.id).item

    assert calls == [
        (
            [
                "sf",
                "sobject",
                "describe",
                "--sobject",
                "Account",
                "--target-org",
                "dod-check",
                "--json",
            ],
            60,
        )
    ]
    assert completed is not None and completed.state is WorkState.SUCCEEDED
    assert completed.result == {
        "environment": "partial",
        "target_alias": "dod-check",
        "object": "Account",
        "label": "Account",
        "field_count": 3,
        "custom_field_count": 1,
        "key_prefix": "001",
        "queryable": True,
    }
    assert completed.evidence[0]["read_only_command"] is True
    assert completed.evidence[0]["field_count"] == 3


def test_prod_uses_the_same_fixed_describe_shape_with_only_the_alias_changed(tmp_path):
    calls = []

    def runner(argv, timeout):
        calls.append((list(argv), timeout))
        return CommandResult(0, _describe(), "")

    with Store(tmp_path / "state.sqlite3") as store:
        item = store.enqueue(ACTION, {"environment": "prod", "object": "Account"})
        engine = Engine(
            store,
            {ACTION: SalesforceDescribe({"partial": "dod-check", "prod": "prod"}, runner=runner)},
            worker_id="test",
            execution_enabled=True,
        )
        completed = engine.run_once(work_id=item.id).item

    assert completed is not None and completed.state is WorkState.SUCCEEDED
    assert calls[0][0] == [
        "sf",
        "sobject",
        "describe",
        "--sobject",
        "Account",
        "--target-org",
        "prod",
        "--json",
    ]


def test_invalid_payload_never_reaches_runner(tmp_path):
    called = False

    def runner(_argv, _timeout):
        nonlocal called
        called = True
        return CommandResult(0, _describe(), "")

    with Store(tmp_path / "state.sqlite3") as store:
        item = store.enqueue(
            ACTION,
            {"environment": "partial", "object": "Account", "query": "SELECT Id FROM User"},
        )
        engine = Engine(
            store,
            {ACTION: SalesforceDescribe({"partial": "dod-check"}, runner=runner)},
            worker_id="test",
            execution_enabled=True,
        )
        completed = engine.run_once(work_id=item.id).item

    assert called is False
    assert completed is not None and completed.state is WorkState.FAILED
    assert "unexpected keys: query" in (completed.error or "")


def test_cli_or_transport_failure_is_terminal_and_keeps_platform_reason(tmp_path):
    def runner(_argv, _timeout):
        return CommandResult(
            1,
            json.dumps({"status": 1, "message": "No authorization found for target"}),
            "",
        )

    with Store(tmp_path / "state.sqlite3") as store:
        item = store.enqueue(ACTION, {"environment": "partial", "object": "Account"})
        engine = Engine(
            store,
            {ACTION: SalesforceDescribe({"partial": "missing"}, runner=runner)},
            worker_id="test",
            execution_enabled=True,
        )
        completed = engine.run_once(work_id=item.id).item

    assert completed is not None and completed.state is WorkState.FAILED
    assert completed.attempts == 1
    assert "No authorization found for target" in (completed.error or "")


def test_zero_field_describe_cannot_mint_success(tmp_path):
    def runner(_argv, _timeout):
        return CommandResult(0, _describe(field_names=()), "")

    with Store(tmp_path / "state.sqlite3") as store:
        item = store.enqueue(ACTION, {"environment": "partial", "object": "Account"})
        engine = Engine(
            store,
            {ACTION: SalesforceDescribe({"partial": "dod-check"}, runner=runner)},
            worker_id="test",
            execution_enabled=True,
        )
        completed = engine.run_once(work_id=item.id).item

    assert completed is not None and completed.state is WorkState.FAILED
    assert "zero fields" in (completed.error or "")


def test_explicit_operator_run_does_not_consume_an_older_unrelated_item(tmp_path):
    def runner(_argv, _timeout):
        return CommandResult(0, _describe(), "")

    with Store(tmp_path / "state.sqlite3") as store:
        older = store.enqueue("other.action", {}, now=1)
        target = store.enqueue(ACTION, {"environment": "partial", "object": "Account"}, now=2)
        engine = Engine(
            store,
            {ACTION: SalesforceDescribe({"partial": "dod-check"}, runner=runner)},
            worker_id="operator",
            execution_enabled=True,
        )

        completed = engine.run_once(work_id=target.id).item

        assert completed is not None and completed.id == target.id
        assert store.get(older.id).state is WorkState.QUEUED

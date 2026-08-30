from __future__ import annotations

import json

import pytest

from atlas_next import Engine, Store, WorkState
from atlas_next.salesforce import (
    ACTION,
    COUNT_ACTION,
    CommandResult,
    CountRequest,
    DescribeRequest,
    SalesforceCount,
    SalesforceDescribe,
)


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


def _count(total=4753, *, done=True) -> str:
    return json.dumps(
        {"status": 0, "result": {"records": [], "totalSize": total, "done": done}}
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


def test_count_contract_rejects_every_freeform_query_shape():
    for key in ("query", "where", "fields", "command", "limit"):
        with pytest.raises(ValueError, match=f"unexpected keys: {key}"):
            CountRequest.from_payload(
                {"environment": "partial", "object": "Account", key: "anything"}
            )
    with pytest.raises(ValueError, match="one Salesforce object API name"):
        CountRequest.from_payload(
            {"environment": "partial", "object": "Account WHERE Name != null"}
        )


@pytest.mark.parametrize(
    ("environment", "alias"),
    [("partial", "dod-check"), ("prod", "prod")],
)
def test_count_emits_only_generated_count_soql(tmp_path, environment, alias):
    calls = []

    def runner(argv, timeout):
        calls.append((list(argv), timeout))
        return CommandResult(0, _count(), "")

    with Store(tmp_path / "state.sqlite3") as store:
        item = store.enqueue(COUNT_ACTION, {"environment": environment, "object": "Account"})
        engine = Engine(
            store,
            {
                COUNT_ACTION: SalesforceCount(
                    {"partial": "dod-check", "prod": "prod"}, runner=runner
                )
            },
            worker_id="test",
            execution_enabled=True,
        )
        completed = engine.run_once(work_id=item.id).item

    assert calls == [
        (
            [
                "sf",
                "data",
                "query",
                "--query",
                "SELECT COUNT() FROM Account",
                "--target-org",
                alias,
                "--json",
            ],
            60,
        )
    ]
    assert completed is not None and completed.state is WorkState.SUCCEEDED
    assert completed.result == {
        "environment": environment,
        "target_alias": alias,
        "object": "Account",
        "count": 4753,
    }
    assert completed.evidence[0]["query_shape"] == "SELECT COUNT() FROM <validated_object>"


def test_zero_count_is_real_success_not_a_zero_denominator_escape(tmp_path):
    def runner(_argv, _timeout):
        return CommandResult(0, _count(0), "")

    with Store(tmp_path / "state.sqlite3") as store:
        item = store.enqueue(COUNT_ACTION, {"environment": "partial", "object": "Task"})
        engine = Engine(
            store,
            {COUNT_ACTION: SalesforceCount({"partial": "dod-check"}, runner=runner)},
            worker_id="test",
            execution_enabled=True,
        )
        completed = engine.run_once(work_id=item.id).item

    assert completed is not None and completed.state is WorkState.SUCCEEDED
    assert completed.result["count"] == 0
    assert completed.evidence[0]["count"] == 0


@pytest.mark.parametrize(
    ("total", "done", "reason"),
    [(-1, True, "non-negative integer"), (True, True, "non-negative integer"), (3, False, "did not finish")],
)
def test_malformed_count_response_cannot_mint_success(tmp_path, total, done, reason):
    def runner(_argv, _timeout):
        return CommandResult(0, _count(total, done=done), "")

    with Store(tmp_path / "state.sqlite3") as store:
        item = store.enqueue(COUNT_ACTION, {"environment": "partial", "object": "Account"})
        engine = Engine(
            store,
            {COUNT_ACTION: SalesforceCount({"partial": "dod-check"}, runner=runner)},
            worker_id="test",
            execution_enabled=True,
        )
        completed = engine.run_once(work_id=item.id).item

    assert completed is not None and completed.state is WorkState.FAILED
    assert reason in (completed.error or "")

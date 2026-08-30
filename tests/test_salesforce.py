from __future__ import annotations

import json

import pytest

from atlas_next import Engine, Store, WorkState
from atlas_next.salesforce import (
    ACTION,
    COUNT_ACTION,
    PICKLIST_COUNTS_ACTION,
    CommandResult,
    CountRequest,
    DescribeRequest,
    PicklistCountsRequest,
    SalesforceCount,
    SalesforceDescribe,
    SalesforcePicklistCounts,
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


def _picklist_describe(*, field_type="picklist", groupable=True) -> str:
    return json.dumps(
        {
            "status": 0,
            "result": {
                "name": "Opportunity",
                "fields": [
                    {"name": "Id", "type": "id", "groupable": True},
                    {"name": "StageName", "type": field_type, "groupable": groupable},
                ],
            },
        }
    )


def _picklist_query(records=None, *, done=True, total_size=None) -> str:
    records = records if records is not None else [
        {"attributes": {"type": "AggregateResult"}, "StageName": "Closed Won", "recordCount": 7},
        {"attributes": {"type": "AggregateResult"}, "StageName": "Qualified", "recordCount": 3},
    ]
    return json.dumps(
        {
            "status": 0,
            "result": {
                "records": records,
                "totalSize": len(records) if total_size is None else total_size,
                "done": done,
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


def test_picklist_counts_rejects_freeform_and_field_injection():
    base = {"environment": "partial", "object": "Opportunity", "field": "StageName"}
    for key in ("query", "where", "limit", "command"):
        with pytest.raises(ValueError, match=f"unexpected keys: {key}"):
            PicklistCountsRequest.from_payload({**base, key: "anything"})
    with pytest.raises(ValueError, match="one Salesforce field API name"):
        PicklistCountsRequest.from_payload({**base, "field": "StageName FROM User"})


@pytest.mark.parametrize(
    ("environment", "alias"),
    [("partial", "dod-check"), ("prod", "prod")],
)
def test_picklist_counts_describes_then_runs_one_fixed_capped_aggregate(
    tmp_path, environment, alias
):
    calls = []
    responses = iter(
        [CommandResult(0, _picklist_describe(), ""), CommandResult(0, _picklist_query(), "")]
    )

    def runner(argv, timeout):
        calls.append((list(argv), timeout))
        return next(responses)

    with Store(tmp_path / "state.sqlite3") as store:
        item = store.enqueue(
            PICKLIST_COUNTS_ACTION,
            {"environment": environment, "object": "Opportunity", "field": "StageName"},
        )
        engine = Engine(
            store,
            {
                PICKLIST_COUNTS_ACTION: SalesforcePicklistCounts(
                    {"partial": "dod-check", "prod": "prod"}, runner=runner
                )
            },
            worker_id="test",
            execution_enabled=True,
        )
        completed = engine.run_once(work_id=item.id).item

    assert calls[0][0] == [
        "sf",
        "sobject",
        "describe",
        "--sobject",
        "Opportunity",
        "--target-org",
        alias,
        "--json",
    ]
    assert calls[1][0] == [
        "sf",
        "data",
        "query",
        "--query",
        "SELECT StageName, COUNT(Id) recordCount FROM Opportunity "
        "GROUP BY StageName ORDER BY COUNT(Id) DESC LIMIT 50",
        "--target-org",
        alias,
        "--json",
    ]
    assert completed is not None and completed.state is WorkState.SUCCEEDED
    assert completed.result["groups"] == [
        {"value": "Closed Won", "count": 7},
        {"value": "Qualified", "count": 3},
    ]
    assert completed.result["record_count"] == 10
    assert completed.evidence[0]["field_type"] == "picklist"


@pytest.mark.parametrize(
    ("field_type", "groupable", "reason"),
    [("string", True, "not 'picklist'"), ("picklist", False, "not groupable")],
)
def test_non_picklist_or_ungroupable_field_never_reaches_query(
    tmp_path, field_type, groupable, reason
):
    calls = 0

    def runner(_argv, _timeout):
        nonlocal calls
        calls += 1
        return CommandResult(
            0, _picklist_describe(field_type=field_type, groupable=groupable), ""
        )

    with Store(tmp_path / "state.sqlite3") as store:
        item = store.enqueue(
            PICKLIST_COUNTS_ACTION,
            {"environment": "partial", "object": "Opportunity", "field": "StageName"},
        )
        engine = Engine(
            store,
            {
                PICKLIST_COUNTS_ACTION: SalesforcePicklistCounts(
                    {"partial": "dod-check"}, runner=runner
                )
            },
            worker_id="test",
            execution_enabled=True,
        )
        completed = engine.run_once(work_id=item.id).item

    assert calls == 1
    assert completed is not None and completed.state is WorkState.FAILED
    assert reason in (completed.error or "")


def test_picklist_result_over_cap_or_with_bad_counts_cannot_succeed(tmp_path):
    too_many = [
        {"StageName": f"S{i}", "recordCount": 1, "attributes": {}} for i in range(51)
    ]
    bad_cases = [
        (_picklist_query(too_many), "at most 50 groups"),
        (_picklist_query([{"StageName": "Won", "recordCount": -1}]), "non-negative integer"),
        (_picklist_query(total_size=3), "totalSize did not match"),
        (_picklist_query(records=["bad"]), "group must be an object"),
        (_picklist_query(total_size=True), "totalSize must be an integer"),
        (_picklist_query(done=False), "did not finish"),
    ]
    for index, (query_payload, reason) in enumerate(bad_cases):
        responses = iter(
            [CommandResult(0, _picklist_describe(), ""), CommandResult(0, query_payload, "")]
        )
        with Store(tmp_path / f"state-{index}.sqlite3") as store:
            item = store.enqueue(
                PICKLIST_COUNTS_ACTION,
                {"environment": "partial", "object": "Opportunity", "field": "StageName"},
            )
            engine = Engine(
                store,
                {
                    PICKLIST_COUNTS_ACTION: SalesforcePicklistCounts(
                        {"partial": "dod-check"}, runner=lambda *_args: next(responses)
                    )
                },
                worker_id="test",
                execution_enabled=True,
            )
            completed = engine.run_once(work_id=item.id).item
        assert completed is not None and completed.state is WorkState.FAILED
        assert reason in (completed.error or "")

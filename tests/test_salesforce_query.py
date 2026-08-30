from __future__ import annotations

import json

import pytest

from atlas_next import Engine, Store, WorkState
from atlas_next.salesforce import CommandResult
from atlas_next.salesforce_query import QUERY_ACTION, QueryRequest, SalesforceQuery


def _describe(*, queryable=True, filterable=True, sortable=True) -> str:
    return json.dumps(
        {
            "status": 0,
            "result": {
                "name": "Opportunity",
                "queryable": queryable,
                "fields": [
                    {"name": "Id", "type": "id", "filterable": True, "sortable": True},
                    {"name": "Name", "type": "string", "filterable": True, "sortable": True},
                    {
                        "name": "StageName",
                        "type": "picklist",
                        "filterable": filterable,
                        "sortable": True,
                    },
                    {
                        "name": "CloseDate",
                        "type": "date",
                        "filterable": True,
                        "sortable": sortable,
                    },
                    {
                        "name": "Secret__c",
                        "type": "encryptedstring",
                        "filterable": True,
                        "sortable": False,
                    },
                ],
            },
        }
    )


def _query(*, done=True, total_size=1, records=None) -> str:
    if records is None:
        records = [
            {
                "attributes": {"type": "Opportunity", "url": "/ignored"},
                "Id": "006000000000001AAA",
                "Name": "Acme",
                "StageName": "Closed Won",
            }
        ]
    return json.dumps(
        {"status": 0, "result": {"done": done, "totalSize": total_size, "records": records}}
    )


def _payload(**overrides):
    value = {
        "environment": "partial",
        "object": "Opportunity",
        "fields": ["Id", "Name", "StageName"],
        "filters": [{"field": "StageName", "operator": "eq", "value": "Closed Won"}],
        "order_by": {"field": "CloseDate", "direction": "desc"},
        "limit": 3,
    }
    value.update(overrides)
    return value


@pytest.mark.parametrize("key", ["query", "where", "command", "target_org", "tool"])
def test_query_contract_rejects_freeform_execution_keys(key):
    with pytest.raises(ValueError, match=f"unexpected keys: {key}"):
        QueryRequest.from_payload({**_payload(), key: "anything"})


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"object": "Opportunity WHERE Name != null"}, "object must be one"),
        ({"fields": ["Id, (SELECT Id FROM Users)"]}, "field must be one"),
        ({"fields": []}, "1 to 12"),
        ({"fields": ["Id", "Id"]}, "duplicates"),
        ({"filters": [{"field": "Name", "operator": "LIKE", "value": "%"}]}, "operator"),
        ({"limit": 201}, "1 to 200"),
    ],
)
def test_query_contract_rejects_injection_and_unbounded_shapes(overrides, reason):
    with pytest.raises(ValueError, match=reason):
        QueryRequest.from_payload(_payload(**overrides))


def test_query_live_validates_schema_then_emits_one_bounded_soql_command(tmp_path):
    calls = []
    responses = iter([CommandResult(0, _describe(), ""), CommandResult(0, _query(), "")])

    def runner(argv, timeout):
        calls.append((list(argv), timeout))
        return next(responses)

    with Store(tmp_path / "state.sqlite3") as store:
        item = store.enqueue(QUERY_ACTION, _payload())
        engine = Engine(
            store,
            {QUERY_ACTION: SalesforceQuery({"partial": "dod-check"}, runner=runner)},
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
        "dod-check",
        "--json",
    ]
    assert calls[1][0] == [
        "sf",
        "data",
        "query",
        "--query",
        "SELECT Id, Name, StageName FROM Opportunity WHERE StageName = 'Closed Won' "
        "ORDER BY CloseDate DESC NULLS LAST LIMIT 3",
        "--target-org",
        "dod-check",
        "--json",
    ]
    assert completed is not None and completed.state is WorkState.SUCCEEDED
    assert completed.result["records"] == [
        {"Id": "006000000000001AAA", "Name": "Acme", "StageName": "Closed Won"}
    ]
    assert completed.evidence[0]["schema_validated"] is True
    assert len(completed.evidence[0]["query_sha256"]) == 64


def test_filter_literals_are_escaped_and_cannot_change_query_shape(tmp_path):
    calls = []
    responses = iter([CommandResult(0, _describe(), ""), CommandResult(0, _query(), "")])

    def runner(argv, timeout):
        calls.append(list(argv))
        return next(responses)

    payload = _payload(
        filters=[{"field": "Name", "operator": "eq", "value": "O'Brien\\test' LIMIT 200"}]
    )
    with Store(tmp_path / "state.sqlite3") as store:
        item = store.enqueue(QUERY_ACTION, payload)
        completed = Engine(
            store,
            {QUERY_ACTION: SalesforceQuery({"partial": "dod-check"}, runner=runner)},
            worker_id="test",
            execution_enabled=True,
        ).run_once(work_id=item.id).item

    assert completed is not None and completed.state is WorkState.SUCCEEDED
    assert calls[1][4] == "SELECT Id, Name, StageName FROM Opportunity WHERE " \
        "Name = 'O\\'Brien\\\\test\\' LIMIT 200' ORDER BY CloseDate DESC NULLS LAST LIMIT 3"


@pytest.mark.parametrize(
    ("payload", "describe", "reason"),
    [
        (_payload(fields=["Missing__c"]), _describe(), "does not exist"),
        (_payload(fields=["Secret__c"]), _describe(), "unsupported type"),
        (_payload(), _describe(queryable=False), "not queryable"),
        (_payload(), _describe(filterable=False), "not filterable"),
        (_payload(), _describe(sortable=False), "not sortable"),
    ],
)
def test_invalid_live_schema_stops_before_query(tmp_path, payload, describe, reason):
    calls = 0

    def runner(_argv, _timeout):
        nonlocal calls
        calls += 1
        return CommandResult(0, describe, "")

    with Store(tmp_path / "state.sqlite3") as store:
        item = store.enqueue(QUERY_ACTION, payload)
        completed = Engine(
            store,
            {QUERY_ACTION: SalesforceQuery({"partial": "dod-check"}, runner=runner)},
            worker_id="test",
            execution_enabled=True,
        ).run_once(work_id=item.id).item
    assert calls == 1
    assert completed is not None and completed.state is WorkState.FAILED
    assert reason in (completed.error or "")


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (_query(done=False), "did not finish"),
        (_query(records=["bad"]), "must be an object"),
        (_query(records=[{"Id": "1", "Name": "x", "StageName": "Won", "Owner": {}}]), "undeclared"),
        (_query(total_size=True), "totalSize"),
    ],
)
def test_malformed_query_response_cannot_mint_success(tmp_path, response, reason):
    responses = iter([CommandResult(0, _describe(), ""), CommandResult(0, response, "")])
    with Store(tmp_path / "state.sqlite3") as store:
        item = store.enqueue(QUERY_ACTION, _payload())
        completed = Engine(
            store,
            {
                QUERY_ACTION: SalesforceQuery(
                    {"partial": "dod-check"}, runner=lambda *_args: next(responses)
                )
            },
            worker_id="test",
            execution_enabled=True,
        ).run_once(work_id=item.id).item
    assert completed is not None and completed.state is WorkState.FAILED
    assert reason in (completed.error or "")

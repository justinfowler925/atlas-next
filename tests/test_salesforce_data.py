from __future__ import annotations

import hashlib
import json

import pytest

from atlas_next import Engine, Store, WorkState
from atlas_next.salesforce import CommandResult
from atlas_next.salesforce_data import (
    ROLLBACK_UPDATE_ACTION,
    UPDATE_RECORDS_ACTION,
    RollbackUpdateRequest,
    SalesforceRollbackUpdate,
    SalesforceUpdateRecords,
    UpdateRecordsRequest,
)


RECORD_ID = "001000000000001AAA"


def _describe():
    return json.dumps(
        {
            "status": 0,
            "result": {
                "name": "Account",
                "queryable": True,
                "keyPrefix": "001",
                "fields": [
                    {"name": "Id", "type": "id", "updateable": False},
                    {
                        "name": "Description",
                        "type": "textarea",
                        "updateable": True,
                        "nillable": True,
                        "length": 32000,
                        "encrypted": False,
                    },
                ],
            },
        }
    )


def _query(value):
    return json.dumps(
        {
            "status": 0,
            "result": {
                "records": [
                    {"attributes": {"type": "Account"}, "Id": RECORD_ID, "Description": value}
                ]
            },
        }
    )


def _hash(value):
    records = {RECORD_ID: {"Description": value}}
    return hashlib.sha256(
        json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def test_update_contract_has_no_environment_command_or_unbounded_population():
    base = {
        "object": "Account",
        "records": [{"id": RECORD_ID, "fields": {"Description": "new"}}],
        "reason": "Repair acceptance record",
    }
    for key in ("environment", "target_org", "command", "soql"):
        with pytest.raises(ValueError, match="only object"):
            UpdateRecordsRequest.from_payload({**base, key: "anything"})
    with pytest.raises(ValueError, match="1 to 10"):
        UpdateRecordsRequest.from_payload({**base, "records": base["records"] * 11})


def test_update_schema_validates_then_atomically_patches_and_requeries(tmp_path):
    commands = []
    query_count = 0

    def runner(argv, _timeout):
        nonlocal query_count
        commands.append(list(argv))
        if argv[:3] == ["sf", "sobject", "describe"]:
            return CommandResult(0, _describe(), "")
        if argv[:3] == ["sf", "data", "query"]:
            query_count += 1
            return CommandResult(0, _query("old" if query_count == 1 else "new"), "")
        if argv[:4] == ["sf", "api", "request", "rest"]:
            body_path = argv[argv.index("--body") + 1][1:]
            body = json.loads(open(body_path, encoding="utf-8").read())
            assert body["allOrNone"] is True
            assert body["records"][0]["Description"] == "new"
            return CommandResult(
                0, json.dumps([{"id": RECORD_ID, "success": True, "errors": []}]), ""
            )
        raise AssertionError(argv)

    with Store(tmp_path / "state.sqlite3") as store:
        item = store.enqueue(
            UPDATE_RECORDS_ACTION,
            {
                "object": "Account",
                "records": [{"id": RECORD_ID, "fields": {"Description": "new"}}],
                "reason": "Repair acceptance record",
            },
        )
        completed = Engine(
            store,
            {
                UPDATE_RECORDS_ACTION: SalesforceUpdateRecords(
                    partial_alias="dod-check",
                    artifact_root=tmp_path / "artifacts",
                    runner=runner,
                )
            },
            worker_id="test",
            execution_enabled=True,
        ).run_once(work_id=item.id).item

    assert completed is not None and completed.state is WorkState.SUCCEEDED
    assert completed.result["before_sha256"] == _hash("old")
    assert completed.result["after_sha256"] == _hash("new")
    assert completed.evidence[0]["all_or_none"] is True
    assert completed.evidence[0]["production_execution"] is False


def test_rollback_requires_unchanged_after_state_then_restores_before(tmp_path):
    query_count = 0

    def runner(argv, _timeout):
        nonlocal query_count
        if argv[:3] == ["sf", "data", "query"]:
            query_count += 1
            return CommandResult(0, _query("new" if query_count == 1 else "old"), "")
        if argv[:4] == ["sf", "api", "request", "rest"]:
            body_path = argv[argv.index("--body") + 1][1:]
            body = json.loads(open(body_path, encoding="utf-8").read())
            assert body["records"][0]["Description"] == "old"
            return CommandResult(
                0, json.dumps([{"id": RECORD_ID, "success": True, "errors": []}]), ""
            )
        raise AssertionError(argv)

    with Store(tmp_path / "state.sqlite3") as store:
        updated = store.enqueue(UPDATE_RECORDS_ACTION, {})
        assert store.claim(updated.id, "updater") is not None
        store.succeed(
            updated.id,
            "updater",
            result={
                "object": "Account",
                "record_ids": [RECORD_ID],
                "fields": ["Description"],
                "before": {RECORD_ID: {"Description": "old"}},
                "after": {RECORD_ID: {"Description": "new"}},
                "before_sha256": _hash("old"),
                "after_sha256": _hash("new"),
            },
            evidence=[{"kind": UPDATE_RECORDS_ACTION}],
        )
        item = store.enqueue(
            ROLLBACK_UPDATE_ACTION,
            {"update_work_id": updated.id, "reason": "Restore acceptance baseline"},
        )
        completed = Engine(
            store,
            {
                ROLLBACK_UPDATE_ACTION: SalesforceRollbackUpdate(
                    store,
                    partial_alias="dod-check",
                    artifact_root=tmp_path / "artifacts",
                    runner=runner,
                )
            },
            worker_id="test",
            execution_enabled=True,
        ).run_once(work_id=item.id).item

    assert completed is not None and completed.state is WorkState.SUCCEEDED
    assert completed.result["restored_sha256"] == _hash("old")
    assert completed.evidence[0]["drift_checked"] is True


def test_rollback_contract_rejects_direct_records_or_commands():
    with pytest.raises(ValueError, match="only update_work_id"):
        RollbackUpdateRequest.from_payload(
            {"update_work_id": "one", "reason": "Restore prior state", "records": []}
        )

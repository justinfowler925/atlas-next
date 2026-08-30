from __future__ import annotations

import hashlib
import json

import pytest

from atlas_next import Engine, Store, WorkState
from atlas_next.salesforce import CommandResult
from atlas_next.salesforce_data import (
    RECONCILE_UPDATE_ACTION,
    ROLLBACK_UPDATE_ACTION,
    UPDATE_RECORDS_ACTION,
    RollbackUpdateRequest,
    SalesforceReconcileUpdate,
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


def test_update_blocks_with_recovery_evidence_after_patch_when_postquery_fails(tmp_path):
    query_count = 0

    def runner(argv, _timeout):
        nonlocal query_count
        if argv[:3] == ["sf", "sobject", "describe"]:
            return CommandResult(0, _describe(), "")
        if argv[:3] == ["sf", "data", "query"]:
            query_count += 1
            if query_count == 1:
                return CommandResult(0, _query("old"), "")
            return CommandResult(1, "", "postcheck unavailable")
        if argv[:4] == ["sf", "api", "request", "rest"]:
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

    assert completed is not None and completed.state is WorkState.BLOCKED
    assert completed.evidence[0]["before_sha256"] == _hash("old")
    assert completed.evidence[0]["after_sha256"] == _hash("new")
    assert completed.evidence[0]["patch_response_validated"] is True


@pytest.mark.parametrize(
    ("current", "disposition"), [("new", "applied"), ("old", "not_applied")]
)
def test_reconcile_update_proves_applied_or_not_applied(tmp_path, current, disposition):
    with Store(tmp_path / "state.sqlite3") as store:
        updated = store.enqueue(UPDATE_RECORDS_ACTION, {})
        assert store.claim(updated.id, "updater") is not None
        store.block(
            updated.id,
            "updater",
            reason="PATCH succeeded but postcheck failed",
            evidence=[
                {
                    "after": {RECORD_ID: {"Description": "new"}},
                    "after_sha256": _hash("new"),
                    "before": {RECORD_ID: {"Description": "old"}},
                    "before_sha256": _hash("old"),
                    "fields": ["Description"],
                    "kind": "salesforce.update_records_reconciliation_required",
                    "object": "Account",
                    "record_ids": [RECORD_ID],
                }
            ],
        )
        item = store.enqueue(RECONCILE_UPDATE_ACTION, {"update_work_id": updated.id})
        completed = Engine(
            store,
            {
                RECONCILE_UPDATE_ACTION: SalesforceReconcileUpdate(
                    store,
                    partial_alias="dod-check",
                    artifact_root=tmp_path / "artifacts",
                    runner=lambda argv, _timeout: CommandResult(0, _query(current), ""),
                )
            },
            worker_id="test",
            execution_enabled=True,
        ).run_once(work_id=item.id).item

    assert completed is not None and completed.state is WorkState.SUCCEEDED
    assert completed.result["disposition"] == disposition
    assert completed.evidence[0]["mutation_applied"] is (disposition == "applied")


def test_reconcile_update_blocks_on_concurrent_drift(tmp_path):
    with Store(tmp_path / "state.sqlite3") as store:
        updated = store.enqueue(UPDATE_RECORDS_ACTION, {})
        assert store.claim(updated.id, "updater") is not None
        store.block(
            updated.id,
            "updater",
            reason="PATCH succeeded but postcheck failed",
            evidence=[
                {
                    "after": {RECORD_ID: {"Description": "new"}},
                    "after_sha256": _hash("new"),
                    "before": {RECORD_ID: {"Description": "old"}},
                    "before_sha256": _hash("old"),
                    "fields": ["Description"],
                    "kind": "salesforce.update_records_reconciliation_required",
                    "object": "Account",
                    "record_ids": [RECORD_ID],
                }
            ],
        )
        item = store.enqueue(RECONCILE_UPDATE_ACTION, {"update_work_id": updated.id})
        completed = Engine(
            store,
            {
                RECONCILE_UPDATE_ACTION: SalesforceReconcileUpdate(
                    store,
                    partial_alias="dod-check",
                    artifact_root=tmp_path / "artifacts",
                    runner=lambda argv, _timeout: CommandResult(
                        0, _query("concurrent"), ""
                    ),
                )
            },
            worker_id="test",
            execution_enabled=True,
        ).run_once(work_id=item.id).item

    assert completed is not None and completed.state is WorkState.BLOCKED
    assert completed.evidence[0]["status"] == "drifted"


def test_rollback_accepts_reconciled_applied_update(tmp_path):
    query_count = 0

    def runner(argv, _timeout):
        nonlocal query_count
        if argv[:3] == ["sf", "data", "query"]:
            query_count += 1
            return CommandResult(0, _query("new" if query_count == 1 else "old"), "")
        if argv[:4] == ["sf", "api", "request", "rest"]:
            return CommandResult(
                0, json.dumps([{"id": RECORD_ID, "success": True, "errors": []}]), ""
            )
        raise AssertionError(argv)

    with Store(tmp_path / "state.sqlite3") as store:
        reconciled = store.enqueue(RECONCILE_UPDATE_ACTION, {})
        assert store.claim(reconciled.id, "reconciler") is not None
        store.succeed(
            reconciled.id,
            "reconciler",
            result={
                "after": {RECORD_ID: {"Description": "new"}},
                "after_sha256": _hash("new"),
                "before": {RECORD_ID: {"Description": "old"}},
                "before_sha256": _hash("old"),
                "disposition": "applied",
                "fields": ["Description"],
                "object": "Account",
                "record_ids": [RECORD_ID],
            },
            evidence=[{"kind": RECONCILE_UPDATE_ACTION}],
        )
        item = store.enqueue(
            ROLLBACK_UPDATE_ACTION,
            {"update_work_id": reconciled.id, "reason": "Restore reconciled update"},
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

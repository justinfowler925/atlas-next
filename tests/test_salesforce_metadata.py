from __future__ import annotations

import json

import pytest

from atlas_next import Engine, Store, WorkState
from atlas_next.salesforce import CommandResult
from atlas_next.salesforce_metadata import (
    METADATA_DIFF_ACTION,
    MetadataDiffRequest,
    SalesforceMetadataDiff,
)


def _inventory(metadata_type, names):
    return json.dumps(
        {
            "status": 0,
            "result": [
                {"fullName": name, "type": metadata_type, "lastModifiedDate": "ignored"}
                for name in names
            ],
        }
    )


def test_metadata_diff_contract_allows_only_a_fixed_type_vocabulary():
    with pytest.raises(ValueError, match="unexpected keys: command"):
        MetadataDiffRequest.from_payload({"type": "Flow", "command": "deploy"})
    with pytest.raises(ValueError, match="type must be one of"):
        MetadataDiffRequest.from_payload({"type": "AnythingAtAll"})


def test_metadata_diff_runs_exactly_two_read_only_inventory_commands(tmp_path):
    calls = []
    responses = iter(
        [
            CommandResult(0, _inventory("Flow", ["Shared", "PartialOnly"]), ""),
            CommandResult(0, _inventory("Flow", ["Shared", "ProdOnly"]), ""),
        ]
    )

    def runner(argv, timeout):
        calls.append((list(argv), timeout))
        return next(responses)

    with Store(tmp_path / "state.sqlite3") as store:
        item = store.enqueue(METADATA_DIFF_ACTION, {"type": "Flow"})
        completed = Engine(
            store,
            {
                METADATA_DIFF_ACTION: SalesforceMetadataDiff(
                    {"partial": "dod-check", "prod": "prod"}, runner=runner
                )
            },
            worker_id="test",
            execution_enabled=True,
        ).run_once(work_id=item.id).item

    assert [call[0] for call in calls] == [
        [
            "sf",
            "org",
            "list",
            "metadata",
            "--metadata-type",
            "Flow",
            "--target-org",
            "dod-check",
            "--json",
        ],
        [
            "sf",
            "org",
            "list",
            "metadata",
            "--metadata-type",
            "Flow",
            "--target-org",
            "prod",
            "--json",
        ],
    ]
    assert completed is not None and completed.state is WorkState.SUCCEEDED
    assert completed.result["partial_only"] == ["PartialOnly"]
    assert completed.result["prod_only"] == ["ProdOnly"]
    assert completed.result["shared_count"] == 1
    assert completed.result["parity"] is False
    assert completed.evidence[0]["read_only_commands"] == 2


def test_empty_inventories_are_truthful_parity_not_failure(tmp_path):
    responses = iter(
        [CommandResult(0, _inventory("ApexTrigger", []), "")] * 2
    )
    with Store(tmp_path / "state.sqlite3") as store:
        item = store.enqueue(METADATA_DIFF_ACTION, {"type": "ApexTrigger"})
        completed = Engine(
            store,
            {
                METADATA_DIFF_ACTION: SalesforceMetadataDiff(
                    {"partial": "dod-check", "prod": "prod"},
                    runner=lambda *_args: next(responses),
                )
            },
            worker_id="test",
            execution_enabled=True,
        ).run_once(work_id=item.id).item
    assert completed is not None and completed.state is WorkState.SUCCEEDED
    assert completed.result["parity"] is True
    assert completed.result["shared_count"] == 0


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (_inventory("ApexClass", ["WrongType"]), "unexpected component type"),
        (json.dumps({"status": 0, "result": ["bad"]}), "row must be an object"),
        (_inventory("Flow", ["Same", "Same"]), "duplicate"),
    ],
)
def test_malformed_inventory_cannot_mint_success(tmp_path, payload, reason):
    with Store(tmp_path / "state.sqlite3") as store:
        item = store.enqueue(METADATA_DIFF_ACTION, {"type": "Flow"})
        completed = Engine(
            store,
            {
                METADATA_DIFF_ACTION: SalesforceMetadataDiff(
                    {"partial": "dod-check", "prod": "prod"},
                    runner=lambda *_args: CommandResult(0, payload, ""),
                )
            },
            worker_id="test",
            execution_enabled=True,
        ).run_once(work_id=item.id).item
    assert completed is not None and completed.state is WorkState.FAILED
    assert reason in (completed.error or "")


def test_second_environment_failure_preserves_salesforce_reason(tmp_path):
    responses = iter(
        [
            CommandResult(0, _inventory("Flow", ["One"]), ""),
            CommandResult(1, json.dumps({"status": 1, "message": "prod auth expired"}), ""),
        ]
    )
    with Store(tmp_path / "state.sqlite3") as store:
        item = store.enqueue(METADATA_DIFF_ACTION, {"type": "Flow"})
        completed = Engine(
            store,
            {
                METADATA_DIFF_ACTION: SalesforceMetadataDiff(
                    {"partial": "dod-check", "prod": "prod"},
                    runner=lambda *_args: next(responses),
                )
            },
            worker_id="test",
            execution_enabled=True,
        ).run_once(work_id=item.id).item
    assert completed is not None and completed.state is WorkState.FAILED
    assert "prod auth expired" in (completed.error or "")

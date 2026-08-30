from __future__ import annotations

import json
from pathlib import Path

import pytest

from atlas_next import Engine, Store, WorkState
from atlas_next.salesforce import CommandResult
from atlas_next.salesforce_metadata import (
    METADATA_CONTENT_DIFF_ACTION,
    METADATA_DIFF_ACTION,
    MetadataContentDiffRequest,
    MetadataDiffRequest,
    SalesforceMetadataContentDiff,
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


def _retrieved() -> str:
    return json.dumps({"status": 0, "result": {"done": True, "status": "Succeeded"}})


def test_content_diff_contract_rejects_wildcards_traversal_and_commands():
    for name in ("*", "../Flow", "Flow*", "/absolute"):
        with pytest.raises(ValueError, match="exactly one"):
            MetadataContentDiffRequest.from_payload({"type": "Flow", "name": name})
    with pytest.raises(ValueError, match="unexpected keys: command"):
        MetadataContentDiffRequest.from_payload(
            {"type": "Flow", "name": "My_Flow", "command": "deploy"}
        )


def test_content_diff_retrieves_exact_component_from_both_orgs_and_hashes(tmp_path):
    project = tmp_path / "sfdc"
    project.mkdir()
    (project / "sfdx-project.json").write_text("{}")
    calls = []

    def runner(argv, cwd, timeout):
        calls.append((list(argv), cwd, timeout))
        output = tmp_path / "artifacts" / item.id / ("partial" if len(calls) == 1 else "prod")
        component = output / "unpackaged" / "flows" / "My_Flow.flow"
        component.parent.mkdir(parents=True)
        component.write_text("<Flow>partial</Flow>" if len(calls) == 1 else "<Flow>prod</Flow>")
        (output / "unpackaged" / "package.xml").write_text("ignored")
        return CommandResult(0, _retrieved(), "")

    with Store(tmp_path / "state.sqlite3") as store:
        item = store.enqueue(
            METADATA_CONTENT_DIFF_ACTION, {"type": "Flow", "name": "My_Flow"}
        )
        completed = Engine(
            store,
            {
                METADATA_CONTENT_DIFF_ACTION: SalesforceMetadataContentDiff(
                    {"partial": "dod-check", "prod": "prod"},
                    project_dir=project,
                    artifact_root=tmp_path / "artifacts",
                    runner=runner,
                )
            },
            worker_id="test",
            execution_enabled=True,
        ).run_once(work_id=item.id).item

    assert [call[0][5] for call in calls] == ["Flow:My_Flow", "Flow:My_Flow"]
    assert [call[0][7] for call in calls] == ["dod-check", "prod"]
    assert all(call[1] == project.resolve() for call in calls)
    assert completed is not None and completed.state is WorkState.SUCCEEDED
    assert completed.result["changed_files"] == ["flows/My_Flow.flow"]
    assert completed.result["content_parity"] is False
    assert completed.evidence[0]["read_only_commands"] == 2


def test_content_diff_proves_identical_multi_file_component(tmp_path):
    project = tmp_path / "sfdc"
    project.mkdir()
    (project / "sfdx-project.json").write_text("{}")

    def runner(argv, _cwd, _timeout):
        output = Path(argv[argv.index("--target-metadata-dir") + 1])
        source = output / "unpackaged" / "classes"
        source.mkdir(parents=True)
        (source / "Service.cls").write_text("public class Service {}")
        (source / "Service.cls-meta.xml").write_text("<ApexClass/>")
        return CommandResult(0, _retrieved(), "")

    with Store(tmp_path / "state.sqlite3") as store:
        item = store.enqueue(
            METADATA_CONTENT_DIFF_ACTION, {"type": "ApexClass", "name": "Service"}
        )
        completed = Engine(
            store,
            {
                METADATA_CONTENT_DIFF_ACTION: SalesforceMetadataContentDiff(
                    {"partial": "dod-check", "prod": "prod"},
                    project_dir=project,
                    artifact_root=tmp_path / "artifacts",
                    runner=runner,
                )
            },
            worker_id="test",
            execution_enabled=True,
        ).run_once(work_id=item.id).item
    assert completed is not None and completed.state is WorkState.SUCCEEDED
    assert completed.result["content_parity"] is True
    assert completed.result["partial_file_count"] == 2
    assert completed.evidence[0]["partial_manifest_sha256"] == (
        completed.evidence[0]["prod_manifest_sha256"]
    )


def test_content_diff_refuses_non_project_and_empty_retrieve(tmp_path):
    with Store(tmp_path / "state.sqlite3") as store:
        item = store.enqueue(
            METADATA_CONTENT_DIFF_ACTION, {"type": "Flow", "name": "My_Flow"}
        )
        completed = Engine(
            store,
            {
                METADATA_CONTENT_DIFF_ACTION: SalesforceMetadataContentDiff(
                    {"partial": "dod-check", "prod": "prod"},
                    project_dir=tmp_path / "not-project",
                    artifact_root=tmp_path / "artifacts",
                )
            },
            worker_id="test",
            execution_enabled=True,
        ).run_once(work_id=item.id).item
    assert completed is not None and completed.state is WorkState.FAILED
    assert "not a Salesforce project" in (completed.error or "")

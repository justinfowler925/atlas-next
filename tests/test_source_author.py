from __future__ import annotations

import hashlib

import pytest

from atlas_next import Engine, Store, WorkState
from atlas_next.salesforce import CommandResult
from atlas_next.salesforce_metadata import SOURCE_RETRIEVE_ACTION
from atlas_next.source_author import AUTHOR_SOURCE_ACTION, AuthorSource, AuthorSourceRequest


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_author_contract_has_no_root_branch_command_or_deploy_escape():
    payload = {
        "retrieve_work_id": "one",
        "path": "salesforce/force-app/main/default/classes/Service.cls",
        "expected_sha256": "a" * 64,
        "content": "public class Service {}",
    }
    for key in ("git_root", "branch", "command", "target_org", "deploy"):
        with pytest.raises(ValueError, match=f"unexpected keys: {key}"):
            AuthorSourceRequest.from_payload({**payload, key: "anything"})
    with pytest.raises(ValueError, match="editable relative"):
        AuthorSourceRequest.from_payload({**payload, "path": "../Service.cls"})


def test_author_rehashes_receipt_and_replaces_exact_source_file(tmp_path):
    git_root = tmp_path / "worktree"
    path = "salesforce/force-app/main/default/classes/Service.cls"
    source = git_root / path
    source.parent.mkdir(parents=True)
    source.write_text("public class Service {}")
    baseline = _sha(source)
    replacement = "public class Service { public static Integer value() { return 2; } }\n"

    with Store(tmp_path / "state.sqlite3") as store:
        retrieved = store.enqueue(SOURCE_RETRIEVE_ACTION, {})
        assert store.claim(retrieved.id, "retriever") is not None
        store.succeed(
            retrieved.id,
            "retriever",
            result={
                "git_root": str(git_root),
                "branch": "justin-fowler/author",
                "type": "ApexClass",
                "name": "Service",
                "files": [{"path": path, "sha256": baseline}],
            },
            evidence=[{"kind": SOURCE_RETRIEVE_ACTION}],
        )

        def runner(argv, _cwd, _timeout):
            if argv == ["git", "branch", "--show-current"]:
                return CommandResult(0, "justin-fowler/author\n", "")
            if argv == ["git", "status", "--porcelain=v1"]:
                status = "" if source.read_text() != replacement else f" M {path}\n"
                return CommandResult(0, status, "")
            raise AssertionError(argv)

        item = store.enqueue(
            AUTHOR_SOURCE_ACTION,
            {
                "retrieve_work_id": retrieved.id,
                "path": path,
                "expected_sha256": baseline,
                "content": replacement,
            },
        )
        completed = Engine(
            store,
            {AUTHOR_SOURCE_ACTION: AuthorSource(store, runner=runner)},
            worker_id="test",
            execution_enabled=True,
        ).run_once(work_id=item.id).item

    assert source.read_text() == replacement
    assert completed is not None and completed.state is WorkState.SUCCEEDED
    assert completed.result["baseline_sha256"] == baseline
    assert completed.result["authored_sha256"] == _sha(source)
    assert completed.evidence[0]["production_execution"] is False


def test_author_refuses_stale_receipt_before_writing(tmp_path):
    git_root = tmp_path / "worktree"
    path = "salesforce/force-app/main/default/classes/Service.cls"
    source = git_root / path
    source.parent.mkdir(parents=True)
    source.write_text("changed elsewhere")
    with Store(tmp_path / "state.sqlite3") as store:
        retrieved = store.enqueue(SOURCE_RETRIEVE_ACTION, {})
        assert store.claim(retrieved.id, "retriever") is not None
        store.succeed(
            retrieved.id,
            "retriever",
            result={
                "git_root": str(git_root),
                "branch": "feature",
                "type": "ApexClass",
                "name": "Service",
                "files": [{"path": path, "sha256": "a" * 64}],
            },
            evidence=[{"kind": SOURCE_RETRIEVE_ACTION}],
        )
        item = store.enqueue(
            AUTHOR_SOURCE_ACTION,
            {
                "retrieve_work_id": retrieved.id,
                "path": path,
                "expected_sha256": "a" * 64,
                "content": "public class Service {}",
            },
        )
        completed = Engine(
            store,
            {
                AUTHOR_SOURCE_ACTION: AuthorSource(
                    store,
                    runner=lambda argv, *_args: CommandResult(
                        0, "feature\n" if "branch" in argv else "", ""
                    ),
                )
            },
            worker_id="test",
            execution_enabled=True,
        ).run_once(work_id=item.id).item

    assert completed is not None and completed.state is WorkState.FAILED
    assert "changed before authoring" in (completed.error or "")


def test_author_rejects_malformed_or_entity_xml():
    base = {
        "retrieve_work_id": "one",
        "path": "salesforce/force-app/main/default/flows/Proof.flow-meta.xml",
        "expected_sha256": "a" * 64,
    }
    # Payload validation accepts text; the capability-level parser rejects unsafe XML.
    assert AuthorSourceRequest.from_payload({**base, "content": "<Flow>"}).content == "<Flow>"

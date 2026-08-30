from __future__ import annotations

import hashlib

import pytest

from atlas_next import Engine, Store, WorkState
from atlas_next.integration_source import (
    CREATE_INTEGRATION_SOURCE_ACTION,
    CreateIntegrationSource,
    CreateIntegrationSourceRequest,
)
from atlas_next.salesforce import CommandResult


PAYLOAD = {
    "name": "AtlasAcceptanceExchangeRate",
    "base_url": "https://open.er-api.com",
    "path": "/v6/latest/USD",
    "marker_field": "result",
    "expected_marker": "success",
}


def test_integration_contract_rejects_command_credentials_and_unbounded_urls():
    for key in ("command", "target_org", "authorization", "token"):
        with pytest.raises(ValueError, match="payload must contain only"):
            CreateIntegrationSourceRequest.from_payload({**PAYLOAD, key: "x"})
    with pytest.raises(ValueError, match="credential-free HTTPS origin"):
        CreateIntegrationSourceRequest.from_payload(
            {**PAYLOAD, "base_url": "https://user:secret@example.com?token=x"}
        )
    with pytest.raises(ValueError, match="absolute URL path"):
        CreateIntegrationSourceRequest.from_payload({**PAYLOAD, "path": "/v1?q=secret"})


def test_integration_creation_writes_exact_generated_bundle(tmp_path):
    git_root = tmp_path / "worktree"
    project = git_root / "salesforce"
    project.mkdir(parents=True)
    (project / "sfdx-project.json").write_text("{}")
    prefix = "salesforce/force-app/main/default"
    expected_paths = {
        f"{prefix}/classes/AtlasAcceptanceExchangeRate.cls",
        f"{prefix}/classes/AtlasAcceptanceExchangeRate.cls-meta.xml",
        f"{prefix}/classes/AtlasAcceptanceExchangeRateTest.cls",
        f"{prefix}/classes/AtlasAcceptanceExchangeRateTest.cls-meta.xml",
        f"{prefix}/remoteSiteSettings/AtlasAcceptanceExchangeRate.remoteSite-meta.xml",
    }

    def runner(argv, _cwd, _timeout):
        if argv[:2] == ["git", "rev-parse"] and argv[-1] == "--show-toplevel":
            return CommandResult(0, str(git_root), "")
        if argv == ["git", "branch", "--show-current"]:
            return CommandResult(0, "justin-fowler/integration\n", "")
        if argv[:2] == ["git", "rev-parse"] and argv[-1] == "--git-dir":
            return CommandResult(0, str(tmp_path / "repo/.git/worktrees/integration"), "")
        if argv[:2] == ["git", "rev-parse"] and argv[-1] == "--git-common-dir":
            return CommandResult(0, str(tmp_path / "repo/.git"), "")
        if argv == ["git", "status", "--porcelain=v1"]:
            return CommandResult(0, "", "")
        if argv == ["git", "status", "--porcelain=v1", "--untracked-files=all"]:
            existing = sorted(path for path in expected_paths if (git_root / path).is_file())
            return CommandResult(0, "".join(f"?? {path}\n" for path in existing), "")
        raise AssertionError(argv)

    with Store(tmp_path / "state.sqlite3") as store:
        item = store.enqueue(CREATE_INTEGRATION_SOURCE_ACTION, PAYLOAD)
        completed = Engine(
            store,
            {
                CREATE_INTEGRATION_SOURCE_ACTION: CreateIntegrationSource(
                    project_dir=project, runner=runner
                )
            },
            worker_id="test",
            execution_enabled=True,
        ).run_once(work_id=item.id).item

    assert completed is not None and completed.state is WorkState.SUCCEEDED
    assert completed.result["file_count"] == 5
    assert {row["path"] for row in completed.result["files"]} == expected_paths
    service = git_root / f"{prefix}/classes/AtlasAcceptanceExchangeRate.cls"
    assert "new Http().send(request)" in service.read_text()
    assert next(row for row in completed.result["files"] if row["path"] == str(service.relative_to(git_root)))[
        "sha256"
    ] == hashlib.sha256(service.read_bytes()).hexdigest()


def test_integration_creation_requires_isolated_clean_worktree(tmp_path):
    project = tmp_path / "salesforce"
    project.mkdir()
    (project / "sfdx-project.json").write_text("{}")

    def runner(argv, _cwd, _timeout):
        if argv[:2] == ["git", "rev-parse"] and argv[-1] == "--show-toplevel":
            return CommandResult(0, str(tmp_path), "")
        if argv == ["git", "branch", "--show-current"]:
            return CommandResult(0, "main\n", "")
        raise AssertionError(argv)

    with Store(tmp_path / "state.sqlite3") as store:
        item = store.enqueue(CREATE_INTEGRATION_SOURCE_ACTION, PAYLOAD)
        completed = Engine(
            store,
            {
                CREATE_INTEGRATION_SOURCE_ACTION: CreateIntegrationSource(
                    project_dir=project, runner=runner
                )
            },
            worker_id="test",
            execution_enabled=True,
        ).run_once(work_id=item.id).item

    assert completed is not None and completed.state is WorkState.FAILED
    assert "non-main branch" in (completed.error or "")

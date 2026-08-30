from __future__ import annotations

import hashlib
import json

import pytest

from atlas_next import Engine, Store, WorkState
from atlas_next.delivery import (
    COMMIT_SOURCE_ACTION,
    MERGE_PR_ACTION,
    OPEN_PR_ACTION,
    VERIFY_PR_ACTION,
    CommitSource,
    CommitSourceRequest,
    MergePr,
    MergePullRequest,
    OpenPr,
    OpenPullRequest,
    VerifyPr,
    _parse_checks,
)
from atlas_next.salesforce import CommandResult
from atlas_next.salesforce_metadata import SOURCE_RETRIEVE_ACTION


def test_commit_contract_rejects_commands_paths_and_bad_messages():
    with pytest.raises(ValueError, match="unexpected keys: command"):
        CommitSourceRequest.from_payload(
            {"source_work_ids": ["one"], "message": "chore: proof", "command": "push"}
        )
    with pytest.raises(ValueError, match="conventional commit"):
        CommitSourceRequest.from_payload(
            {"source_work_ids": ["one"], "message": "proof\nmore"}
        )


def test_commit_stages_exact_evidence_linked_files_and_returns_sha(tmp_path):
    git_root = tmp_path / "worktree"
    path = "salesforce/force-app/main/default/classes/Service.cls"
    source_file = git_root / path
    source_file.parent.mkdir(parents=True)
    source_file.write_text("public class Service {}")
    digest = hashlib.sha256(source_file.read_bytes()).hexdigest()
    status = f"?? {path}\n"
    sha = "a" * 40
    commands = []

    with Store(tmp_path / "state.sqlite3") as store:
        source = store.enqueue(SOURCE_RETRIEVE_ACTION, {})
        claimed = store.claim(source.id, "producer")
        assert claimed is not None
        store.succeed(
            source.id,
            "producer",
            result={
                "git_root": str(git_root),
                "branch": "justin-fowler/proof",
                "files": [{"path": path, "sha256": digest}],
            },
            evidence=[{"kind": SOURCE_RETRIEVE_ACTION}],
        )
        committed = False

        def runner(argv, _cwd, _timeout):
            nonlocal committed
            commands.append(list(argv))
            if argv == ["git", "branch", "--show-current"]:
                return CommandResult(0, "justin-fowler/proof\n", "")
            if argv == ["git", "status", "--porcelain=v1"]:
                return CommandResult(0, "" if committed else status, "")
            if argv[:4] == ["git", "diff", "--cached", "--name-only"]:
                return CommandResult(0, path + "\0", "")
            if argv[:2] == ["git", "commit"]:
                committed = True
                return CommandResult(0, "committed", "")
            if argv == ["git", "rev-parse", "HEAD"]:
                return CommandResult(0, sha + "\n", "")
            return CommandResult(0, "", "")

        item = store.enqueue(
            COMMIT_SOURCE_ACTION,
            {"source_work_ids": [source.id], "message": "chore: capture acceptance service"},
        )
        completed = Engine(
            store,
            {COMMIT_SOURCE_ACTION: CommitSource(store, runner=runner)},
            worker_id="test",
            execution_enabled=True,
        ).run_once(work_id=item.id).item

    assert ["git", "add", "--", path] in commands
    assert completed is not None and completed.state is WorkState.SUCCEEDED
    assert completed.result["commit_sha"] == sha
    assert completed.evidence[0]["clean_after_commit"] is True
    assert completed.evidence[0]["pushed"] is False


def test_commit_refuses_changed_bytes_or_unrelated_dirt(tmp_path):
    git_root = tmp_path / "worktree"
    path = "salesforce/force-app/main/default/classes/Service.cls"
    source_file = git_root / path
    source_file.parent.mkdir(parents=True)
    source_file.write_text("changed")
    with Store(tmp_path / "state.sqlite3") as store:
        source = store.enqueue(SOURCE_RETRIEVE_ACTION, {})
        assert store.claim(source.id, "producer") is not None
        store.succeed(
            source.id,
            "producer",
            result={
                "git_root": str(git_root),
                "branch": "justin-fowler/proof",
                "files": [{"path": path, "sha256": "0" * 64}],
            },
            evidence=[{"kind": SOURCE_RETRIEVE_ACTION}],
        )
        item = store.enqueue(
            COMMIT_SOURCE_ACTION,
            {"source_work_ids": [source.id], "message": "chore: capture acceptance service"},
        )
        completed = Engine(
            store,
            {
                COMMIT_SOURCE_ACTION: CommitSource(
                    store,
                    runner=lambda argv, *_args: CommandResult(
                        0, "justin-fowler/proof\n" if "branch" in argv else "", ""
                    ),
                )
            },
            worker_id="test",
            execution_enabled=True,
        ).run_once(work_id=item.id).item
    assert completed is not None and completed.state is WorkState.FAILED
    assert "changed after production" in (completed.error or "")


def test_open_pr_contract_has_no_repo_branch_push_or_merge_escape():
    base = {
        "commit_work_ids": ["one"],
        "title": "Capture acceptance service",
        "body": "Retrieved exactly from Partial and hash-verified.",
    }
    for key in ("repo", "branch", "base", "command", "merge", "force"):
        with pytest.raises(ValueError, match=f"unexpected keys: {key}"):
            OpenPullRequest.from_payload({**base, key: "anything"})


def test_open_pr_pushes_evidence_branch_and_creates_pr_against_current_main(tmp_path):
    git_root = tmp_path / "worktree"
    git_root.mkdir()
    head = "a" * 40
    base = "b" * 40
    branch = "justin-fowler/atlas-proof"
    commands = []

    with Store(tmp_path / "state.sqlite3") as store:
        commit = store.enqueue(COMMIT_SOURCE_ACTION, {})
        assert store.claim(commit.id, "committer") is not None
        store.succeed(
            commit.id,
            "committer",
            result={"git_root": str(git_root), "branch": branch, "commit_sha": head},
            evidence=[{"kind": COMMIT_SOURCE_ACTION}],
        )

        def runner(argv, _cwd, _timeout):
            commands.append(list(argv))
            if argv == ["git", "branch", "--show-current"]:
                return CommandResult(0, branch + "\n", "")
            if argv == ["git", "status", "--porcelain=v1"]:
                return CommandResult(0, "", "")
            if argv == ["git", "rev-parse", "HEAD"]:
                return CommandResult(0, head + "\n", "")
            if argv == ["git", "rev-parse", "origin/main"]:
                return CommandResult(0, base + "\n", "")
            if argv[:3] == ["git", "rev-list", "--count"]:
                return CommandResult(0, "1\n", "")
            if argv[:3] == ["gh", "repo", "view"]:
                return CommandResult(0, '{"nameWithOwner":"ClearspeedRevOps/sfdc"}', "")
            if argv[:3] == ["gh", "pr", "list"]:
                return CommandResult(0, "[]", "")
            if argv[:3] == ["gh", "pr", "create"]:
                return CommandResult(0, "https://github.com/ClearspeedRevOps/sfdc/pull/999\n", "")
            return CommandResult(0, "", "")

        item = store.enqueue(
            OPEN_PR_ACTION,
            {
                "commit_work_ids": [commit.id],
                "title": "Capture acceptance service",
                "body": "Retrieved exactly from Partial and independently hash-verified.",
            },
        )
        completed = Engine(
            store,
            {OPEN_PR_ACTION: OpenPr(store, runner=runner)},
            worker_id="test",
            execution_enabled=True,
        ).run_once(work_id=item.id).item

    assert ["git", "fetch", "origin", "main"] in commands
    assert ["git", "push", "-u", "origin", f"HEAD:refs/heads/{branch}"] in commands
    assert completed is not None and completed.state is WorkState.SUCCEEDED
    assert completed.result["pr_number"] == 999
    assert completed.evidence[0]["contains_current_main"] is True
    assert completed.evidence[0]["merged"] is False


def test_open_pr_refuses_branch_behind_current_main_before_push(tmp_path):
    git_root = tmp_path / "worktree"
    git_root.mkdir()
    head = "a" * 40
    base = "b" * 40
    pushed = False
    with Store(tmp_path / "state.sqlite3") as store:
        commit = store.enqueue(COMMIT_SOURCE_ACTION, {})
        assert store.claim(commit.id, "committer") is not None
        store.succeed(
            commit.id,
            "committer",
            result={"git_root": str(git_root), "branch": "feature", "commit_sha": head},
            evidence=[{"kind": COMMIT_SOURCE_ACTION}],
        )

        def runner(argv, _cwd, _timeout):
            nonlocal pushed
            if argv[:2] == ["git", "push"]:
                pushed = True
            if argv == ["git", "branch", "--show-current"]:
                return CommandResult(0, "feature\n", "")
            if argv == ["git", "status", "--porcelain=v1"]:
                return CommandResult(0, "", "")
            if argv == ["git", "rev-parse", "HEAD"]:
                return CommandResult(0, head, "")
            if argv == ["git", "rev-parse", "origin/main"]:
                return CommandResult(0, base, "")
            if argv[:3] == ["git", "merge-base", "--is-ancestor"] and base in argv:
                return CommandResult(1, "", "behind")
            return CommandResult(0, "", "")

        item = store.enqueue(
            OPEN_PR_ACTION,
            {
                "commit_work_ids": [commit.id],
                "title": "Capture acceptance service",
                "body": "Retrieved exactly from Partial and independently hash-verified.",
            },
        )
        completed = Engine(
            store,
            {OPEN_PR_ACTION: OpenPr(store, runner=runner)},
            worker_id="test",
            execution_enabled=True,
        ).run_once(work_id=item.id).item
    assert pushed is False
    assert completed is not None and completed.state is WorkState.FAILED
    assert "behind current origin/main" in (completed.error or "")


def test_verify_pr_waits_for_named_checks_and_proves_current_main(tmp_path):
    git_root = tmp_path / "worktree"
    git_root.mkdir()
    head = "a" * 40
    base = "b" * 40
    checks = [
        {"name": "Validate (sandbox)", "status": "COMPLETED", "conclusion": "SUCCESS"},
        {"name": "LWC unit tests", "status": "COMPLETED", "conclusion": "SUCCESS"},
        {
            "name": "PM Tracker + revops-dash verify",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
        },
        {"name": "Deploy (production)", "status": "COMPLETED", "conclusion": "SKIPPED"},
    ]
    pr = {
        "number": 999,
        "url": "https://github.com/ClearspeedRevOps/sfdc/pull/999",
        "state": "OPEN",
        "isDraft": False,
        "mergeStateStatus": "CLEAN",
        "headRefOid": head,
        "baseRefOid": base,
        "statusCheckRollup": checks,
    }
    commands = []
    with Store(tmp_path / "state.sqlite3") as store:
        opened = store.enqueue(OPEN_PR_ACTION, {})
        assert store.claim(opened.id, "opener") is not None
        store.succeed(
            opened.id,
            "opener",
            result={"git_root": str(git_root), "pr_number": 999, "head_sha": head},
            evidence=[{"kind": OPEN_PR_ACTION}],
        )

        def runner(argv, _cwd, _timeout):
            commands.append(list(argv))
            if argv[:3] == ["gh", "pr", "view"]:
                return CommandResult(0, json.dumps(pr), "")
            if argv == ["git", "rev-parse", "origin/main"]:
                return CommandResult(0, base, "")
            return CommandResult(0, "green", "")

        item = store.enqueue(VERIFY_PR_ACTION, {"open_pr_work_id": opened.id})
        completed = Engine(
            store,
            {VERIFY_PR_ACTION: VerifyPr(store, runner=runner)},
            worker_id="test",
            execution_enabled=True,
        ).run_once(work_id=item.id).item

    assert ["gh", "pr", "checks", "999", "--watch", "--interval", "10"] in commands
    assert completed is not None and completed.state is WorkState.SUCCEEDED
    assert completed.result["check_count"] == 4
    assert completed.evidence[0]["sandbox_validation"] == "SUCCESS"
    assert completed.evidence[0]["merge_state"] == "CLEAN"


def test_parse_checks_rejects_pending_duplicate_or_zero_population():
    with pytest.raises(ValueError, match="no status checks"):
        _parse_checks([])
    with pytest.raises(ValueError, match="incomplete"):
        _parse_checks([{"name": "Validate (sandbox)", "status": "IN_PROGRESS", "conclusion": ""}])
    with pytest.raises(ValueError, match="duplicate"):
        _parse_checks(
            [
                {"name": "same", "status": "COMPLETED", "conclusion": "SUCCESS"},
                {"name": "same", "status": "COMPLETED", "conclusion": "SUCCESS"},
            ]
        )


def test_merge_pr_contract_has_only_verified_receipt():
    with pytest.raises(ValueError, match="unexpected keys: command"):
        MergePullRequest.from_payload(
            {"verify_pr_work_id": "one", "command": "gh pr merge"}
        )


def test_merge_pr_rechecks_head_and_current_main_then_records_receipt(tmp_path):
    git_root = tmp_path / "worktree"
    git_root.mkdir()
    head = "a" * 40
    base = "b" * 40
    merge_sha = "c" * 40
    checks = [
        {"name": "Validate (sandbox)", "status": "COMPLETED", "conclusion": "SUCCESS"},
        {"name": "LWC unit tests", "status": "COMPLETED", "conclusion": "SUCCESS"},
        {
            "name": "PM Tracker + revops-dash verify",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
        },
    ]
    open_pr = {
        "number": 999,
        "url": "https://github.com/ClearspeedRevOps/sfdc/pull/999",
        "state": "OPEN",
        "isDraft": False,
        "mergeStateStatus": "CLEAN",
        "headRefOid": head,
        "baseRefOid": base,
        "statusCheckRollup": checks,
    }
    merged_pr = {
        "number": 999,
        "url": "https://github.com/ClearspeedRevOps/sfdc/pull/999",
        "state": "MERGED",
        "mergedAt": "2026-08-30T01:02:03Z",
        "mergeCommit": {"oid": merge_sha},
        "headRefOid": head,
    }
    commands = []
    with Store(tmp_path / "state.sqlite3") as store:
        verified = store.enqueue(VERIFY_PR_ACTION, {})
        assert store.claim(verified.id, "verifier") is not None
        store.succeed(
            verified.id,
            "verifier",
            result={
                "git_root": str(git_root),
                "pr_number": 999,
                "head_sha": head,
            },
            evidence=[{"kind": VERIFY_PR_ACTION}],
        )
        view_count = 0

        def runner(argv, _cwd, _timeout):
            nonlocal view_count
            commands.append(list(argv))
            if argv[:3] == ["gh", "pr", "view"]:
                view_count += 1
                return CommandResult(
                    0, json.dumps(open_pr if view_count == 1 else merged_pr), ""
                )
            if argv == ["git", "rev-parse", "origin/main"]:
                return CommandResult(0, base, "")
            return CommandResult(0, "", "")

        item = store.enqueue(MERGE_PR_ACTION, {"verify_pr_work_id": verified.id})
        completed = Engine(
            store,
            {MERGE_PR_ACTION: MergePr(store, runner=runner)},
            worker_id="test",
            execution_enabled=True,
        ).run_once(work_id=item.id).item

    assert [
        "gh", "pr", "merge", "999", "--squash", "--match-head-commit", head
    ] in commands
    assert completed is not None and completed.state is WorkState.SUCCEEDED
    assert completed.result["merge_sha"] == merge_sha
    assert completed.evidence[0]["merged"] is True


def test_merge_pr_refuses_changed_head_before_merge(tmp_path):
    git_root = tmp_path / "worktree"
    git_root.mkdir()
    head = "a" * 40
    merged = False
    with Store(tmp_path / "state.sqlite3") as store:
        verified = store.enqueue(VERIFY_PR_ACTION, {})
        assert store.claim(verified.id, "verifier") is not None
        store.succeed(
            verified.id,
            "verifier",
            result={"git_root": str(git_root), "pr_number": 999, "head_sha": head},
            evidence=[{"kind": VERIFY_PR_ACTION}],
        )

        def runner(argv, _cwd, _timeout):
            nonlocal merged
            if argv[:3] == ["gh", "pr", "merge"]:
                merged = True
            if argv[:3] == ["gh", "pr", "view"]:
                return CommandResult(
                    0,
                    json.dumps(
                        {
                            "state": "OPEN",
                            "isDraft": False,
                            "headRefOid": "d" * 40,
                        }
                    ),
                    "",
                )
            return CommandResult(0, "", "")

        item = store.enqueue(MERGE_PR_ACTION, {"verify_pr_work_id": verified.id})
        completed = Engine(
            store,
            {MERGE_PR_ACTION: MergePr(store, runner=runner)},
            worker_id="test",
            execution_enabled=True,
        ).run_once(work_id=item.id).item

    assert merged is False
    assert completed is not None and completed.state is WorkState.FAILED
    assert "head changed" in (completed.error or "")

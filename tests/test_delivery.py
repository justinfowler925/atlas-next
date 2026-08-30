from __future__ import annotations

import hashlib

import pytest

from atlas_next import Engine, Store, WorkState
from atlas_next.delivery import (
    COMMIT_SOURCE_ACTION,
    OPEN_PR_ACTION,
    CommitSource,
    CommitSourceRequest,
    OpenPr,
    OpenPullRequest,
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

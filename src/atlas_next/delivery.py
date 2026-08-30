from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .engine import Outcome
from .models import WorkItem, WorkState
from .salesforce import CommandResult, _failure_detail
from .salesforce_metadata import SOURCE_RETRIEVE_ACTION
from .source_author import AUTHOR_SOURCE_ACTION
from .flow_source import CREATE_FLOW_SOURCE_ACTION
from .lwc_source import CREATE_LWC_SOURCE_ACTION
from .report_source import CREATE_REPORT_SOURCE_ACTION
from .integration_source import CREATE_INTEGRATION_SOURCE_ACTION
from .store import Store


COMMIT_SOURCE_ACTION = "delivery.commit_source"
OPEN_PR_ACTION = "delivery.open_pr"
VERIFY_PR_ACTION = "delivery.verify_pr"
MERGE_PR_ACTION = "delivery.merge_pr"
VERIFY_SANDBOX_DEPLOY_ACTION = "delivery.verify_sandbox_deploy"
_MESSAGE_RE = re.compile(r"^(feat|fix|chore|test|docs|refactor): [^\r\n]{3,100}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class CommitSourceRequest:
    source_work_ids: tuple[str, ...]
    message: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> CommitSourceRequest:
        if set(payload) != {"source_work_ids", "message"}:
            unexpected = sorted(set(payload) - {"source_work_ids", "message"})
            missing = sorted({"source_work_ids", "message"} - set(payload))
            details = []
            if missing:
                details.append(f"missing keys: {', '.join(missing)}")
            if unexpected:
                details.append(f"unexpected keys: {', '.join(unexpected)}")
            raise ValueError(
                "invalid delivery.commit_source payload (" + "; ".join(details) + ")"
            )
        raw_ids = payload["source_work_ids"]
        if not isinstance(raw_ids, list) or not 1 <= len(raw_ids) <= 20:
            raise ValueError("source_work_ids must contain 1 to 20 work item ids")
        ids = tuple(raw_ids)
        if any(not isinstance(value, str) or not value for value in ids):
            raise ValueError("every source work id must be non-empty text")
        if len(ids) != len(set(ids)):
            raise ValueError("source_work_ids must not contain duplicates")
        message = payload["message"]
        if not isinstance(message, str) or not _MESSAGE_RE.fullmatch(message):
            raise ValueError("message must be one single-line conventional commit subject")
        return cls(ids, message)


@dataclass(frozen=True)
class OpenPullRequest:
    commit_work_ids: tuple[str, ...]
    title: str
    body: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> OpenPullRequest:
        if set(payload) != {"commit_work_ids", "title", "body"}:
            unexpected = sorted(set(payload) - {"commit_work_ids", "title", "body"})
            missing = sorted({"commit_work_ids", "title", "body"} - set(payload))
            details = []
            if missing:
                details.append(f"missing keys: {', '.join(missing)}")
            if unexpected:
                details.append(f"unexpected keys: {', '.join(unexpected)}")
            raise ValueError("invalid delivery.open_pr payload (" + "; ".join(details) + ")")
        raw_ids = payload["commit_work_ids"]
        if not isinstance(raw_ids, list) or not 1 <= len(raw_ids) <= 20:
            raise ValueError("commit_work_ids must contain 1 to 20 work item ids")
        ids = tuple(raw_ids)
        if any(not isinstance(value, str) or not value for value in ids):
            raise ValueError("every commit work id must be non-empty text")
        if len(ids) != len(set(ids)):
            raise ValueError("commit_work_ids must not contain duplicates")
        title = payload["title"]
        body = payload["body"]
        if not isinstance(title, str) or not 10 <= len(title) <= 120 or "\n" in title:
            raise ValueError("title must be one line containing 10 to 120 characters")
        if not isinstance(body, str) or not 20 <= len(body) <= 5000:
            raise ValueError("body must contain 20 to 5000 characters")
        return cls(ids, title, body)


@dataclass(frozen=True)
class VerifyPullRequest:
    open_pr_work_id: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> VerifyPullRequest:
        if set(payload) != {"open_pr_work_id"}:
            unexpected = sorted(set(payload) - {"open_pr_work_id"})
            missing = sorted({"open_pr_work_id"} - set(payload))
            details = []
            if missing:
                details.append("missing keys: open_pr_work_id")
            if unexpected:
                details.append(f"unexpected keys: {', '.join(unexpected)}")
            raise ValueError("invalid delivery.verify_pr payload (" + "; ".join(details) + ")")
        value = payload["open_pr_work_id"]
        if not isinstance(value, str) or not value:
            raise ValueError("open_pr_work_id must be non-empty text")
        return cls(value)


@dataclass(frozen=True)
class MergePullRequest:
    verify_pr_work_id: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> MergePullRequest:
        if set(payload) != {"verify_pr_work_id"}:
            unexpected = sorted(set(payload) - {"verify_pr_work_id"})
            missing = sorted({"verify_pr_work_id"} - set(payload))
            details = []
            if missing:
                details.append("missing keys: verify_pr_work_id")
            if unexpected:
                details.append(f"unexpected keys: {', '.join(unexpected)}")
            raise ValueError("invalid delivery.merge_pr payload (" + "; ".join(details) + ")")
        value = payload["verify_pr_work_id"]
        if not isinstance(value, str) or not value:
            raise ValueError("verify_pr_work_id must be non-empty text")
        return cls(value)


@dataclass(frozen=True)
class VerifySandboxDeployRequest:
    merge_pr_work_id: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> VerifySandboxDeployRequest:
        if set(payload) != {"merge_pr_work_id"}:
            unexpected = sorted(set(payload) - {"merge_pr_work_id"})
            missing = sorted({"merge_pr_work_id"} - set(payload))
            details = []
            if missing:
                details.append("missing keys: merge_pr_work_id")
            if unexpected:
                details.append(f"unexpected keys: {', '.join(unexpected)}")
            raise ValueError(
                "invalid delivery.verify_sandbox_deploy payload ("
                + "; ".join(details)
                + ")"
            )
        value = payload["merge_pr_work_id"]
        if not isinstance(value, str) or not value:
            raise ValueError("merge_pr_work_id must be non-empty text")
        return cls(value)


ProjectRunner = Callable[[Sequence[str], Path, float], CommandResult]


def run_project_command(argv: Sequence[str], cwd: Path, timeout_seconds: float) -> CommandResult:
    environment = None
    if argv and (argv[0] == "gh" or list(argv[:2]) in (["git", "fetch"], ["git", "push"])):
        environment = _github_environment(cwd)
    completed = subprocess.run(
        list(argv),
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _github_environment(cwd: Path) -> dict[str, str]:
    """Resolve GitHub identity for the target repo, never the caller shell."""
    mapping = Path.home() / ".config/zsh/gh-owner-map.sh"
    if not mapping.is_file():
        raise ValueError("target-owner GitHub mapping is unavailable")
    clean_environment = os.environ.copy()
    clean_environment.pop("GH_TOKEN", None)
    clean_environment.pop("GITHUB_TOKEN", None)
    resolved = subprocess.run(
        [
            "/bin/sh",
            "-c",
            '. "$1"; gh_token_for_dir "$2"',
            "atlas-next-github-auth",
            str(mapping),
            str(cwd),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=clean_environment,
    )
    token = resolved.stdout.strip()
    if resolved.returncode != 0 or not token:
        detail = resolved.stderr.strip()[:300] or "target owner has no mapped GitHub token"
        raise ValueError(f"target-owner GitHub auth failed: {detail}")
    clean_environment["GH_TOKEN"] = token
    clean_environment["GITHUB_TOKEN"] = token
    return clean_environment


class CommitSource:
    """Commit only files proven by successful source-producing ledger items."""

    def __init__(
        self,
        store: Store,
        *,
        runner: ProjectRunner = run_project_command,
        timeout_seconds: float = 120,
    ) -> None:
        self.store = store
        self.runner = runner
        self.timeout_seconds = timeout_seconds

    def __call__(self, item: WorkItem) -> Outcome:
        try:
            request = CommitSourceRequest.from_payload(item.payload)
            sources = [self.store.get(source_id) for source_id in request.source_work_ids]
            if any(source is None for source in sources):
                raise ValueError("one or more source work items do not exist")
            if any(
                source.state is not WorkState.SUCCEEDED
                or source.action
                not in {
                    SOURCE_RETRIEVE_ACTION,
                    AUTHOR_SOURCE_ACTION,
                    CREATE_FLOW_SOURCE_ACTION,
                    CREATE_LWC_SOURCE_ACTION,
                    CREATE_REPORT_SOURCE_ACTION,
                    CREATE_INTEGRATION_SOURCE_ACTION,
                }
                for source in sources
                if source is not None
            ):
                raise ValueError("every source work item must be a successful source producer")
            roots = {str(source.result.get("git_root", "")) for source in sources if source}
            branches = {str(source.result.get("branch", "")) for source in sources if source}
            if len(roots) != 1 or len(branches) != 1:
                raise ValueError("all source work items must target the same git root and branch")
            git_root = Path(roots.pop()).resolve()
            branch = branches.pop()
            if not git_root.is_dir() or not branch or branch in {"main", "master"}:
                raise ValueError("source work items do not identify a valid non-main worktree")
            current_branch = self._git(git_root, ["git", "branch", "--show-current"]).stdout.strip()
            if current_branch != branch:
                raise ValueError("worktree branch changed after source production")
            expected = {}
            for source in sources:
                assert source is not None
                for file in source.result.get("files", []):
                    path = file.get("path")
                    digest = file.get("sha256")
                    if not isinstance(path, str) or not isinstance(digest, str):
                        raise ValueError("source evidence contains an invalid file receipt")
                    if path in expected and expected[path] != digest:
                        raise ValueError("source evidence disagrees about a file hash")
                    expected[path] = digest
            if not expected:
                raise ValueError("source work items prove zero files")
            for path, digest in expected.items():
                absolute = (git_root / path).resolve()
                if not absolute.is_relative_to(git_root) or not absolute.is_file():
                    raise ValueError(f"proven source file is missing: {path}")
                if hashlib.sha256(absolute.read_bytes()).hexdigest() != digest:
                    raise ValueError(f"proven source file changed after production: {path}")
            dirty = _porcelain_paths(
                self._git(
                    git_root,
                    ["git", "status", "--porcelain=v1", "--untracked-files=all"],
                ).stdout
            )
            if dirty != set(expected):
                raise ValueError("worktree dirt does not exactly match the proven source files")
            self._git(git_root, ["git", "add", "--", *sorted(expected)])
            staged = self._git(
                git_root, ["git", "diff", "--cached", "--name-only", "-z"]
            ).stdout.split("\0")
            if {path for path in staged if path} != set(expected):
                raise ValueError("staged files do not exactly match the proven source files")
            self._git(git_root, ["git", "commit", "-m", request.message])
            sha = self._git(git_root, ["git", "rev-parse", "HEAD"]).stdout.strip()
            if not _SHA_RE.fullmatch(sha):
                raise ValueError("git commit produced no valid SHA")
            if self._git(git_root, ["git", "status", "--porcelain=v1"]).stdout:
                raise ValueError("worktree is dirty after commit")
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            return Outcome.failed(f"source commit refused: {exc}")

        result = {
            "git_root": str(git_root),
            "branch": branch,
            "commit_sha": sha,
            "message": request.message,
            "files": sorted(expected),
            "file_count": len(expected),
            "source_work_ids": list(request.source_work_ids),
        }
        evidence = [
            {
                "kind": COMMIT_SOURCE_ACTION,
                "git_root": str(git_root),
                "branch": branch,
                "commit_sha": sha,
                "file_count": len(expected),
                "source_work_ids": list(request.source_work_ids),
                "clean_after_commit": True,
                "pushed": False,
            }
        ]
        return Outcome.success(result, evidence)

    def _git(self, cwd: Path, argv: Sequence[str]) -> CommandResult:
        completed = self.runner(argv, cwd, self.timeout_seconds)
        if completed.returncode != 0:
            raise ValueError(f"git command failed: {_failure_detail(completed)}")
        return completed


class OpenPr:
    """Push evidence-linked commits and open one PR against current main."""

    def __init__(
        self,
        store: Store,
        *,
        runner: ProjectRunner = run_project_command,
        timeout_seconds: float = 180,
    ) -> None:
        self.store = store
        self.runner = runner
        self.timeout_seconds = timeout_seconds

    def __call__(self, item: WorkItem) -> Outcome:
        try:
            request = OpenPullRequest.from_payload(item.payload)
            commits = [self.store.get(work_id) for work_id in request.commit_work_ids]
            if any(commit is None for commit in commits):
                raise ValueError("one or more commit work items do not exist")
            if any(
                commit.state is not WorkState.SUCCEEDED or commit.action != COMMIT_SOURCE_ACTION
                for commit in commits
                if commit is not None
            ):
                raise ValueError("every commit work item must be a successful source commit")
            roots = {str(commit.result.get("git_root", "")) for commit in commits if commit}
            branches = {str(commit.result.get("branch", "")) for commit in commits if commit}
            if len(roots) != 1 or len(branches) != 1:
                raise ValueError("all commits must belong to the same git root and branch")
            git_root = Path(roots.pop()).resolve()
            branch = branches.pop()
            if not git_root.is_dir() or branch in {"main", "master"}:
                raise ValueError("commit evidence does not identify a non-main worktree")
            if self._run(git_root, ["git", "branch", "--show-current"]).stdout.strip() != branch:
                raise ValueError("worktree branch changed after commit")
            if self._run(git_root, ["git", "status", "--porcelain=v1"]).stdout:
                raise ValueError("worktree must be clean before push")
            head = self._run(git_root, ["git", "rev-parse", "HEAD"]).stdout.strip()
            if not _SHA_RE.fullmatch(head):
                raise ValueError("worktree has no valid HEAD SHA")
            for commit in commits:
                assert commit is not None
                sha = str(commit.result.get("commit_sha", ""))
                if not _SHA_RE.fullmatch(sha):
                    raise ValueError("commit evidence contains an invalid SHA")
                ancestor = self.runner(
                    ["git", "merge-base", "--is-ancestor", sha, head],
                    git_root,
                    self.timeout_seconds,
                )
                if ancestor.returncode != 0:
                    raise ValueError("an evidence-linked commit is not an ancestor of HEAD")
            self._run(git_root, ["git", "fetch", "origin", "main"])
            current_main = self._run(git_root, ["git", "rev-parse", "origin/main"]).stdout.strip()
            contains_main = self.runner(
                ["git", "merge-base", "--is-ancestor", current_main, head],
                git_root,
                self.timeout_seconds,
            )
            if contains_main.returncode != 0:
                raise ValueError("branch is behind current origin/main")
            ahead = self._run(
                git_root, ["git", "rev-list", "--count", "origin/main..HEAD"]
            ).stdout.strip()
            if not ahead.isdigit() or int(ahead) < 1:
                raise ValueError("branch has zero commits ahead of current main")
            owner = self._run(
                git_root, ["gh", "repo", "view", "--json", "nameWithOwner"]
            ).stdout
            if json.loads(owner).get("nameWithOwner") != "ClearspeedRevOps/sfdc":
                raise ValueError("git root is not the governed ClearspeedRevOps/sfdc repository")
            self._run(git_root, ["git", "push", "-u", "origin", f"HEAD:refs/heads/{branch}"])
            existing_raw = self._run(
                git_root,
                [
                    "gh", "pr", "list", "--head", branch, "--base", "main", "--state", "open",
                    "--json", "number,url,headRefOid,title",
                ],
            ).stdout
            existing = json.loads(existing_raw)
            if not isinstance(existing, list) or len(existing) > 1:
                raise ValueError("GitHub returned an invalid open PR population")
            if existing:
                pr = existing[0]
            else:
                url = self._run(
                    git_root,
                    [
                        "gh", "pr", "create", "--base", "main", "--head", branch,
                        "--title", request.title, "--body", request.body,
                    ],
                ).stdout.strip()
                match = re.fullmatch(r"https://github\.com/ClearspeedRevOps/sfdc/pull/(\d+)", url)
                if match is None:
                    raise ValueError("GitHub did not return the expected PR URL")
                pr = {"number": int(match.group(1)), "url": url, "headRefOid": head}
            if pr.get("headRefOid") != head:
                raise ValueError("open PR head does not match pushed HEAD")
            number = pr.get("number")
            url = pr.get("url")
            if isinstance(number, bool) or not isinstance(number, int) or not isinstance(url, str):
                raise ValueError("GitHub PR receipt is incomplete")
        except (ValueError, OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            return Outcome.failed(f"open PR refused: {exc}")

        result = {
            "git_root": str(git_root),
            "branch": branch,
            "head_sha": head,
            "base_sha": current_main,
            "pr_number": number,
            "pr_url": url,
            "commit_work_ids": list(request.commit_work_ids),
        }
        evidence = [
            {
                "kind": OPEN_PR_ACTION,
                "repository": "ClearspeedRevOps/sfdc",
                "branch": branch,
                "head_sha": head,
                "base_sha": current_main,
                "pr_number": number,
                "pr_url": url,
                "pushed": True,
                "contains_current_main": True,
                "merged": False,
            }
        ]
        return Outcome.success(result, evidence)

    def _run(self, cwd: Path, argv: Sequence[str]) -> CommandResult:
        completed = self.runner(argv, cwd, self.timeout_seconds)
        if completed.returncode != 0:
            raise ValueError(f"delivery command failed: {_failure_detail(completed)}")
        return completed


class VerifyPr:
    """Wait for all PR checks and prove current-main merge readiness."""

    def __init__(
        self,
        store: Store,
        *,
        runner: ProjectRunner = run_project_command,
        timeout_seconds: float = 1800,
    ) -> None:
        self.store = store
        self.runner = runner
        self.timeout_seconds = timeout_seconds

    def __call__(self, item: WorkItem) -> Outcome:
        try:
            request = VerifyPullRequest.from_payload(item.payload)
            opened = self.store.get(request.open_pr_work_id)
            if opened is None or opened.state is not WorkState.SUCCEEDED:
                raise ValueError("open PR work item is missing or unsuccessful")
            if opened.action != OPEN_PR_ACTION:
                raise ValueError("referenced work item is not an open PR receipt")
            git_root = Path(str(opened.result.get("git_root", ""))).resolve()
            number = opened.result.get("pr_number")
            expected_head = str(opened.result.get("head_sha", ""))
            if (
                not git_root.is_dir()
                or isinstance(number, bool)
                or not isinstance(number, int)
                or not _SHA_RE.fullmatch(expected_head)
            ):
                raise ValueError("open PR receipt is incomplete")
            watched = self.runner(
                ["gh", "pr", "checks", str(number), "--watch", "--interval", "10"],
                git_root,
                self.timeout_seconds,
            )
            if watched.returncode != 0:
                raise ValueError(f"PR checks failed: {_failure_detail(watched)}")
            raw = self._run(
                git_root,
                [
                    "gh", "pr", "view", str(number),
                    "--json", "number,url,state,isDraft,mergeStateStatus,headRefOid,baseRefOid,statusCheckRollup",
                ],
            ).stdout
            pr = json.loads(raw)
            if pr.get("state") != "OPEN" or pr.get("isDraft") is not False:
                raise ValueError("PR is not an open non-draft pull request")
            if pr.get("headRefOid") != expected_head:
                raise ValueError("PR head changed after the evidence-linked push")
            checks = _parse_checks(pr.get("statusCheckRollup"))
            required = {"Validate (sandbox)", "LWC unit tests", "PM Tracker + revops-dash verify"}
            missing = sorted(required - set(checks))
            if missing:
                raise ValueError(f"required checks were not created: {', '.join(missing)}")
            bad = sorted(name for name, conclusion in checks.items() if conclusion not in {"SUCCESS", "NEUTRAL", "SKIPPED"})
            if bad:
                raise ValueError(f"checks are not green: {', '.join(bad)}")
            if checks["Validate (sandbox)"] != "SUCCESS":
                raise ValueError("Validate (sandbox) did not succeed")
            self._run(git_root, ["git", "fetch", "origin", "main"])
            current_main = self._run(git_root, ["git", "rev-parse", "origin/main"]).stdout.strip()
            if pr.get("baseRefOid") != current_main:
                raise ValueError("PR base receipt is not current origin/main")
            contains = self.runner(
                ["git", "merge-base", "--is-ancestor", current_main, expected_head],
                git_root,
                self.timeout_seconds,
            )
            if contains.returncode != 0 or pr.get("mergeStateStatus") != "CLEAN":
                raise ValueError("PR is not cleanly mergeable on current main")
        except (ValueError, OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            return Outcome.failed(f"verify PR refused: {exc}")

        result = {
            "git_root": str(git_root),
            "pr_number": number,
            "pr_url": pr["url"],
            "head_sha": expected_head,
            "base_sha": current_main,
            "checks": checks,
            "check_count": len(checks),
            "merge_state": "CLEAN",
            "open_pr_work_id": request.open_pr_work_id,
        }
        evidence = [
            {
                "kind": VERIFY_PR_ACTION,
                "repository": "ClearspeedRevOps/sfdc",
                "pr_number": number,
                "pr_url": pr["url"],
                "head_sha": expected_head,
                "base_sha": current_main,
                "check_count": len(checks),
                "sandbox_validation": "SUCCESS",
                "contains_current_main": True,
                "merge_state": "CLEAN",
                "merged": False,
            }
        ]
        return Outcome.success(result, evidence)

    def _run(self, cwd: Path, argv: Sequence[str]) -> CommandResult:
        completed = self.runner(argv, cwd, self.timeout_seconds)
        if completed.returncode != 0:
            raise ValueError(f"PR verification command failed: {_failure_detail(completed)}")
        return completed


class MergePr:
    """Re-verify a governed PR at merge time and record the immutable merge receipt."""

    def __init__(
        self,
        store: Store,
        *,
        runner: ProjectRunner = run_project_command,
        timeout_seconds: float = 300,
    ) -> None:
        self.store = store
        self.runner = runner
        self.timeout_seconds = timeout_seconds

    def __call__(self, item: WorkItem) -> Outcome:
        try:
            request = MergePullRequest.from_payload(item.payload)
            verified = self.store.get(request.verify_pr_work_id)
            if verified is None or verified.state is not WorkState.SUCCEEDED:
                raise ValueError("verify PR work item is missing or unsuccessful")
            if verified.action != VERIFY_PR_ACTION:
                raise ValueError("referenced work item is not a verify PR receipt")
            git_root = Path(str(verified.result.get("git_root", ""))).resolve()
            number = verified.result.get("pr_number")
            expected_head = str(verified.result.get("head_sha", ""))
            if (
                not git_root.is_dir()
                or isinstance(number, bool)
                or not isinstance(number, int)
                or not _SHA_RE.fullmatch(expected_head)
            ):
                raise ValueError("verify PR receipt is incomplete")

            raw = self._run(
                git_root,
                [
                    "gh", "pr", "view", str(number), "--json",
                    "number,url,state,isDraft,mergeStateStatus,headRefOid,baseRefOid,statusCheckRollup",
                ],
            ).stdout
            pr = json.loads(raw)
            if pr.get("state") != "OPEN" or pr.get("isDraft") is not False:
                raise ValueError("PR is not an open non-draft pull request")
            if pr.get("headRefOid") != expected_head:
                raise ValueError("PR head changed after verification")
            checks = _parse_checks(pr.get("statusCheckRollup"))
            required = {"Validate (sandbox)", "LWC unit tests", "PM Tracker + revops-dash verify"}
            missing = sorted(required - set(checks))
            if missing:
                raise ValueError(f"required checks were not created: {', '.join(missing)}")
            bad = sorted(
                name
                for name, conclusion in checks.items()
                if conclusion not in {"SUCCESS", "NEUTRAL", "SKIPPED"}
            )
            if bad or checks["Validate (sandbox)"] != "SUCCESS":
                raise ValueError("PR checks are no longer green")
            self._run(git_root, ["git", "fetch", "origin", "main"])
            current_main = self._run(
                git_root, ["git", "rev-parse", "origin/main"]
            ).stdout.strip()
            if pr.get("baseRefOid") != current_main:
                raise ValueError("PR base receipt is not current origin/main")
            contains = self.runner(
                ["git", "merge-base", "--is-ancestor", current_main, expected_head],
                git_root,
                self.timeout_seconds,
            )
            if contains.returncode != 0 or pr.get("mergeStateStatus") != "CLEAN":
                raise ValueError("PR is not cleanly mergeable on current main")

            self._run(
                git_root,
                [
                    "gh", "pr", "merge", str(number), "--squash",
                    "--match-head-commit", expected_head,
                ],
            )
            merged_raw = self._run(
                git_root,
                [
                    "gh", "pr", "view", str(number), "--json",
                    "number,url,state,mergedAt,mergeCommit,headRefOid",
                ],
            ).stdout
            merged = json.loads(merged_raw)
            merge_commit = merged.get("mergeCommit")
            merge_sha = merge_commit.get("oid") if isinstance(merge_commit, dict) else None
            merged_at = merged.get("mergedAt")
            if (
                merged.get("state") != "MERGED"
                or merged.get("headRefOid") != expected_head
                or not isinstance(merge_sha, str)
                or not _SHA_RE.fullmatch(merge_sha)
                or not isinstance(merged_at, str)
                or not merged_at
            ):
                raise ValueError("GitHub merge receipt is incomplete")
            self._run(git_root, ["git", "fetch", "origin", "main"])
            landed = self.runner(
                ["git", "merge-base", "--is-ancestor", merge_sha, "origin/main"],
                git_root,
                self.timeout_seconds,
            )
            if landed.returncode != 0:
                raise ValueError("merge commit is not present on current origin/main")
        except (ValueError, OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            return Outcome.failed(f"merge PR refused: {exc}")

        result = {
            "git_root": str(git_root),
            "pr_number": number,
            "pr_url": merged["url"],
            "head_sha": expected_head,
            "base_sha": current_main,
            "merge_sha": merge_sha,
            "merged_at": merged_at,
            "verify_pr_work_id": request.verify_pr_work_id,
        }
        evidence = [
            {
                "kind": MERGE_PR_ACTION,
                "repository": "ClearspeedRevOps/sfdc",
                "pr_number": number,
                "pr_url": merged["url"],
                "head_sha": expected_head,
                "base_sha": current_main,
                "merge_sha": merge_sha,
                "merged_at": merged_at,
                "sandbox_validation": "SUCCESS",
                "contains_current_main": True,
                "merged": True,
            }
        ]
        return Outcome.success(result, evidence)

    def _run(self, cwd: Path, argv: Sequence[str]) -> CommandResult:
        completed = self.runner(argv, cwd, self.timeout_seconds)
        if completed.returncode != 0:
            raise ValueError(f"PR merge command failed: {_failure_detail(completed)}")
        return completed


class VerifySandboxDeploy:
    """Prove the exact merge SHA completed the governed Partial deployment job."""

    def __init__(
        self,
        store: Store,
        *,
        runner: ProjectRunner = run_project_command,
        timeout_seconds: float = 1800,
    ) -> None:
        self.store = store
        self.runner = runner
        self.timeout_seconds = timeout_seconds

    def __call__(self, item: WorkItem) -> Outcome:
        try:
            request = VerifySandboxDeployRequest.from_payload(item.payload)
            merged = self.store.get(request.merge_pr_work_id)
            if merged is None or merged.state is not WorkState.SUCCEEDED:
                raise ValueError("merge PR work item is missing or unsuccessful")
            if merged.action != MERGE_PR_ACTION:
                raise ValueError("referenced work item is not a merge PR receipt")
            git_root = Path(str(merged.result.get("git_root", ""))).resolve()
            merge_sha = str(merged.result.get("merge_sha", ""))
            if not git_root.is_dir() or not _SHA_RE.fullmatch(merge_sha):
                raise ValueError("merge PR receipt is incomplete")
            runs_raw = self._run(
                git_root,
                [
                    "gh", "run", "list", "--workflow", "Salesforce CI", "--branch", "main",
                    "--commit", merge_sha, "--limit", "10", "--json",
                    "databaseId,headSha,status,conclusion,workflowName,url,event",
                ],
            ).stdout
            runs = json.loads(runs_raw)
            if not isinstance(runs, list) or len(runs) != 1:
                raise ValueError("expected exactly one Salesforce CI run for the merge SHA")
            run = runs[0]
            run_id = run.get("databaseId")
            if (
                isinstance(run_id, bool)
                or not isinstance(run_id, int)
                or run.get("headSha") != merge_sha
                or run.get("workflowName") != "Salesforce CI"
                or run.get("event") != "push"
            ):
                raise ValueError("Salesforce CI run receipt does not match the merge")
            watched = self.runner(
                ["gh", "run", "watch", str(run_id), "--exit-status"],
                git_root,
                self.timeout_seconds,
            )
            if watched.returncode != 0:
                raise ValueError(f"Salesforce CI failed: {_failure_detail(watched)}")
            detail_raw = self._run(
                git_root,
                [
                    "gh", "run", "view", str(run_id), "--json",
                    "conclusion,headBranch,headSha,jobs,status,url,workflowName",
                ],
            ).stdout
            detail = json.loads(detail_raw)
            if (
                detail.get("status") != "completed"
                or detail.get("conclusion") != "success"
                or detail.get("headBranch") != "main"
                or detail.get("headSha") != merge_sha
                or detail.get("workflowName") != "Salesforce CI"
            ):
                raise ValueError("Salesforce CI did not complete successfully for merged main")
            jobs = detail.get("jobs")
            if not isinstance(jobs, list):
                raise ValueError("Salesforce CI returned no job receipts")
            deploys = [job for job in jobs if job.get("name") == "Deploy (sandbox)"]
            if len(deploys) != 1 or deploys[0].get("conclusion") != "success":
                raise ValueError("Deploy (sandbox) did not succeed exactly once")
            deploy = deploys[0]
            job_id = deploy.get("databaseId")
            if isinstance(job_id, bool) or not isinstance(job_id, int):
                raise ValueError("Deploy (sandbox) job receipt is incomplete")
        except (ValueError, OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            return Outcome.failed(f"verify sandbox deploy refused: {exc}")

        result = {
            "git_root": str(git_root),
            "merge_sha": merge_sha,
            "run_id": run_id,
            "run_url": detail["url"],
            "deploy_job_id": job_id,
            "merge_pr_work_id": request.merge_pr_work_id,
        }
        evidence = [
            {
                "kind": VERIFY_SANDBOX_DEPLOY_ACTION,
                "repository": "ClearspeedRevOps/sfdc",
                "merge_sha": merge_sha,
                "workflow": "Salesforce CI",
                "run_id": run_id,
                "run_url": detail["url"],
                "run_conclusion": "success",
                "deploy_job": "Deploy (sandbox)",
                "deploy_job_id": job_id,
                "deploy_conclusion": "success",
                "environment": "partial",
                "production_write": False,
            }
        ]
        return Outcome.success(result, evidence)

    def _run(self, cwd: Path, argv: Sequence[str]) -> CommandResult:
        completed = self.runner(argv, cwd, self.timeout_seconds)
        if completed.returncode != 0:
            raise ValueError(f"sandbox deploy command failed: {_failure_detail(completed)}")
        return completed


def _porcelain_paths(status: str) -> set[str]:
    paths = set()
    for line in status.splitlines():
        if not line:
            continue
        if len(line) < 4 or " -> " in line:
            raise ValueError("git status contains an unsupported path shape")
        paths.add(line[3:])
    return paths


def _parse_checks(value: Any) -> dict[str, str]:
    if not isinstance(value, list) or not value:
        raise ValueError("PR has no status checks")
    checks = {}
    for row in value:
        if not isinstance(row, dict):
            raise ValueError("PR status check is not an object")
        name = row.get("name") or row.get("context")
        status = row.get("status")
        conclusion = row.get("conclusion")
        if not isinstance(name, str) or not name or name in checks:
            raise ValueError("PR status checks contain an invalid or duplicate name")
        if status != "COMPLETED" or not isinstance(conclusion, str) or not conclusion:
            raise ValueError(f"PR status check is incomplete: {name}")
        checks[name] = conclusion
    return checks

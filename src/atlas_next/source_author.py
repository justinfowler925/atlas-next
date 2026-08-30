from __future__ import annotations

import hashlib
import re
import subprocess
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .engine import Outcome
from .models import WorkItem, WorkState
from .salesforce import CommandResult, _failure_detail
from .salesforce_metadata import SOURCE_RETRIEVE_ACTION
from .store import Store


AUTHOR_SOURCE_ACTION = "salesforce.author_source"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_EDITABLE_SUFFIXES = (
    ".cls",
    ".trigger",
    ".js",
    ".html",
    ".css",
    ".svg",
    ".xml",
)


@dataclass(frozen=True)
class AuthorSourceRequest:
    retrieve_work_id: str
    path: str
    expected_sha256: str
    content: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> AuthorSourceRequest:
        expected = {"retrieve_work_id", "path", "expected_sha256", "content"}
        if set(payload) != expected:
            unexpected = sorted(set(payload) - expected)
            missing = sorted(expected - set(payload))
            details = []
            if missing:
                details.append(f"missing keys: {', '.join(missing)}")
            if unexpected:
                details.append(f"unexpected keys: {', '.join(unexpected)}")
            raise ValueError(
                "invalid salesforce.author_source payload (" + "; ".join(details) + ")"
            )
        retrieve_work_id = payload["retrieve_work_id"]
        path = payload["path"]
        expected_sha256 = payload["expected_sha256"]
        content = payload["content"]
        if not isinstance(retrieve_work_id, str) or not retrieve_work_id:
            raise ValueError("retrieve_work_id must be non-empty text")
        if (
            not isinstance(path, str)
            or not path
            or path.startswith("/")
            or ".." in Path(path).parts
            or not path.endswith(_EDITABLE_SUFFIXES)
        ):
            raise ValueError("path must be one editable relative Salesforce source file")
        if not isinstance(expected_sha256, str) or not _SHA256_RE.fullmatch(expected_sha256):
            raise ValueError("expected_sha256 must be one lowercase SHA-256 digest")
        if (
            not isinstance(content, str)
            or not content
            or "\x00" in content
            or len(content.encode("utf-8")) > 1_000_000
        ):
            raise ValueError("content must be non-empty UTF-8 text of at most 1 MB")
        return cls(retrieve_work_id, path, expected_sha256, content)


ProjectRunner = Callable[[Sequence[str], Path, float], CommandResult]


def run_project_command(argv: Sequence[str], cwd: Path, timeout_seconds: float) -> CommandResult:
    completed = subprocess.run(
        list(argv),
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


class AuthorSource:
    """Replace one retrieved Salesforce source file with hash-locked text."""

    def __init__(
        self,
        store: Store,
        *,
        runner: ProjectRunner = run_project_command,
        timeout_seconds: float = 30,
    ) -> None:
        self.store = store
        self.runner = runner
        self.timeout_seconds = timeout_seconds

    def __call__(self, item: WorkItem) -> Outcome:
        try:
            request = AuthorSourceRequest.from_payload(item.payload)
            retrieved = self.store.get(request.retrieve_work_id)
            if retrieved is None or retrieved.state is not WorkState.SUCCEEDED:
                raise ValueError("retrieve work item is missing or unsuccessful")
            if retrieved.action != SOURCE_RETRIEVE_ACTION:
                raise ValueError("referenced work item is not a source retrieve receipt")
            git_root = Path(str(retrieved.result.get("git_root", ""))).resolve()
            branch = str(retrieved.result.get("branch", ""))
            metadata_type = str(retrieved.result.get("type", ""))
            component_name = str(retrieved.result.get("name", ""))
            if not git_root.is_dir() or not branch or branch in {"main", "master"}:
                raise ValueError("retrieve receipt does not identify a non-main worktree")
            receipts = _file_receipts(retrieved.result.get("files"))
            if request.path not in receipts or receipts[request.path] != request.expected_sha256:
                raise ValueError("requested path and hash do not match the retrieve receipt")
            if not request.path.startswith("salesforce/force-app/main/default/"):
                raise ValueError("retrieved path is outside governed Salesforce source")
            absolute = (git_root / request.path).resolve()
            if (
                not absolute.is_relative_to(git_root)
                or not absolute.is_file()
                or absolute.is_symlink()
            ):
                raise ValueError("retrieved source is not one regular file")
            if self._git(git_root, ["git", "branch", "--show-current"]).stdout.strip() != branch:
                raise ValueError("worktree branch changed after source retrieval")
            for path, digest in receipts.items():
                candidate = (git_root / path).resolve()
                if (
                    not candidate.is_relative_to(git_root)
                    or not candidate.is_file()
                    or candidate.is_symlink()
                    or _sha256(candidate) != digest
                ):
                    raise ValueError(f"retrieved source changed before authoring: {path}")
            dirty_before = _porcelain_paths(
                self._git(git_root, ["git", "status", "--porcelain=v1"]).stdout
            )
            if not dirty_before <= set(receipts):
                raise ValueError("worktree contains dirt outside the retrieved component")
            _validate_content(request.path, request.content, metadata_type, component_name)
            absolute.write_text(request.content, encoding="utf-8")
            authored_sha = _sha256(absolute)
            if authored_sha == request.expected_sha256:
                raise ValueError("authored content is identical to the retrieved source")
            dirty_after = _porcelain_paths(
                self._git(git_root, ["git", "status", "--porcelain=v1"]).stdout
            )
            if request.path not in dirty_after or not dirty_after <= set(receipts):
                raise ValueError("authoring did not produce only the retrieved component dirt")
            files = [
                {
                    "path": path,
                    "sha256": _sha256(git_root / path),
                    "bytes": (git_root / path).stat().st_size,
                }
                for path in sorted(dirty_after)
            ]
        except (ValueError, OSError, subprocess.SubprocessError, UnicodeError, ET.ParseError) as exc:
            return Outcome.failed(f"Salesforce source authoring refused: {exc}")

        result = {
            "environment": "partial",
            "type": metadata_type,
            "name": component_name,
            "git_root": str(git_root),
            "branch": branch,
            "authored_path": request.path,
            "baseline_sha256": request.expected_sha256,
            "authored_sha256": authored_sha,
            "files": files,
            "file_count": len(files),
            "retrieve_work_id": request.retrieve_work_id,
        }
        evidence = [
            {
                "kind": AUTHOR_SOURCE_ACTION,
                "environment": "partial",
                "type": metadata_type,
                "name": component_name,
                "branch": branch,
                "authored_path": request.path,
                "baseline_sha256": request.expected_sha256,
                "authored_sha256": authored_sha,
                "file_count": len(files),
                "production_execution": False,
                "metadata_deployed": False,
            }
        ]
        return Outcome.success(result, evidence)

    def _git(self, cwd: Path, argv: Sequence[str]) -> CommandResult:
        completed = self.runner(argv, cwd, self.timeout_seconds)
        if completed.returncode != 0:
            raise ValueError(f"source authoring git check failed: {_failure_detail(completed)}")
        return completed


def _file_receipts(value: Any) -> dict[str, str]:
    if not isinstance(value, list) or not 1 <= len(value) <= 20:
        raise ValueError("retrieve receipt has no bounded file population")
    receipts = {}
    for row in value:
        if not isinstance(row, dict):
            raise ValueError("retrieve receipt contains an invalid file")
        path = row.get("path")
        digest = row.get("sha256")
        if (
            not isinstance(path, str)
            or not isinstance(digest, str)
            or not _SHA256_RE.fullmatch(digest)
            or path in receipts
        ):
            raise ValueError("retrieve receipt contains an invalid file hash")
        receipts[path] = digest
    return receipts


def _validate_content(path: str, content: str, metadata_type: str, component_name: str) -> None:
    if path.endswith(".xml"):
        if "<!DOCTYPE" in content.upper() or "<!ENTITY" in content.upper():
            raise ValueError("XML document types and entities are not allowed")
        ET.fromstring(content)
    if metadata_type == "ApexClass" and path.endswith(".cls"):
        if re.search(rf"\bclass\s+{re.escape(component_name)}\b", content) is None:
            raise ValueError("Apex class body does not declare the retrieved component")
    if metadata_type == "ApexTrigger" and path.endswith(".trigger"):
        if re.search(rf"\btrigger\s+{re.escape(component_name)}\b", content, re.I) is None:
            raise ValueError("Apex trigger body does not declare the retrieved component")


def _porcelain_paths(status: str) -> set[str]:
    paths = set()
    for line in status.splitlines():
        if not line:
            continue
        if len(line) < 4 or line[:2] not in {"??", " M", "M ", "MM", "A "} or " -> " in line:
            raise ValueError("git status contains unsupported source dirt")
        paths.add(line[3:])
    return paths


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

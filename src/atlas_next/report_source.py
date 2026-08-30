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
from .models import WorkItem
from .salesforce import CommandResult, _failure_detail


CREATE_REPORT_SOURCE_ACTION = "salesforce.create_report_source"
_REPORT_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{1,79}$")
_FIELD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.]{0,159}$")
_METADATA_NAMESPACE = "http://soap.sforce.com/2006/04/metadata"


@dataclass(frozen=True)
class CreateReportSourceRequest:
    name: str
    content: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> CreateReportSourceRequest:
        if set(payload) != {"name", "content"}:
            raise ValueError("payload must contain only name and content")
        name = payload["name"]
        content = payload["content"]
        if not isinstance(name, str) or not _REPORT_NAME_RE.fullmatch(name):
            raise ValueError("name must be one exact report DeveloperName")
        if (
            not isinstance(content, str)
            or not content
            or "\x00" in content
            or len(content.encode("utf-8")) > 1_000_000
        ):
            raise ValueError("content must be non-empty UTF-8 report XML of at most 1 MB")
        _validate_report_xml(content)
        return cls(name, content)


ProjectRunner = Callable[[Sequence[str], Path, float], CommandResult]


def run_project_command(argv: Sequence[str], cwd: Path, timeout_seconds: float) -> CommandResult:
    completed = subprocess.run(
        list(argv), cwd=cwd, capture_output=True, text=True, timeout=timeout_seconds, check=False
    )
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


class CreateReportSource:
    """Create one bounded public report definition at a deterministic source path."""

    def __init__(
        self,
        *,
        project_dir: Path,
        runner: ProjectRunner = run_project_command,
        timeout_seconds: float = 30,
    ) -> None:
        self.project_dir = project_dir.resolve()
        self.runner = runner
        self.timeout_seconds = timeout_seconds

    def __call__(self, item: WorkItem) -> Outcome:
        created: tuple[Path, bytes] | None = None
        try:
            request = CreateReportSourceRequest.from_payload(item.payload)
            if not (self.project_dir / "sfdx-project.json").is_file():
                raise ValueError("configured project_dir is not a Salesforce project")
            git_root = Path(
                self._git(["git", "rev-parse", "--show-toplevel"]).stdout.strip()
            ).resolve()
            branch = self._git(["git", "branch", "--show-current"]).stdout.strip()
            if not branch or branch in {"main", "master"}:
                raise ValueError("report creation requires a named non-main branch")
            git_dir = Path(
                self._git(
                    ["git", "rev-parse", "--path-format=absolute", "--git-dir"]
                ).stdout.strip()
            ).resolve()
            common_dir = Path(
                self._git(
                    ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"]
                ).stdout.strip()
            ).resolve()
            if git_dir == common_dir:
                raise ValueError("report creation requires an isolated linked worktree")
            if self._git(["git", "status", "--porcelain=v1"]).stdout:
                raise ValueError("report creation requires a clean worktree")
            project_prefix = self.project_dir.relative_to(git_root).as_posix()
            relative = (
                f"{project_prefix}/force-app/main/default/reports/"
                f"unfiled$public/{request.name}.report-meta.xml"
            )
            absolute = (git_root / relative).resolve()
            if not absolute.is_relative_to(git_root) or absolute.exists():
                raise ValueError("report source path already exists or escaped the worktree")
            encoded = request.content.encode("utf-8")
            absolute.parent.mkdir(parents=True, exist_ok=True)
            absolute.write_bytes(encoded)
            created = (absolute, encoded)
            status = self._git(
                ["git", "status", "--porcelain=v1", "--untracked-files=all"]
            ).stdout
            if status.strip() != f"?? {relative}":
                raise ValueError("report creation did not produce exactly one expected source file")
            digest = hashlib.sha256(encoded).hexdigest()
        except (ValueError, OSError, subprocess.SubprocessError, ET.ParseError) as exc:
            _remove_created(created)
            return Outcome.failed(f"Salesforce report source creation refused: {exc}")

        files = [{"path": relative, "sha256": digest, "bytes": len(encoded)}]
        result = {
            "environment": "partial",
            "type": "Report",
            "name": request.name,
            "folder": "unfiled$public",
            "git_root": str(git_root),
            "branch": branch,
            "files": files,
            "file_count": 1,
        }
        evidence = [
            {
                "kind": CREATE_REPORT_SOURCE_ACTION,
                "environment": "partial",
                "type": "Report",
                "name": request.name,
                "folder": "unfiled$public",
                "branch": branch,
                "sha256": digest,
                "production_execution": False,
                "metadata_deployed": False,
            }
        ]
        return Outcome.success(result, evidence)

    def _git(self, argv: Sequence[str]) -> CommandResult:
        completed = self.runner(argv, self.project_dir, self.timeout_seconds)
        if completed.returncode != 0:
            raise ValueError(f"report creation git check failed: {_failure_detail(completed)}")
        return completed


def _validate_report_xml(content: str) -> None:
    upper = content.upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        raise ValueError("XML document types and entities are not allowed")
    root = ET.fromstring(content)
    ns = f"{{{_METADATA_NAMESPACE}}}"
    if root.tag != f"{ns}Report":
        raise ValueError("content must be one Salesforce metadata Report document")
    columns = root.findall(f"{ns}columns")
    if not 1 <= len(columns) <= 12:
        raise ValueError("report must contain 1 to 12 columns")
    for column in columns:
        field = column.find(f"{ns}field")
        if field is None or not isinstance(field.text, str) or not _FIELD_RE.fullmatch(field.text):
            raise ValueError("every report column must contain one bounded field identifier")
    report_type = root.find(f"{ns}reportType")
    report_format = root.find(f"{ns}format")
    scope = root.find(f"{ns}scope")
    show_details = root.find(f"{ns}showDetails")
    if (
        report_type is None
        or not isinstance(report_type.text, str)
        or not _FIELD_RE.fullmatch(report_type.text.replace("$", ""))
    ):
        raise ValueError("reportType is missing or invalid")
    if report_format is None or report_format.text not in {"Tabular", "Summary", "Matrix"}:
        raise ValueError("report format must be Tabular, Summary, or Matrix")
    if scope is None or scope.text not in {"organization", "user"}:
        raise ValueError("report scope must be organization or user")
    if show_details is None or show_details.text not in {"true", "false"}:
        raise ValueError("showDetails must be explicit")


def _remove_created(created: tuple[Path, bytes] | None) -> None:
    if created is None:
        return
    path, expected = created
    try:
        if path.is_file() and not path.is_symlink() and path.read_bytes() == expected:
            path.unlink()
    except OSError:
        return

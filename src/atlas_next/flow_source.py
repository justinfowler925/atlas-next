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


CREATE_FLOW_SOURCE_ACTION = "salesforce.create_flow_source"
_FLOW_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,79}$")
_SOAP_NAMESPACE = "http://soap.sforce.com/2006/04/metadata"


@dataclass(frozen=True)
class CreateFlowSourceRequest:
    name: str
    content: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> CreateFlowSourceRequest:
        if set(payload) != {"name", "content"}:
            unexpected = sorted(set(payload) - {"name", "content"})
            missing = sorted({"name", "content"} - set(payload))
            details = []
            if missing:
                details.append(f"missing keys: {', '.join(missing)}")
            if unexpected:
                details.append(f"unexpected keys: {', '.join(unexpected)}")
            raise ValueError(
                "invalid salesforce.create_flow_source payload (" + "; ".join(details) + ")"
            )
        name = payload["name"]
        content = payload["content"]
        if not isinstance(name, str) or not _FLOW_NAME_RE.fullmatch(name):
            raise ValueError("name must be one exact Salesforce Flow API name")
        if (
            not isinstance(content, str)
            or not content
            or "\x00" in content
            or len(content.encode("utf-8")) > 1_000_000
        ):
            raise ValueError("content must be non-empty UTF-8 XML of at most 1 MB")
        return cls(name, content)


ProjectRunner = Callable[[Sequence[str], Path, float], CommandResult]


def run_project_command(argv: Sequence[str], cwd: Path, timeout_seconds: float) -> CommandResult:
    completed = subprocess.run(
        list(argv), cwd=cwd, capture_output=True, text=True, timeout=timeout_seconds, check=False
    )
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


class CreateFlowSource:
    """Create one active Flow XML file at its deterministic governed path."""

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
        try:
            request = CreateFlowSourceRequest.from_payload(item.payload)
            _validate_flow_xml(request.content)
            if not (self.project_dir / "sfdx-project.json").is_file():
                raise ValueError("configured project_dir is not a Salesforce project")
            git_root = Path(
                self._git(["git", "rev-parse", "--show-toplevel"]).stdout.strip()
            ).resolve()
            branch = self._git(["git", "branch", "--show-current"]).stdout.strip()
            if not branch or branch in {"main", "master"}:
                raise ValueError("Flow creation requires a named non-main branch")
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
                raise ValueError("Flow creation requires an isolated linked worktree")
            if self._git(["git", "status", "--porcelain=v1"]).stdout:
                raise ValueError("Flow creation requires a clean worktree")
            project_prefix = self.project_dir.relative_to(git_root).as_posix()
            relative = (
                f"{project_prefix}/force-app/main/default/flows/"
                f"{request.name}.flow-meta.xml"
            )
            absolute = (git_root / relative).resolve()
            if not absolute.is_relative_to(git_root) or absolute.exists():
                raise ValueError("Flow source path already exists or escaped the worktree")
            absolute.parent.mkdir(parents=True, exist_ok=True)
            absolute.write_text(request.content, encoding="utf-8")
            status = self._git(["git", "status", "--porcelain=v1"]).stdout
            if status.strip() != f"?? {relative}":
                raise ValueError("Flow creation did not produce exactly one expected source file")
            digest = hashlib.sha256(absolute.read_bytes()).hexdigest()
        except (ValueError, OSError, subprocess.SubprocessError, ET.ParseError) as exc:
            return Outcome.failed(f"Salesforce Flow source creation refused: {exc}")

        files = [{"path": relative, "sha256": digest, "bytes": absolute.stat().st_size}]
        result = {
            "environment": "partial",
            "type": "Flow",
            "name": request.name,
            "git_root": str(git_root),
            "branch": branch,
            "files": files,
            "file_count": 1,
            "status": "Active",
        }
        evidence = [
            {
                "kind": CREATE_FLOW_SOURCE_ACTION,
                "environment": "partial",
                "type": "Flow",
                "name": request.name,
                "branch": branch,
                "sha256": digest,
                "status": "Active",
                "production_execution": False,
                "metadata_deployed": False,
            }
        ]
        return Outcome.success(result, evidence)

    def _git(self, argv: Sequence[str]) -> CommandResult:
        completed = self.runner(argv, self.project_dir, self.timeout_seconds)
        if completed.returncode != 0:
            raise ValueError(f"Flow creation git check failed: {_failure_detail(completed)}")
        return completed


def _validate_flow_xml(content: str) -> None:
    upper = content.upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        raise ValueError("XML document types and entities are not allowed")
    root = ET.fromstring(content)
    if root.tag != f"{{{_SOAP_NAMESPACE}}}Flow":
        raise ValueError("content must be one Salesforce metadata Flow document")
    statuses = root.findall(f"{{{_SOAP_NAMESPACE}}}status")
    if len(statuses) != 1 or statuses[0].text != "Active":
        raise ValueError("Flow source must explicitly deploy its latest version as Active")

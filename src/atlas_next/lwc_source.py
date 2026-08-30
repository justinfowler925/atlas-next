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


CREATE_LWC_SOURCE_ACTION = "salesforce.create_lwc_source"
_LWC_NAME_RE = re.compile(r"^[a-z][A-Za-z0-9]{1,79}$")
_METADATA_NAMESPACE = "http://soap.sforce.com/2006/04/metadata"


@dataclass(frozen=True)
class CreateLwcSourceRequest:
    name: str
    files: dict[str, str]

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> CreateLwcSourceRequest:
        if set(payload) != {"name", "files"}:
            raise ValueError("payload must contain only name and files")
        name = payload["name"]
        files = payload["files"]
        if not isinstance(name, str) or not _LWC_NAME_RE.fullmatch(name):
            raise ValueError("name must be one lower-camel-case LWC bundle API name")
        expected = {
            f"{name}.js",
            f"{name}.html",
            f"{name}.css",
            f"{name}.js-meta.xml",
            f"__tests__/{name}.test.js",
        }
        if not isinstance(files, dict) or set(files) != expected:
            raise ValueError("files must contain the exact JS, HTML, CSS, metadata, and Jest set")
        total = 0
        for filename, content in files.items():
            if (
                not isinstance(content, str)
                or not content
                or "\x00" in content
                or len(content.encode("utf-8")) > 500_000
            ):
                raise ValueError(f"LWC file {filename!r} must be bounded non-empty UTF-8 text")
            total += len(content.encode("utf-8"))
        if total > 1_000_000:
            raise ValueError("LWC bundle exceeds the 1 MB source bound")
        _validate_lwc_files(name, files)
        return cls(name, files)


ProjectRunner = Callable[[Sequence[str], Path, float], CommandResult]


def run_project_command(argv: Sequence[str], cwd: Path, timeout_seconds: float) -> CommandResult:
    completed = subprocess.run(
        list(argv), cwd=cwd, capture_output=True, text=True, timeout=timeout_seconds, check=False
    )
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


class CreateLwcSource:
    """Create one complete LWC bundle and its behavioral Jest test at derived paths."""

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
            request = CreateLwcSourceRequest.from_payload(item.payload)
            if not (self.project_dir / "sfdx-project.json").is_file():
                raise ValueError("configured project_dir is not a Salesforce project")
            git_root = Path(
                self._git(["git", "rev-parse", "--show-toplevel"]).stdout.strip()
            ).resolve()
            branch = self._git(["git", "branch", "--show-current"]).stdout.strip()
            if not branch or branch in {"main", "master"}:
                raise ValueError("LWC creation requires a named non-main branch")
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
                raise ValueError("LWC creation requires an isolated linked worktree")
            if self._git(["git", "status", "--porcelain=v1"]).stdout:
                raise ValueError("LWC creation requires a clean worktree")
            project_prefix = self.project_dir.relative_to(git_root).as_posix()
            bundle_root = (
                f"{project_prefix}/force-app/main/default/lwc/{request.name}"
            )
            paths = {}
            for filename, content in request.files.items():
                relative = f"{bundle_root}/{filename}"
                absolute = (git_root / relative).resolve()
                if not absolute.is_relative_to(git_root) or absolute.exists():
                    raise ValueError("LWC source path already exists or escaped the worktree")
                absolute.parent.mkdir(parents=True, exist_ok=True)
                absolute.write_text(content, encoding="utf-8")
                paths[relative] = absolute
            status = self._git(["git", "status", "--porcelain=v1"]).stdout
            dirty = {line[3:] for line in status.splitlines() if line.startswith("?? ")}
            if dirty != set(paths) or len(status.splitlines()) != len(paths):
                raise ValueError("LWC creation did not produce exactly the expected bundle files")
            files = [
                {
                    "path": relative,
                    "sha256": hashlib.sha256(absolute.read_bytes()).hexdigest(),
                    "bytes": absolute.stat().st_size,
                }
                for relative, absolute in sorted(paths.items())
            ]
        except (ValueError, OSError, subprocess.SubprocessError, ET.ParseError) as exc:
            return Outcome.failed(f"Salesforce LWC source creation refused: {exc}")

        result = {
            "environment": "partial",
            "type": "LightningComponentBundle",
            "name": request.name,
            "git_root": str(git_root),
            "branch": branch,
            "files": files,
            "file_count": len(files),
            "has_behavioral_test": True,
        }
        evidence = [
            {
                "kind": CREATE_LWC_SOURCE_ACTION,
                "environment": "partial",
                "type": "LightningComponentBundle",
                "name": request.name,
                "branch": branch,
                "file_count": len(files),
                "has_behavioral_test": True,
                "production_execution": False,
                "metadata_deployed": False,
            }
        ]
        return Outcome.success(result, evidence)

    def _git(self, argv: Sequence[str]) -> CommandResult:
        completed = self.runner(argv, self.project_dir, self.timeout_seconds)
        if completed.returncode != 0:
            raise ValueError(f"LWC creation git check failed: {_failure_detail(completed)}")
        return completed


def _validate_lwc_files(name: str, files: dict[str, str]) -> None:
    javascript = files[f"{name}.js"]
    html = files[f"{name}.html"]
    css = files[f"{name}.css"]
    metadata = files[f"{name}.js-meta.xml"]
    test = files[f"__tests__/{name}.test.js"]
    if "extends LightningElement" not in javascript or "export default class" not in javascript:
        raise ValueError("LWC JavaScript must export one LightningElement class")
    if "<template" not in html or "</template>" not in html or "<script" in html.lower():
        raise ValueError("LWC HTML must contain one script-free template")
    if ":host" not in css or "--slds-g-" not in css:
        raise ValueError("LWC CSS must use host-scoped SLDS global hooks with fallbacks")
    root = ET.fromstring(metadata)
    if root.tag != f"{{{_METADATA_NAMESPACE}}}LightningComponentBundle":
        raise ValueError("LWC metadata root is invalid")
    exposed = root.find(f"{{{_METADATA_NAMESPACE}}}isExposed")
    targets = {
        node.text for node in root.findall(f"{{{_METADATA_NAMESPACE}}}targets/{{{_METADATA_NAMESPACE}}}target")
    }
    if exposed is None or exposed.text != "true" or "lightning__RecordPage" not in targets:
        raise ValueError("LWC metadata must expose the component on Lightning record pages")
    required_test_markers = ("createElement", "document.body.appendChild", ".click()", "expect(")
    if any(marker not in test for marker in required_test_markers):
        raise ValueError("LWC Jest file must mount, interact with, and assert the component")

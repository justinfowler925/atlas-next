from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .engine import Outcome
from .models import WorkItem
from .salesforce import CommandResult, CommandRunner, _failure_detail, _json_error, run_command


METADATA_DIFF_ACTION = "salesforce.metadata_diff"
METADATA_CONTENT_DIFF_ACTION = "salesforce.metadata_content_diff"
SOURCE_RETRIEVE_ACTION = "salesforce.retrieve_source"
_COMPONENT_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_. /-]{0,199}$")
SUPPORTED_METADATA_TYPES = frozenset(
    {
        "ApexClass",
        "ApexTrigger",
        "CustomField",
        "CustomObject",
        "CustomPermission",
        "ExternalCredential",
        "FlexiPage",
        "Flow",
        "Layout",
        "LightningComponentBundle",
        "NamedCredential",
        "PermissionSet",
        "Profile",
        "RemoteSiteSetting",
        "ValidationRule",
    }
)


@dataclass(frozen=True)
class MetadataDiffRequest:
    metadata_type: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> MetadataDiffRequest:
        if set(payload) != {"type"}:
            unexpected = sorted(set(payload) - {"type"})
            missing = sorted({"type"} - set(payload))
            details = []
            if missing:
                details.append("missing keys: type")
            if unexpected:
                details.append(f"unexpected keys: {', '.join(unexpected)}")
            raise ValueError(
                "invalid salesforce.metadata_diff payload (" + "; ".join(details) + ")"
            )
        metadata_type = payload["type"]
        if metadata_type not in SUPPORTED_METADATA_TYPES:
            allowed = ", ".join(sorted(SUPPORTED_METADATA_TYPES))
            raise ValueError(f"type must be one of: {allowed}")
        return cls(metadata_type)


@dataclass(frozen=True)
class MetadataContentDiffRequest:
    metadata_type: str
    component_name: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> MetadataContentDiffRequest:
        if set(payload) != {"type", "name"}:
            unexpected = sorted(set(payload) - {"type", "name"})
            missing = sorted({"type", "name"} - set(payload))
            details = []
            if missing:
                details.append(f"missing keys: {', '.join(missing)}")
            if unexpected:
                details.append(f"unexpected keys: {', '.join(unexpected)}")
            raise ValueError(
                "invalid salesforce.metadata_content_diff payload ("
                + "; ".join(details)
                + ")"
            )
        metadata_type = payload["type"]
        if metadata_type not in SUPPORTED_METADATA_TYPES:
            raise ValueError("type is not in the supported metadata vocabulary")
        name = payload["name"]
        if (
            not isinstance(name, str)
            or not _COMPONENT_NAME_RE.fullmatch(name)
            or ".." in name
            or "*" in name
        ):
            raise ValueError("name must identify exactly one metadata component")
        return cls(metadata_type, name)


@dataclass(frozen=True)
class SourceRetrieveRequest:
    metadata_type: str
    component_name: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> SourceRetrieveRequest:
        parsed = MetadataContentDiffRequest.from_payload(payload)
        return cls(parsed.metadata_type, parsed.component_name)


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


class SalesforceMetadataDiff:
    """Compare component-name inventory between Partial and production."""

    def __init__(
        self,
        targets: dict[str, str],
        *,
        runner: CommandRunner = run_command,
        timeout_seconds: float = 120,
    ) -> None:
        self.targets = {key: value.strip() for key, value in targets.items()}
        self.runner = runner
        self.timeout_seconds = timeout_seconds

    def __call__(self, item: WorkItem) -> Outcome:
        try:
            request = MetadataDiffRequest.from_payload(item.payload)
            partial = self.targets.get("partial", "")
            prod = self.targets.get("prod", "")
            if not partial or not prod:
                raise ValueError("both partial and prod target aliases are required")
            inventories = {}
            for environment, target in (("partial", partial), ("prod", prod)):
                completed = self.runner(
                    [
                        "sf",
                        "org",
                        "list",
                        "metadata",
                        "--metadata-type",
                        request.metadata_type,
                        "--target-org",
                        target,
                        "--json",
                    ],
                    self.timeout_seconds,
                )
                if completed.returncode != 0:
                    return Outcome.failed(
                        f"salesforce metadata list failed for "
                        f"{request.metadata_type}@{target}: {_failure_detail(completed)}"
                    )
                inventories[environment] = _parse_inventory(
                    completed.stdout, request.metadata_type
                )
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            return Outcome.failed(f"salesforce metadata diff refused: {exc}")

        partial_names = inventories["partial"]
        prod_names = inventories["prod"]
        partial_only = sorted(partial_names - prod_names)
        prod_only = sorted(prod_names - partial_names)
        shared = partial_names & prod_names
        result = {
            "type": request.metadata_type,
            "partial_alias": partial,
            "prod_alias": prod,
            "partial_count": len(partial_names),
            "prod_count": len(prod_names),
            "shared_count": len(shared),
            "partial_only": partial_only,
            "prod_only": prod_only,
            "parity": not partial_only and not prod_only,
        }
        evidence = [
            {
                "kind": METADATA_DIFF_ACTION,
                "type": request.metadata_type,
                "partial_alias": partial,
                "prod_alias": prod,
                "partial_count": len(partial_names),
                "prod_count": len(prod_names),
                "shared_count": len(shared),
                "partial_only_count": len(partial_only),
                "prod_only_count": len(prod_only),
                "partial_inventory_sha256": _names_hash(partial_names),
                "prod_inventory_sha256": _names_hash(prod_names),
                "read_only_commands": 2,
            }
        ]
        return Outcome.success(result, evidence)


class SalesforceMetadataContentDiff:
    """Retrieve one exact component from both orgs and compare source bytes."""

    def __init__(
        self,
        targets: dict[str, str],
        *,
        project_dir: Path,
        artifact_root: Path,
        runner: ProjectRunner = run_project_command,
        timeout_seconds: float = 660,
    ) -> None:
        self.targets = {key: value.strip() for key, value in targets.items()}
        self.project_dir = project_dir.resolve()
        self.artifact_root = artifact_root.resolve()
        self.runner = runner
        self.timeout_seconds = timeout_seconds

    def __call__(self, item: WorkItem) -> Outcome:
        try:
            request = MetadataContentDiffRequest.from_payload(item.payload)
            if not (self.project_dir / "sfdx-project.json").is_file():
                raise ValueError("configured project_dir is not a Salesforce project")
            partial = self.targets.get("partial", "")
            prod = self.targets.get("prod", "")
            if not partial or not prod:
                raise ValueError("both partial and prod target aliases are required")
            work_root = self.artifact_root / item.id
            work_root.mkdir(parents=True, exist_ok=False)
            manifests = {}
            for environment, target in (("partial", partial), ("prod", prod)):
                output_dir = work_root / environment
                output_dir.mkdir()
                completed = self.runner(
                    [
                        "sf",
                        "project",
                        "retrieve",
                        "start",
                        "--metadata",
                        f"{request.metadata_type}:{request.component_name}",
                        "--target-org",
                        target,
                        "--target-metadata-dir",
                        str(output_dir),
                        "--unzip",
                        "--single-package",
                        "--wait",
                        "10",
                        "--json",
                    ],
                    self.project_dir,
                    self.timeout_seconds,
                )
                if completed.returncode != 0:
                    return Outcome.failed(
                        f"salesforce metadata retrieve failed for "
                        f"{request.metadata_type}:{request.component_name}@{target}: "
                        f"{_failure_detail(completed)}"
                    )
                _validate_retrieve_response(completed.stdout)
                manifests[environment] = _artifact_manifest(output_dir)
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            return Outcome.failed(f"salesforce metadata content diff refused: {exc}")

        partial_manifest = manifests["partial"]
        prod_manifest = manifests["prod"]
        partial_files = set(partial_manifest)
        prod_files = set(prod_manifest)
        shared_files = partial_files & prod_files
        changed = sorted(
            path for path in shared_files if partial_manifest[path] != prod_manifest[path]
        )
        partial_only = sorted(partial_files - prod_files)
        prod_only = sorted(prod_files - partial_files)
        parity = not changed and not partial_only and not prod_only
        result = {
            "type": request.metadata_type,
            "name": request.component_name,
            "partial_alias": partial,
            "prod_alias": prod,
            "artifact_root": str(work_root),
            "partial_file_count": len(partial_manifest),
            "prod_file_count": len(prod_manifest),
            "changed_files": changed,
            "partial_only_files": partial_only,
            "prod_only_files": prod_only,
            "content_parity": parity,
        }
        evidence = [
            {
                "kind": METADATA_CONTENT_DIFF_ACTION,
                "type": request.metadata_type,
                "name": request.component_name,
                "partial_alias": partial,
                "prod_alias": prod,
                "partial_manifest_sha256": _manifest_hash(partial_manifest),
                "prod_manifest_sha256": _manifest_hash(prod_manifest),
                "changed_file_count": len(changed),
                "partial_only_file_count": len(partial_only),
                "prod_only_file_count": len(prod_only),
                "content_parity": parity,
                "artifact_root": str(work_root),
                "read_only_commands": 2,
            }
        ]
        return Outcome.success(result, evidence)


class SalesforceSourceRetrieve:
    """Retrieve one exact Partial component into a clean isolated git worktree."""

    def __init__(
        self,
        *,
        partial_alias: str,
        project_dir: Path,
        runner: ProjectRunner = run_project_command,
        timeout_seconds: float = 660,
    ) -> None:
        self.partial_alias = partial_alias.strip()
        self.project_dir = project_dir.resolve()
        self.runner = runner
        self.timeout_seconds = timeout_seconds

    def __call__(self, item: WorkItem) -> Outcome:
        try:
            request = SourceRetrieveRequest.from_payload(item.payload)
            if not self.partial_alias:
                raise ValueError("partial target alias is required")
            if not (self.project_dir / "sfdx-project.json").is_file():
                raise ValueError("configured project_dir is not a Salesforce project")
            git_root = Path(
                self._git(["git", "rev-parse", "--show-toplevel"]).stdout.strip()
            ).resolve()
            branch = self._git(["git", "branch", "--show-current"]).stdout.strip()
            if not branch or branch in {"main", "master"}:
                raise ValueError("source retrieve requires a named non-main branch")
            git_dir = Path(self._git(["git", "rev-parse", "--path-format=absolute", "--git-dir"]).stdout.strip())
            common_dir = Path(
                self._git(
                    ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"]
                ).stdout.strip()
            )
            if git_dir.resolve() == common_dir.resolve():
                raise ValueError("source retrieve requires an isolated linked worktree")
            if self._git(["git", "status", "--porcelain=v1"]).stdout:
                raise ValueError("source retrieve requires a clean worktree")
            project_prefix = self.project_dir.relative_to(git_root).as_posix()
            completed = self.runner(
                [
                    "sf",
                    "project",
                    "retrieve",
                    "start",
                    "--metadata",
                    f"{request.metadata_type}:{request.component_name}",
                    "--target-org",
                    self.partial_alias,
                    "--ignore-conflicts",
                    "--wait",
                    "10",
                    "--json",
                ],
                self.project_dir,
                self.timeout_seconds,
            )
            if completed.returncode != 0:
                return Outcome.failed(
                    f"salesforce source retrieve failed for "
                    f"{request.metadata_type}:{request.component_name}@{self.partial_alias}: "
                    f"{_failure_detail(completed)}"
                )
            _validate_retrieve_response(completed.stdout)
            retrieved = _retrieved_source_paths(
                completed.stdout,
                git_root=git_root,
                project_prefix=project_prefix,
                metadata_type=request.metadata_type,
                component_name=request.component_name,
            )
            status = self._git(
                ["git", "status", "--porcelain=v1", "--untracked-files=all"]
            ).stdout
            changed = _validate_retrieved_changes(
                status, git_root, project_prefix, allow_empty=bool(retrieved)
            )
            if retrieved:
                if not set(changed) <= set(retrieved):
                    raise ValueError("source retrieve dirt exceeds the returned component files")
                files_to_record = retrieved
            else:
                files_to_record = changed
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            return Outcome.failed(f"salesforce source retrieve refused: {exc}")

        files = [
            {
                "path": path,
                "sha256": hashlib.sha256((git_root / path).read_bytes()).hexdigest(),
                "bytes": (git_root / path).stat().st_size,
            }
            for path in files_to_record
        ]
        result = {
            "environment": "partial",
            "target_alias": self.partial_alias,
            "type": request.metadata_type,
            "name": request.component_name,
            "git_root": str(git_root),
            "branch": branch,
            "files": files,
            "file_count": len(files),
            "dirty_files": changed,
        }
        evidence = [
            {
                "kind": SOURCE_RETRIEVE_ACTION,
                "environment": "partial",
                "target_alias": self.partial_alias,
                "type": request.metadata_type,
                "name": request.component_name,
                "branch": branch,
                "file_count": len(files),
                "isolated_worktree": True,
                "production_execution": False,
                "metadata_deployed": False,
            }
        ]
        return Outcome.success(result, evidence)

    def _git(self, argv: Sequence[str]) -> CommandResult:
        completed = self.runner(argv, self.project_dir, 30)
        if completed.returncode != 0:
            raise ValueError(f"git preflight failed: {_failure_detail(completed)}")
        return completed


def _parse_inventory(stdout: str, expected_type: str) -> set[str]:
    envelope = json.loads(stdout)
    if int(envelope.get("status", 0)) != 0:
        raise ValueError(_json_error(envelope) or "Salesforce CLI returned non-zero status")
    rows = envelope["result"]
    if not isinstance(rows, list) or len(rows) > 10_000:
        raise ValueError("metadata inventory must be a list of at most 10000 components")
    names = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("metadata inventory row must be an object")
        if row.get("type") != expected_type:
            raise ValueError("metadata inventory returned an unexpected component type")
        name = row.get("fullName")
        if not isinstance(name, str) or not name or len(name) > 500:
            raise ValueError("metadata inventory returned an invalid component name")
        if name in names:
            raise ValueError("metadata inventory returned duplicate component names")
        names.add(name)
    return names


def _names_hash(names: set[str]) -> str:
    return hashlib.sha256("\n".join(sorted(names)).encode()).hexdigest()


def _validate_retrieve_response(stdout: str) -> None:
    envelope = json.loads(stdout)
    if int(envelope.get("status", 0)) != 0:
        raise ValueError(_json_error(envelope) or "Salesforce CLI returned non-zero status")
    result = envelope["result"]
    if result.get("done") is not True or result.get("status") != "Succeeded":
        raise ValueError("metadata retrieve did not report Succeeded and done")


def _retrieved_source_paths(
    stdout: str,
    *,
    git_root: Path,
    project_prefix: str,
    metadata_type: str,
    component_name: str,
) -> list[str]:
    """Return the CLI's exact component files, including when retrieval is git-clean."""
    result = json.loads(stdout)["result"]
    rows = result.get("files")
    if rows is None:
        return []
    if not isinstance(rows, list) or not 1 <= len(rows) <= 20:
        raise ValueError("source retrieve returned an invalid file population")
    allowed_root = f"{project_prefix}/force-app/main/default/"
    paths = []
    for row in rows:
        if (
            not isinstance(row, dict)
            or row.get("type") != metadata_type
            or row.get("fullName") != component_name
        ):
            raise ValueError("source retrieve returned a different metadata component")
        raw_path = row.get("filePath")
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError("source retrieve returned an invalid file path")
        absolute = Path(raw_path).resolve()
        if (
            not absolute.is_relative_to(git_root)
            or not absolute.is_file()
            or absolute.is_symlink()
        ):
            raise ValueError("source retrieve returned a non-regular project file")
        relative = absolute.relative_to(git_root).as_posix()
        if not relative.startswith(allowed_root):
            raise ValueError("source retrieve returned an out-of-scope project file")
        if absolute.stat().st_size > 5_000_000 or relative in paths:
            raise ValueError("source retrieve returned an invalid or duplicate file")
        paths.append(relative)
    return sorted(paths)


def _artifact_manifest(output_dir: Path) -> dict[str, str]:
    files = []
    total_bytes = 0
    for path in output_dir.rglob("*"):
        if path.is_symlink():
            raise ValueError("metadata retrieve produced a symbolic link")
        if not path.is_file() or path.name in {"package.xml", "unpackaged.zip"}:
            continue
        size = path.stat().st_size
        if size > 5_000_000:
            raise ValueError("metadata retrieve produced a file larger than 5 MB")
        total_bytes += size
        files.append(path)
    if not files:
        raise ValueError("metadata retrieve produced no component files")
    if len(files) > 20 or total_bytes > 10_000_000:
        raise ValueError("metadata retrieve exceeded the component artifact bounds")
    manifest = {}
    for path in files:
        relative = path.relative_to(output_dir)
        parts = relative.parts[1:] if relative.parts[:1] == ("unpackaged",) else relative.parts
        key = "/".join(parts)
        if key in manifest:
            raise ValueError("metadata retrieve produced duplicate relative paths")
        manifest[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    return manifest


def _manifest_hash(manifest: dict[str, str]) -> str:
    canonical = "\n".join(f"{path}\0{digest}" for path, digest in sorted(manifest.items()))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _validate_retrieved_changes(
    status: str, git_root: Path, project_prefix: str, *, allow_empty: bool = False
) -> list[str]:
    lines = [line for line in status.splitlines() if line]
    if not lines and not allow_empty:
        raise ValueError("source retrieve produced zero git changes")
    if len(lines) > 20:
        raise ValueError("source retrieve changed more than 20 files")
    allowed_root = f"{project_prefix}/force-app/main/default/"
    changed = []
    total_bytes = 0
    for line in lines:
        if len(line) < 4 or line[:2] not in {"??", " M", "M ", "MM", "A "}:
            raise ValueError(f"source retrieve produced unsupported git status {line[:2]!r}")
        path = line[3:]
        if not path.startswith(allowed_root) or " -> " in path:
            raise ValueError(f"source retrieve changed an out-of-scope path: {path}")
        absolute = (git_root / path).resolve()
        if not absolute.is_relative_to(git_root) or not absolute.is_file() or absolute.is_symlink():
            raise ValueError(f"source retrieve did not produce one regular file: {path}")
        size = absolute.stat().st_size
        if size > 5_000_000:
            raise ValueError(f"source retrieve produced a file larger than 5 MB: {path}")
        total_bytes += size
        changed.append(path)
    if total_bytes > 10_000_000:
        raise ValueError("source retrieve exceeded the 10 MB artifact bound")
    return sorted(changed)

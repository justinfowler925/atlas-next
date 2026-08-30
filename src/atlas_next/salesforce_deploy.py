from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .engine import Outcome
from .models import WorkItem
from .salesforce import CommandResult, _failure_detail, _json_error


DRY_RUN_ACTION = "salesforce.deploy_dry_run"
_CLASS_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,79}$")
_SOURCE_ROOT = PurePosixPath("force-app/main/default")


@dataclass(frozen=True)
class DeployDryRunRequest:
    source_paths: tuple[str, ...]
    tests: tuple[str, ...]

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> DeployDryRunRequest:
        if set(payload) != {"source_paths", "tests"}:
            unexpected = sorted(set(payload) - {"source_paths", "tests"})
            missing = sorted({"source_paths", "tests"} - set(payload))
            details = []
            if missing:
                details.append(f"missing keys: {', '.join(missing)}")
            if unexpected:
                details.append(f"unexpected keys: {', '.join(unexpected)}")
            raise ValueError(
                "invalid salesforce.deploy_dry_run payload (" + "; ".join(details) + ")"
            )
        raw_paths = payload["source_paths"]
        if not isinstance(raw_paths, list) or not 1 <= len(raw_paths) <= 20:
            raise ValueError("source_paths must contain 1 to 20 project-relative files")
        paths = tuple(_source_path(path) for path in raw_paths)
        if len(paths) != len(set(paths)):
            raise ValueError("source_paths must not contain duplicates")
        raw_tests = payload["tests"]
        if not isinstance(raw_tests, list) or not 1 <= len(raw_tests) <= 10:
            raise ValueError("tests must contain 1 to 10 Apex test class API names")
        tests = tuple(raw_tests)
        if any(not isinstance(name, str) or not _CLASS_RE.fullmatch(name) for name in tests):
            raise ValueError("each test must be one Apex test class API name")
        if len(tests) != len(set(tests)):
            raise ValueError("tests must not contain duplicates")
        return cls(paths, tests)


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


class SalesforceDeployDryRun:
    """Validate exact source files in Partial without saving metadata."""

    def __init__(
        self,
        *,
        partial_alias: str,
        project_dir: Path,
        runner: ProjectRunner = run_project_command,
        timeout_seconds: float = 1860,
    ) -> None:
        self.partial_alias = partial_alias.strip()
        self.project_dir = project_dir.resolve()
        self.runner = runner
        self.timeout_seconds = timeout_seconds

    def __call__(self, item: WorkItem) -> Outcome:
        try:
            request = DeployDryRunRequest.from_payload(item.payload)
            if not self.partial_alias:
                raise ValueError("partial target alias is required")
            if not (self.project_dir / "sfdx-project.json").is_file():
                raise ValueError("configured project_dir is not a Salesforce project")
            for relative in request.source_paths:
                path = (self.project_dir / relative).resolve()
                if not path.is_relative_to(self.project_dir) or not path.is_file():
                    raise ValueError(f"source path is not one existing project file: {relative}")
                if path.is_symlink() or path.stat().st_size > 5_000_000:
                    raise ValueError(f"source path is symbolic or larger than 5 MB: {relative}")
            argv = ["sf", "project", "deploy", "start", "--dry-run"]
            for relative in request.source_paths:
                argv.extend(["--source-dir", relative])
            argv.extend(["--target-org", self.partial_alias, "--test-level", "RunSpecifiedTests"])
            for test in request.tests:
                argv.extend(["--tests", test])
            argv.extend(["--wait", "30", "--json"])
            completed = self.runner(argv, self.project_dir, self.timeout_seconds)
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            return Outcome.failed(f"salesforce deploy dry-run refused: {exc}")
        if completed.returncode != 0:
            return Outcome.failed(
                f"salesforce deploy dry-run failed@{self.partial_alias}: "
                f"{_failure_detail(completed)}"
            )
        try:
            result = _parse_deploy_result(completed.stdout)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return Outcome.failed(f"salesforce deploy dry-run response rejected: {exc}")

        output = {
            "environment": "partial",
            "target_alias": self.partial_alias,
            "source_paths": list(request.source_paths),
            "tests": list(request.tests),
            **result,
        }
        evidence = [
            {
                "kind": DRY_RUN_ACTION,
                "environment": "partial",
                "target_alias": self.partial_alias,
                "deploy_id": result["deploy_id"],
                "check_only": True,
                "component_count": result["component_count"],
                "component_errors": 0,
                "tests_completed": result["tests_completed"],
                "test_errors": 0,
                "production_execution": False,
                "metadata_saved": False,
            }
        ]
        return Outcome.success(output, evidence)


def _source_path(value: Any) -> str:
    if not isinstance(value, str) or "\\" in value:
        raise ValueError("each source path must be a POSIX project-relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.is_relative_to(_SOURCE_ROOT):
        raise ValueError("each source path must be below force-app/main/default")
    if len(path.parts) <= len(_SOURCE_ROOT.parts) or len(value) > 500:
        raise ValueError("each source path must identify one metadata file")
    return path.as_posix()


def _parse_deploy_result(stdout: str) -> dict[str, Any]:
    envelope = json.loads(stdout)
    if int(envelope.get("status", 0)) != 0:
        raise ValueError(_json_error(envelope) or "Salesforce CLI returned non-zero status")
    result = envelope["result"]
    if result.get("status") != "Succeeded" or result.get("success") is not True:
        raise ValueError("deploy result did not report success")
    if result.get("done") is not True or result.get("checkOnly") is not True:
        raise ValueError("deploy result was not a completed check-only run")
    deploy_id = result.get("id")
    if not isinstance(deploy_id, str) or not deploy_id.startswith("0Af"):
        raise ValueError("deploy result has no valid deployment id")
    component_count = _positive_int(result, "numberComponentsTotal")
    deployed = _nonnegative_int(result, "numberComponentsDeployed")
    component_errors = _nonnegative_int(result, "numberComponentErrors")
    tests_total = _positive_int(result, "numberTestsTotal")
    tests_completed = _nonnegative_int(result, "numberTestsCompleted")
    test_errors = _nonnegative_int(result, "numberTestErrors")
    if deployed != component_count or component_errors != 0:
        raise ValueError("deploy component counts do not prove success")
    if tests_completed != tests_total or test_errors != 0:
        raise ValueError("deploy test counts do not prove success")
    return {
        "outcome": "Succeeded",
        "deploy_id": deploy_id,
        "check_only": True,
        "component_count": component_count,
        "tests_completed": tests_completed,
    }


def _positive_int(result: dict[str, Any], key: str) -> int:
    value = _nonnegative_int(result, key)
    if value < 1:
        raise ValueError(f"deploy result {key} must be positive")
    return value


def _nonnegative_int(result: dict[str, Any], key: str) -> int:
    value = result[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"deploy result {key} must be a non-negative integer")
    return value

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .delivery import (
    COMMIT_SOURCE_ACTION,
    MERGE_PR_ACTION,
    OPEN_PR_ACTION,
    VERIFY_PR_ACTION,
    VERIFY_SANDBOX_DEPLOY_ACTION,
)
from .engine import Outcome
from .integration_source import CREATE_INTEGRATION_SOURCE_ACTION
from .models import WorkItem, WorkState
from .salesforce import CommandRunner, _failure_detail, require_partial_target, run_command
from .store import Store


VERIFY_INTEGRATION_EXECUTION_ACTION = "salesforce.verify_integration_execution"
_APEX_NAME_RE = re.compile(r"^[A-Z][A-Za-z0-9]{2,35}$")
_MARKER_RE = re.compile(r"^[^\r\n]{1,100}$")


@dataclass(frozen=True)
class VerifyIntegrationExecutionRequest:
    deploy_work_id: str
    source_work_id: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> VerifyIntegrationExecutionRequest:
        if set(payload) != {"deploy_work_id", "source_work_id"}:
            raise ValueError("payload must contain only deploy_work_id and source_work_id")
        deploy = payload["deploy_work_id"]
        source = payload["source_work_id"]
        if not isinstance(deploy, str) or not deploy or not isinstance(source, str) or not source:
            raise ValueError("work item ids must be non-empty text")
        return cls(deploy, source)


class VerifyIntegrationExecution:
    """Execute one lineage-proven Atlas REST integration in live Partial."""

    def __init__(
        self,
        store: Store,
        *,
        partial_alias: str,
        artifact_root: Path,
        runner: CommandRunner = run_command,
        timeout_seconds: float = 120,
    ) -> None:
        self.store = store
        self.partial_alias = partial_alias.strip()
        self.artifact_root = artifact_root.resolve()
        self.runner = runner
        self.timeout_seconds = timeout_seconds

    def __call__(self, item: WorkItem) -> Outcome:
        try:
            request = VerifyIntegrationExecutionRequest.from_payload(item.payload)
            deploy = self.store.get(request.deploy_work_id)
            source = self.store.get(request.source_work_id)
            if (
                deploy is None
                or deploy.state is not WorkState.SUCCEEDED
                or deploy.action != VERIFY_SANDBOX_DEPLOY_ACTION
            ):
                raise ValueError("deploy receipt is missing or unsuccessful")
            if (
                source is None
                or source.state is not WorkState.SUCCEEDED
                or source.action != CREATE_INTEGRATION_SOURCE_ACTION
            ):
                raise ValueError("integration source receipt is missing or unsuccessful")
            verified = _prove_integration_lineage(self.store, deploy, request.source_work_id)
            checks = verified.result.get("checks")
            if not isinstance(checks, dict) or checks.get("Validate (sandbox)") != "SUCCESS":
                raise ValueError("deployed PR has no successful sandbox validation receipt")
            name = str(source.result.get("name", ""))
            expected = str(source.result.get("expected_marker", ""))
            if (
                not _APEX_NAME_RE.fullmatch(name)
                or not _MARKER_RE.fullmatch(expected)
                or not self.partial_alias
            ):
                raise ValueError("integration source or Partial alias is invalid")
            partial = require_partial_target(
                self.runner, self.partial_alias, self.timeout_seconds
            )
            escaped = expected.replace("\\", "\\\\").replace("'", "\\'")
            script = (
                f"String result = {name}.fetchMarker();\n"
                f"System.assertEquals('{escaped}', result, 'Atlas integration marker must match');\n"
                "System.debug('ATLAS_INTEGRATION_RESULT=' + result);\n"
            )
            self.artifact_root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=self.artifact_root) as temporary:
                script_path = Path(temporary) / "run.apex"
                script_path.write_text(script, encoding="utf-8")
                completed = self.runner(
                    [
                        "sf", "apex", "run", "--file", str(script_path),
                        "--target-org", self.partial_alias, "--json",
                    ],
                    self.timeout_seconds,
                )
            if completed.returncode != 0:
                raise ValueError(f"integration runtime execution failed: {_failure_detail(completed)}")
            envelope = json.loads(completed.stdout)
            runtime = envelope.get("result", {})
            if runtime.get("compiled") is not True or runtime.get("success") is not True:
                raise ValueError("integration runtime Apex did not compile and execute")
            logs = runtime.get("logs")
            marker = f"ATLAS_INTEGRATION_RESULT={expected}"
            if not isinstance(logs, str) or re.search(
                rf"\|USER_DEBUG\|[^\n]*\|DEBUG\|{re.escape(marker)}(?:\r?$)", logs, re.M
            ) is None:
                raise ValueError("integration runtime marker did not match")
            log_sha = hashlib.sha256(logs.encode()).hexdigest()
        except (ValueError, OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            return Outcome.failed(f"integration execution verification refused: {exc}")

        result = {
            "environment": "partial",
            "target_alias": self.partial_alias,
            "target_org_id": partial["org_id"],
            "name": name,
            "host": source.result.get("base_url"),
            "path": source.result.get("path"),
            "marker": expected,
            "log_sha256": log_sha,
            "deploy_work_id": request.deploy_work_id,
            "source_work_id": request.source_work_id,
        }
        evidence = [
            {
                "kind": VERIFY_INTEGRATION_EXECUTION_ACTION,
                "environment": "partial",
                "name": name,
                "external_callout": True,
                "marker": expected,
                "compiled": True,
                "executed": True,
                "log_sha256": log_sha,
                "production_execution": False,
            }
        ]
        return Outcome.success(result, evidence)


def _prove_integration_lineage(store: Store, deploy: WorkItem, source_work_id: str) -> WorkItem:
    merge = store.get(str(deploy.result.get("merge_pr_work_id", "")))
    if merge is None or merge.action != MERGE_PR_ACTION or merge.state is not WorkState.SUCCEEDED:
        raise ValueError("deploy receipt has no successful merge parent")
    verified = store.get(str(merge.result.get("verify_pr_work_id", "")))
    if verified is None or verified.action != VERIFY_PR_ACTION:
        raise ValueError("merge receipt has no verified PR parent")
    opened = store.get(str(verified.result.get("open_pr_work_id", "")))
    if opened is None or opened.action != OPEN_PR_ACTION:
        raise ValueError("verify receipt has no open PR parent")
    commit_ids = opened.result.get("commit_work_ids")
    if not isinstance(commit_ids, list):
        raise ValueError("open PR receipt has no commit lineage")
    source_ids: set[str] = set()
    for commit_id in commit_ids:
        commit = store.get(str(commit_id))
        if commit is None or commit.action != COMMIT_SOURCE_ACTION:
            raise ValueError("open PR lineage contains a non-source commit")
        values = commit.result.get("source_work_ids")
        if isinstance(values, list):
            source_ids.update(str(value) for value in values)
    if source_work_id not in source_ids:
        raise ValueError("integration source is not in the deployed PR lineage")
    return verified

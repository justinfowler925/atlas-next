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
from .flow_source import CREATE_FLOW_SOURCE_ACTION
from .models import WorkItem, WorkState
from .salesforce import CommandRunner, _failure_detail, require_partial_target, run_command
from .store import Store


VERIFY_FLOW_ACTIVATION_ACTION = "salesforce.verify_flow_activation"
RUN_CREATED_FLOW_ACTION = "salesforce.run_created_flow"
_API_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,79}$")
_VALUE_RE = re.compile(r"^[A-Za-z0-9 _.,:@/+\-]{1,200}$")


@dataclass(frozen=True)
class VerifyFlowActivationRequest:
    deploy_work_id: str
    source_work_id: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> VerifyFlowActivationRequest:
        if set(payload) != {"deploy_work_id", "source_work_id"}:
            raise ValueError("payload must contain only deploy_work_id and source_work_id")
        deploy = payload["deploy_work_id"]
        source = payload["source_work_id"]
        if not isinstance(deploy, str) or not deploy or not isinstance(source, str) or not source:
            raise ValueError("work item ids must be non-empty text")
        return cls(deploy, source)


@dataclass(frozen=True)
class RunCreatedFlowRequest:
    activation_work_id: str
    output_variable: str
    expected_string: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> RunCreatedFlowRequest:
        expected = {"activation_work_id", "output_variable", "expected_string"}
        if set(payload) != expected:
            raise ValueError(
                "payload must contain only activation_work_id, output_variable, and expected_string"
            )
        activation = payload["activation_work_id"]
        variable = payload["output_variable"]
        value = payload["expected_string"]
        if not isinstance(activation, str) or not activation:
            raise ValueError("activation_work_id must be non-empty text")
        if not isinstance(variable, str) or not _API_NAME_RE.fullmatch(variable):
            raise ValueError("output_variable must be one exact Flow variable API name")
        if not isinstance(value, str) or not _VALUE_RE.fullmatch(value):
            raise ValueError("expected_string contains unsupported characters or length")
        return cls(activation, variable, value)


class VerifyFlowActivation:
    """Prove an Atlas-created Flow's active and latest Partial versions match."""

    def __init__(
        self,
        store: Store,
        *,
        partial_alias: str,
        runner: CommandRunner = run_command,
        timeout_seconds: float = 120,
    ) -> None:
        self.store = store
        self.partial_alias = partial_alias.strip()
        self.runner = runner
        self.timeout_seconds = timeout_seconds

    def __call__(self, item: WorkItem) -> Outcome:
        try:
            request = VerifyFlowActivationRequest.from_payload(item.payload)
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
                or source.action != CREATE_FLOW_SOURCE_ACTION
            ):
                raise ValueError("Flow source receipt is missing or unsuccessful")
            _prove_delivery_lineage(self.store, deploy, request.source_work_id)
            name = str(source.result.get("name", ""))
            if not _API_NAME_RE.fullmatch(name) or not self.partial_alias:
                raise ValueError("Flow source or Partial alias is invalid")
            partial = require_partial_target(
                self.runner, self.partial_alias, self.timeout_seconds
            )
            query = (
                "SELECT DeveloperName, ActiveVersion.VersionNumber, "
                "LatestVersion.VersionNumber FROM FlowDefinition "
                f"WHERE DeveloperName = '{name}'"
            )
            completed = self.runner(
                [
                    "sf", "data", "query", "--target-org", self.partial_alias,
                    "--use-tooling-api", "--query", query, "--json",
                ],
                self.timeout_seconds,
            )
            if completed.returncode != 0:
                raise ValueError(f"Flow activation query failed: {_failure_detail(completed)}")
            envelope = json.loads(completed.stdout)
            rows = envelope.get("result", {}).get("records")
            if not isinstance(rows, list) or len(rows) != 1:
                raise ValueError("FlowDefinition query did not return exactly one Flow")
            row = rows[0]
            active = row.get("ActiveVersion", {}).get("VersionNumber")
            latest = row.get("LatestVersion", {}).get("VersionNumber")
            if (
                row.get("DeveloperName") != name
                or isinstance(active, bool)
                or not isinstance(active, int)
                or isinstance(latest, bool)
                or not isinstance(latest, int)
                or active != latest
            ):
                raise ValueError("Flow active version does not equal its latest version")
        except (ValueError, OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            return Outcome.failed(f"Flow activation verification refused: {exc}")

        result = {
            "environment": "partial",
            "target_alias": self.partial_alias,
            "target_org_id": partial["org_id"],
            "name": name,
            "active_version": active,
            "latest_version": latest,
            "deploy_work_id": request.deploy_work_id,
            "source_work_id": request.source_work_id,
        }
        evidence = [
            {
                "kind": VERIFY_FLOW_ACTIVATION_ACTION,
                "environment": "partial",
                "name": name,
                "active_version": active,
                "latest_version": latest,
                "active_equals_latest": True,
                "production_execution": False,
            }
        ]
        return Outcome.success(result, evidence)


class RunCreatedFlow:
    """Execute only an activation-proven Atlas-created Flow and assert one output."""

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
            request = RunCreatedFlowRequest.from_payload(item.payload)
            activation = self.store.get(request.activation_work_id)
            if (
                activation is None
                or activation.state is not WorkState.SUCCEEDED
                or activation.action != VERIFY_FLOW_ACTIVATION_ACTION
            ):
                raise ValueError("activation receipt is missing or unsuccessful")
            name = str(activation.result.get("name", ""))
            if not _API_NAME_RE.fullmatch(name) or not self.partial_alias:
                raise ValueError("activation receipt or Partial alias is invalid")
            partial = require_partial_target(
                self.runner, self.partial_alias, self.timeout_seconds
            )
            script = _runtime_script(name, request.output_variable, request.expected_string)
            self.artifact_root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=self.artifact_root) as temp_dir:
                script_path = Path(temp_dir) / "run.apex"
                script_path.write_text(script, encoding="utf-8")
                completed = self.runner(
                    [
                        "sf", "apex", "run", "--file", str(script_path),
                        "--target-org", self.partial_alias, "--json",
                    ],
                    self.timeout_seconds,
                )
            if completed.returncode != 0:
                raise ValueError(f"Flow runtime execution failed: {_failure_detail(completed)}")
            envelope = json.loads(completed.stdout)
            result = envelope.get("result", {})
            if result.get("compiled") is not True or result.get("success") is not True:
                raise ValueError("Flow runtime Apex did not compile and execute successfully")
            logs = result.get("logs")
            marker = f"ATLAS_FLOW_RESULT={request.expected_string}"
            if not isinstance(logs, str) or re.search(
                rf"\|USER_DEBUG\|[^\n]*\|DEBUG\|{re.escape(marker)}(?:\r?$)", logs, re.M
            ) is None:
                raise ValueError("Flow runtime output marker did not match")
            log_sha = hashlib.sha256(logs.encode()).hexdigest()
        except (ValueError, OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            return Outcome.failed(f"created Flow execution refused: {exc}")

        result_payload = {
            "environment": "partial",
            "target_alias": self.partial_alias,
            "target_org_id": partial["org_id"],
            "name": name,
            "output_variable": request.output_variable,
            "output_value": request.expected_string,
            "log_sha256": log_sha,
            "activation_work_id": request.activation_work_id,
        }
        evidence = [
            {
                "kind": RUN_CREATED_FLOW_ACTION,
                "environment": "partial",
                "name": name,
                "compiled": True,
                "executed": True,
                "output_variable": request.output_variable,
                "output_value": request.expected_string,
                "log_sha256": log_sha,
                "production_execution": False,
            }
        ]
        return Outcome.success(result_payload, evidence)


def _prove_delivery_lineage(store: Store, deploy: WorkItem, source_work_id: str) -> None:
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
    source_ids = set()
    for commit_id in commit_ids:
        commit = store.get(str(commit_id))
        if commit is None or commit.action != COMMIT_SOURCE_ACTION:
            raise ValueError("open PR lineage contains a non-source commit")
        values = commit.result.get("source_work_ids")
        if isinstance(values, list):
            source_ids.update(str(value) for value in values)
    if source_work_id not in source_ids:
        raise ValueError("Flow source is not in the deployed PR lineage")


def _runtime_script(name: str, variable: str, value: str) -> str:
    return (
        "Flow.Interview interview = Flow.Interview.createInterview("
        f"'{name}', new Map<String, Object>());\n"
        "interview.start();\n"
        f"String result = (String) interview.getVariableValue('{variable}');\n"
        f"System.assertEquals('{value}', result, 'Atlas Flow output must match');\n"
        f"System.debug('ATLAS_FLOW_RESULT={value}');\n"
    )

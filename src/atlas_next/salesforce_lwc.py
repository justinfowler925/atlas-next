from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from typing import Any

from .delivery import (
    COMMIT_SOURCE_ACTION,
    MERGE_PR_ACTION,
    OPEN_PR_ACTION,
    VERIFY_PR_ACTION,
    VERIFY_SANDBOX_DEPLOY_ACTION,
)
from .engine import Outcome
from .lwc_source import CREATE_LWC_SOURCE_ACTION
from .source_author import AUTHOR_SOURCE_ACTION
from .models import WorkItem, WorkState
from .salesforce import CommandRunner, _failure_detail, run_command
from .store import Store


VERIFY_LWC_DEPLOYMENT_ACTION = "salesforce.verify_lwc_deployment"
_LWC_NAME_RE = re.compile(r"^[a-z][A-Za-z0-9]{1,79}$")


@dataclass(frozen=True)
class VerifyLwcDeploymentRequest:
    deploy_work_id: str
    source_work_id: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> VerifyLwcDeploymentRequest:
        if set(payload) != {"deploy_work_id", "source_work_id"}:
            raise ValueError("payload must contain only deploy_work_id and source_work_id")
        deploy = payload["deploy_work_id"]
        source = payload["source_work_id"]
        if not isinstance(deploy, str) or not deploy or not isinstance(source, str) or not source:
            raise ValueError("work item ids must be non-empty text")
        return cls(deploy, source)


class VerifyLwcDeployment:
    """Prove one Atlas-created LWC passed Jest and exists in live Partial metadata."""

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
            request = VerifyLwcDeploymentRequest.from_payload(item.payload)
            deploy = self.store.get(request.deploy_work_id)
            source = self.store.get(request.source_work_id)
            if (
                deploy is None
                or deploy.state is not WorkState.SUCCEEDED
                or deploy.action != VERIFY_SANDBOX_DEPLOY_ACTION
            ):
                raise ValueError("deploy receipt is missing or unsuccessful")
            if source is None or source.state is not WorkState.SUCCEEDED:
                raise ValueError("LWC source receipt is missing or unsuccessful")
            if source.action == CREATE_LWC_SOURCE_ACTION:
                created = source
            elif source.action == AUTHOR_SOURCE_ACTION:
                created = self.store.get(str(source.result.get("retrieve_work_id", "")))
                if (
                    created is None
                    or created.state is not WorkState.SUCCEEDED
                    or created.action != CREATE_LWC_SOURCE_ACTION
                ):
                    raise ValueError("authored LWC has no governed creation parent")
            else:
                raise ValueError("source receipt is not a governed LWC source")
            verified = _prove_lwc_lineage(self.store, deploy, request.source_work_id)
            checks = verified.result.get("checks")
            if not isinstance(checks, dict) or checks.get("LWC unit tests") != "SUCCESS":
                raise ValueError("deployed PR has no successful LWC unit test receipt")
            name = str(source.result.get("name", ""))
            if name != created.result.get("name"):
                raise ValueError("authored LWC name differs from its creation receipt")
            if not _LWC_NAME_RE.fullmatch(name) or not self.partial_alias:
                raise ValueError("LWC source or Partial alias is invalid")
            completed = self.runner(
                [
                    "sf", "org", "list", "metadata", "--metadata-type",
                    "LightningComponentBundle", "--target-org", self.partial_alias, "--json",
                ],
                self.timeout_seconds,
            )
            if completed.returncode != 0:
                raise ValueError(f"LWC metadata inventory failed: {_failure_detail(completed)}")
            envelope = json.loads(completed.stdout)
            rows = envelope.get("result")
            if not isinstance(rows, list):
                raise ValueError("LWC metadata inventory is not a list")
            matches = [
                row
                for row in rows
                if isinstance(row, dict)
                and row.get("type") == "LightningComponentBundle"
                and row.get("fullName") == name
            ]
            if len(matches) != 1:
                raise ValueError("live Partial metadata does not contain exactly one LWC bundle")
            component_id = matches[0].get("id")
            if not isinstance(component_id, str) or not component_id:
                raise ValueError("live LWC metadata receipt has no component ID")
        except (ValueError, OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            return Outcome.failed(f"LWC deployment verification refused: {exc}")

        result = {
            "environment": "partial",
            "target_alias": self.partial_alias,
            "name": name,
            "component_id": component_id,
            "jest_gate": "SUCCESS",
            "deploy_work_id": request.deploy_work_id,
            "source_work_id": request.source_work_id,
        }
        evidence = [
            {
                "kind": VERIFY_LWC_DEPLOYMENT_ACTION,
                "environment": "partial",
                "name": name,
                "component_id": component_id,
                "jest_gate": "SUCCESS",
                "live_metadata_present": True,
                "production_execution": False,
            }
        ]
        return Outcome.success(result, evidence)


def _prove_lwc_lineage(store: Store, deploy: WorkItem, source_work_id: str) -> WorkItem:
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
        raise ValueError("LWC source is not in the deployed PR lineage")
    return verified

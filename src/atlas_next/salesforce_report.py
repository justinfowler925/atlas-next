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
from .models import WorkItem, WorkState
from .report_source import CREATE_REPORT_SOURCE_ACTION
from .salesforce import CommandRunner, _failure_detail, run_command
from .store import Store


VERIFY_REPORT_EXECUTION_ACTION = "salesforce.verify_report_execution"
_REPORT_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{1,79}$")
_REPORT_ID_RE = re.compile(r"^00O[A-Za-z0-9]{12}(?:[A-Za-z0-9]{3})?$")
_ALLOWED_FORMATS = frozenset({"TABULAR", "SUMMARY", "MATRIX"})


@dataclass(frozen=True)
class VerifyReportExecutionRequest:
    deploy_work_id: str
    source_work_id: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> VerifyReportExecutionRequest:
        if set(payload) != {"deploy_work_id", "source_work_id"}:
            raise ValueError("payload must contain only deploy_work_id and source_work_id")
        deploy = payload["deploy_work_id"]
        source = payload["source_work_id"]
        if not isinstance(deploy, str) or not deploy or not isinstance(source, str) or not source:
            raise ValueError("work item ids must be non-empty text")
        return cls(deploy, source)


class VerifyReportExecution:
    """Prove one Atlas-created report exists and executes in live Partial."""

    def __init__(
        self,
        store: Store,
        *,
        partial_alias: str,
        runner: CommandRunner = run_command,
        timeout_seconds: float = 120,
        api_version: str = "v65.0",
    ) -> None:
        self.store = store
        self.partial_alias = partial_alias.strip()
        self.runner = runner
        self.timeout_seconds = timeout_seconds
        self.api_version = api_version

    def __call__(self, item: WorkItem) -> Outcome:
        try:
            request = VerifyReportExecutionRequest.from_payload(item.payload)
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
                or source.action != CREATE_REPORT_SOURCE_ACTION
            ):
                raise ValueError("report source receipt is missing or unsuccessful")
            _prove_report_lineage(self.store, deploy, request.source_work_id)
            name = str(source.result.get("name", ""))
            if not _REPORT_NAME_RE.fullmatch(name) or not self.partial_alias:
                raise ValueError("report source or Partial alias is invalid")

            query = (
                "SELECT Id, DeveloperName, Name, FolderName FROM Report "
                f"WHERE DeveloperName = '{name}'"
            )
            query_result = self.runner(
                [
                    "sf",
                    "data",
                    "query",
                    "--target-org",
                    self.partial_alias,
                    "--query",
                    query,
                    "--json",
                ],
                self.timeout_seconds,
            )
            if query_result.returncode != 0:
                raise ValueError(f"report identity query failed: {_failure_detail(query_result)}")
            query_envelope = json.loads(query_result.stdout)
            rows = query_envelope.get("result", {}).get("records")
            if not isinstance(rows, list) or len(rows) != 1:
                raise ValueError("live Partial does not contain exactly one matching report")
            report_id = rows[0].get("Id")
            if (
                not isinstance(report_id, str)
                or not _REPORT_ID_RE.fullmatch(report_id)
                or rows[0].get("DeveloperName") != name
            ):
                raise ValueError("live report identity is invalid")

            runtime = self.runner(
                [
                    "sf",
                    "api",
                    "request",
                    "rest",
                    f"/services/data/{self.api_version}/analytics/reports/"
                    f"{report_id}?includeDetails=false",
                    "--target-org",
                    self.partial_alias,
                ],
                self.timeout_seconds,
            )
            if runtime.returncode != 0:
                raise ValueError(f"report execution failed: {_failure_detail(runtime)}")
            execution = json.loads(runtime.stdout)
            metadata = execution.get("reportMetadata")
            attributes = execution.get("attributes")
            if not isinstance(metadata, dict) or not isinstance(attributes, dict):
                raise ValueError("report execution omitted metadata or attributes")
            columns = metadata.get("detailColumns")
            report_format = metadata.get("reportFormat")
            report_type = metadata.get("reportType")
            if (
                attributes.get("reportId") != report_id
                or metadata.get("id") != report_id
                or metadata.get("developerName") != name
                or not isinstance(columns, list)
                or not 1 <= len(columns) <= 12
                or any(not isinstance(column, str) or not column for column in columns)
                or report_format not in _ALLOWED_FORMATS
                or not isinstance(report_type, dict)
                or not isinstance(report_type.get("type"), str)
                or not report_type.get("type")
            ):
                raise ValueError("executed report metadata does not match its governed source")
            fact = execution.get("factMap", {}).get("T!T")
            aggregates = fact.get("aggregates") if isinstance(fact, dict) else None
            configured_aggregates = metadata.get("aggregates")
            if not isinstance(aggregates, list) or not isinstance(configured_aggregates, list):
                raise ValueError("report execution omitted aggregate results")
            row_count = _row_count(configured_aggregates, aggregates)
        except (ValueError, OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            return Outcome.failed(f"report execution verification refused: {exc}")

        result = {
            "environment": "partial",
            "target_alias": self.partial_alias,
            "name": name,
            "report_id": report_id,
            "report_format": report_format,
            "report_type": report_type["type"],
            "columns": columns,
            "row_count": row_count,
            "deploy_work_id": request.deploy_work_id,
            "source_work_id": request.source_work_id,
        }
        evidence = [
            {
                "kind": VERIFY_REPORT_EXECUTION_ACTION,
                "environment": "partial",
                "name": name,
                "report_id": report_id,
                "report_format": report_format,
                "column_count": len(columns),
                "row_count": row_count,
                "executed": True,
                "production_execution": False,
            }
        ]
        return Outcome.success(result, evidence)


def _row_count(configured: list[Any], values: list[Any]) -> int:
    try:
        index = configured.index("RowCount")
        value = values[index]["value"]
    except (ValueError, IndexError, KeyError, TypeError) as exc:
        raise ValueError("report execution has no RowCount aggregate") from exc
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("report RowCount aggregate is invalid")
    return value


def _prove_report_lineage(store: Store, deploy: WorkItem, source_work_id: str) -> None:
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
        raise ValueError("report source is not in the deployed PR lineage")

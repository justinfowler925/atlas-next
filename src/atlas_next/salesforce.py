from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .engine import Outcome
from .models import WorkItem


DESCRIBE_ACTION = "salesforce.describe"
COUNT_ACTION = "salesforce.count"
PICKLIST_COUNTS_ACTION = "salesforce.picklist_counts"
# Compatibility for the first admitted capability.
ACTION = DESCRIBE_ACTION
_ENVIRONMENTS = frozenset({"partial", "prod"})
_OBJECT_API_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,79}$")
_PAYLOAD_KEYS = frozenset({"environment", "object"})
_PICKLIST_PAYLOAD_KEYS = frozenset({"environment", "object", "field"})


@dataclass(frozen=True)
class DescribeRequest:
    environment: str
    object_api: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> DescribeRequest:
        unexpected = sorted(set(payload) - _PAYLOAD_KEYS)
        missing = sorted(_PAYLOAD_KEYS - set(payload))
        if unexpected or missing:
            detail: list[str] = []
            if missing:
                detail.append(f"missing keys: {', '.join(missing)}")
            if unexpected:
                detail.append(f"unexpected keys: {', '.join(unexpected)}")
            raise ValueError("invalid salesforce.describe payload (" + "; ".join(detail) + ")")
        environment = str(payload["environment"]).strip().lower()
        object_api = str(payload["object"]).strip()
        if environment not in _ENVIRONMENTS:
            raise ValueError("environment must be exactly 'partial' or 'prod'")
        if not _OBJECT_API_RE.fullmatch(object_api):
            raise ValueError("object must be one Salesforce object API name")
        return cls(environment=environment, object_api=object_api)


@dataclass(frozen=True)
class CountRequest:
    environment: str
    object_api: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> CountRequest:
        described = DescribeRequest.from_payload(payload)
        return cls(environment=described.environment, object_api=described.object_api)


@dataclass(frozen=True)
class PicklistCountsRequest:
    environment: str
    object_api: str
    field_api: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> PicklistCountsRequest:
        unexpected = sorted(set(payload) - _PICKLIST_PAYLOAD_KEYS)
        missing = sorted(_PICKLIST_PAYLOAD_KEYS - set(payload))
        if unexpected or missing:
            detail: list[str] = []
            if missing:
                detail.append(f"missing keys: {', '.join(missing)}")
            if unexpected:
                detail.append(f"unexpected keys: {', '.join(unexpected)}")
            raise ValueError(
                "invalid salesforce.picklist_counts payload (" + "; ".join(detail) + ")"
            )
        base = DescribeRequest.from_payload(
            {"environment": payload["environment"], "object": payload["object"]}
        )
        field_api = str(payload["field"]).strip()
        if not _OBJECT_API_RE.fullmatch(field_api):
            raise ValueError("field must be one Salesforce field API name")
        return cls(base.environment, base.object_api, field_api)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


CommandRunner = Callable[[Sequence[str], float], CommandResult]


def run_command(argv: Sequence[str], timeout_seconds: float) -> CommandResult:
    completed = subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


class SalesforceDescribe:
    """One hardcoded read-only Salesforce capability.

    The caller supplies aliases, but never a command or SOQL string. The only
    subprocess shape this class can emit is ``sf sobject describe ... --json``.
    """

    def __init__(
        self,
        targets: Mapping[str, str],
        *,
        runner: CommandRunner = run_command,
        timeout_seconds: float = 60,
    ) -> None:
        self.targets = {key: value.strip() for key, value in targets.items()}
        self.runner = runner
        self.timeout_seconds = timeout_seconds

    def __call__(self, item: WorkItem) -> Outcome:
        try:
            request = DescribeRequest.from_payload(item.payload)
            target = self.targets.get(request.environment, "")
            if not target:
                raise ValueError(f"no target alias configured for {request.environment}")
            argv = [
                "sf",
                "sobject",
                "describe",
                "--sobject",
                request.object_api,
                "--target-org",
                target,
                "--json",
            ]
            completed = self.runner(argv, self.timeout_seconds)
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            return Outcome.failed(f"salesforce describe refused: {exc}")

        if completed.returncode != 0:
            detail = _failure_detail(completed)
            return Outcome.failed(
                f"salesforce describe failed for {request.object_api}@{target}: {detail}"
            )
        try:
            envelope = json.loads(completed.stdout)
            if int(envelope.get("status", 0)) != 0:
                raise ValueError(_json_error(envelope) or "Salesforce CLI returned non-zero status")
            describe = envelope["result"]
            fields = describe["fields"]
            if not isinstance(fields, list) or not fields:
                raise ValueError("describe returned zero fields")
            field_names = [str(field["name"]) for field in fields]
            if len(field_names) != len(set(field_names)):
                raise ValueError("describe returned duplicate field API names")
            actual_name = str(describe.get("name") or "")
            if actual_name.lower() != request.object_api.lower():
                raise ValueError(
                    f"describe returned object {actual_name!r}, expected {request.object_api!r}"
                )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return Outcome.failed(f"salesforce describe response rejected: {exc}")

        custom_count = sum(name.endswith("__c") for name in field_names)
        result = {
            "environment": request.environment,
            "target_alias": target,
            "object": actual_name,
            "label": str(describe.get("label") or ""),
            "field_count": len(field_names),
            "custom_field_count": custom_count,
            "key_prefix": describe.get("keyPrefix"),
            "queryable": bool(describe.get("queryable")),
        }
        evidence = [
            {
                "kind": "salesforce.sobject.describe",
                "environment": request.environment,
                "target_alias": target,
                "object": actual_name,
                "field_count": len(field_names),
                "key_prefix": describe.get("keyPrefix"),
                "transport": "salesforce-cli-json",
                "read_only_command": True,
            }
        ]
        return Outcome.success(result, evidence)


class SalesforceCount:
    """Count every record in one validated object with generated SOQL only."""

    def __init__(
        self,
        targets: Mapping[str, str],
        *,
        runner: CommandRunner = run_command,
        timeout_seconds: float = 60,
    ) -> None:
        self.targets = {key: value.strip() for key, value in targets.items()}
        self.runner = runner
        self.timeout_seconds = timeout_seconds

    def __call__(self, item: WorkItem) -> Outcome:
        try:
            request = CountRequest.from_payload(item.payload)
            target = self.targets.get(request.environment, "")
            if not target:
                raise ValueError(f"no target alias configured for {request.environment}")
            soql = f"SELECT COUNT() FROM {request.object_api}"
            argv = [
                "sf",
                "data",
                "query",
                "--query",
                soql,
                "--target-org",
                target,
                "--json",
            ]
            completed = self.runner(argv, self.timeout_seconds)
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            return Outcome.failed(f"salesforce count refused: {exc}")

        if completed.returncode != 0:
            detail = _failure_detail(completed)
            return Outcome.failed(
                f"salesforce count failed for {request.object_api}@{target}: {detail}"
            )
        try:
            envelope = json.loads(completed.stdout)
            if int(envelope.get("status", 0)) != 0:
                raise ValueError(_json_error(envelope) or "Salesforce CLI returned non-zero status")
            query_result = envelope["result"]
            count = query_result["totalSize"]
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError("count result must be a non-negative integer")
            if query_result.get("done") is not True:
                raise ValueError("count query did not finish")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return Outcome.failed(f"salesforce count response rejected: {exc}")

        result = {
            "environment": request.environment,
            "target_alias": target,
            "object": request.object_api,
            "count": count,
        }
        evidence = [
            {
                "kind": "salesforce.data.count",
                "environment": request.environment,
                "target_alias": target,
                "object": request.object_api,
                "count": count,
                "transport": "salesforce-cli-json",
                "read_only_command": True,
                "query_shape": "SELECT COUNT() FROM <validated_object>",
            }
        ]
        return Outcome.success(result, evidence)


class SalesforcePicklistCounts:
    """Return a capped distribution for one live-validated picklist field."""

    def __init__(
        self,
        targets: Mapping[str, str],
        *,
        runner: CommandRunner = run_command,
        timeout_seconds: float = 60,
    ) -> None:
        self.targets = {key: value.strip() for key, value in targets.items()}
        self.runner = runner
        self.timeout_seconds = timeout_seconds

    def __call__(self, item: WorkItem) -> Outcome:
        try:
            request = PicklistCountsRequest.from_payload(item.payload)
            target = self.targets.get(request.environment, "")
            if not target:
                raise ValueError(f"no target alias configured for {request.environment}")
            describe_command = [
                "sf",
                "sobject",
                "describe",
                "--sobject",
                request.object_api,
                "--target-org",
                target,
                "--json",
            ]
            described = self.runner(describe_command, self.timeout_seconds)
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            return Outcome.failed(f"salesforce picklist counts refused: {exc}")

        if described.returncode != 0:
            return Outcome.failed(
                f"salesforce describe failed for {request.object_api}@{target}: "
                f"{_failure_detail(described)}"
            )
        try:
            envelope = json.loads(described.stdout)
            if int(envelope.get("status", 0)) != 0:
                raise ValueError(_json_error(envelope) or "Salesforce CLI returned non-zero status")
            fields = envelope["result"]["fields"]
            if not isinstance(fields, list) or not all(isinstance(field, dict) for field in fields):
                raise ValueError("describe fields must be a list of objects")
            matches = [field for field in fields if field.get("name") == request.field_api]
            if len(matches) != 1:
                raise ValueError(
                    f"field {request.field_api!r} was not present exactly once in live describe"
                )
            field = matches[0]
            if field.get("type") != "picklist":
                raise ValueError(
                    f"field {request.field_api!r} is type {field.get('type')!r}, not 'picklist'"
                )
            if field.get("groupable") is not True:
                raise ValueError(f"field {request.field_api!r} is not groupable")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return Outcome.failed(f"salesforce picklist describe rejected: {exc}")

        soql = (
            f"SELECT {request.field_api}, COUNT(Id) recordCount "
            f"FROM {request.object_api} GROUP BY {request.field_api} "
            "ORDER BY COUNT(Id) DESC LIMIT 50"
        )
        query_command = [
            "sf",
            "data",
            "query",
            "--query",
            soql,
            "--target-org",
            target,
            "--json",
        ]
        try:
            queried = self.runner(query_command, self.timeout_seconds)
        except (OSError, subprocess.SubprocessError) as exc:
            return Outcome.failed(f"salesforce picklist query failed: {exc}")
        if queried.returncode != 0:
            return Outcome.failed(
                f"salesforce picklist query failed for "
                f"{request.object_api}.{request.field_api}@{target}: {_failure_detail(queried)}"
            )
        try:
            envelope = json.loads(queried.stdout)
            if int(envelope.get("status", 0)) != 0:
                raise ValueError(_json_error(envelope) or "Salesforce CLI returned non-zero status")
            query_result = envelope["result"]
            if query_result.get("done") is not True:
                raise ValueError("aggregate query did not finish")
            records = query_result["records"]
            if not isinstance(records, list) or len(records) > 50:
                raise ValueError("aggregate result must contain at most 50 groups")
            groups: list[dict[str, Any]] = []
            for record in records:
                if not isinstance(record, dict):
                    raise ValueError("picklist group must be an object")
                value = record.get(request.field_api)
                count = record.get("recordCount")
                if value is not None and not isinstance(value, str):
                    raise ValueError("picklist group value must be text or null")
                if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                    raise ValueError("picklist group count must be a non-negative integer")
                groups.append({"value": value, "count": count})
            total_size = query_result["totalSize"]
            if isinstance(total_size, bool) or not isinstance(total_size, int):
                raise ValueError("aggregate totalSize must be an integer")
            if total_size != len(groups):
                raise ValueError("aggregate totalSize did not match returned groups")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return Outcome.failed(f"salesforce picklist response rejected: {exc}")

        record_count = sum(group["count"] for group in groups)
        result = {
            "environment": request.environment,
            "target_alias": target,
            "object": request.object_api,
            "field": request.field_api,
            "groups": groups,
            "group_count": len(groups),
            "record_count": record_count,
        }
        evidence = [
            {
                "kind": "salesforce.picklist_counts",
                "environment": request.environment,
                "target_alias": target,
                "object": request.object_api,
                "field": request.field_api,
                "field_type": "picklist",
                "group_count": len(groups),
                "record_count": record_count,
                "group_limit": 50,
                "read_only_command": True,
            }
        ]
        return Outcome.success(result, evidence)


def _failure_detail(result: CommandResult) -> str:
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError:
        parsed = None
    detail = _json_error(parsed) if isinstance(parsed, dict) else ""
    return detail or result.stderr.strip()[:500] or result.stdout.strip()[:500] or "unknown error"


def _json_error(envelope: dict[str, Any]) -> str:
    message = envelope.get("message")
    if message:
        return str(message)[:500]
    result = envelope.get("result")
    if isinstance(result, dict):
        return str(result.get("message") or result.get("error") or "")[:500]
    return ""

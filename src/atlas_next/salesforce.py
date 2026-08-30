from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .engine import Outcome
from .models import WorkItem


ACTION = "salesforce.describe"
_ENVIRONMENTS = frozenset({"partial", "prod"})
_OBJECT_API_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,79}$")
_PAYLOAD_KEYS = frozenset({"environment", "object"})


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

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from typing import Any

from .engine import Outcome
from .models import WorkItem
from .salesforce import CommandRunner, _failure_detail, _json_error, run_command


METADATA_DIFF_ACTION = "salesforce.metadata_diff"
SUPPORTED_METADATA_TYPES = frozenset(
    {
        "ApexClass",
        "ApexTrigger",
        "CustomField",
        "CustomObject",
        "CustomPermission",
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

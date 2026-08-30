from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .engine import Outcome
from .models import WorkItem, WorkState
from .salesforce import CommandRunner, _failure_detail, run_command
from .salesforce_query import _parse_describe
from .store import Store


UPDATE_RECORDS_ACTION = "salesforce.update_records"
RECONCILE_UPDATE_ACTION = "salesforce.reconcile_update"
ROLLBACK_UPDATE_ACTION = "salesforce.rollback_update"
_API_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,79}$")
_ID_RE = re.compile(r"^[A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?$")
_REASON_RE = re.compile(r"^[^\r\n]{10,200}$")
_SUPPORTED_TYPES = {
    "boolean",
    "currency",
    "date",
    "datetime",
    "double",
    "email",
    "id",
    "int",
    "long",
    "percent",
    "phone",
    "picklist",
    "reference",
    "string",
    "textarea",
    "time",
    "url",
}


@dataclass(frozen=True)
class RecordUpdate:
    record_id: str
    fields: dict[str, Any]


@dataclass(frozen=True)
class UpdateRecordsRequest:
    object_api: str
    records: tuple[RecordUpdate, ...]
    reason: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> UpdateRecordsRequest:
        if set(payload) != {"object", "records", "reason"}:
            raise ValueError("payload must contain only object, records, and reason")
        object_api = payload["object"]
        reason = payload["reason"]
        if not isinstance(object_api, str) or not _API_NAME_RE.fullmatch(object_api):
            raise ValueError("object must be one Salesforce object API name")
        if not isinstance(reason, str) or not _REASON_RE.fullmatch(reason):
            raise ValueError("reason must be one line containing 10 to 200 characters")
        raw_records = payload["records"]
        if not isinstance(raw_records, list) or not 1 <= len(raw_records) <= 10:
            raise ValueError("records must contain 1 to 10 updates")
        records = []
        seen_ids = set()
        for raw in raw_records:
            if not isinstance(raw, dict) or set(raw) != {"id", "fields"}:
                raise ValueError("each update must contain exactly id and fields")
            record_id = raw["id"]
            fields = raw["fields"]
            if not isinstance(record_id, str) or not _ID_RE.fullmatch(record_id):
                raise ValueError("every record id must be one 15- or 18-character Salesforce ID")
            if record_id in seen_ids:
                raise ValueError("record ids must not be duplicated")
            if not isinstance(fields, dict) or not 1 <= len(fields) <= 8:
                raise ValueError("fields must contain 1 to 8 field updates")
            if any(not isinstance(name, str) or not _API_NAME_RE.fullmatch(name) for name in fields):
                raise ValueError("every field must be one Salesforce field API name")
            if "Id" in fields:
                raise ValueError("record Id cannot be updated")
            for value in fields.values():
                _validate_scalar_shape(value)
            seen_ids.add(record_id)
            records.append(RecordUpdate(record_id, fields))
        return cls(object_api, tuple(records), reason)


@dataclass(frozen=True)
class RollbackUpdateRequest:
    update_work_id: str
    reason: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> RollbackUpdateRequest:
        if set(payload) != {"update_work_id", "reason"}:
            raise ValueError("payload must contain only update_work_id and reason")
        work_id = payload["update_work_id"]
        reason = payload["reason"]
        if not isinstance(work_id, str) or not work_id:
            raise ValueError("update_work_id must be non-empty text")
        if not isinstance(reason, str) or not _REASON_RE.fullmatch(reason):
            raise ValueError("reason must be one line containing 10 to 200 characters")
        return cls(work_id, reason)


@dataclass(frozen=True)
class ReconcileUpdateRequest:
    update_work_id: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ReconcileUpdateRequest:
        if set(payload) != {"update_work_id"}:
            raise ValueError("payload must contain only update_work_id")
        work_id = payload["update_work_id"]
        if not isinstance(work_id, str) or not work_id:
            raise ValueError("update_work_id must be non-empty text")
        return cls(work_id)


class SalesforceUpdateRecords:
    """Atomically update a bounded Partial record set with before/after receipts."""

    def __init__(
        self,
        *,
        partial_alias: str,
        artifact_root: Path,
        runner: CommandRunner = run_command,
        timeout_seconds: float = 120,
    ) -> None:
        self.partial_alias = partial_alias.strip()
        self.artifact_root = artifact_root.resolve()
        self.runner = runner
        self.timeout_seconds = timeout_seconds

    def __call__(self, item: WorkItem) -> Outcome:
        patch_applied = False
        try:
            request = UpdateRecordsRequest.from_payload(item.payload)
            if not self.partial_alias:
                raise ValueError("Partial target alias is required")
            described = self._run(
                [
                    "sf", "sobject", "describe", "--sobject", request.object_api,
                    "--target-org", self.partial_alias, "--json",
                ]
            )
            fields = _parse_describe(described.stdout, request.object_api)
            key_prefix = json.loads(described.stdout)["result"].get("keyPrefix")
            _validate_updates(request, fields, key_prefix)
            field_names = sorted({name for record in request.records for name in record.fields})
            ids = [record.record_id for record in request.records]
            before = self._query(request.object_api, ids, field_names)
            expected = {
                record.record_id: {**before[record.record_id], **record.fields}
                for record in request.records
            }
            response = self._composite_patch(request.object_api, request.records)
            _validate_patch_response(response, ids)
            patch_applied = True
            after = self._query(request.object_api, ids, field_names)
            _require_expected(after, expected, field_names)
        except (ValueError, OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            if patch_applied:
                recovery = {
                    "after": expected,
                    "after_sha256": _records_hash(expected),
                    "before": before,
                    "before_sha256": _records_hash(before),
                    "environment": "partial",
                    "fields": field_names,
                    "kind": "salesforce.update_records_reconciliation_required",
                    "object": request.object_api,
                    "patch_response_validated": True,
                    "production_execution": False,
                    "reason": request.reason,
                    "record_ids": ids,
                }
                return Outcome.blocked(
                    f"Salesforce PATCH succeeded but postcondition needs reconciliation: {exc}",
                    [recovery],
                )
            return Outcome.failed(f"Salesforce record update refused: {exc}")

        before_sha = _records_hash(before)
        after_sha = _records_hash(after)
        result = {
            "environment": "partial",
            "target_alias": self.partial_alias,
            "object": request.object_api,
            "record_ids": ids,
            "fields": field_names,
            "record_count": len(ids),
            "before": before,
            "after": after,
            "before_sha256": before_sha,
            "after_sha256": after_sha,
            "reason": request.reason,
        }
        evidence = [
            {
                "kind": UPDATE_RECORDS_ACTION,
                "environment": "partial",
                "object": request.object_api,
                "record_count": len(ids),
                "field_count": len(field_names),
                "before_sha256": before_sha,
                "after_sha256": after_sha,
                "schema_validated": True,
                "all_or_none": True,
                "postcondition_verified": True,
                "production_execution": False,
            }
        ]
        return Outcome.success(result, evidence)

    def _run(self, argv: list[str]):
        completed = self.runner(argv, self.timeout_seconds)
        if completed.returncode != 0:
            raise ValueError(f"Salesforce command failed: {_failure_detail(completed)}")
        return completed

    def _query(self, object_api: str, ids: list[str], fields: list[str]) -> dict[str, dict[str, Any]]:
        soql = _record_query(object_api, ids, fields)
        completed = self._run(
            [
                "sf", "data", "query", "--query", soql,
                "--target-org", self.partial_alias, "--json",
            ]
        )
        return _parse_records(completed.stdout, ids, fields)

    def _composite_patch(self, object_api: str, records: tuple[RecordUpdate, ...]) -> Any:
        body = {
            "allOrNone": True,
            "records": [
                {"attributes": {"type": object_api}, "Id": record.record_id, **record.fields}
                for record in records
            ],
        }
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.artifact_root) as temp_dir:
            body_path = Path(temp_dir) / "request.json"
            body_path.write_text(json.dumps(body, sort_keys=True), encoding="utf-8")
            completed = self._run(
                [
                    "sf", "api", "request", "rest",
                    "/services/data/v65.0/composite/sobjects",
                    "--method", "PATCH", "--body", f"@{body_path}",
                    "--target-org", self.partial_alias,
                ]
            )
        return json.loads(completed.stdout)


class SalesforceReconcileUpdate(SalesforceUpdateRecords):
    """Resolve a PATCH-success/postcheck-unknown update without issuing another write."""

    def __init__(self, store: Store, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.store = store

    def __call__(self, item: WorkItem) -> Outcome:
        try:
            request = ReconcileUpdateRequest.from_payload(item.payload)
            updated = self.store.get(request.update_work_id)
            if (
                updated is None
                or updated.state is not WorkState.BLOCKED
                or updated.action != UPDATE_RECORDS_ACTION
            ):
                raise ValueError("blocked update receipt is missing")
            recovery = next(
                (
                    row
                    for row in updated.evidence
                    if row.get("kind")
                    == "salesforce.update_records_reconciliation_required"
                ),
                None,
            )
            if not isinstance(recovery, dict):
                raise ValueError("blocked update has no recovery evidence")
            object_api = recovery.get("object")
            ids = recovery.get("record_ids")
            fields = recovery.get("fields")
            before = recovery.get("before")
            expected = recovery.get("after")
            if (
                not isinstance(object_api, str)
                or not _API_NAME_RE.fullmatch(object_api)
                or not isinstance(ids, list)
                or not isinstance(fields, list)
                or not isinstance(before, dict)
                or not isinstance(expected, dict)
                or _records_hash(before) != recovery.get("before_sha256")
                or _records_hash(expected) != recovery.get("after_sha256")
            ):
                raise ValueError("blocked update recovery evidence is invalid")
            current = self._query(object_api, ids, fields)
            current_hash = _records_hash(current)
            before_hash = _records_hash(before)
            expected_hash = _records_hash(expected)
            if current_hash == expected_hash:
                disposition = "applied"
            elif current_hash == before_hash:
                disposition = "not_applied"
            else:
                return Outcome.blocked(
                    "Salesforce records differ from both before and intended states; "
                    "reconciliation would overwrite newer work",
                    [
                        {
                            "current_sha256": current_hash,
                            "environment": "partial",
                            "kind": RECONCILE_UPDATE_ACTION,
                            "object": object_api,
                            "production_execution": False,
                            "record_count": len(ids),
                            "status": "drifted",
                            "update_work_id": request.update_work_id,
                        }
                    ],
                )
        except (ValueError, OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            return Outcome.failed(f"Salesforce update reconciliation refused: {exc}")

        result = {
            "after": expected,
            "after_sha256": expected_hash,
            "before": before,
            "before_sha256": before_hash,
            "disposition": disposition,
            "environment": "partial",
            "fields": fields,
            "object": object_api,
            "record_count": len(ids),
            "record_ids": ids,
            "target_alias": self.partial_alias,
            "update_work_id": request.update_work_id,
        }
        evidence = [
            {
                "environment": "partial",
                "kind": RECONCILE_UPDATE_ACTION,
                "mutation_applied": disposition == "applied",
                "object": object_api,
                "postcondition_verified": True,
                "production_execution": False,
                "record_count": len(ids),
            }
        ]
        return Outcome.success(result, evidence)


class SalesforceRollbackUpdate(SalesforceUpdateRecords):
    """Rollback only a successful update whose post-state has not drifted."""

    def __init__(self, store: Store, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.store = store

    def __call__(self, item: WorkItem) -> Outcome:
        try:
            request = RollbackUpdateRequest.from_payload(item.payload)
            updated = self.store.get(request.update_work_id)
            if (
                updated is None
                or updated.state is not WorkState.SUCCEEDED
                or updated.action not in {UPDATE_RECORDS_ACTION, RECONCILE_UPDATE_ACTION}
            ):
                raise ValueError("update receipt is missing or unsuccessful")
            if (
                updated.action == RECONCILE_UPDATE_ACTION
                and updated.result.get("disposition") != "applied"
            ):
                raise ValueError("reconciled update was not applied and cannot be rolled back")
            object_api = str(updated.result.get("object", ""))
            ids = updated.result.get("record_ids")
            fields = updated.result.get("fields")
            before = updated.result.get("before")
            after = updated.result.get("after")
            if (
                not _API_NAME_RE.fullmatch(object_api)
                or not isinstance(ids, list)
                or not isinstance(fields, list)
                or not isinstance(before, dict)
                or not isinstance(after, dict)
            ):
                raise ValueError("update receipt is incomplete")
            current = self._query(object_api, ids, fields)
            if _records_hash(current) != updated.result.get("after_sha256"):
                raise ValueError("records drifted after update; rollback would overwrite newer work")
            records = tuple(
                RecordUpdate(record_id, {field: before[record_id].get(field) for field in fields})
                for record_id in ids
            )
            response = self._composite_patch(object_api, records)
            _validate_patch_response(response, ids)
            restored = self._query(object_api, ids, fields)
            if _records_hash(restored) != updated.result.get("before_sha256"):
                raise ValueError("rollback postcondition does not match the original before-state")
        except (ValueError, OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            return Outcome.failed(f"Salesforce record rollback refused: {exc}")

        result = {
            "environment": "partial",
            "target_alias": self.partial_alias,
            "object": object_api,
            "record_ids": ids,
            "record_count": len(ids),
            "restored_sha256": _records_hash(restored),
            "update_work_id": request.update_work_id,
            "reason": request.reason,
        }
        evidence = [
            {
                "kind": ROLLBACK_UPDATE_ACTION,
                "environment": "partial",
                "object": object_api,
                "record_count": len(ids),
                "drift_checked": True,
                "restored_before_state": True,
                "all_or_none": True,
                "production_execution": False,
            }
        ]
        return Outcome.success(result, evidence)


def _validate_scalar_shape(value: Any) -> None:
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("numeric values must be finite")
        return
    if isinstance(value, str) and len(value) <= 5000 and "\x00" not in value:
        return
    raise ValueError("field values must be null, boolean, finite numbers, or bounded text")


def _validate_updates(
    request: UpdateRecordsRequest, fields: dict[str, dict[str, Any]], key_prefix: Any
) -> None:
    if not isinstance(key_prefix, str) or len(key_prefix) != 3:
        raise ValueError("object describe returned no stable key prefix")
    for record in request.records:
        if not record.record_id.startswith(key_prefix):
            raise ValueError("record id does not match the described object key prefix")
        for name, value in record.fields.items():
            metadata = fields.get(name)
            if metadata is None or metadata.get("updateable") is not True:
                raise ValueError(f"field {name!r} does not exist or is not updateable")
            field_type = metadata.get("type")
            if field_type not in _SUPPORTED_TYPES or metadata.get("encrypted") is True:
                raise ValueError(f"field {name!r} has unsupported type or encryption")
            if value is None:
                if metadata.get("nillable") is not True:
                    raise ValueError(f"field {name!r} is not nillable")
                continue
            _validate_typed_value(name, value, metadata)


def _validate_typed_value(name: str, value: Any, metadata: dict[str, Any]) -> None:
    field_type = metadata["type"]
    if field_type == "boolean" and not isinstance(value, bool):
        raise ValueError(f"field {name!r} requires a boolean")
    if field_type in {"currency", "double", "percent"} and (
        isinstance(value, bool) or not isinstance(value, (int, float))
    ):
        raise ValueError(f"field {name!r} requires a number")
    if field_type in {"int", "long"} and (isinstance(value, bool) or not isinstance(value, int)):
        raise ValueError(f"field {name!r} requires an integer")
    if field_type in {"id", "reference"} and (
        not isinstance(value, str) or not _ID_RE.fullmatch(value)
    ):
        raise ValueError(f"field {name!r} requires a Salesforce ID")
    if field_type == "date" and (not isinstance(value, str) or not _is_date(value)):
        raise ValueError(f"field {name!r} requires an ISO date")
    if field_type == "datetime" and (not isinstance(value, str) or not _is_datetime(value)):
        raise ValueError(f"field {name!r} requires an ISO datetime")
    if field_type in {"string", "textarea", "email", "phone", "url", "picklist", "time"}:
        if not isinstance(value, str):
            raise ValueError(f"field {name!r} requires text")
        length = metadata.get("length")
        if isinstance(length, int) and length > 0 and len(value) > length:
            raise ValueError(f"field {name!r} exceeds its described length")
        if field_type == "picklist":
            allowed = {
                row.get("value")
                for row in metadata.get("picklistValues", [])
                if isinstance(row, dict) and row.get("active") is True
            }
            if value not in allowed:
                raise ValueError(f"field {name!r} value is not an active picklist option")


def _record_query(object_api: str, ids: list[str], fields: list[str]) -> str:
    rendered_ids = ", ".join(f"'{record_id}'" for record_id in sorted(ids))
    return f"SELECT Id, {', '.join(fields)} FROM {object_api} WHERE Id IN ({rendered_ids}) ORDER BY Id"


def _parse_records(stdout: str, ids: list[str], fields: list[str]) -> dict[str, dict[str, Any]]:
    envelope = json.loads(stdout)
    rows = envelope.get("result", {}).get("records")
    if not isinstance(rows, list) or len(rows) != len(ids):
        raise ValueError("record query did not return every exact target")
    parsed = {}
    for row in rows:
        record_id = row.get("Id") if isinstance(row, dict) else None
        if record_id not in ids or record_id in parsed:
            raise ValueError("record query returned an unexpected or duplicate ID")
        parsed[record_id] = {field: row.get(field) for field in fields}
    return dict(sorted(parsed.items()))


def _validate_patch_response(value: Any, ids: list[str]) -> None:
    if isinstance(value, dict) and "result" in value:
        value = value["result"]
    if not isinstance(value, list) or len(value) != len(ids):
        raise ValueError("composite update returned an invalid result population")
    returned = []
    for row in value:
        if not isinstance(row, dict) or row.get("success") is not True or row.get("errors") != []:
            raise ValueError("composite update did not succeed without errors")
        returned.append(row.get("id"))
    if sorted(returned) != sorted(ids):
        raise ValueError("composite update returned different record IDs")


def _require_expected(
    actual: dict[str, dict[str, Any]], expected: dict[str, dict[str, Any]], fields: list[str]
) -> None:
    for record_id, row in actual.items():
        for field in fields:
            if row.get(field) != expected[record_id].get(field):
                raise ValueError(f"postcondition mismatch for {record_id}.{field}")


def _records_hash(records: dict[str, dict[str, Any]]) -> str:
    return hashlib.sha256(
        json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _is_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
        return True
    except ValueError:
        return False


def _is_datetime(value: str) -> bool:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False

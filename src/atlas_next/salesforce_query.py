from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
from dataclasses import dataclass
from typing import Any

from .engine import Outcome
from .models import WorkItem
from .salesforce import CommandRunner, _failure_detail, _json_error, run_command


QUERY_ACTION = "salesforce.query"
_API_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,79}$")
_REQUIRED_KEYS = frozenset({"environment", "object", "fields"})
_OPTIONAL_KEYS = frozenset({"filters", "order_by", "limit"})
_FILTER_KEYS = frozenset({"field", "operator", "value"})
_ORDER_KEYS = frozenset({"field", "direction"})
_OPERATORS = {
    "eq": "=",
    "ne": "!=",
    "lt": "<",
    "lte": "<=",
    "gt": ">",
    "gte": ">=",
    "in": "IN",
}


@dataclass(frozen=True)
class Filter:
    field: str
    operator: str
    value: Any


@dataclass(frozen=True)
class Order:
    field: str
    direction: str


@dataclass(frozen=True)
class QueryRequest:
    environment: str
    object_api: str
    fields: tuple[str, ...]
    filters: tuple[Filter, ...]
    order_by: Order | None
    limit: int

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> QueryRequest:
        allowed = _REQUIRED_KEYS | _OPTIONAL_KEYS
        unexpected = sorted(set(payload) - allowed)
        missing = sorted(_REQUIRED_KEYS - set(payload))
        if unexpected or missing:
            details = []
            if missing:
                details.append(f"missing keys: {', '.join(missing)}")
            if unexpected:
                details.append(f"unexpected keys: {', '.join(unexpected)}")
            raise ValueError("invalid salesforce.query payload (" + "; ".join(details) + ")")

        environment = payload["environment"]
        object_api = payload["object"]
        if environment not in {"partial", "prod"}:
            raise ValueError("environment must be exactly 'partial' or 'prod'")
        _require_api_name(object_api, "object")

        raw_fields = payload["fields"]
        if not isinstance(raw_fields, list) or not 1 <= len(raw_fields) <= 12:
            raise ValueError("fields must be a list containing 1 to 12 API names")
        fields = tuple(_require_api_name(field, "field") for field in raw_fields)
        if len(fields) != len(set(fields)):
            raise ValueError("fields must not contain duplicates")

        raw_filters = payload.get("filters", [])
        if not isinstance(raw_filters, list) or len(raw_filters) > 5:
            raise ValueError("filters must be a list containing at most 5 entries")
        filters = tuple(_parse_filter(raw_filter) for raw_filter in raw_filters)

        raw_order = payload.get("order_by")
        order_by = None if raw_order is None else _parse_order(raw_order)
        limit = payload.get("limit", 100)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("limit must be an integer from 1 to 200")
        return cls(environment, object_api, fields, filters, order_by, limit)


class SalesforceQuery:
    """Run a bounded record query assembled only from validated structured input."""

    def __init__(
        self,
        targets: dict[str, str],
        *,
        runner: CommandRunner = run_command,
        timeout_seconds: float = 60,
    ) -> None:
        self.targets = {key: value.strip() for key, value in targets.items()}
        self.runner = runner
        self.timeout_seconds = timeout_seconds

    def __call__(self, item: WorkItem) -> Outcome:
        try:
            request = QueryRequest.from_payload(item.payload)
            target = self.targets.get(request.environment, "")
            if not target:
                raise ValueError(f"no target alias configured for {request.environment}")
            described = self.runner(
                [
                    "sf",
                    "sobject",
                    "describe",
                    "--sobject",
                    request.object_api,
                    "--target-org",
                    target,
                    "--json",
                ],
                self.timeout_seconds,
            )
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            return Outcome.failed(f"salesforce query refused: {exc}")
        if described.returncode != 0:
            return Outcome.failed(
                f"salesforce describe failed for {request.object_api}@{target}: "
                f"{_failure_detail(described)}"
            )

        try:
            metadata = _parse_describe(described.stdout, request.object_api)
            _validate_fields(request, metadata)
            soql = _build_soql(request)
            queried = self.runner(
                [
                    "sf",
                    "data",
                    "query",
                    "--query",
                    soql,
                    "--target-org",
                    target,
                    "--json",
                ],
                self.timeout_seconds,
            )
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            return Outcome.failed(f"salesforce query refused after live describe: {exc}")
        if queried.returncode != 0:
            return Outcome.failed(
                f"salesforce query failed for {request.object_api}@{target}: "
                f"{_failure_detail(queried)}"
            )

        try:
            records, total_size = _parse_query_response(queried.stdout, request)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return Outcome.failed(f"salesforce query response rejected: {exc}")

        fingerprint = hashlib.sha256(soql.encode()).hexdigest()
        result = {
            "environment": request.environment,
            "target_alias": target,
            "object": request.object_api,
            "fields": list(request.fields),
            "records": records,
            "record_count": len(records),
            "total_size": total_size,
            "limit": request.limit,
        }
        evidence = [
            {
                "kind": QUERY_ACTION,
                "environment": request.environment,
                "target_alias": target,
                "object": request.object_api,
                "field_count": len(request.fields),
                "filter_count": len(request.filters),
                "record_count": len(records),
                "limit": request.limit,
                "query_sha256": fingerprint,
                "schema_validated": True,
                "read_only_command": True,
            }
        ]
        return Outcome.success(result, evidence)


def _require_api_name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _API_NAME_RE.fullmatch(value):
        raise ValueError(f"{label} must be one Salesforce API name")
    return value


def _parse_filter(value: Any) -> Filter:
    if not isinstance(value, dict) or set(value) != _FILTER_KEYS:
        raise ValueError("each filter must contain exactly field, operator, and value")
    field = _require_api_name(value["field"], "filter field")
    operator = value["operator"]
    if operator not in _OPERATORS:
        raise ValueError("filter operator must be one of eq, ne, lt, lte, gt, gte, in")
    raw_value = value["value"]
    if operator == "in":
        if not isinstance(raw_value, list) or not 1 <= len(raw_value) <= 20:
            raise ValueError("in filter value must be a list containing 1 to 20 scalars")
        if any(item is None for item in raw_value):
            raise ValueError("in filter values cannot contain null")
        for item in raw_value:
            _literal(item)
    else:
        _literal(raw_value)
        if raw_value is None and operator not in {"eq", "ne"}:
            raise ValueError("null is allowed only with eq or ne")
    return Filter(field, operator, raw_value)


def _parse_order(value: Any) -> Order:
    if not isinstance(value, dict) or set(value) != _ORDER_KEYS:
        raise ValueError("order_by must contain exactly field and direction")
    field = _require_api_name(value["field"], "order field")
    direction = value["direction"]
    if direction not in {"asc", "desc"}:
        raise ValueError("order direction must be exactly 'asc' or 'desc'")
    return Order(field, direction)


def _literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("numeric filter values must be finite")
        return repr(value)
    if isinstance(value, str) and len(value) <= 200:
        escaped = value.replace("\\", "\\\\").replace("'", "\\'")
        return f"'{escaped}'"
    raise ValueError("filter values must be null, boolean, finite number, or text up to 200 chars")


def _parse_describe(stdout: str, expected_object: str) -> dict[str, dict[str, Any]]:
    envelope = json.loads(stdout)
    if int(envelope.get("status", 0)) != 0:
        raise ValueError(_json_error(envelope) or "Salesforce CLI returned non-zero status")
    result = envelope["result"]
    if str(result.get("name", "")).lower() != expected_object.lower():
        raise ValueError("describe returned a different object")
    if result.get("queryable") is not True:
        raise ValueError(f"object {expected_object!r} is not queryable")
    fields = result["fields"]
    if not isinstance(fields, list) or not fields or not all(isinstance(field, dict) for field in fields):
        raise ValueError("describe fields must be a non-empty list of objects")
    by_name = {field.get("name"): field for field in fields}
    if None in by_name or len(by_name) != len(fields):
        raise ValueError("describe returned missing or duplicate field names")
    return by_name


def _validate_fields(request: QueryRequest, fields: dict[str, dict[str, Any]]) -> None:
    for field in request.fields:
        metadata = fields.get(field)
        if metadata is None:
            raise ValueError(f"selected field {field!r} does not exist in live describe")
        if metadata.get("type") in {"address", "location", "encryptedstring"}:
            raise ValueError(f"selected field {field!r} has unsupported type {metadata.get('type')!r}")
    for item in request.filters:
        metadata = fields.get(item.field)
        if metadata is None:
            raise ValueError(f"filter field {item.field!r} does not exist in live describe")
        if metadata.get("filterable") is not True:
            raise ValueError(f"filter field {item.field!r} is not filterable")
    if request.order_by is not None:
        metadata = fields.get(request.order_by.field)
        if metadata is None:
            raise ValueError(f"order field {request.order_by.field!r} does not exist in live describe")
        if metadata.get("sortable") is not True:
            raise ValueError(f"order field {request.order_by.field!r} is not sortable")


def _build_soql(request: QueryRequest) -> str:
    soql = f"SELECT {', '.join(request.fields)} FROM {request.object_api}"
    if request.filters:
        clauses = []
        for item in request.filters:
            if item.operator == "in":
                rendered = ", ".join(_literal(value) for value in item.value)
                clauses.append(f"{item.field} IN ({rendered})")
            else:
                clauses.append(f"{item.field} {_OPERATORS[item.operator]} {_literal(item.value)}")
        soql += " WHERE " + " AND ".join(clauses)
    if request.order_by is not None:
        soql += f" ORDER BY {request.order_by.field} {request.order_by.direction.upper()} NULLS LAST"
    return f"{soql} LIMIT {request.limit}"


def _parse_query_response(stdout: str, request: QueryRequest) -> tuple[list[dict[str, Any]], int]:
    envelope = json.loads(stdout)
    if int(envelope.get("status", 0)) != 0:
        raise ValueError(_json_error(envelope) or "Salesforce CLI returned non-zero status")
    result = envelope["result"]
    if result.get("done") is not True:
        raise ValueError("bounded query did not finish")
    records = result["records"]
    if not isinstance(records, list) or len(records) > request.limit:
        raise ValueError("query returned more records than its limit")
    clean = []
    allowed = set(request.fields) | {"attributes"}
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("each query record must be an object")
        if set(record) - allowed:
            raise ValueError("query record contained an undeclared field")
        clean.append({field: record.get(field) for field in request.fields})
    total_size = result["totalSize"]
    if isinstance(total_size, bool) or not isinstance(total_size, int) or total_size < len(clean):
        raise ValueError("query totalSize must be an integer at least as large as returned records")
    return clean, total_size

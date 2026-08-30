from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class WorkState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    BLOCKED = "blocked"
    FAILED = "failed"


@dataclass(frozen=True)
class WorkItem:
    id: str
    action: str
    payload: dict[str, Any]
    state: WorkState
    attempts: int
    max_attempts: int
    created_at: float
    updated_at: float
    available_at: float
    lease_owner: str | None = None
    lease_until: float | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)


from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from .models import WorkItem
from .store import Store


class OutcomeState(StrEnum):
    SUCCEEDED = "succeeded"
    BLOCKED = "blocked"
    FAILED = "failed"


@dataclass(frozen=True)
class Outcome:
    state: OutcomeState
    result: dict[str, Any] | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    retryable: bool = False
    retry_delay_seconds: float = 0

    @classmethod
    def success(cls, result: dict[str, Any], evidence: list[dict[str, Any]]) -> Outcome:
        return cls(OutcomeState.SUCCEEDED, result=result, evidence=evidence)

    @classmethod
    def blocked(
        cls, reason: str, evidence: list[dict[str, Any]] | None = None
    ) -> Outcome:
        return cls(OutcomeState.BLOCKED, evidence=evidence or [], error=reason)

    @classmethod
    def failed(
        cls,
        error: str,
        *,
        evidence: list[dict[str, Any]] | None = None,
        retryable: bool = False,
        retry_delay_seconds: float = 0,
    ) -> Outcome:
        return cls(
            OutcomeState.FAILED,
            evidence=evidence or [],
            error=error,
            retryable=retryable,
            retry_delay_seconds=retry_delay_seconds,
        )


class Handler(Protocol):
    def __call__(self, item: WorkItem) -> Outcome: ...


@dataclass(frozen=True)
class RunResult:
    claimed: bool
    item: WorkItem | None = None


class Engine:
    def __init__(
        self,
        store: Store,
        handlers: dict[str, Handler],
        *,
        worker_id: str,
        execution_enabled: bool = False,
        lease_seconds: float = 300,
    ) -> None:
        self.store = store
        self.handlers = dict(handlers)
        self.worker_id = worker_id
        self.execution_enabled = execution_enabled
        self.lease_seconds = lease_seconds

    def run_once(self, *, work_id: str | None = None, now: float | None = None) -> RunResult:
        if not self.execution_enabled:
            return RunResult(claimed=False)
        self.store.expire_leases(now=now)
        item = (
            self.store.claim(
                work_id,
                self.worker_id,
                lease_seconds=self.lease_seconds,
                now=now,
            )
            if work_id is not None
            else self.store.claim_next(
                self.worker_id, lease_seconds=self.lease_seconds, now=now
            )
        )
        if item is None:
            return RunResult(claimed=False)
        handler = self.handlers.get(item.action)
        if handler is None:
            item = self.store.fail(
                item.id,
                self.worker_id,
                error=f"no registered handler for action {item.action!r}",
                now=now,
            )
            return RunResult(claimed=True, item=item)
        try:
            outcome = handler(item)
        except Exception as exc:
            item = self.store.fail(
                item.id,
                self.worker_id,
                error=f"handler raised {type(exc).__name__}: {exc}",
                now=now,
            )
            return RunResult(claimed=True, item=item)
        if outcome.state is OutcomeState.SUCCEEDED:
            if not outcome.evidence:
                item = self.store.fail(
                    item.id,
                    self.worker_id,
                    error="success rejected: no structured evidence",
                    now=now,
                )
            else:
                item = self.store.succeed(
                    item.id,
                    self.worker_id,
                    result=outcome.result or {},
                    evidence=outcome.evidence,
                    now=now,
                )
        elif outcome.state is OutcomeState.BLOCKED:
            item = self.store.block(
                item.id,
                self.worker_id,
                reason=outcome.error or "handler blocked without a reason",
                evidence=outcome.evidence,
                now=now,
            )
        else:
            item = self.store.fail(
                item.id,
                self.worker_id,
                error=outcome.error or "handler failed without an error",
                evidence=outcome.evidence,
                retryable=outcome.retryable,
                retry_delay_seconds=outcome.retry_delay_seconds,
                now=now,
            )
        return RunResult(claimed=True, item=item)

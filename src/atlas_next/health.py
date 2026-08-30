from __future__ import annotations

from typing import Any

from .store import Store


def snapshot(
    store: Store,
    *,
    execution_enabled: bool,
    executor_running: bool,
    registered_actions: set[str],
    worker_id: str,
    now: float | None = None,
) -> dict[str, Any]:
    counts = store.counts()
    queued_actions = store.queued_actions()
    uncovered = sorted(queued_actions - registered_actions)
    expired = store.expired_running_count(now=now)

    if expired:
        overall = "unhealthy"
        reason = f"{expired} running lease(s) expired"
    elif not execution_enabled:
        overall = "paused"
        reason = "execution is disabled"
    elif not executor_running:
        overall = "unhealthy"
        reason = "execution is enabled but no executor is running"
    elif not registered_actions:
        overall = "degraded"
        reason = "executor has no registered capabilities"
    elif uncovered:
        overall = "degraded"
        reason = f"queued actions have no handler: {', '.join(uncovered)}"
    else:
        overall = "ok"
        reason = "executor enabled; queued actions covered; no expired leases"

    return {
        "overall": overall,
        "reason": reason,
        "executor": {
            "enabled": execution_enabled,
            "running": executor_running,
            "ready": (
                execution_enabled
                and executor_running
                and bool(registered_actions)
                and not uncovered
                and expired == 0
            ),
            "worker_id": worker_id,
        },
        "counts": counts,
        "queued_actions": sorted(queued_actions),
        "registered_actions": sorted(registered_actions),
        "uncovered_actions": uncovered,
        "expired_running": expired,
    }

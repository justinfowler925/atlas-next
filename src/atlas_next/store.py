from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .models import WorkItem, WorkState


_SCHEMA = """
CREATE TABLE IF NOT EXISTS work_items (
    id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('queued','running','succeeded','blocked','failed')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts INTEGER NOT NULL CHECK (max_attempts >= 1),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    available_at REAL NOT NULL,
    lease_owner TEXT,
    lease_until REAL,
    result_json TEXT,
    error TEXT,
    evidence_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_work_claim
ON work_items(state, available_at, created_at);

CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    work_id TEXT NOT NULL REFERENCES work_items(id),
    kind TEXT NOT NULL,
    at REAL NOT NULL,
    data_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_work ON events(work_id, seq);
"""


class TransitionError(RuntimeError):
    pass


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(_SCHEMA)

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("COMMIT")

    def _event(self, work_id: str, kind: str, at: float, data: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO events(work_id, kind, at, data_json) VALUES(?,?,?,?)",
            (work_id, kind, at, json.dumps(data, sort_keys=True)),
        )

    def enqueue(
        self,
        action: str,
        payload: dict[str, Any],
        *,
        max_attempts: int = 1,
        work_id: str | None = None,
        available_at: float | None = None,
        now: float | None = None,
    ) -> WorkItem:
        action = action.strip()
        if not action:
            raise ValueError("action must not be empty")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        timestamp = time.time() if now is None else now
        ready_at = timestamp if available_at is None else available_at
        item_id = work_id or str(uuid.uuid4())
        with self._transaction():
            self.db.execute(
                """INSERT INTO work_items(
                    id, action, payload_json, state, attempts, max_attempts,
                    created_at, updated_at, available_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    item_id,
                    action,
                    json.dumps(payload, sort_keys=True),
                    WorkState.QUEUED,
                    0,
                    max_attempts,
                    timestamp,
                    timestamp,
                    ready_at,
                ),
            )
            self._event(item_id, "enqueued", timestamp, {"action": action})
        return self.get(item_id)

    def claim_next(
        self,
        worker_id: str,
        *,
        lease_seconds: float = 300,
        now: float | None = None,
    ) -> WorkItem | None:
        if not worker_id.strip():
            raise ValueError("worker_id must not be empty")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        timestamp = time.time() if now is None else now
        with self._transaction():
            row = self.db.execute(
                """SELECT id FROM work_items
                   WHERE state='queued' AND available_at <= ?
                   ORDER BY created_at, id LIMIT 1""",
                (timestamp,),
            ).fetchone()
            if row is None:
                return None
            work_id = str(row["id"])
            changed = self.db.execute(
                """UPDATE work_items
                   SET state='running', attempts=attempts+1, updated_at=?,
                       lease_owner=?, lease_until=?, error=NULL
                   WHERE id=? AND state='queued'""",
                (timestamp, worker_id, timestamp + lease_seconds, work_id),
            ).rowcount
            if changed != 1:
                raise TransitionError(f"atomic claim failed for {work_id}")
            self._event(
                work_id,
                "claimed",
                timestamp,
                {"worker_id": worker_id, "lease_until": timestamp + lease_seconds},
            )
        return self.get(work_id)

    def succeed(
        self,
        work_id: str,
        worker_id: str,
        *,
        result: dict[str, Any],
        evidence: list[dict[str, Any]],
        now: float | None = None,
    ) -> WorkItem:
        if not evidence:
            raise TransitionError("success requires at least one evidence record")
        return self._finish(
            work_id,
            worker_id,
            WorkState.SUCCEEDED,
            result=result,
            evidence=evidence,
            error=None,
            retryable=False,
            now=now,
        )

    def block(
        self,
        work_id: str,
        worker_id: str,
        *,
        reason: str,
        evidence: list[dict[str, Any]] | None = None,
        now: float | None = None,
    ) -> WorkItem:
        if not reason.strip():
            raise ValueError("blocked work requires a reason")
        return self._finish(
            work_id,
            worker_id,
            WorkState.BLOCKED,
            result=None,
            evidence=evidence or [],
            error=reason,
            retryable=False,
            now=now,
        )

    def fail(
        self,
        work_id: str,
        worker_id: str,
        *,
        error: str,
        evidence: list[dict[str, Any]] | None = None,
        retryable: bool = False,
        retry_delay_seconds: float = 0,
        now: float | None = None,
    ) -> WorkItem:
        if not error.strip():
            raise ValueError("failed work requires an error")
        return self._finish(
            work_id,
            worker_id,
            WorkState.FAILED,
            result=None,
            evidence=evidence or [],
            error=error,
            retryable=retryable,
            retry_delay_seconds=retry_delay_seconds,
            now=now,
        )

    def _finish(
        self,
        work_id: str,
        worker_id: str,
        state: WorkState,
        *,
        result: dict[str, Any] | None,
        evidence: list[dict[str, Any]],
        error: str | None,
        retryable: bool,
        retry_delay_seconds: float = 0,
        now: float | None,
    ) -> WorkItem:
        timestamp = time.time() if now is None else now
        with self._transaction():
            row = self.db.execute(
                "SELECT attempts, max_attempts, state, lease_owner FROM work_items WHERE id=?",
                (work_id,),
            ).fetchone()
            if row is None:
                raise KeyError(work_id)
            if row["state"] != WorkState.RUNNING or row["lease_owner"] != worker_id:
                raise TransitionError(
                    f"{work_id} is not running under lease owner {worker_id!r}"
                )
            will_retry = (
                state is WorkState.FAILED
                and retryable
                and int(row["attempts"]) < int(row["max_attempts"])
            )
            persisted_state = WorkState.QUEUED if will_retry else state
            available_at = timestamp + max(0, retry_delay_seconds) if will_retry else timestamp
            self.db.execute(
                """UPDATE work_items SET state=?, updated_at=?, available_at=?,
                       lease_owner=NULL, lease_until=NULL, result_json=?, error=?, evidence_json=?
                   WHERE id=?""",
                (
                    persisted_state,
                    timestamp,
                    available_at,
                    json.dumps(result, sort_keys=True) if result is not None else None,
                    error,
                    json.dumps(evidence, sort_keys=True),
                    work_id,
                ),
            )
            self._event(
                work_id,
                "retry_scheduled" if will_retry else str(state),
                timestamp,
                {"error": error, "retryable": retryable, "evidence_count": len(evidence)},
            )
        return self.get(work_id)

    def expire_leases(self, *, now: float | None = None) -> list[str]:
        timestamp = time.time() if now is None else now
        expired: list[str] = []
        with self._transaction():
            rows = self.db.execute(
                "SELECT id, lease_owner FROM work_items "
                "WHERE state='running' AND lease_until IS NOT NULL AND lease_until < ?",
                (timestamp,),
            ).fetchall()
            for row in rows:
                work_id = str(row["id"])
                expired.append(work_id)
                error = f"lease expired under worker {row['lease_owner']}; operator review required"
                self.db.execute(
                    """UPDATE work_items SET state='failed', updated_at=?,
                           lease_owner=NULL, lease_until=NULL, error=? WHERE id=?""",
                    (timestamp, error, work_id),
                )
                self._event(work_id, "lease_expired", timestamp, {"error": error})
        return expired

    def requeue(self, work_id: str, *, reason: str, now: float | None = None) -> WorkItem:
        if not reason.strip():
            raise ValueError("requeue requires an operator reason")
        timestamp = time.time() if now is None else now
        with self._transaction():
            row = self.db.execute(
                "SELECT state FROM work_items WHERE id=?", (work_id,)
            ).fetchone()
            if row is None:
                raise KeyError(work_id)
            if row["state"] not in (WorkState.FAILED, WorkState.BLOCKED):
                raise TransitionError("only failed or blocked work can be requeued")
            self.db.execute(
                """UPDATE work_items SET state='queued', updated_at=?, available_at=?,
                       error=NULL, result_json=NULL, evidence_json='[]' WHERE id=?""",
                (timestamp, timestamp, work_id),
            )
            self._event(work_id, "operator_requeued", timestamp, {"reason": reason})
        return self.get(work_id)

    def get(self, work_id: str) -> WorkItem:
        row = self.db.execute("SELECT * FROM work_items WHERE id=?", (work_id,)).fetchone()
        if row is None:
            raise KeyError(work_id)
        return self._item(row)

    def list(self, *, state: WorkState | None = None) -> list[WorkItem]:
        if state is None:
            rows = self.db.execute("SELECT * FROM work_items ORDER BY created_at, id").fetchall()
        else:
            rows = self.db.execute(
                "SELECT * FROM work_items WHERE state=? ORDER BY created_at, id", (state,)
            ).fetchall()
        return [self._item(row) for row in rows]

    def events(self, work_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT seq, kind, at, data_json FROM events WHERE work_id=? ORDER BY seq",
            (work_id,),
        ).fetchall()
        return [
            {"seq": row["seq"], "kind": row["kind"], "at": row["at"], **json.loads(row["data_json"])}
            for row in rows
        ]

    def counts(self) -> dict[str, int]:
        result = {state.value: 0 for state in WorkState}
        for row in self.db.execute("SELECT state, COUNT(*) AS n FROM work_items GROUP BY state"):
            result[str(row["state"])] = int(row["n"])
        return result

    def queued_actions(self) -> set[str]:
        return {
            str(row["action"])
            for row in self.db.execute("SELECT DISTINCT action FROM work_items WHERE state='queued'")
        }

    def expired_running_count(self, *, now: float | None = None) -> int:
        timestamp = time.time() if now is None else now
        row = self.db.execute(
            "SELECT COUNT(*) AS n FROM work_items "
            "WHERE state='running' AND lease_until IS NOT NULL AND lease_until < ?",
            (timestamp,),
        ).fetchone()
        return int(row["n"])

    @staticmethod
    def _item(row: sqlite3.Row) -> WorkItem:
        return WorkItem(
            id=str(row["id"]),
            action=str(row["action"]),
            payload=json.loads(row["payload_json"]),
            state=WorkState(row["state"]),
            attempts=int(row["attempts"]),
            max_attempts=int(row["max_attempts"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            available_at=float(row["available_at"]),
            lease_owner=row["lease_owner"],
            lease_until=row["lease_until"],
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            error=row["error"],
            evidence=json.loads(row["evidence_json"]),
        )


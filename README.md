# Atlas Next

Atlas Next is a clean replacement for the legacy Atlas control plane. It is a
small deterministic work engine, not an autonomous chatbot.

The foundation has five rules:

1. One SQLite database owns work state and its append-only event history.
2. A work item advances only through explicit state transitions.
3. Success requires structured evidence; prose cannot mark work complete.
4. Retries are opt-in, bounded, and visible in the ledger.
5. Health reports executor readiness, handler coverage, and expired leases—not
   merely whether a process exists.

LLMs are outside the control plane. A future planner may propose a typed work
item, but deterministic code must validate it and a registered capability must
execute it.

## Current scope

This repository contains the replacement kernel only:

- durable work and event ledger;
- atomic claim/lease semantics;
- bounded execution with explicit outcomes;
- evidence-gated success;
- truthful health snapshots;
- tests for the failure classes that made legacy Atlas untrustworthy.

It does **not** contain Salesforce, Linear, GitHub, Slack, deployment, scheduling,
or LLM adapters yet. The old Atlas remains untouched and is not a runtime
dependency.

## Verify

```bash
python3 -m pytest -q
python3 -m ruff check .
```

Inspect a new ledger without enabling execution:

```bash
atlas-next --db /tmp/atlas-next.sqlite init
atlas-next --db /tmp/atlas-next.sqlite status
```


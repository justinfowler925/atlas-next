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

The admitted capabilities are `salesforce.describe` and `salesforce.count`:
hardcoded read-only Salesforce CLI calls for one object in Partial or production.
Count generates exactly `SELECT COUNT() FROM <validated_object>`; callers cannot
supply SOQL, filters, fields, command strings, mutation verbs, or arbitrary targets.
Linear, GitHub, Slack,
deployment, scheduling, and LLM adapters remain absent. The old Atlas is not a
runtime dependency.

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

Run the single read-only Salesforce capability explicitly:

```bash
atlas-next --db /tmp/atlas-next.sqlite salesforce-inspect Account \
  --environment partial --partial-alias dod-check
atlas-next --db /tmp/atlas-next.sqlite salesforce-count Account \
  --environment partial --partial-alias dod-check
```

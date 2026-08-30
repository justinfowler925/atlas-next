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

The admitted capabilities are `salesforce.describe`, `salesforce.count`,
`salesforce.picklist_counts`, `salesforce.query`, `salesforce.metadata_diff`, and
`salesforce.metadata_content_diff`, plus Partial-only `salesforce.apex_test`:
hardcoded read-only Salesforce CLI calls for one object in Partial or production.
Count generates exactly `SELECT COUNT() FROM <validated_object>`; callers cannot
supply SOQL, filters, fields, command strings, mutation verbs, or arbitrary targets.
Picklist counts is the only field-level read: it live-validates the field as a
groupable Salesforce `picklist` before running a generated aggregate capped at
50 groups, so arbitrary string/PII fields cannot pass.
Record query accepts structured fields, filters, ordering, and a limit only. It
live-validates the object and every field, generates SOQL internally, rejects
relationship paths and encrypted fields, and caps output at 200 records.
Metadata diff runs the same fixed inventory command in Partial and production
for one whitelisted metadata type, then reports shared and org-only components.
Content diff retrieves one exact component from both orgs into Atlas-owned
artifacts and compares every retrieved source file by SHA-256.
Named Apex tests are live-validated against Partial metadata, capped at ten
classes, run with coverage, and succeed only on a reconciled non-zero pass.
Source retrieve admits one exact Partial component into a clean, named, linked
SFDC worktree and rejects deletions, unrelated paths, broad retrieves, and main.
Commit source stages and commits only files whose current hashes match successful
source-producing ledger evidence; it rejects unrelated dirt and does not push.

`salesforce.authenticated_get` performs a bounded GET through a Partial Named
Credential. The secret is referenced only by Salesforce credential merge-field name;
Atlas records the HTTP status, response byte count, and response hash, never the body
or credential value.

Open PR accepts only successful commit work-item IDs, proves their branch contains
current `origin/main`, pushes that exact HEAD, and targets `ClearspeedRevOps/sfdc`.
Verify PR waits for GitHub checks, requires the sandbox validation and sibling
gates to exist and finish green, and re-proves that HEAD contains current main.
The delivery chain then head-locks the squash merge and admits delivery coverage
only after the exact merge SHA has a successful `Salesforce CI`
`Deploy (sandbox)` job. Production writes are not available. Linear, Slack,
scheduling, and LLM adapters remain absent. The old Atlas is not a runtime
dependency.

Historical coverage is measured against 163 real Salesforce workflows from the
legacy 200-ticket corpus. See `docs/HISTORICAL_COVERAGE.md`; current strict
capability-family coverage is 163/163 (100%).

The representative replay gate is separate from static coverage. It requires 15
real ticket IDs spanning all nine families and fresh successful terminal receipts
from the live Salesforce workflows. Run it with `scripts/replay-history.py`; the
checked-in sample is `replays/historical-15.json` and credential values never enter
its receipt manifest.

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
atlas-next --db /tmp/atlas-next.sqlite salesforce-picklist-counts \
  Opportunity StageName --environment partial --partial-alias dod-check
atlas-next --db /tmp/atlas-next.sqlite salesforce-query Opportunity \
  --fields Id,Name,StageName,CloseDate \
  --filter-json '[{"field":"StageName","operator":"eq","value":"Closed Won"}]' \
  --order-field CloseDate --order-direction desc --limit 10
atlas-next --db /tmp/atlas-next.sqlite salesforce-metadata-diff Flow
atlas-next --db /tmp/atlas-next.sqlite salesforce-metadata-content-diff \
  Flow Set_Close_Date_Last_Updated
atlas-next --db /tmp/atlas-next.sqlite salesforce-apex-test \
  CsHandoffIntakeSchemaTest
atlas-next --db /tmp/atlas-next.sqlite salesforce-retrieve-source \
  ApexClass AtlasAcceptanceApexService \
  --project-dir /path/to/isolated-sfdc-worktree/salesforce
atlas-next --db /tmp/atlas-next.sqlite salesforce-author-source \
  <retrieve-work-id> --path salesforce/force-app/main/default/classes/Service.cls \
  --expected-sha256 <retrieved-sha256> --content-file /path/to/replacement.cls
atlas-next --db /tmp/atlas-next.sqlite salesforce-create-flow-source \
  Atlas_Acceptance_Flow --content-file /path/to/flow-meta.xml \
  --project-dir /path/to/isolated-sfdc-worktree/salesforce
atlas-next --db /tmp/atlas-next.sqlite salesforce-verify-flow-activation \
  <deploy-work-id> <flow-source-work-id>
atlas-next --db /tmp/atlas-next.sqlite salesforce-run-created-flow \
  <activation-work-id> --output-variable result --expected-string atlas-flow-ok
atlas-next --db /tmp/atlas-next.sqlite salesforce-update-records Account \
  --records-json '[{"id":"001...","fields":{"Description":"repaired"}}]' \
  --reason 'Repair approved test record state'
atlas-next --db /tmp/atlas-next.sqlite salesforce-rollback-update \
  <update-work-id> --reason 'Restore acceptance baseline after proof'
atlas-next --db /tmp/atlas-next.sqlite salesforce-create-lwc-source \
  atlasAcceptanceCard --source-dir /path/to/bundle \
  --project-dir /path/to/isolated-sfdc-worktree/salesforce
atlas-next --db /tmp/atlas-next.sqlite salesforce-verify-lwc-deployment \
  <deploy-work-id> <lwc-source-work-id>
atlas-next --db /tmp/atlas-next.sqlite salesforce-create-report-source \
  Atlas_Acceptance_Opportunity_Report --content-file /path/to/report-meta.xml \
  --project-dir /path/to/isolated-sfdc-worktree/salesforce
atlas-next --db /tmp/atlas-next.sqlite salesforce-verify-report-execution \
  <deploy-work-id> <report-source-work-id>
atlas-next --db /tmp/atlas-next.sqlite salesforce-create-integration-source \
  AtlasAcceptanceExchangeRate --base-url https://open.er-api.com \
  --path /v6/latest/USD --marker-field result --expected-marker success \
  --project-dir /path/to/isolated-sfdc-worktree/salesforce
atlas-next --db /tmp/atlas-next.sqlite salesforce-verify-integration-execution \
  <deploy-work-id> <integration-source-work-id>
atlas-next --db /tmp/atlas-next.sqlite commit-source <source-work-id> \
  --message 'chore: capture Partial acceptance service'
atlas-next --db /tmp/atlas-next.sqlite open-pr <commit-work-id> \
  --title 'Capture Partial acceptance service' \
  --body 'Exact Partial retrieval with independent hashes and named Apex tests.'
atlas-next --db /tmp/atlas-next.sqlite verify-pr <open-pr-work-id>
atlas-next --db /tmp/atlas-next.sqlite merge-pr <verify-pr-work-id>
atlas-next --db /tmp/atlas-next.sqlite verify-sandbox-deploy <merge-pr-work-id>
```

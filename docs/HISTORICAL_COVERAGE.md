# Historical Salesforce coverage

The denominator is the legacy Atlas ledger captured from Studio on 2026-08-29:
207 work items, including 200 `REV-*` tickets. The classifier excludes 37 rows
that are synthetic acceptance probes, Atlas/Brutus internals, templates, or
explicitly outside the Salesforce lane. That leaves 163 real Salesforce ticket
workflows.

Coverage is deliberately strict. A ticket counts only when every workflow
family detected in its title and stored goal has an admitted Atlas Next
capability. A test runner does not count as Apex authoring; metadata name parity
does not count as Flow repair; a local command does not count as CI delivery.

Current measured coverage is **67/163 tickets (41.10%)**. The current capabilities
cover bounded investigation/query, metadata/schema inspection, hash-locked Apex
authoring and testing, active Flow creation/editing with runtime proof, and governed
commit-to-PR-to-Partial delivery with an exact CI deployment receipt. They also
perform bounded, schema-validated, all-or-none Partial data repair with verified
rollback. The family occurrences across the 163 workflows are:

- investigation/query: 114
- metadata/schema/access: 110
- Flow/automation: 103
- delivery/CI/release: 69
- integration/pipeline: 65
- data repair/migration: 46
- reporting/analytics: 43
- Apex logic/tests: 40
- LWC/page experience: 38

Run the measurement against a read-only copy of the legacy ledger:

```bash
uv run python scripts/history-coverage.py /path/to/job_ledger.sqlite
```

The script emits the complete covered and uncovered ticket populations so rule
changes can be reviewed against individual ticket IDs rather than only totals.

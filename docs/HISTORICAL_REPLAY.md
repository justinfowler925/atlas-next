# Historical workflow replay

The representative replay started at `2026-08-30T13:01:46Z` against the 200-ticket
legacy ledger and the live Partial sandbox. The checked-in 15-ticket sample spans all
nine capability families and includes eight compound workflows.

The run finished 15/15. Fresh terminal evidence included a bounded Account query;
Partial-versus-production CustomObject inventory; executed Flow output; 2/2 Apex tests
with 100% run coverage; live LWC metadata; a 107-row report; an all-or-none data update
and exact-hash rollback; an external callout; and the exact merged-SHA sandbox deploy
receipt. Apex authoring uses its governed source receipt from earlier the same day and
requires a fresh live test terminal.

The executable gate is `scripts/replay-history.py`. It opens both the historical ledger
and receipt ledgers read-only, rejects stale or failed terminal receipts, requires the
full action chain for Apex and data repair, and refuses samples that omit a capability
family. Live negative controls proved that an empty sample and a nonexistent ticket both
exit nonzero; the unit suite also covers missing-family, stale-receipt, and failed-receipt
controls.

# Architecture

## Boundary

Atlas Next replaces the legacy orchestration layer. It does not port the legacy
worker loop, conductor, retry gates, launchd inventory, health aggregators, or
multiple ledgers.

Potentially reusable code is admitted later only as a capability adapter with a
typed input, deterministic validation, structured evidence, and no direct write
to the work ledger. This includes Salesforce recipes and external-system clients.

## State machine

```text
queued -> running -> succeeded
                  -> blocked
                  -> failed
                  -> queued   (only when the handler explicitly marks a failure
                               retryable and attempts remain)
```

An expired lease becomes `failed`. It is never silently reclaimed. Recovery is
an operator decision that creates a visible event.

## Authority

- `Store` is the only component allowed to mutate work state.
- `Engine` maps an action to one registered handler and translates its typed
  outcome into one store transition.
- A handler may touch an external system, but cannot declare success without at
  least one structured evidence record.
- Health reads effective executor configuration and handler coverage. A dormant
  executor is `paused`, not healthy.

## Migration rule

No legacy module is copied wholesale. Each imported capability must pass a
contract test against its real external layer and must remain callable without an
LLM. Migration starts with read-only Salesforce inspection, then sandbox writes,
then delivery handback. Production writes remain out of scope until separately
authorized and proven.

## Admitted capability: Salesforce describe

`salesforce.describe` is the first vertical slice. Its request has exactly two
fields (`environment`, `object`), its environment is exactly `partial` or `prod`,
and its object is one validated API name. The implementation constructs one
argument-vector subprocess call to `sf sobject describe`; it has no shell, SOQL,
free-form command, or mutation path. The ledger accepts success only after the
CLI JSON contains a non-zero, duplicate-free field population.

`salesforce.count` uses the same two-field request and target map. It constructs
exactly `SELECT COUNT() FROM <validated_object>` and accepts only a completed,
non-negative integer result. A zero count is valid evidence; a missing, boolean,
negative, or incomplete result fails the ledger item.

`salesforce.picklist_counts` adds one field API name, live-describes it, and
continues only when its type is exactly `picklist` and `groupable=true`. It then
generates one `GROUP BY` query capped at 50 groups. No caller-supplied filter,
SOQL, limit, or arbitrary text field can reach Salesforce.

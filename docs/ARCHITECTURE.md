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


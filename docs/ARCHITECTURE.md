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

`salesforce.query` is the bounded investigation surface. Callers provide only a
structured object, field list, typed filters, one optional ordering, and a limit
of at most 200. Atlas live-describes the object, proves that selected, filtered,
and ordered fields support the requested operations, escapes every literal, and
then generates the query. Relationship paths, raw SOQL, shell commands,
encrypted fields, unbounded results, and undeclared response fields fail closed.

`salesforce.metadata_diff` compares the component-name inventory for one fixed,
supported metadata type across Partial and production. It runs exactly two
read-only Metadata API list commands and records population counts plus hashes;
it does not mistake matching names for matching component content.

`salesforce.metadata_content_diff` closes that admitted gap for one exact
component. It retrieves the component from both orgs into a bounded,
Atlas-owned artifact directory, excludes transport manifests, hashes every
source file, and reports content, missing-file, and extra-file differences.
Wildcards, traversal, arbitrary metadata types, deploy flags, and caller-chosen
commands cannot reach the subprocess.

`salesforce.apex_test` is the first execution capability and remains Partial
only. It accepts one to ten exact class API names, proves every class exists in
the live Partial inventory, runs only those classes with coverage enabled, and
requires a non-zero, reconciled, passing test summary with a `707` run id.

`salesforce.retrieve_source` is the first GitHub-delivery building block. It
retrieves one exact component from Partial into a clean, named, linked SFDC
worktree. The postcondition is a non-zero bounded set of regular files only
under that project's `force-app/main/default`; deletions, renames, unrelated
paths, primary/main checkouts, wildcard components, and production are refused.

`delivery.commit_source` consumes successful source-producing ledger item IDs,
re-hashes every proven file, requires the complete worktree dirt population to
equal those files, stages exactly that set, and records the resulting commit SHA.
It cannot accept caller-supplied paths, stage unrelated work, push, or open a PR.

`delivery.open_pr` consumes only successful commit ledger IDs from one clean
branch. It fetches `origin/main`, refuses a behind branch, proves every linked
commit is in HEAD, asserts the governed `ClearspeedRevOps/sfdc` repository,
pushes that exact branch, and creates or reuses one PR targeting `main`. It does
not merge, deploy, or infer that CI passed.

`delivery.verify_pr` waits on the actual PR checks, requires the named sandbox
validation, LWC, and RevOps release gates to exist, rejects every non-green
non-skipped check, refreshes `origin/main`, and proves both the GitHub base and
the evidence-linked head are cleanly mergeable on that current base.

`delivery.merge_pr` consumes only that verification receipt, rechecks the head,
current main, checks, and clean merge state, then squash-merges with GitHub's
head-SHA lock. `delivery.verify_sandbox_deploy` links the immutable merge SHA to
exactly one successful `Salesforce CI` push run and its successful
`Deploy (sandbox)` job. This final receipt is the only capability admitted for
historical delivery/CI/release coverage; production remains untouched.

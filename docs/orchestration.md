# Kubernetes v2 orchestration

The executor composes source-boundary reads, initial-copy Jobs, journal readers
and destination loaders. Its implementation and local tests are separate from
qualification of the exact release on a real IBM i and cluster.

Code lives in `src/quadringent_control_plane/v2/executor/` and the
`k8s_*` clients. [API v2](api-v2.md) documents declared-state actions;
[native qualification](product/native-qualification.md) establishes runtime proof.

## Initial-copy boundary

Read the attached journal receiver and last sequence **before** launching the
copy. Persist that immutable boundary with a fresh UUID run and empty output
directory. Capture begins at boundary sequence +1. Never infer a later boundary
from copy completion.

`plan_bootstrap` rejects same-receiver sequence regression and receiver-time
regression with `BoundaryRegressionError`. A newly attached receiver with no
entry uses its observed first sequence minus one; this is not an invented tail.

JDBC position and table image are not one atomic snapshot. Concurrent writes
during copying require journal replay and position-aware mirror convergence.
The snapshot's boundary precedes actual replayed changes; later committed changes
must win for a business key. Test that invariant under concurrent writes.
A successful copy Job alone establishes neither delivery nor losslessness.

The copy Job publishes `InitialCopyEvidence`: pipeline/table/run, boundary,
rows, completion time and exact snapshot-batch keys. The control plane reads it;
it does not write a synthetic completion proof. Missing evidence means pending;
malformed evidence fails closed.

## Workloads and binding

| Workload | Scope |
|---|---|
| `qdt-reader-<hash(source,journal)>` Deployment | One reader per source/journal for its effective table set |
| `qdt-copy-<hash(table,run_id)>` Job | One table and fresh initial-copy attempt |
| Loader Deployment | Immutable destination/table scopes with initial-copy evidence binding |
| Diagnostic Jobs | Bounded source probe or table discovery |

Readers use `Recreate` and at most one replica to avoid concurrent readers.
Pure manifest construction is separated from API application. The desired spec's
SHA256 annotation supports idempotent reconciliation rather than comparing
server-added defaults. Jobs are immutable; failed attempts require fresh identity.

Capture declares source time zone, reader timeout, process budget, storage
backend and table-specific bootstrap. Budget exceeds reader timeout with safe
shutdown reserve. Source testing must first establish `detected_timezone`;
start/restart fails closed without it.

Passwords/keys use referenced Secrets, never plaintext arguments.
Workloads run non-root UID 10001 with the capture ServiceAccount, whose cloud
identity is separate from the control-plane ServiceAccount.

## Pause and resume

An individually paused table is removed from the effective reader set.
Other tables on the journal can keep that reader running. If none remain
copying/live, remove the reader Deployment. Durable checkpoints are preserved.

Source, destination and organization pause/resume use the same pipeline executor.
`paused_by_scope_action` marks only pipelines paused by that scope operation.
A scope resume restores those pipelines; it never overrides an individual pause.
Ineligible pipelines are listed as skipped. If required execution is absent,
fail the scope with `503 executor_unavailable` rather than silently recording
partial operation. Audit and SSE retain each affected pipeline and cause.

Sensitive actions require exact confirmations. When executor invocation precedes
declared-state persistence, desired-set calculation uses an explicit next-state
override rather than rereading a state not yet committed.

## Reconciliation loop

For `copying` pipelines with an active run, read durable evidence and transition
to `bootstrap_completed` when valid. Otherwise inspect copy Job terminal
failure and transition to `attention`. A new copy is an explicit sensitive
operation, not an automatic same-name retry.

A portable lease in the shared `leases` table protects background reconciliation
and observation scheduling. Generation fencing detects a lost lease before
action. Optional app wiring uses `reconciliation_executor` and
`reconciliation_interval_seconds`; tests/off-cluster construction create no
implicit task.

The background tick handles completion/failure state. Desired Deployments/Jobs
are reconciled synchronously through executor actions. A background state
transition is not by itself proof that Snowflake received the copied rows.

## Runtime wiring

Entrypoint wiring requires in-cluster configuration and a declared
`QUADRINGENT_V2_CAPTURE_IMAGE`. Pipeline execution additionally requires
`QUADRINGENT_V2_CAPTURE_SERVICE_ACCOUNT`, raw bucket/prefix and backend state
location. AWS additionally requires an explicit region. Missing inputs leave
execution unavailable without inventing an installation.

The control-plane image contains Java/JTOpen. `JavaBoundaryReader` performs a
short in-process subprocess catalog call per boundary read; the IBM i password
is confined to that subprocess environment. No persistent worker per journal
or diagnostic Job is needed for this read.

Source probes and discovery use bounded ephemeral Jobs, temporary Secrets,
non-root security context and prefixed JSON Pod-log results. Cleanup runs in
`finally`, including creation failure and timeout.
`DiagnosticWorker` shares TLS/connection plumbing but does not require a
captured table, journal or declared capture time zone. Actual capture workers
still validate the source clock and journal.

Discovery's inherited single-source adapter refuses zero or multiple configured
sources rather than choosing arbitrarily. Source probe measures authentication,
IBM i version, `QTIMZON` and separate TLS/network evidence.
A capture clock mismatch is `SOURCE_CLOCK_MISMATCH`, a nonretryable
configuration gate, rather than an ordinary connection outage.

`QUADRINGENT_V2_LOADER_IMAGE` wires the destination loader independently of
the capture image. Explicit profile variables control SQL/Streaming history,
polling and flush. Do not substitute old roadmap assumptions that the loader is
unwired; inspect runtime adapter availability and actual manifests.

## Secrets and RBAC

`SecretsProvisioner` decrypts database credentials and upserts deterministic
`qdt-source-<id>` / `qdt-destination-<id>` Secrets. It returns names only.
Durable source/destination Secrets are distinct from temporary diagnostic
Secrets, which are removed after the operation.

The namespace-scoped executor role supports declared Deployment operations,
Job operations and Secret provision operations. Diagnostic/telemetry adapters
add their bounded Pod/log and cleanup permissions. The chart is the authoritative
rendered permission set; review it against the actual clients. The executor
needs no `pods/exec` permission. Preserve separation from cloud workload roles.

## Raw layout and destination order

`quadringent.storage_layout` defines paths:

```text
<raw-root>/<lowercase-table>/journal/...
<raw-root>/<lowercase-table>/snapshot/...
<raw-root>/<lowercase-table>/evidence/<run-id>.json
```

Single-table capture writes receipted windows. Fleet capture writes a combined
journal-level batch/receipt and routed table batches. A combined batch is shared
window evidence, not an erroneous duplicate. The loader reads receipts at their
actual journal root and splits the validated combined population per table.

A loader with fresh-copy binding waits for exact durable snapshot evidence.
It loads snapshot batches, materializes mirror and stores the boundary before
post-boundary journal events. Validate/filter overlapping receipts only after
checking their population. Binding persists beyond promotion to live.
Immutable loader scopes prevent unrelated table/destination reuse.

Receipt chain ordering covers empty windows and receiver rotation. Never order
receivers by lexical name or subtract sequences across unrelated receivers.
History identity and mirror application must both remain idempotent on restart.
See [Snowflake destination](product/snowflake-destination.md).

## External qualification

Verify concurrent-copy writes, reader rollout downtime, real namespace RBAC,
diagnostic TLS/clock behavior, copy failure reporting, snapshot directory
isolation, exact raw/snapshot/evidence binding, loader history/mirror parity,
receipt/CAS recovery and independent source reconciliation on each target.

Local fakes and Helm rendering cover deterministic contracts. They cannot prove
real receiver retention, destination visibility, clock authority, production
stability or ten-second end-to-end latency.

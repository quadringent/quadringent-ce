# End-to-end destination evidence contract

The optional `destination_proof` extension to `as400-console-v1`
separates capture publication, Snowflake loading and business application.
It is server-produced evidence, not destination setup, credentials or a mutation
command. It cannot convert a simulation or historical snapshot into live evidence.

## Compatibility and freshness

Keep `format_version=as400-console-v1`. Preserve original
`capture_observed_at` when composing or rechecking destination evidence.
`generated_at` dates combined-document publication, not source refresh.
An invalid explicit capture timestamp cannot silently fall back.
Legacy documents without it retain their historical behavior.

Without destination proof, load/destination remain unknown and coverage partial.
Legacy target/duplicate counters are insufficient for healthy delivery.
Only an admissible matched window supplies ledger and duplicate counts; these
are window counts, not total business rows.

Validate every present field. Unknown properties are refused.
Invalid syntax returns stable `invalid_destination_proof` without reflecting
input. Refresh failure retains the last sanitized projection as unknown/stale.

Timestamps require RFC3339 with timezone and normalize to UTC.
Observations more than one minute in the future are `clock_untrusted`;
freshness lasts at most five minutes. Apply these bounds separately to capture,
proof, activation, load, destination and reconciliation. Sub-observations cannot
be later than proof, or proof later than publication, beyond that tolerance.

## Closed input model

The extension has `schema_version=destination-proof-v1`,
`observed_at`, `source_checkpoint`, `target`, `activation`,
`load`, `destination` and `reconciliation`.

Positions contain only `receiver` (nonempty, ≤128 characters) and
`sequence` (nonnegative integer, never boolean). Counters are nonnegative
integers ≤9,223,372,036,854,775,807. Public identifiers match
`[a-z0-9][a-z0-9-]{0,62}`; they contain no URL or physical qualified name.
Only explicitly optional fields may be omitted; null is accepted only where
the schema permits it.

`source_checkpoint` must equal the capture's `position.checkpoint`.
Mismatch produces `destination_source_checkpoint_mismatch`.
Unknown capture position prevents downstream health.
Different receivers are incomparable in this v1 contract.

`target.kind` is `snowflake`. Its opaque `destination_id` and
`environment` carry no physical account/object identity. Environment must
match the source descriptor case-insensitively; mismatch is
`destination_environment_mismatch`.

## Activation

States are `not_configured`, `validating`, `active`, `blocked`,
`disabled` and `unknown`. The five closed checks are:

| Check | Values | Positive |
|---|---|---|
| `configuration` | valid, missing, invalid, unknown | valid |
| `credential` | available, missing, denied, invalid, unknown | available |
| `connectivity` | reachable, unreachable, unknown | reachable |
| `authorization` | allowed, denied, unknown | allowed |
| `contract` | compatible, incompatible, unknown | compatible |

Active requires all five positive checks. Contradiction produces unknown
activation and `destination_activation_inconsistent`.
Blocked requires an allowlisted `blocker_code`; otherwise it is null:

```text
destination_configuration_missing
destination_configuration_invalid
destination_credential_missing
destination_credential_denied
destination_credential_invalid
destination_unreachable
destination_authorization_denied
destination_contract_incompatible
```

Backend exception text and secret references never enter the proof.

## Load and application

| Block | States | Required completion evidence |
|---|---|---|
| `load` | not_started, running, succeeded, failed, unknown, planned_stop | checkpoint, event_count, failed_event_count, timestamp |
| `destination` | not_started, applying, applied, failed, unknown, planned_stop | apply_checkpoint, failed_mutation_count, timestamp |

Observed timestamps are required except where not_started/unknown explicitly
permits omission. Running/succeeded/planned_stop load requires checkpoint;
applying/applied/planned_stop application requires apply checkpoint.
Completion and failure require their failed-event/mutation counts.
Load `batch_count` is optional.

Failed state requires a nonnull allowlisted `incident_code`; otherwise null.
Load codes are `destination_load_failed`, `destination_load_timeout`,
`destination_load_contract_invalid`, `destination_load_checkpoint_conflict`.
Application has the corresponding `destination_apply_*` codes.

Healthy load requires active activation, fresh trusted time, zero failed events,
checkpoint equal to source, event count equal to reconciled loaded count,
and fresh matched reconciliation.

Healthy destination additionally requires zero failed mutations,
`apply_checkpoint == load.checkpoint == source_checkpoint`, exact matched
window and healthy load. Same-receiver delay is degraded; load ahead of source
or application ahead of load is an incident
(`destination_load_ahead_of_source`, `destination_apply_ahead_of_load`).
Incomparable receivers produce unknown.

## Reconciliation

States: `not_run`, `running`, `matched`, `mismatch`, `failed`,
`unknown`. Running/matched/mismatch/failed requires observation time.
Matched/mismatch requires a window with `from_exclusive` (position or null
for known start) and `to_inclusive`. Both positions use the same receiver.

The following counters are required for matched/mismatch:

```text
captured_event_count = loaded_event_count = ledger_event_count = distinct_event_count
duplicate_event_count = missing_event_count = unexpected_event_count = failed_mutation_count = 0
```

These describe technical event identity, not final business-row count.
Healthy requires the upper boundary to equal source/load/application checkpoint.
A declared match violating its counts is
`destination_reconciliation_inconsistent`. Mismatch or any discrepancy is
`destination_reconciliation_mismatch`; operational failure is
`destination_reconciliation_failed`.
Pending/unknown reconciliation prevents health without itself inventing an
incident.

## Projection and operator verdict

Stage statuses remain `healthy`, `degraded`, `incident`,
`unknown`, `planned_stop`. Headlines/details are fixed operator copy,
not input strings.

Evaluate contradiction, explicit failure, blocked activation or reconciliation
incident first; then untrusted clock, incomparable receiver or missing required
evidence; then unconfigured/disabled state; then staleness; then planned stop;
then ongoing/delayed work; finally exact healthy conditions.
Otherwise remain unknown.

Pipeline priority is:

```text
incident > unknown > degraded > planned_stop > healthy
```

Global freshness failure makes it unknown. Historical/simulation provenance
limits it to degraded. Missing/noncomparable lag makes it unknown;
`CATCHING_UP` limits it to degraded. Only nonnegative lag with
`STABLE` or `BOUNDED` is compatible with health.
`STOPPED_BUDGET` limits the result to planned_stop.

Complete coverage requires exact load/application/reconciliation coverage
of the source checkpoint. Green additionally requires live fresh provenance,
available source, active activation and all five healthy stages.
Historical, simulated and planned-stop evidence cannot become green.

Transit motion requires consecutive green revisions of the same pipeline,
RUNNING process, live SSE, strictly newer revision and proved common checkpoint
progress. In v1 this means same receiver and increasing sequence.
Rotation interrupts that inference. A heartbeat alone can animate connection,
not data transit. Respect reduced motion.

Source absence becomes stale/unknown; a calendar or assumed weekend cannot
manufacture planned_stop.

## Security and independent meaning

Build public evidence from allowlisted fields, never by serializing driver
results. Exclude credentials, key material/fingerprints, secret references,
physical accounts/hosts/roles/objects, SQL/query IDs, business values, stack
traces, raw keys/manifests and arbitrary backend text. The existing 2 MiB input
limit still applies. Reject booleans as numbers, negative/nonfinite/overflow
values. Fingerprint the sanitized projection so private changes alone do not
create SSE revisions.

COPY history can support loading only after exact raw-file/checkpoint binding.
Successful SQL execution alone cannot establish an application checkpoint:
commit and persist its IBM i mapping. Snowflake stream offsets are not IBM i
receiver/sequence positions. Counts and equal timestamps do not infer that
mapping. Business C/U/D correctness needs value reconciliation in addition
to technical event identity.

See [native qualification](native-qualification.md), [Snowflake destination](snowflake-destination.md)
and the projector tests for accepted examples and refusal cases.

To evaluate this contract on two DEV/INT tables, review the
[installation prerequisites](install-default.md) and [pilot preparation](positioning-and-offer.md).
The pilot guide links to an public interest request using anonymized examples; agree the observation
window and acceptance criteria before installation.

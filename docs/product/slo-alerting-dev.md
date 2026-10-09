# DEV SLO evaluation and alerts

These tools evaluate existing evidence. They do not start capture or load files.
The site configuration and code restrict the DEV/INT target. Repository
identities are synthetic; adapt them only within an authorized installation.

## Policy

`infra-values/slo-policy-dev.json` defines the initial DEV contract:

| Measurement | Threshold |
|---|---:|
| Capture snapshot age | 120 s |
| Capture state | `RUNNING` or bounded `STOPPED_BUDGET` |
| Capture errors | 0 |
| Same-receiver checkpoint lag | 1,000 sequences |
| Durable S3 snapshot age | 180 s |
| Whole-bucket S3 `AllRequests` per 24 h | 500,000 |
| Pending Snowpipe files | 10 |
| Destination evidence age | 300 s |
| Source-to-canonical delivery p95 / p99 | 60 s / 180 s |
| Missing, extra, duplicate or rejected mutations | 0 |
| Declared warehouse credits per 24 h | 1 credit |

These are DEV thresholds, not an acquired commercial guarantee.
Missing, unreadable, out-of-window or incomparable-receiver measurements are
`unobserved`. Whole-bucket `AllRequests` is a conservative spending envelope,
not per-prefix attribution. Missing datapoints are not zero.

## Collect exact observations

The supplied proof fixes latency bounds from `run.started_at` to
`destination_proof.observed_at`. Reject reversed, future or >25-hour windows.

```sh
PYTHONPATH=src:scripts python3 scripts/quadringent_slo_collect.py \
  --proof /private/path/autonomy-proof.json \
  --out /private/path/slo-telemetry.json
```

Use the required AWS and Snowflake dependencies. Optional `--run-id <run>`
rereads the reserved raw population, validates batch/manifest identities and
compares counts and identity hashes to `stored_event_identity_proof` before
connecting to Snowflake. Without it, temporal/global-prefix observations do
not establish an exact run population. CloudWatch remains bucket-wide.

Archive mode supports historical observability, not a new canary. The live
verifier retains its five-minute limit. Collector output contains counts,
timestamps and percentiles; failures expose only source and error type.
Exit 0 means all six external observations are present, 2 means at least one
is unobserved, and 3 means invalid input/execution.

## Refresh, evaluate and project

```sh
python scripts/quadringent_observability_refresh.py \
  --policy infra-values/slo-policy-dev.json --out /private/path/observation.json
```

Refresh preserves existing delivery fields and verifies the archived run.
It publishes only with the explicit
`--publish-confirm REFRESH_AS400_RD_OBSERVABILITY_DEV`.
Close connections and write local output before conditional ETag PUT.
A conflict requires rereading/recalculating; never force it.
Inspect actual object/version after `publication_status=unknown`.
Exit 0 proves execution, not SLO compliance; exit 3 is execution failure.
The command alone installs no scheduler or notification channel.

```sh
python scripts/quadringent_slo_check.py \
  --proof /private/path/autonomy-proof.json \
  --telemetry /private/path/slo-telemetry.json \
  --policy infra-values/slo-policy-dev.json > /private/path/slo-report.json
python scripts/quadringent_slo_alerts.py \
  --report /private/path/slo-report.json --state /private/path/alert-state.json
```

Evaluation uses telemetry `collected_at`, not evaluator launch time.
Replay cannot refresh evidence. Reject future, invalid or timezone-free dates.
Without `collected_at`, external observations remain unknown. A collection
cannot certify a newer proof. Collect again rather than editing timestamps.

Evaluator exits: 0 all pass, 1 breach, 2 required observation missing without an
observed breach, 3 invalid proof/telemetry/policy.

The atomic `quadringent-alert-batch-v1` includes persistent
`quadringent-alert-state-v1`. Stable SHA256 fingerprints identify pipeline
and check. Validate the complete state's integrity before transition.
Open once, suppress identical repeats, mark changed status/severity/reason/
threshold/unit, resolve only on explicit pass of the same check, and count
reopening after resolution. An omitted check does not resolve.
Reject old, contradictory reports and tampered state, retaining the last valid
file. The digest detects accidental corruption; it is not a signature.

Projector exits: 0 no active signal, 1 active critical breach, 2 only incomplete
observability signals, 3 invalid report/state. Printed output contains status,
counts or error type rather than backend messages.

## Attach to the cockpit

```sh
python scripts/quadringent_observability_snapshot.py \
  --proof /private/path/autonomy-proof.json \
  --report /private/path/slo-report.json \
  --alerts /private/path/alert-state.json \
  --out /private/path/autonomy-observed.json
```

The atomic composition verifies pipeline, environment, timestamp and digest,
and embeds `quadringent-observability-v1` in the same delivery proof.
REST/SSE project that combined contract. Missing or invalid observability cannot
make delivery healthy.

Incidents retain first occurrence, latest observation and resolution.
Telemetry exposes values and thresholds. Historical, simulated, stale and
partial evidence remains retained evidence, not a current operator action.
Resolved alerts preserve the values/threshold at resolution.

Operational scheduling and notification delivery require separate site
configuration and runtime verification; see
[scheduler](observability-scheduler-dev.md) and [on-call](oncall-runbook.md).

## Canary/soak gate

Require matched autonomous reconciliation, collector exit 0, evaluator exit 0,
projector exit 0 with no active alert, return of capture to zero, and no mutation
of unrelated reference connectors. Evidence for another canonical table cannot
certify this pipe. Throughput comparisons require equivalent populations,
windows and proofs.

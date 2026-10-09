# DEV/INT on-call runbook

Use the installation's declared namespace, bucket/prefix, checkpoint store
and destination. Examples use `quadringent-demo`; confirm actual identity
before an operation. This runbook supplies no production authorization.

Policy defaults are capture/S3/destination ages 120/180/300 seconds,
1,000 same-receiver sequences, delivery p95/p99 60/180 seconds,
500,000 whole-bucket S3 requests/day, 10 pending Snowpipe files and one warehouse
credit/day. The observability deadman default is 3,600 seconds.

## Evidence and first reads

Capture's `console-snapshot.json` is throttled to ten seconds and written on
closure. The combined autonomous proof has separate delivery and observability
blocks. Fleet history progress uses one document per run.
The fleet-state PVC holds prepare/history/pause state under
`/var/lib/quadringent-fleet`. Checkpoints remain in the declared durable store.

A Pending PVC with no suitable StorageClass blocks startup.
Disabling persistence is an explicit volatile `emptyDir` choice; do not treat
it as equivalent recovery.

```sh
kubectl -n quadringent-demo get pods -l app.kubernetes.io/instance=quadringent
kubectl -n quadringent-demo get cronjob,job -l app.kubernetes.io/component=observability
kubectl -n quadringent-demo logs deploy/quadringent-quadringent -c capture --tail=200
aws s3api head-object --bucket <authorized-bucket> --key <capture-snapshot-key>
```

Preserve relevant logs privately. Head-object modification time and payload
observation time are different facts.

## Capture absent, crashed or stopped

Inspect desired replicas, Pod status, previous logs and container last state.
`replicaCount=0` is normal during a bounded pilot. Before rearming, confirm
that no pilot/fleet Job owns the same reader. Keep one reader per journal,
with Recreate and at most one replica.

Repeated restarts may indicate a frozen loop rather than an exception.
Read liveness evidence and previous logs before restarting.
Correct configuration through reviewed values.

Snapshot `run.state` and `last_error` distinguish:

- `STOPPED_BUDGET` without error: expected bounded stop.
- `STOPPED_FAIL_CLOSED`: repeated source failure; inspect TLS and account.
- `STOPPED_PROOF_CHAIN`: incomplete durable proof/continuity.
- `STOPPED_CONFIG_BLOCKED`: configuration/clock failure; correct and reset
  the source gate rather than retrying indefinitely.

A living Pod alone establishes no reader progress.

## Stale capture or storage proof

Compare snapshot publication, capture observation and current clock.
If only storage publishing is stale, inspect throttling and
`console_snapshot_write_failed`. Do not manually manufacture a fresh snapshot.
If capture is stalled, inspect the last poll, liveness and source gate first.

## Checkpoint not progressing

Compare durable checkpoint and source tail. A numeric lag is meaningful only
within a receiver or an explicitly proved chain. Different receivers without a
chain produce `receiver_chain_required` / `unobserved`.

Do not rewrite a checkpoint. Inspect receipt publication, CAS and raw failures.
An authorized restart resumes from durable state; it cannot repair missing
receiver retention or a broken receipt chain.

## Receiver rotation

Use the run's continuity artifacts and source receiver catalog:

```sh
python scripts/quadringent_fleet_continuity.py --run-prefix <authorized-run-prefix>
```

Receiver/sequence alone does not order all receivers, especially across reset
conditions. With proved continuity, projection can recover. Otherwise the fleet
remains `unproven_continuity`; keep live/certified claims blocked.

## Destination latency

Inspect destination proof age before interpreting p95/p99. Check the exact run
window and Snowpipe queue or SQL/Streaming loader stages. Increasing backlog
requires diagnosis; historical percentile values are not current delivery lag.

An observability job measures rather than loads. Suspending it cannot resolve
a loading delay. Preserve exact table binding and separate history commit,
mirror application and checkpoint persistence.

## Storage publication failure

Inspect workload identity, scoped prefix permissions and the affected operation.
A conditional ETag conflict means a newer proof wins; reread/recalculate rather
than overriding it. An uncertain PUT can have committed: inspect actual
object/version before a retry. Do not perform blind replacement.

## Silent observability

An absent observability block or observation older than the deadman threshold
is an observability failure even without a reported alert.

```sh
kubectl -n quadringent-demo get cronjob -l app.kubernetes.io/component=observability \
  -o jsonpath='{.items[0].spec.suspend} {.items[0].status.lastScheduleTime}'
kubectl -n quadringent-demo get jobs -l app.kubernetes.io/component=observability \
  --sort-by=.metadata.creationTimestamp
python scripts/quadringent_slo_alerts.py --report <latest-report.json> \
  --state <alert-state.json> --observability-snapshot <cockpit-proof.json>
```

Check reviewed `observability.suspend`, Job logs and bounded deadlines.
No delivery proof yet is a missing prerequisite, not necessarily a broken
collector. Notification emission additionally requires a configured and
verified transport.

## Fleet delivery unobserved

A running raw reader or Snowpipe does not measure destination reconciliation.
The configured `fleetObserve` job reads fleet capture, measures pipe status
and raw/canonical population, and writes a separate combined proof.
One key has one writer; the reader snapshot remains unchanged.

```sh
kubectl -n quadringent-demo get cronjob -l app.kubernetes.io/component=fleet-observability \
  -o jsonpath='{.items[0].spec.suspend} {.items[0].status.lastScheduleTime}'
kubectl -n quadringent-demo logs job/<fleet-observe-job> -c observer --tail=5
aws s3api head-object --bucket <authorized-bucket> --key <fleet-proof-key>
python scripts/quadringent_fleet_observe.py \
  --aws-profile <authorized-profile> --connection-name <authorized-connection>
```

The last command defaults to bounded dry run.
Read observed load/destination/reconciliation and freshness in addition to
`publication_status=confirmed`. Check `fleetObserve.suspend` and source
state before blaming Snowflake. Counts-only evidence cannot establish
business-value parity.

See [operations](../operations.md), [SLOs](slo-alerting-dev.md),
[installation](install-client.md) and [backup/restore](backup-restore.md).

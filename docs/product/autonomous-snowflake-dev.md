# Autonomous Snowflake loading in DEV

This AWS v1 path leaves IBM i capture and its checkpoint unchanged:
capture writes immutable raw objects before durable checkpoint advancement,
then classic Snowpipe consumes new files independently.
Use isolated DEV/INT resources and adapt the synthetic site identities.

## Objects and preflight

The reference plan targets SALE JSONL under the declared raw prefix, an existing
external stage/storage integration, dedicated raw table and Snowpipe, ordinary
canonical view, and an X-Small single-cluster verification warehouse with
60-second autosuspend. The canonical view keeps one row per deterministic
`event_id` using `QUALIFY ROW_NUMBER()`; it has no refresh compute.

Before setup, use validated source, keep the permanent reader at zero, verify
the exact stage prefix, and inspect existing bucket notifications for overlapping
ObjectCreated filters. Keep unrelated connectors, credentials and warehouses
outside the scope.

```sh
PYTHONPATH=src python3 scripts/quadringent_snowpipe_setup.py
PYTHONPATH=src python3 scripts/quadringent_s3_snowpipe_setup.py \
  --notification-channel '<snowpipe-sqs-arn>' --profile <authorized-dev-profile>
```

Dry run prints the target and required confirmation without AWS inspection.
Execution rereads live configuration, rejects overlapping filters, preserves
unrelated notification entries and applies only the reviewed complete document.

## Controlled setup

With the required Python connector and an already authorized connection:

```sh
PYTHONPATH=src python3 scripts/quadringent_snowpipe_setup.py \
  --connection-name <authorized-connection> \
  --execute --confirm AS400_RD_AUTONOMOUS_LOAD_DEV
PYTHONPATH=src python3 scripts/quadringent_s3_snowpipe_setup.py \
  --notification-channel '<snowpipe-sqs-arn>' --profile <authorized-dev-profile> \
  --execute --confirm AS400_RD_SNOWPIPE_S3_DEV
```

The first creates the dedicated DEV objects and returns Snowflake's managed
notification channel. The second adds only the exact SALE JSONL filter.
Snowpipe reads through the storage integration; it needs no Snowflake private
key in the capture Pod.

## Runtime autonomy evidence

Installation is not proof of autonomy. Declare T0, one bounded capture Job,
permanent Deployment zero, exact produced keys and final snapshot.
Wait for the exact pipe to be RUNNING with no pending files, then reconcile
using the read-only verifier:

```sh
PYTHONPATH=src python3 scripts/quadringent_autonomous_verify.py \
  --connection-name <authorized-connection> \
  --capture-snapshot /private/path/capture.json \
  --object-keys-file /private/path/object-keys.txt \
  --run-tag DEV_BOUNDED_RUN --proof-output /private/path/autonomy-proof.json \
  --aws-profile <authorized-dev-profile>
```

This example does not publish. Publishing additionally requires the exact site's
`--proof-s3-uri` and
`--publish-confirm PUBLISH_AS400_RD_AUTONOMOUS_PROOF_DEV`.
The configured `quadringent-autonomous-latest.json` key is separate from
Snowpipe JSONL, uses no-store and versioned storage, and has one writer.
The publisher refuses other site/bucket/prefix/name bindings.

An autonomy claim for the same window requires error-free bounded capture,
strict checkpoint progression through restart, all files automatically loaded,
`captured = raw distinct = canonical`, zero duplicates, a post-restart event
in canonical, no manual load during the run, no access or mutation of unrelated
connectors, and return to zero readers/pilot disabled. If unrelated workload
UIDs were not observed, do not claim their measured stability.

## Workload identities

`--aws-default-credentials` explicitly selects the SDK credential chain and
is mutually exclusive with `--aws-profile`. STS must verify the declared
DEV account before publishing. The option creates no identity and does not
prove that IRSA was selected: exclude static credentials/local profiles in
the workload and verify its actual role.

For Snowflake, `--snowflake-oidc-token-file` is mutually exclusive with a local
connection profile and accepts only an absolute projected-token path.
No authentication failure falls back to a local profile.
Use the dedicated verifier ServiceAccount, audience
`snowflakecomputing.com`, the declared verifier role/warehouse and a
WIF-compatible locked connector in `docker/verifier.Dockerfile`.
Snowflake identity alone grants no AWS publication permission.

These options do not install a periodic verifier or acquire capture windows.
Requalify workload identity and exact window on the target site.

## Isolated-run collection

Use `--run-id <run>` instead of capture-snapshot/object-key files; the modes
cannot be mixed. Reserve a fresh run before capture:

```text
<raw-prefix>/runs/<run>/
  console-snapshot.json
  batch-<identity>.jsonl
  batch-<identity>.manifest.json
```

The collector requires STOPPED_BUDGET, no run error and a snapshot at most
300 seconds old. It validates payload/manifest bytes, batch identity, counts
and cross-batch uniqueness, then rereads the snapshot to detect concurrency.
It selects exact reserved population rather than upload timestamps.

The resulting `stored_event_identity_proof` is based on
`verified_s3_batches`; it is **not an independent source oracle**.
RAW and canonical identities are read in sorted 1,000-row pages and compared
by counts and hash. Substitution, duplicates or population changes fail even
when counts match. Legacy local-file mode remains a counts check without this
identity supplement.

Budgets are 1,000 batches, ten listing pages, 32 MiB per object and 256 MiB
per window. Larger runs require explicit subdivision; no sampling or partial
success is produced.

The control plane consumes the combined proof read-only, retaining DEV
environment and live/historical/simulation provenance. Costs require the
separate metering view and collector in [FinOps](../finops.md).
Evaluate [SLOs](slo-alerting-dev.md) before a soak gate, and use
[C/U/D certification](sale-mutation-certification.md) for business correctness.
A reconciled technical ledger does not establish business mutations.

## Non-destructive rollback

First stop capture and disable pilot. Remove only Quadringent's notification:

```sh
python scripts/quadringent_s3_snowpipe_setup.py --rollback \
  --profile <authorized-dev-profile> \
  --execute --confirm ROLLBACK_AS400_RD_SNOWPIPE_S3_DEV
python scripts/quadringent_snowpipe_setup.py --rollback \
  --connection-name <authorized-connection> \
  --execute --confirm ROLLBACK_AS400_RD_AUTONOMOUS_LOAD_DEV
```

The Snowflake path pauses its pipe/warehouse; canonical view has no compute
to suspend. Preserve raw objects, checkpoints and tables.
Deletion requires a separate data-owner decision.

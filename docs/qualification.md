# Qualification harness

`src/quadringent_qualification/` provides a generic source/capture/storage/
warehouse harness without real site identities. Run it only on a dedicated
synthetic non-production table and journal under an explicit duration/cost budget.

## Verdict and coverage

PASS covers only the selected executed steps. `--steps all` means the steps
present in `config.steps`; it does not add omitted steps.
Reports expose `coverage.selected_steps_status`, `execution_mode`,
`required_steps`, `missing_required_steps` and
`complete_product_status`.

Required steps are seed, snapshot, capture, reconcile, freshness, changes1,
changes2, rotate and changes3. Missing or unsuccessful steps are uncovered.
The complete-product result remains NOT_VALIDATED without native outage,
observability and target-platform evidence. Even all selected steps passing in
real mode cannot substitute for that evidence.

`offline_fake` is simulation. Old reports missing execution mode or mirror
measurement retain unknown provenance; legacy PASS is not full qualification.
See [native qualification](product/native-qualification.md).

## Scenario

The default generator expects ORDER_ID, LABEL, CODE, AMOUNT, EVENT_DATE,
UPDATED_AT and NOTE:

1. Seed 100 deterministic synthetic rows covering null, empty text,
   accented/apostrophe text, padded CHAR, signed decimals, dates and timestamps.
2. Read and preserve the source journal boundary, then take the initial snapshot.
   Keep the source quiescent for this harness's snapshot boundary.
3. changes1: ten inserts, ten updates and five deletes; capture starts at the
   preserved boundary +1.
4. changes2: inserts/updates while the reader is stopped; resume from durable
   checkpoint, or the snapshot boundary if no checkpoint exists.
5. Rotate the dedicated receiver, apply changes3, capture and prove continuity.
6. Reconcile synthetic oracle, directly reread source, materialized history
   and mirror values. List exact missing/extra/duplicate keys, divergent values,
   before-image inconsistencies, journal gaps/duplicates/unexpected events,
   pre-bootstrap events and divergent replays.
7. Measure freshness using three isolated run-bound update markers, each
   captured, loaded and reread in the mirror before the next.

Receiver order comes from the source's attachment catalog and receiver/sequence
row positions; a sequence-only oracle is refused. Counts alone cannot pass.
Missing mirror read cannot pass.

## Bounded freshness measurement

`freshness.details.mirror_measurement` records:

```text
target: snowflake_mirror
metric: write_to_mirror_observed_upper_bound
scope: sql_loader_warmed_continuous_docker_capture
steady_state_streaming: false
count: 3
p95_seconds, max_seconds, slo_seconds, accepted, status, probes
```

The default SLO is ten seconds. PASS requires three distinct nonempty markers,
finite positive durations, integer poll counts 1–40, a positive threshold,
and maximum ≤ threshold. For three nearest-rank probes, p95 and maximum must
equal the observed maximum. Validate the declared scope/mode. Raw freshness
never establishes mirror visibility.

Warm the continuous reader to a completed idle/caught-up poll, the SQL
connection with a no-cache control query, the loader's schema/catch-up/receipt
cache, and the JDBC measurement client before timing. Start each update timer
before sending it to that ready client. Raw publication, real receipt validation,
SQL loading and mirror read remain included. This is an observed upper bound,
not physical JDBC acknowledgement timing.

Missing marker or exhausted budget fails. Keep reader/connection active through
all three probes, perform exhaustive raw reconciliation afterward, and close/
verify removal of the owned container even on failure. Do not relabel earlier
cold-start observations as warm ones. This does not qualify permanent Streaming,
all platforms or a universal latency maximum.

## Configuration

Use YAML/JSON with secret references, never plaintext credentials.
The full schema is `quadringent_qualification.config`.

```yaml
run_id: "${QUALIF_RUN_UUID}"
table:
  qualified_name: "${QUALIF_LIBRARY}.QUALIF_ORDERS"
  primary_key: ORDER_ID
  columns:
    - {name: ORDER_ID, kind: integer}
    - {name: LABEL, kind: varchar, length: 40}
    - {name: CODE, kind: char, length: 8}
    - {name: AMOUNT, kind: decimal, precision: 11, scale: 2}
    - {name: EVENT_DATE, kind: date}
    - {name: UPDATED_AT, kind: timestamp, timestamp_precision: 6}
    - {name: NOTE, kind: varchar, length: 80}
source:
  driver: ibmi_java
  library_whitelist: ["${QUALIF_LIBRARY}"]
  connection_secret_file: "${QUALIF_SOURCE_SECRET_FILE}"
  journal_library: "${QUALIF_LIBRARY}"
  journal_name: "${QUALIF_JOURNAL_NAME}"
  source_time_zone: "${QUALIF_SOURCE_TIME_ZONE}"
  allow_dml: false
  allow_rotation: false
capture:
  image: "${QUALIF_CAPTURE_IMAGE}"
  max_seconds: 180
storage:
  backend: gcs
  bucket: "${QUALIF_BUCKET}"
  raw_prefix: "qualification/${QUALIF_RUN_UUID}"
  checkpoint_location: "${QUALIF_STATE_LOCATION}"
warehouse:
  loader: snowflake
  account_secret_file: "${QUALIF_SNOWFLAKE_SECRET_FILE}"
  database: "${QUALIF_SF_DATABASE}"
  schema: "${QUALIF_SF_SCHEMA}"
steps: [seed, snapshot, changes1, capture, changes2, capture, rotate, changes3, capture, reconcile, freshness]
bootstrap_receiver: null
bootstrap_sequence: null
```

Unresolved variables fail with the complete missing-variable list.
Bootstrap fields are both null when snapshot establishes the boundary, otherwise
provided together. The real loader requires a canonical UUID run ID and a
dedicated Snowflake schema ending with `_` plus the first twelve uppercase
hex characters of SHA256(run_id). It creates no schema or grant.

`checkpoint_location` is an existing DynamoDB table for S3 or state bucket for
GCS. AWS capture requires `AWS_DEFAULT_REGION`, falling back to
`AWS_REGION`; no region is invented. AWS variables are not sent to GCS capture.

## Real adapter prerequisites

Use Linux with Docker, an already locally available capture image pinned by
digest (`--pull=never`), cloud identity and private source/warehouse files.
Host networking permits the VM workload identity; static AWS keys are not
passed as container arguments.

The IBM i file contains host/user/password, is runner-owned `0600` and is not
a symlink. A macOS `keychain:<service>` reference additionally requires
source host/user configuration. The password reaches the driver by stdin.
TLS is required.

DML and receiver rotation are independent opt-in permissions, false by default.
Verify dedicated ownership/content before enabling them. The driver restricts
the generated operations, table and actual attached journal.

The Snowflake file contains account/user/role/private_key_pem, is runner-owned
`0600` without a symlink, and uses the product QDT_ROLE_* / QDT_WH_* convention.
Keys never enter process arguments or reports.

Real storage reuses bounded S3/GCS reads. The warehouse adapter runs product SQL
loading, reads history and mirror under row limits, and checks complete canonical
envelopes against verified raw references. Repeated identical/divergent raw
references are reported explicitly; their counts do not measure internal SDK
retries. Retain raw evidence even when loading rejects a conflict.

## CLI and failure classes

```sh
python -m quadringent_qualification.cli run --config /private/path/run.yaml --steps all
python -m quadringent_qualification.cli run --config /private/path/run.yaml --steps seed,snapshot,reconcile
python -m quadringent_qualification.cli report --run-json run/<run_id>/report.json \
  --out-markdown run/<run_id>/report.md
```

Run output includes full differences in report.json and a scoped Markdown summary.
Costs remain explicitly absent until measured. `--offline-fake` exercises
composition with in-memory adapters and records simulation; it does not certify
a source or warehouse.

Exit 2 denotes configuration failure, 3 incomplete adapter wiring, and 1 a
failed run. Distinguish product failure, unavailable source infrastructure,
and harness failure; do not collapse them into one product-defect boolean.
Adapter exceptions expose type/classification rather than potentially sensitive
server text.

For a new target, provide a dedicated scenario generator if its columns differ,
validate configuration offline, then run a bounded authorized real subset before
the full scenario. A real nightly service also requires protected CI secret
delivery, scheduling, measured complete runs and multi-run reporting.
This package alone installs no nightly qualification service.

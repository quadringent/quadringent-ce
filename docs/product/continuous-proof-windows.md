# Proof windows during continuous capture

Capture is a process; a closed proof window is a separate immutable evidence
object. Closing a window never asserts that capture stopped. A passed window
does not certify later windows or current process freshness.

## Durable scans and closure

The raw-first ordering is **raw/manifest → receipt → scan index → checkpoint**.
Receipts bind exact raw keys, counts, hashes, scanned start, final watermark,
predecessor and receiver rotation. Empty scans also receive receipts.
A callback after checkpoint commit is insufficient.

The checkpoint uses compare-and-swap against the exact predecessor:
a DynamoDB condition, GCS generation condition, or local JSON-store locking.
Do not substitute a later checkpoint read. Publication and CAS durations remain
distinct.

An intent fixes identity, boundary and duration before capture. Durations are
600–3600 seconds; closure grace is 0–120 seconds, default 60.
v2 receipts record `scan_completed_at` immediately after the source runner
returns, before CAS. Replay preserves the original date and population;
v1 archives do not receive fabricated dates.

Closure uses the first committed complete scan reaching the deadline and keeps
the whole receipt chain. Dates must be ordered, within bounds and nonfuture.
No latency-based exclusions, overlaps, gaps or retrospective boundary changes
are allowed. An idle poll without a scan is not an empty receipt.
Expiration without an eligible scan fails qualification.

`sealed_at` records the closure decision before recovery reads and PUT.
It does not measure object-storage durability or establish clock authority.

## Recovery and exact raw reads

Recover an existing closure before consulting the current checkpoint.
Under writer exclusion, reconstruct only the committed chain and exclude its
uncommitted tail. Permission errors or corrupt closures must not trigger
reconstruction.

The immutable `scan-index/end-<sha256>.json` binds a receipt key and hash.
Missing indexes for committed checkpoints are errors. The 4,096-receipt limit
applies to the recovered window; no historical prefix-wide LIST is required.
Revalidate collected hashes at sealing. Exceeding a budget means incomplete
evidence, never sampling.

`collect_closed_window_raw` rereads exact receipt, manifest and payload bytes,
checks hashes, batch identity, interval, unique population and table scope.
Its cumulative 256 MiB raw-byte budget does not bound decoded Python memory.
Local exclusive writes and directory synchronization cover tested concurrent
writers; they do not certify power-loss behavior or cloud concurrency.

## Destination verification

`verify_closed_window_destination` observes already loaded Snowflake data
through read-only reconciliation. It requires temporal v2 closure, exact DEV
storage binding and an observation no earlier than the closure decision.
An empty window yields `destination.state=not_tested`, with no SQL query or
invented latency. Size windows to the 1,000-file destination limit independently
of the 4,096-receipt metadata limit.

Legacy `*_after_second` counter names do not prove replay here: this path
loads nothing. Its independent process state remains `not_observed`.

```sh
python scripts/quadringent_window_verify.py \
  --run-id <run> --window-id <window> \
  --proof-output /private/path/new-window-proof.json \
  --connection-name <authorized-connection>
```

Exit codes: 0 matched, 2 pending/not_tested, 3 operational error.
`--await-window` permits pending when the initial closure is absent, before
Snowflake connection. Raw missing after closure remains an error.
Pending creates no proof; not_tested records that explicit result.
Existing output files are not overwritten. There is no implicit wait loop,
capture start, permission change or DML.

## Publication and supervision

`--publish-window-proof` is opt-in. It requires a v2 intent bound to the stream;
local simulation cannot be published. Publication uses conditional create and
exact readback. A differing existing object is never replaced.

States distinguish `not_attempted`, `confirmed` and `unknown`.
An uncertain PUT remains unknown even if it may have created the object.
Preserve the local original before retrying. `--resume-publication` rereads
raw and Snowflake and compares canonical bytes, retaining only the original
observation date. That date must fall between closure and the new observation;
it is retained provenance, not a newly certified historical observation.
Any other difference rejects resumption.

`quadringent_window_supervise.py` verifies already closed windows under a
global budget: 30–5,400 seconds total, 5–120 seconds per attempt and
1–60 seconds between attempts; defaults 300/60/10. It kills and waits for
timed-out children. Pending and uncertain publication can retry; permanent
verification errors stop. A local error after uncertain publication retains
unknown status.

Single-window mode uses `--window-id` and `--proof-output`.
Chain mode uses `--chain-proof-directory`, verifies closed windows in order
under one deadline, and refuses changed stream/count/initial-window bindings
or changed completed prefixes. Neither mode starts capture.

## Worker and cockpit

The worker's opt-in `--proof-window-id` /
`--proof-window-seconds` path requires the isolated DEV table, journal,
reserved run and existing checkpoint. Tail or explicit bootstrap is refused.
Prepare/recover the immutable intent before JVM/catalog access. A stale open
intent cannot be redated into a new window.

The pilot chart exposes `pilot.proofWindow.enabled`, `id` and `seconds`,
disabled by default. It requires checkpoint bootstrap and a capture budget
covering window duration, rounded reader reserve and 60 seconds for closure.
The permanent Deployment remains at zero. The verifier's publication supervisor
has additional bounded shutdown/verification reserve. See the generated
[values reference](chart-values.md) for current controls.

REST carries `window_destination_proof` and projects `window_delivery`
separately from current capture. The intent's immutable `stream_id` must
match. v1 intents remain archival inputs, not attached delivery proof.
Freshness comes from closure, not a new Snowflake read. Local evidence remains
simulation. Invalid evidence is explicit; unreadable delivery leaves capture
available with `unavailable` delivery. Capture-read failure also limits the
freshness of retained window proof.

`--window-proof source-id=URI` joins a declared proof without rewriting capture.
Storage path, run/window identities and stream must agree.
No synthetic `STOPPED_BUDGET` snapshot may substitute for closed-window evidence.

Runtime qualification still requires authenticated clock/durability observations,
independent source oracles, business C/U/D, outage recovery and the approved
capture/cost budget. Implementation and local tests alone do not establish those
external effects.

# Snowflake history and mirror

Each source table has append-oriented technical history and a business mirror.
A new installation uses synchronous SQL history followed by loader-driven mirror
MERGE. Snowpipe Streaming is an explicit alternative. See
[the product decision](../decisions/2026-09-23-snowflake-mirror.md).

```text
durable raw batch
  → HistorySqlLoader: idempotent HISTORY MERGE
  → confirmed history write
  → MirrorMergePlan: business-key MIRROR MERGE
  → durable loader checkpoint
```

Streaming replaces the history MERGE with a channel append and confirmed
commit. The default loader-driven mirror requires no scheduled task.
Expert task-driven mirroring is separate and requires its task permissions.

## DDL and type fidelity

`TableDestinationPlan` generates history/mirror DDL from discovered IBM i
columns and adds technical columns.

| IBM i | Snowflake | Bound |
|---|---|---|
| CHAR/VARCHAR | VARCHAR | Declared character length |
| GRAPHIC/VARGRAPHIC | VARCHAR | Unicode character length |
| DECIMAL/NUMERIC | NUMBER(p,s) | Precision ≤38 |
| SMALLINT / INTEGER / BIGINT | NUMBER(5,0) / NUMBER(10,0) / NUMBER(19,0) | Exact integers |
| DATE / TIME | DATE / TIME | Declared type |
| TIMESTAMP(p) | TIMESTAMP_NTZ(p) | Precision 0–9 |
| BINARY/VARBINARY/BLOB | BINARY(n) | ≤8,388,608 bytes |
| CLOB/DBCLOB | VARCHAR(n) | ≤16,777,216 characters |

Reject unmapped types with `UnsupportedColumnTypeError`; never silently cast
or truncate. Character CCSID 65535 must be reclassified as binary during
discovery.

Both tables retain `EVENT_ID`, `JOURNAL_RECEIVER`,
`JOURNAL_SEQUENCE NUMBER(38,0)` and `COMMIT_TIMESTAMP TIMESTAMP_NTZ(6)`.
History adds `OPERATION` and `INGESTED_AT TIMESTAMP_LTZ`.
Mirror adds `MIRROR_UPDATED_AT TIMESTAMP_LTZ` and an informational,
unenforced primary key on the declared business columns.

## SQL history

`QUADRINGENT_HISTORY_MODE=sql` uses parameterized history MERGE batches of
at most 100 events, with one writer per table. `EVENT_ID` binds identity.
After interruption between history and mirror, reread the durable receipt:
history skips present events, mirror replays, then checkpoint advances.

Preserve raw decimal text rather than converting through float. Validate binary
base64 envelopes before mutation. Warehouse usage occurs per batch; large
backlogs can multiply queries. Measure site costs and keep autosuspend bounded.

New-install defaults use one-second reader/loader polling, SQL history and
disabled SDK per-batch flush. These target low latency but do not guarantee
visibility within ten seconds. Warehouse wake-up, source capture, volume and
MERGE time remain part of the observed path.

Reinstallation reads effective Helm values and preserves history mode, intervals
and flush. An unreadable or incomplete existing profile blocks upgrade.
Offline dry run shows the new-install profile. Manual Helm upgrades must carry
reviewed effective values. Deliberate SQL/Streaming changes require stopping
the previous writer and a new qualified baseline.

Idle cycles reread object receipts without periodic Snowflake lag queries,
allowing warehouse autosuspend. Historical deliveries remain visible with their
age; idle source freshness is not invented.

The shared receipt LRU holds at most 5,000 entries and 16 MiB. Scope includes
storage identity and prefix. Cache hits revalidate chain conflicts and
predecessors; mutable or unrelated objects are not cached. Listing and
population checks remain real on every cycle.

## Streaming history and restart

Channels are deterministic per table and initial-copy run
(`channel_name_for(scope, history_table, stream_id)`). Restarting that run
reopens the same channel; a fresh copy gets a new channel/checkpoint.
Offset tokens encode `RECEIVER:SEQUENCE` with padded sequence text.

Restart combines committed channel offset and durable loader checkpoint.
Receipts link windows and empty receiver rotations. An interruption after
Streaming commit but before checkpoint replays mirror without reappending
confirmed receipts. A recreated channel without an offset checks history
`EVENT_ID` before append.

A new initial copy waits for durable evidence, clears/rebuilds its mirror,
preserves prior-run history and binds checkpoint to the copy boundary.
A receipt crossing the boundary is validated before selecting post-boundary
events.

Mirror MERGE deduplicates `EVENT_ID`, then selects the latest business-key
change using receipt-proved receiver order and sequence. Receiver names or
sequence alone do not order rotations. Ignore `u_before`; it must not replace
current state. A matched delete removes the row; an unmatched delete inserts
nothing. Replay is safe after a confirmed history write.

## Cycle measurements

Existing loader logs emit `loader_cycle` for a pass that appends events or
materializes mirror, and for failure. Idle or checkpoint-only empty windows
emit no success-cycle record.

`quadringent-loader-cycle-v1` carries UTC `started_at`/`finished_at`,
monotonic `cycle_ms` and nullable cumulative `stages_ms`:

| Stage | Included work |
|---|---|
| `discovery_raw` | Raw discovery/read |
| `open_channel` | Client/channel creation |
| `append` | Append |
| `flush` | Explicit flush request |
| `commit` | History commit wait |
| `merge` | Mirror materialization, including fresh-copy clearing |
| `checkpoint` | Durable checkpoint |

Unexecuted stages remain null. Validation, existing EVENT_ID checks and transient
client cleanup remain in total time without assignment to those seven stages.
`failed_stage` names the interruption or `prepare`.

The record contains no exception, business key, journal position or account
identity. `table_tag` hashes the internal manifest table identity. v2 only
projects records for the exact resolved pipeline/table/destination, from at most
three Pods and 2,000 log lines.

These are **loader cycle durations**, not IBM i acknowledgement-to-mirror
latency or pre-discovery waiting time. They do not populate business-lag series
or prove the ten-second threshold.

## Configuration, grants and observations

See [chart values](chart-values.md) and [API v2](../api-v2.md) for current
loader mode, polling, destination and profile-secret wiring. The executor uses
its explicitly declared loader image and workload identity.

Streaming destination setup permits the application role to create and own
history/mirror tables in its authorized schema. Ownership covers table DML;
task-driven mirroring additionally needs `EXECUTE TASK`. Classic Snowpipe
and Streaming have distinct setup paths; do not assume a classic pipe is
required for every Streaming SDK mode.

`LagQueryPlan` measures age of the latest committed event using
`DATEDIFF` on `MAX(COMMIT_TIMESTAMP)`. Empty tables return None, not zero.
`PipelineObservation.history_lag_seconds` and `mirror_lag_seconds`
remain None with explicit reasons until a real observation provider supplies
them. Age of the latest mutation is distinct from measured write-to-mirror
delivery latency.

Historical DEV experiments had slow Streaming commits and cold-start SQL
latencies above ten seconds. They support bounded diagnosis rather than a
maximum-latency promise. This release still requires fresh native qualification;
see [native qualification](native-qualification.md).

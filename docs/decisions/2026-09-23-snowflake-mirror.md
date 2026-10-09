# Decision: Snowflake history and mirror

Date: 2026-09-23. Status: accepted historical design decision.
The current small-flow installer defaults to SQL history; this decision records
the separate Streaming comparison, not a qualification of the current release.

Each source table has a timestamped change history and a mirror containing one
row per key with the source's columns/types. The target is seconds between an
IBM i mutation and Snowflake visibility; measurements must establish it.

## Historical measurements

One qualification account, 103 actual events replayed, five-second send spacing.
These measure submission to the Snowflake client → visibility:

| Path | Median | p95 | Notes |
|---|---:|---:|---|
| Snowpipe Streaming high-performance Python SDK history | 5.2 s | 7.2 s | Channel and offset token |
| Mirror B: loader MERGE after each flush | 6.3 s | 8.3 s | No scheduled object or extra privilege |
| Mirror A: stream + triggered task | ≈4.5 s incremental | ≈8.7 s | Minimum interval 10 s; owner needs EXECUTE TASK |
| Mirror C: dynamic table | — | ≈41 s | Minimum TARGET_LAG one minute |

All three mirrors reconciled to the oracle (110 keys, effective deletes, zero
differences). Separate IBM i → durable raw measurements were median 4.5 s and p95
8 s. Estimated end-to-end mirror B was about 11 s median and 15 s p95; these
estimates are not measured end-to-end proof.

## Decision and limits

Streaming history uses a stable named channel and resumes from its last known
offset. Mirror B is the default mirror; task-driven A remains an expert option
requiring EXECUTE TASK. Dynamic tables are not used for the real-time mirror.

event_id deduplication remains required in reads and MERGE: replaying an offset
or changing channel names can reinsert rows, observed on both old and new channels.

One account/table at low volume does not establish universal latency or cost.
Streaming cost views were not available when these measurements were taken.
Continuous qualification must repeat the measurement.

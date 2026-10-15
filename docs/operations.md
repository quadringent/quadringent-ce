# Operations and recovery

Read `run.state`, source errors, checkpoints and continuity evidence before
acting. `phase` describes table progress and does not prove an active reader.
A fresh probe must confirm that the stop cause is resolved; repeated sign-ons
are unsuitable for checking a blocked account.

Available actions are `prepare`, `start`, `pause`, `resume`, `refresh`.
The service reports capabilities; the interface offers no unavailable action.
Mutations require explicit confirmation. A receipt separates recorded intent,
execution and observed effects read back afterward.

## Parked reader and completed Job

Resuming a parked reader requires a valid checkpoint, proven continuity,
a catalog probe less than 900 s old and never future-dated, consistent durable
preparation, and a successful read proving the old Job is absent. The Job name
remains derived from the intent; concurrent requests cannot create two readers
under different names.

The reader template requires a post-completion TTL (60 s by default if absent
from the template). The example uses 60 s. Kubernetes removes the Job **after**
completion; the control plane has no deletion permission. While the Job exists,
whether active or terminal, parked resume is unavailable. A Complete or Failed
Job never yields a RUNNING receipt. TTL affects neither S3 raw data,
checkpoints nor audit. Collect pod logs before cleanup when retention is needed.

After cleanup, the action recreates the same name at the durable checkpoint.
A `reader_relaunched` receipt proves a nonterminal Job was read back; it does
not yet prove arrival in Snowflake. Wait for new delivery evidence. Without
a TTL controller, an old Job TTL or continuity, resume remains blocked for
authorized intervention. There is no automatic restart loop or generic pod
restart button.

## Journal freshness and adaptive idle sleep

The full receiver catalog (`QSYS2.JOURNAL_RECEIVER_INFO`, all recent receivers)
takes several seconds on a shared IBM i. It is reused up to
`AS400_RECEIVER_CATALOG_CACHE_SECONDS` /
`AS400_RECEIVER_CATALOG_CACHE_POLLS` (defaults: 60 s / 30 polls). Without another
mechanism, a write just after refresh could remain invisible for a minute.

**Tail probe** (`AS400_TAIL_PROBE`, on by default): at each poll while the
catalog remains cached, the Java worker answers a `tail` command limited to
one SQL row filtered on `STATUS = 'ATTACHED'`. This is cheaper than the full
catalog. If it sees the same attached receiver with a larger
`LAST_SEQUENCE_NUMBER`, the cache advances in place, never backward. The next
poll sees the entry without a full catalog read. If the attached receiver
changes through rotation, the cache is invalidated and the next poll reloads
the complete catalog. A timeout, exception or absent attached receiver leaves
the cache unchanged. `AS400_TAIL_PROBE=false` restores full-catalog reads only
on cache expiry.

**Adaptive idle sleep:** after publication or catch-up, wait restarts at
`AS400_MIN_POLL_SECONDS` (default: 1 s), then doubles on consecutive idle polls
up to `AS400_POLL_SECONDS`. A quiet flow reaches the ceiling only after several
idle polls; it returns to roughly one-second polling when writes resume.

Measure on the target IBM i: actual tail-probe cost compared with a full
catalog, and p50/p95 write-to-raw-publication latency with both mechanisms
enabled. Offline development does not establish those measurements.

## Stage timing

For a source build after 0.4.5, set `QUADRINGENT_STAGE_TIMINGS=true` in the
reader and destination-loader processes to log `stage_timing` JSON records.
The flag is off by default. Records separate native checks, capture, RAW
publication, both loader preflights, SQL execution and checkpoint CAS, using
hashed capture/checkpoint identities and batch digests for correlation.
SQL text, parameters, row values and exception messages are omitted.
Each trace limits stage records and reports truncation. Durations use the
process monotonic clock; UTC observations have no measured clock-error bound.
These records do not prove source-to-mirror latency or a committed transaction.

## Audit

`actions.jsonl` lives on the fleet state volume and records intent, request
ID, action, connection, author, timestamp, receipt and observed effect.
Files use mode 0600, are synchronized before execution and reject symlinks.
Back up this volume; the local log is not tamperproof. Define rotation and
retention per site and monitor disk space.

With an authenticated proxy, the author is its verified identity. Without
authentication, attribution is only `local-port-forward`, never a human
identity. `audit_unavailable` means no action executed.
`audit_result_unavailable` means an action may have occurred but its result was
not written: read the state before retrying. Capabilities then remain hidden
until repair and a controlled restart.

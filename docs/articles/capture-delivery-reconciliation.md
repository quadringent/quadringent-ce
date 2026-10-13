# Capture is not delivery: proving IBM i data reached Snowflake

<!-- description: Why a running IBM i reader does not prove that rows reached Snowflake, which evidence to ask for at each stage, and how Quadringent shows it. -->

A green status on a capture tool usually means one thing: a process is running.
It does not tell you that the change you made on the IBM i at 10:02 is in
Snowflake at 10:03, and that it is the same change. This article separates three
questions that are easy to blur, says what evidence answers each, and lists what
to ask any vendor to show you before you rely on a copy.

## Three questions, three kinds of evidence

1. **Capture.** Did the tool read the change from the IBM i journal?
2. **Delivery.** Did the change land in Snowflake, once, in order?
3. **Reconciliation.** Does what is in Snowflake match what is in the source?

Each question needs its own evidence. Evidence for the first does not answer the
second, and evidence for the second does not answer the third. Quadringent's
README says it plainly: active capture does not guarantee that the destination is
up to date ([README](../../README.md)).
<!-- source: README.md, docs/product/operator-guide.md -->

## Why "the reader is running" proves little

A reader process can be healthy while the data path is not. Each failure below is
plausible, and a status page that shows only process health would miss it:

- The reader is stopped at a checkpoint and polls an empty window. The process
  is alive. The journal moved on.
- The reader read the change and wrote it to storage, but the loader failed on
  that batch, for example because of a column type it cannot map, a permission,
  or a suspended or over-quota warehouse.
- The loader retried after a timeout and applied a batch twice.
- An update or delete did not find its row, so a `MERGE` changed nothing, and the
  call still succeeded.
- Two changes to the same key sit in one batch. Snowflake documents that when
  several source rows match one target row, `MERGE` returns an error by default,
  and with `ERROR_ON_NONDETERMINISTIC_MERGE` set to false it uses a row that is
  "not defined" ([Snowflake: MERGE](https://docs.snowflake.com/en/sql-reference/sql/merge)).

In each case a pod is Ready, an API answers, and an action was accepted. The
Quadringent operator guide says that none of these is enough to accept
replication: acceptance concerns the data that was actually delivered
([operator guide](../product/operator-guide.md), [pilot preparation](../product/positioning-and-offer.md)).
<!-- source: docs/product/operator-guide.md, docs/product/positioning-and-offer.md -->

## Position is the common language

To compare stages you need one coordinate that every stage can state. On IBM i
that coordinate is the journal position: the receiver name and the sequence
number ([IBM: DISPLAY_JOURNAL](https://www.ibm.com/docs/en/i/7.5?topic=services-display-journal-table-function),
[IBM: receiver chains](https://www.ibm.com/docs/en/i/7.5.0?topic=receivers-journal-receiver-chains)). Capture can say "I have read up to receiver R, sequence N". Delivery can
say "I have loaded everything up to the same R and N". Reconciliation can say
"for the window after A and up to N, these counts and keys agree".

If a stage gives only a time, or a count with no position, you cannot tell
whether it is ahead, behind, or looking at a different window. Quadringent's
contract goes as far as stating that counts and equal timestamps do not infer the
mapping between a Snowflake offset and an IBM i position
([delivery evidence contract](../product/quadringent-destination-proof-contract.md)).

## What evidence to ask for

| Stage | The question | Evidence worth asking for | What it still does not prove |
|---|---|---|---|
| Capture | Is the reader at the head of the journal? | Last position read, time of that observation, and the journal's own last position | That the data left the reader |
| Stored | Is the change durable before the position moves? | A durable batch, then the position advance, in that order | That Snowflake has it |
| Delivery | Did the loader apply everything up to the position? | Loader position equal to source position, events loaded, failed events (should be zero) | That nothing is missing or duplicated inside the window |
| Application | Did the destination tables change? | Apply position equal to loader position, failed mutations (should be zero) | That values are right |
| Reconciliation | Do source and destination agree on one window? | Captured, loaded and distinct event counts equal; duplicate, missing and unexpected counts zero; the window's start and end | That business rows match, if it counts events only |
| Freshness | Is each observation current? | A timestamp on every observation, and a rule for when it goes stale | Anything about the period after it |

The wording of the last column matters. A tool that counts technical events can
say "every event arrived once". It cannot say "the balance on account 12 is
right". That needs a comparison of values or checksums by key, in addition to
event identity ([delivery evidence contract](../product/quadringent-destination-proof-contract.md)).

## Row counts alone are weak

A count of rows in the source table and in Snowflake is a useful smoke test, and
no more. Equal counts can hide one missing insert and one duplicate. They say
nothing about updates, which change values but not counts. A delete followed by a
re-insert can leave the count unchanged. If you reconcile with counts, add a
checksum or a key-by-key comparison on a bounded set, at a known position, and
state how you handled changes that arrived during the comparison.

Some tools add sync columns to the destination, for example `_fivetran_synced`
in Fivetran's Db2 for i connector
([Fivetran documentation](https://fivetran.com/docs/connectors/databases/hva-db2i)).
Such a column says when a row was synced. It does not say that the row matches
the source.

## Missing evidence is not zero

A dashboard that shows 0 failed events when it did not measure is worse than one
that shows nothing. Ask the vendor what the screen shows when a stage has no data,
when an observation is old, and when the data is a demonstration or a replay of
history. In Quadringent these cases have names: Observed, Stale, Synthetic or
Historical, and **Not measured**. "Not measured" shows no time and no count
([operator guide](../product/operator-guide.md#4-read-the-evidence)).

## Questions to ask any vendor

1. Show me the position of the reader and the position of the last row applied in
   Snowflake, side by side, for one table.
2. What do you check to say that a window is reconciled, and what does it count?
3. What happens if the loader fails after the reader advanced?
4. What does the screen say when you have not measured something?
5. Can I run your check myself against my own tables, and see the query?
6. Which of these did you test on my IBM i release, and when?

## A check you can run yourself

Before you trust any tool, do a small controlled test on a non-production table.

1. Pick a journaled table with a primary key and a few thousand rows.
2. After the initial copy, record the source position and a checksum per key.
3. Make known changes: insert a few rows, update some, delete some.
4. Wait for the tool to report the new position, then compare keys and checksums
   in Snowflake with the source at that same position.
5. Stop the reader, make more changes, restart it, and compare again.

The last step is the one that tells you the most.

## What Quadringent does

Quadringent keeps the three questions apart. Journal capture, Snowflake delivery
and destination reconciliation are separate stages in the cockpit, each with its
own state and time. The optional `destination_proof` extension carries the
positions, the counts and the activation checks, and the verdict follows fixed
rules. Healthy delivery needs a matching position at every stage, zero failed
events, and a fresh matched reconciliation of the same window. Simulated and
historical evidence cannot show as healthy. If a source goes quiet, the stage
becomes stale or unknown instead of showing green.
<!-- source: docs/product/quadringent-destination-proof-contract.md, docs/product/operator-guide.md, docs/architecture.md -->

Its limits are plain. The reconciliation counts technical events, not final
business rows. A successful SQL call alone is not accepted as proof of application:
the checkpoint and its IBM i position must be committed and persisted.
Value reconciliation by key is a separate check that this contract does not
cover. And the evidence is a preview: the published receipts are dated, name their version and
scope, and describe synthetic tables. For example, the 0.3.4 readback matched 136
history images and 110 mirror rows per site on two synthetic tables, and the same
campaign recorded latency measurements where the ten-second gate failed on three
targets. Those facts apply to 0.3.4 and nothing else. See the
[README](../../README.md), the [qualification summary](../../site/qualification-0.3.4.json)
and [qualification](../qualification.md).
<!-- source: README.md, site/qualification-0.3.4.json, docs/qualification.md, docs/product/quadringent-destination-proof-contract.md -->

You can see how the evidence looks without any IBM i or Snowflake: the
[synthetic demo](../product/synthetic-demo.md) runs the real cockpit with generated
data and makes no network connection. Quadringent is a DEV/INT preview and no
production qualification is claimed.
<!-- source: docs/product/synthetic-demo.md -->

For how the reader gets the data in the first place, read
[How IBM i journals work for change data capture](ibm-i-journals-for-cdc.md). For
the other ways to move data to Snowflake, read
[Options to replicate AS/400 (IBM i) data to Snowflake](as400-to-snowflake-options.md).

## Sources

All pages were accessed on 2026-10-13.

- Quadringent: [delivery evidence contract](../product/quadringent-destination-proof-contract.md), [operator guide](../product/operator-guide.md), [README](../../README.md)
- Fivetran: [Db2 for i connector](https://fivetran.com/docs/connectors/databases/hva-db2i), for the sync system columns
- IBM: [DISPLAY_JOURNAL](https://www.ibm.com/docs/en/i/7.5?topic=services-display-journal-table-function) and [journal receiver chains](https://www.ibm.com/docs/en/i/7.5.0?topic=receivers-journal-receiver-chains) for the receiver and sequence number that make up a journal position
- Snowflake: [MERGE](https://docs.snowflake.com/en/sql-reference/sql/merge), for the behavior when several source rows match one target row

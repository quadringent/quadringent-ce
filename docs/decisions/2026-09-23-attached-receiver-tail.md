# Decision: read the final entry of an attached receiver

Date: 2026-09-23. Status: accepted.

plan_next_window previously excluded last_sequence on an ATTACHED receiver.
An isolated change could remain invisible until another write or receiver rotation.

## Historical measurements

A shared IBM i 7.5 qualification server, *BOTH-journaled test table, isolated
writes without commitment control. Read [last_sequence, last_sequence] immediately,
then reread five seconds later:

| Path | Iterations | Complete first read | Identical after five seconds |
|---|---:|---:|---:|
| QSYS2.DISPLAY_JOURNAL / displayJournalSql | 20 | 20 | 20 |
| RetrieveJournal / PersistentJournalWorker.processWindow | 10 | 10 durable u_after batches | 10 |

An apparent first-harness hang after window_flushed came from select() on an
already buffered stream; it did not establish a reader fault.

## Decision and limits

Both paths may end a window at an attached receiver's last_sequence, including
immediately after a receiver transition.

This measurement excludes deferred COMMIT behavior; uncommitted transaction
entries remain a separate question. It covers one IBM i version on a shared
server and must be repeated in continuous qualification.

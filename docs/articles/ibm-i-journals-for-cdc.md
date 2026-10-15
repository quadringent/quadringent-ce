# How IBM i journals work for change data capture

<!-- description: How IBM i journals, receivers, record images and retention work, and what a change data capture reader needs from them, from DISPLAY_JOURNAL to MNGRCV. -->

Every change data capture tool for IBM i (AS/400) reads the same thing: the
journal. The journal settings on your system decide what a capture tool can see,
how long it can be offline, and whether it can recover. We explain those
settings in plain terms below and link the IBM documentation behind each one.
The article is for an IBM i administrator and the data engineer who works with
one.

## Journals and receivers

A journal is an object that records changes. Each record in it is a journal
entry. The journal writes its entries into another object, a journal receiver,
and a receiver is attached to the journal at any time to receive them
([IBM: journal receiver chains](https://www.ibm.com/docs/en/i/7.5.0?topic=receivers-journal-receiver-chains)).
IBM describes journaling as a way to recover changes after an outage, to keep an
audit trail, and to support replication of objects
([IBM: journal management concepts](https://www.ibm.com/docs/en/i/7.5?topic=ssw_ibm_i_75/rzaki/rzakiconcepts.htm)).
Capture tools use the third.

A journal can protect many tables. Capture tools read one journal at a time. Qlik
documents that all source tables of one task must share a journal, and Fivetran
HVR says the same for tables in one channel. Tables in different journals need
separate readers.

## What a row change looks like

Changes to rows of a database file use journal code `R`. The entry type says what
happened. IBM lists the record-level types selected by `*RCD` as `BR`, `DL`, `DR`,
`IL`, `PT`, `PX`, `UB`, `UP` and `UR`
([IBM: QjoRetrieveJournalEntries](https://www.ibm.com/docs/en/i/7.5?topic=ssw_ibm_i_75/apis/QJORJRNE.htm)).
For capture, the types for a row added, a row updated and a row deleted matter
most. IBM's support page on entry-specific data defines `UB` and `UP` as the
before and after images of an updated row
([IBM support](https://www.ibm.com/support/pages/reading-entry-specific-data-joesd-journal-receiver)).
The API page lists `PT` and `DL` but does not define them. Look up the added and
deleted types in the journal entry reference of IBM documentation for your
release before you build on them.

The row itself is in the entry-specific data (`ENTRY_DATA`). It is a block of
bytes in the layout of the file. A reader must cut it into fields using the file
definition and handle character sets and null indicators. IBM's own example does
this by hand with SQL for fixed-length fields only
([IBM support](https://www.ibm.com/support/pages/example-how-extract-and-read-entrydata-joesd-journal-receiver-field-using-displayjournal-table-function)).
Variable-length and large-object columns are harder.

## Images: `*AFTER` and `*BOTH`

`STRJRNPF` starts journaling a file. Its `IMAGES` parameter chooses what is
written: `*AFTER`, the default, writes the row after the change, and `*BOTH` also
writes the row before it
([IBM: STRJRNPF](https://www.ibm.com/docs/en/i/7.5?topic=ssw_ibm_i_75/cl/strjrnpf.htm)).
With `*AFTER` only, Qlik documents that deleted rows have null data fields. The
Fivetran Db2 for i connector and the Debezium IBM i connector both ask for
`*BOTH`. Qlik and Fivetran also ask for `MINENTDTA(*NONE)` on the journal, so that
entries are not minimized
([IBM: CRTJRN](https://www.ibm.com/docs/en/i/7.5?topic=ssw_ibm_i_75/cl/crtjrn.htm)).

A reader also needs a way to tell rows apart, normally a primary key or a unique
constraint. Without one, tools fall back to the relative record number (RRN).
Qlik documents that reorganizing a table gives rows new RRNs, so a reorganize
then needs a resynchronization.

## Receivers: attach, detach and chains

When a receiver is full, or an administrator runs `CHGJRN`, a new receiver is
attached and the old one is detached. A detached receiver stays on the system
until someone removes it. `SEQOPT(*CONT)` continues the sequence numbers and
`SEQOPT(*RESET)` restarts them at 1
([IBM: CHGJRN](https://www.ibm.com/docs/en/i/7.5?topic=ssw_ibm_i_75/cl/chgjrn.htm)).
A sequence number only means something with its receiver name.

Receivers link to each other as a chain. A chain break means a complete copy of a
receiver is missing, so the changes between two parts of the chain are stored
nowhere on the system. IBM says to avoid breaks. `DSPJRN`, `RCVJRNE` and the
`QjoRetrieveJournalEntries` API cannot span chains
([IBM: journal receiver chains](https://www.ibm.com/docs/en/i/7.5.0?topic=receivers-journal-receiver-chains)).
A reader must tell "no new entries" from "there is a gap".

`QSYS2.JOURNAL_RECEIVER_INFO` lists receivers with status, attach and detach
times, size, first and last sequence number, and the previous and next receiver
([IBM: JOURNAL_RECEIVER_INFO](https://www.ibm.com/docs/en/i/7.5?topic=services-journal-receiver-info-view)).

## Retention: `MNGRCV` and `DLTRCV`

Who changes receivers, and who deletes them, often decides whether a capture tool
survives an outage ([IBM: CRTJRN](https://www.ibm.com/docs/en/i/7.5?topic=ssw_ibm_i_75/cl/crtjrn.htm)):

- `MNGRCV(*SYSTEM)`: the system detaches a full receiver and attaches a new one.
  It needs a threshold other than `*NONE`.
- `MNGRCV(*USER)`: you run `CHGJRN` yourself.
- `DLTRCV(*NO)`: detached receivers stay until you delete them.
- `DLTRCV(*YES)`: the system deletes detached receivers. IBM says this can be
  specified only with `MNGRCV(*SYSTEM)`, and that deletion is retried if, for
  example, a lock conflict or an exit program prevents it.

`DLTRCV(*YES)` is kind to the disk and risky for a reader that is behind. A
receiver that is gone cannot be read. Fivetran HVR says receivers
should not be removed before it has processed them, the Fivetran Db2 for i
connector asks for at least 48 hours of retention, and the Debezium IBM i README
says a journal deleted before it is read needs a resync.

A simple rule: **retention ≥ maximum outage + maximum lag + recovery time**, plus
your own margin. Measure your journal's growth rate; do not guess it.

## Three ways to read

**`QSYS2.DISPLAY_JOURNAL`** is a SQL table function. You choose the journal,
codes, entry types, object, and a start and end by receiver and sequence number or
by timestamp. By default it returns a finite result set. `EOF_DELAY` above zero
is restricted to the audit journal `QSYS/QAUDJRN`, with `GENERATE_SYSLOG` set to
`RFC3164` or `RFC5424`; it is not a wait mode for business-table journals.
If journal caching is configured, it does not return entries that are not yet on disk.
For an exact position use receiver and sequence number, not the timestamp
([IBM: DISPLAY_JOURNAL](https://www.ibm.com/docs/en/i/7.5?topic=services-display-journal-table-function)).

**`RCVJRNE`** is a CL command that passes entries to an exit program that you
write. It can keep running and wait for new entries. A range holds at most 2045 receivers, and the command is not threadsafe
([IBM: RCVJRNE](https://www.ibm.com/docs/en/i/7.5?topic=ssw_ibm_i_75/cl/rcvjrne.htm)).
Because it calls an exit program, it fits products that install a program on the
IBM i.

**`QjoRetrieveJournalEntries`** is an API. It fills a program's buffer, with
filters, a maximum entry count, and a continuation position so that a later call
resumes without skipping entries. Large-object columns may come back as pointers
that must be freed
([IBM: QjoRetrieveJournalEntries](https://www.ibm.com/docs/en/i/7.5?topic=ssw_ibm_i_75/apis/QJORJRNE.htm)).

IBM says `DISPLAY_JOURNAL` returns information similar to `DSPJRN` and compares
it with the API on its page. Which one a tool uses
changes what you must install and what a large backlog costs. Ask the vendor which
interface it uses, and why.

## Authorities

For `DISPLAY_JOURNAL`, IBM lists `*USE` on the journal and `*EXECUTE` on its
library, `*USE` on each receiver read and `*EXECUTE` on its library, and
`*OBJEXIST` on the journal when no object is named. The API page lists similar
needs plus locks. [One IBM installer page](https://www.ibm.com/docs/en/idr/11.4.0?topic=credi-installing-cdc-replication-engine-db2-i-version-114)
lists several special authorities for its engine. Ask whether a profile without
`*ALLOBJ` has been tested, and by whom. Receivers hold copies of your data, so restrict who can read them.

## What breaks a reader

The cited pages and the Quadringent compatibility page point to this list:

1. A receiver was deleted before it was read.
2. A table is not journaled, or is journaled with `*AFTER` only.
3. A table has no key and was reorganized.
4. A table definition changed and the reader holds the old layout.
5. A journal or receiver was restored and the chain broke.
6. The profile lost an authority.
7. The clock or time zone differs between the IBM i and the reader.


## What Quadringent does

Our reader reads the journal from outside the IBM i, over TLS, with `SELECT`
statements. Near the journal tail it calls `QSYS2.DISPLAY_JOURNAL` with journal
code `R`. For a larger backlog it uses the journal-parsing library from the
Debezium project, which calls IBM's retrieval APIs. It installs nothing on the IBM
i. The reader path runs no write statement or CL command of its own. The
backlog path goes through the library, and whether the library uses the IBM i
remote command server is Not tested. One class in the Java archive, used for
qualification runs only, can change data and run `CHGJRN`. See
[What Quadringent does on your IBM i](../product/ibm-i-footprint.md).
<!-- source: docs/product/ibm-i-footprint.md, java/src/main/java/io/quadringent/as400/JournalSession.java, java/pom.xml, THIRD_PARTY_NOTICES.md -->

A table is ready only when it is journaled with `*BOTH` images and has a primary
or unique key. For the others it shows CL commands that an administrator can
choose to run, and never runs them. It follows receivers in order and stops on a
sequence gap instead of skipping it. Receiver rotation is Not tested. The
[compatibility and prerequisites](../product/compatibility.md) page has the
retention rule and the authority per operation, and marks what was not tested. Only IBM i 7.5, on one shared server, has been
tested. How a reader that works can still leave Snowflake behind is the subject of
[Capture is not delivery](capture-delivery-reconciliation.md).
What we have measured, and on which version, is on the
[evidence page](../evidence.md).
<!-- source: docs/product/compatibility.md, src/quadringent/table_discovery.py, docs/product/ibm-i-footprint.md -->

## Sources

All pages were accessed on 2026-10-13.

- IBM: [journal receiver chains](https://www.ibm.com/docs/en/i/7.5.0?topic=receivers-journal-receiver-chains), [journal management concepts](https://www.ibm.com/docs/en/i/7.5?topic=ssw_ibm_i_75/rzaki/rzakiconcepts.htm), [DISPLAY_JOURNAL](https://www.ibm.com/docs/en/i/7.5?topic=services-display-journal-table-function), [QjoRetrieveJournalEntries](https://www.ibm.com/docs/en/i/7.5?topic=ssw_ibm_i_75/apis/QJORJRNE.htm), [RCVJRNE](https://www.ibm.com/docs/en/i/7.5?topic=ssw_ibm_i_75/cl/rcvjrne.htm), [JOURNAL_RECEIVER_INFO](https://www.ibm.com/docs/en/i/7.5?topic=services-journal-receiver-info-view), [CRTJRN](https://www.ibm.com/docs/en/i/7.5?topic=ssw_ibm_i_75/cl/crtjrn.htm), [CHGJRN](https://www.ibm.com/docs/en/i/7.5?topic=ssw_ibm_i_75/cl/chgjrn.htm), [STRJRNPF](https://www.ibm.com/docs/en/i/7.5?topic=ssw_ibm_i_75/cl/strjrnpf.htm)
- IBM support: [reading entry specific data](https://www.ibm.com/support/pages/reading-entry-specific-data-joesd-journal-receiver), [reading ENTRY_DATA with DISPLAY_JOURNAL](https://www.ibm.com/support/pages/example-how-extract-and-read-entrydata-joesd-journal-receiver-field-using-displayjournal-table-function), [IBM Data Replication engine for Db2 for i, install page](https://www.ibm.com/docs/en/idr/11.4.0?topic=credi-installing-cdc-replication-engine-db2-i-version-114)
- Vendor and project documentation for their own requirements: [Qlik IBM DB2 for iSeries prerequisites](https://help.qlik.com/en-US/replicate/May2026/Content/Replicate/Main/IBM%20DB2%20for%20iSeries/prereq_db2.htm), [Fivetran HVR 6 Db2 for i as source](https://fivetran.com/docs/hvr6/requirements/source-and-target-requirements/db2-for-i-requirements/db2-for-i-as-source), [Fivetran Db2 for i setup guide](https://fivetran.com/docs/connectors/databases/hva-db2i/setup-guide), [Debezium connector for IBM i README](https://github.com/debezium/debezium-connector-ibmi)

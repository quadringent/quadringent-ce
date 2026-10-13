# Options to replicate AS/400 (IBM i) data to Snowflake

<!-- description: Four ways to get IBM i (AS/400) data into Snowflake: batch extracts, commercial CDC, Kafka-based CDC and Quadringent, with trade-offs and questions to ask. -->

You have Db2 for i tables on an IBM i (formerly AS/400 or iSeries), and you want
them in Snowflake. There are four common ways to do it, and we do not rank them.
The right one depends on freshness, deletes, who will operate the tool, and cost.
For each, we say what you give up, and the article ends with questions to ask
before you choose.

Where a statement is about another product, it comes from that vendor's public
documentation, linked in the text and read on 2026-10-13. Where a statement
follows from how a method works, it says so.

## Option 1: batch extracts

A scheduled job reads tables with SQL and writes files, and Snowflake loads them.
Stage the files in cloud storage and use
`COPY INTO`, or let Snowpipe load files as they arrive, which Snowflake says is
typically within minutes
([Snowflake: data loading overview](https://docs.snowflake.com/en/user-guide/data-load-overview)).
On the IBM i side, the job selects either the whole table or the rows that
changed since the last run.

What this gives you: it is simple, it needs no journaling, and your team
probably knows the tools.

What you give up. These points follow from how an extract works, not from a
measurement:

- A nightly job gives you yesterday: freshness is the schedule.
- A row that is gone cannot be selected, so deletes are invisible to a query on
  the current table. You need a full comparison, a soft-delete flag in the
  application, or a journal.
- If a row changes three times between runs, you see only the last state.
- Load on the IBM i grows with table size for full extracts, and with the
  cost of finding changed rows for incremental ones.

Batch fits slowly changing reference tables and a first proof of concept. It
is a poor fit when you need deletes, history or a short delay.

## Option 2: commercial CDC products

Several vendors publish products that read the IBM i journal. Their public
documentation shows differences in where the software runs. According to Qlik
documentation, the Replicate IBM i source uses an ODBC driver on a server outside
the IBM i, and an optional component, installed on the IBM i, filters changes
there ([Qlik: prerequisites](https://help.qlik.com/en-US/replicate/May2026/Content/Replicate/Main/IBM%20DB2%20for%20iSeries/prereq_db2.htm),
[Qlik: advanced properties](https://help.qlik.com/en-US/replicate/May2026/Content/Replicate/Main/IBM%20DB2%20for%20iSeries/advanced_prop_db2_source.htm)).
According to Fivetran documentation, the Db2 for i connector uses an agent on a
server in the same network as the IBM i
([Fivetran: Db2 for i connector](https://fivetran.com/docs/connectors/databases/hva-db2i)).
According to IBM documentation, the replication engine for Db2 for i is
installed on the IBM i itself
([IBM: install the engine](https://www.ibm.com/docs/en/idr/11.4.0?topic=credi-installing-cdc-replication-engine-db2-i-version-114)).
Whether each product delivers to Snowflake is in
[How Quadringent compares](../product/comparison.md).

Fivetran publishes a usage-based pricing model, measured in monthly active rows
per connection ([Fivetran: usage-based pricing](https://fivetran.com/docs/usage-based-pricing)).
For the others, check with the vendor. This article gives no price figures.

What to ask for: support terms, production references on your IBM i release,
the list of supported sources and targets, and who fixes a fault in the product.
We did not verify what each vendor offers, so check with the vendor.

What you give up: you depend on a vendor's roadmap and licensing model, you
need to confirm what runs on your IBM i and what it needs, and you should ask
what the product shows you about delivery, not only about capture.

The page [How Quadringent compares](../product/comparison.md) sets out, product by
product and with a source for each statement, where the software runs, how it
reads the journal, how it delivers to Snowflake, its licensing model, the evidence
it shows and its maturity.

## Other routes

Some teams write their own code: SQL triggers on the tables, or a script that
polls `QSYS2.DISPLAY_JOURNAL` (see [how journals work](ibm-i-journals-for-cdc.md)).
These are batch extracts or a small version of Option 4, and you own the gaps, such as a missed receiver or a failed retry.
IBM also documents its own replication products, which this article does not
cover.

## Option 3: Kafka-based CDC

Here the journal goes into Kafka, and a second component moves it from Kafka into
Snowflake. The open-source Debezium connector for IBM i reads the journal and runs
under Kafka Connect. Its README calls it incubating, asks for `*BOTH` images,
lists Kafka and a schema registry for Avro output, and lists limits: no remote
journals or failover, no large-object or XML types, and a resync when a journal is
lost before it is read
([Debezium connector for IBM i](https://github.com/debezium/debezium-connector-ibmi)).
Snowflake provides a connector that reads Kafka topics and loads them into tables
([Snowflake: Kafka connector](https://docs.snowflake.com/en/user-guide/kafka-connector)).

What this gives you: if you already run Kafka, the IBM i changes become one more
topic that other consumers can read. You use open-source parts that many teams
know, and you control each one.

What you give up: you run and size Kafka, Kafka Connect and a schema registry,
and you own the glue. The IBM i connector is described as incubating. The pages
read here describe no check that what left the IBM i is what arrived in
Snowflake, so you may have to add one.

## Option 4: Quadringent

Start with a proof of concept on a non-production IBM i; we help with pilots.
What we have measured, and what we have not yet, is on the
[evidence page](../evidence.md). What you take on:

- There is no support contract: issues are answered on a best-effort basis, with
  no response-time commitment ([support](../../SUPPORT.md)). We maintain the
  project under a pseudonymous identity ([governance](../governance.md)).
- The scope is narrow: IBM i as the source and Snowflake as the destination,
  nothing else, at most 32 tables per capture session and one reader per journal.
- Installation, upgrades, backups and receiver retention are yours to run ([operator guide](../product/operator-guide.md)).

Details are in [compatibility and prerequisites](../product/compatibility.md) and
the [IBM i footprint](../product/ibm-i-footprint.md). Its journal reader builds on
the journal-parsing library from the Debezium project
([third-party notices](../../THIRD_PARTY_NOTICES.md)).
<!-- source: docs/product/compatibility.md, chart/templates/_helpers.tpl, SUPPORT.md, docs/governance.md, THIRD_PARTY_NOTICES.md, docs/product/ibm-i-footprint.md -->

What it does: we read the journal from a container in your own AWS or GCP
account, over TLS, and install nothing on the IBM i. Near the journal tail we
use `SELECT` statements on `QSYS2.DISPLAY_JOURNAL`. For a larger backlog we use a
library that calls IBM retrieval APIs, and whether that library uses the remote
command server is Not tested. The reader writes raw batches to your S3 or GCS
bucket, and loaders write a history table and a mirror table in your Snowflake
account. There is no Kafka. The engine, API and cockpit are open source under
Apache-2.0, with no license key.
<!-- source: docs/product/ibm-i-footprint.md, docs/product/your-data.md, docs/architecture.md, docs/licensing.md, java/pom.xml -->

The cockpit shows what was read, what was delivered and whether the two agree as
separate evidence for each table, and says Not measured when nothing was
measured ([Capture is not delivery](capture-delivery-reconciliation.md)).
<!-- source: docs/product/operator-guide.md, docs/product/quadringent-destination-proof-contract.md -->

## Side by side

| | Batch extracts | Commercial CDC | Kafka-based CDC | Quadringent |
|---|---|---|---|---|
| Needs IBM i journaling | No | Check with the vendor | Yes | Yes, with `*BOTH` images and a key |
| Sees deletes | Not without extra work | Check with the vendor | Check with the project | Reads delete entries when images are `*BOTH`; delete replication Not tested on 0.3.5 |
| Freshness | The schedule | Check with the vendor | Not measured here | Dated receipts, not a guarantee |
| Who runs it | You | Vendor, you, or both | You | You |
| Support | None | Check with the vendor | Check with the project | Best effort on public issues |
| Licensing | Your tools | Check with the vendor | Apache-2.0 for the connector, according to its README | Apache-2.0 |
| Maturity | Established method | Check with the vendor | Incubating connector, according to its README | Version 0.4, dated receipts |

## Questions to ask before you choose

1. How fresh must the data be, and what do you do when it is late?
2. Do you need deletes and history, or only the current state?
3. Which tables, how big, and how often do they change? Measure it.
4. Is journaling already on, with `*BOTH` images and a key, and how long do you
   keep receivers?
5. Who will operate it at 2 a.m., and what do they see when it breaks?
6. What does the tool show you about whether rows arrived, per table?
7. What does it cost at your change rate, including the IBM i, the cloud and the
   warehouse?
8. What happens if the vendor or the maintainers stop?

Start from the answers. If you want a vendor support contract today, ask the
commercial vendors what support they offer;
[How Quadringent compares](../product/comparison.md) can help you ask the right
questions. If you want to run an open-source, Kafka-free copy in your own account,
read [Questions for a first call](../product/faq.md) and the
[pilot preparation guide](../product/positioning-and-offer.md), then start a
proof of concept on a non-production IBM i.

## Sources

All pages were accessed on 2026-10-13.

- Snowflake: [data loading overview](https://docs.snowflake.com/en/user-guide/data-load-overview), [Kafka connector](https://docs.snowflake.com/en/user-guide/kafka-connector)
- Fivetran: [usage-based pricing](https://fivetran.com/docs/usage-based-pricing), [Db2 for i connector](https://fivetran.com/docs/connectors/databases/hva-db2i)
- IBM: [install the CDC Replication Engine for Db2 for i, 11.4](https://www.ibm.com/docs/en/idr/11.4.0?topic=credi-installing-cdc-replication-engine-db2-i-version-114)
- Debezium: [connector for IBM i README](https://github.com/debezium/debezium-connector-ibmi)
- Qlik: [advanced properties](https://help.qlik.com/en-US/replicate/May2026/Content/Replicate/Main/IBM%20DB2%20for%20iSeries/advanced_prop_db2_source.htm), [IBM DB2 for iSeries prerequisites](https://help.qlik.com/en-US/replicate/May2026/Content/Replicate/Main/IBM%20DB2%20for%20iSeries/prereq_db2.htm)

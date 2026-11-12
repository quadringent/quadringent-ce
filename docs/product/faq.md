# Questions for a first call

Every answer comes from this repository: the code, the tests and the dated
qualification files. Where nothing was measured, we say Not measured. The
[evidence page](../evidence.md) lists what was measured, on which version.

Answers distinguish repository tests, prerequisites, unsupported features and
maintainer-reported experience. A first-hand report is not a measured SLA.

| Tag | Meaning |
|---|---|
| Tested | A dated qualification file or an automated test in this repository covers it. The answer gives the scope, and the result applies to nothing else. |
| Required | The code or the installer needs it. A prerequisite, not a test result. |
| Not tested | No qualification covers it. It may work; we do not say so. |
| Not supported | The repository has no code for it, the product refuses it, or we do not offer it. |
| Maintainer-reported experience | First-hand use reported by the maintainer, without release-specific measurements or a general qualification claim. |

The version and volume answers were refreshed on 2026-11-12 against
source version 0.4.9 at `fe4db47` and the published 0.4.5 prerelease receipts,
including the [2026-10-16 closeout](../../site/qualification-0.4.5.json).
Historical answers below retain their explicit 0.3.x scopes; this refresh is not
a new qualification run. See [compatibility and prerequisites](compatibility.md).
If you are still choosing a method, [Options to replicate AS/400 (IBM i) data to Snowflake](../articles/as400-to-snowflake-options.md)
covers batch extracts, commercial CDC and Kafka-based CDC, and
[how Quadringent compares](comparison.md) names the products.

## Short answers

| Question | Short answer |
|---|---|
| IBM i release | 7.5 was tested, on one server. Other releases: Not tested. [Details](#which-ibm-i-release-was-tested). |
| Primary key | Needed. A table must be journaled with before and after images and have a primary key or unique constraint. [Details](#which-tables-can-be-replicated). |
| Production | Maintainer-reported production testing on multi-million-row tables; packaged installation gates remain. [Details](#can-i-run-it-in-production). |
| Latency | In the first 0.3.4 cold-warehouse trial, three of four targets missed our 10 s bar. In a later run, eight observations on two synthetic tables stayed within 10 s. On the current version: Not measured. [Details](#what-latency-should-i-expect-on-the-current-version). |
| Cost | No license fee and no table limit. You pay for your own IBM i, cloud and Snowflake usage; the amounts are Not measured. [Details](#is-there-a-license-fee-or-a-table-limit). |

<!-- source: docs/product/compatibility.md, site/recovery-0.3.4.json, docs/licensing.md -->

## IBM i source

### Which IBM i release was tested?

Tested. IBM i 7.5, on one shared qualification server, in a historical
measurement on one table. The published qualification summaries do not name the
IBM i release, so no published receipt ties 7.5 to 0.3.4 or 0.3.5.

<!-- source: docs/product/compatibility.md, docs/decisions/2026-09-23-attached-receiver-tail.md, site/qualification-0.3.4.json -->

### Does it work on IBM i 7.4, 7.6, or 7.3 and earlier?

Not tested. We ran no replication test on these releases. The suggested CL
syntax was checked on 7.4 and 7.5 only. On 7.4 and 7.3, some of the Db2 for i
services views that the reader queries need specific PTF group levels. The
levels, and the IBM page they come from, are in [compatibility](compatibility.md).

<!-- source: docs/product/compatibility.md, src/quadringent/table_discovery.py -->

## Tables and data

### Which tables can be replicated?

Required. A table must be journaled with before and after images (`*BOTH`)
and have a primary key or a unique constraint. Discovery marks a table `ready`
only then. With `*AFTER` images only, updates and deletes are not replicated
correctly. Tables of one journal use one reader, with at most 32 tables per
capture session. Tables from different journals need separate readers.

<!-- source: docs/product/compatibility.md, docs/product/install-client.md, docs/product/operator-guide.md, java/src/main/java/io/quadringent/as400/JournalSession.java, java/src/main/java/io/quadringent/as400/TableDiscovery.java -->

### Can it replicate a table without a key?

Not tested. The product allows it only with an explicit acknowledgement, and
then identifies rows by relative record number. `RGZPFM` or `CLRPFM` on such a
table invalidates that identity and needs a resynchronization. A unique index
without a constraint is not detected.

<!-- source: docs/api-v2.md, docs/product/compatibility.md -->

### Which column types have been checked?

Tested. Integer, varchar, char, decimal(11,2), date and timestamp(6). In the
0.3.4 readback all seven business columns of the synthetic qualification table
matched. The test data generator also produces nulls, empty and accented text,
padded CHAR and signed decimals.

<!-- source: site/qualification-0.3.4.json, docs/qualification.md, docs/product/compatibility.md -->

### What about SMALLINT, BIGINT, TIME, GRAPHIC, BINARY, LOB, DECFLOAT and XML?

Not tested. The Snowflake mapping lists SMALLINT, BIGINT, TIME, GRAPHIC,
BINARY and LOB types. DECFLOAT and XML are not in it. A type without a mapping
stops with `UnsupportedColumnTypeError` instead of being cast or truncated. No
qualification covers these types end to end.

<!-- source: docs/product/snowflake-destination.md, docs/product/compatibility.md -->

### How are CCSIDs handled?

Required. A character column with CCSID 65535 must be reclassified as binary
during discovery, otherwise the column is rejected. Other CCSIDs than the ones
of the test server: Not tested.

<!-- source: docs/product/snowflake-destination.md, docs/product/compatibility.md -->

### Does it work with commitment control?

Not tested. Our measurements used isolated writes without commitment control.
A rollback entry stops the capture window without advancing the checkpoint.
Uncommitted changes are not filtered by commit cycle.

<!-- source: docs/product/compatibility.md, java/src/main/java/io/quadringent/as400/JournalSession.java -->

## Initial copy and schema changes

### How does the initial load work?

Tested. One copy Job per table reads the rows with a `SELECT` over a
read-only JDBC connection and writes them as create events to raw storage. The
cockpit shows `Copying` until the copy completes. A new initial copy rebuilds the
mirror and keeps the earlier history. The scope tested on 0.3.5: two synthetic
tables with 55 rows each, on AWS VM, EKS, GCP VM and GKE. Each target read back
110 mirror rows and 110 history C images with exact typed values, and no source
write occurred during these trials.

<!-- source: site/runtime-0.3.5.json, java/src/main/java/io/quadringent/as400/ReadOnlyTableSnapshot.java, docs/api-v2.md, docs/product/snowflake-destination.md, docs/product/operator-guide.md -->

### Can the source keep changing during the initial copy?

Not tested. The 0.3.5 copy trials made no new source write. The qualification
harness keeps the source quiescent for its snapshot boundary. Plan the first copy
of each table in a quiet window until this is qualified.

<!-- source: site/runtime-0.3.5.json, docs/qualification.md -->

### How long does the copy of a large table take?

Tested. This is a scoped dataset result, not a throughput benchmark. The published
0.4.5 campaign includes a 1,000,000-row synthetic fixture. Its 2026-10-16 final
comparison reconciled 1,000,000 source and mirror rows, 124 independent native
journal images against 124 stored raw images, and 1,000,124 history rows.
This result came from a separate qualification harness, not automatic product
verification. The earlier 55-row trials belong to 0.3.5, not the largest
published dataset. See the [dated receipt](../../site/qualification-0.4.5.json).

A portable copy-duration, memory-use or cost estimate for your table is Not measured. Final equality does not establish sustained throughput, 24-hour
continuity or a latency SLA.

<!-- source: site/runtime-0.3.5.json, site/qualification-0.4.5.json, docs/product/compatibility.md -->

### What happens when I add a column or change a table?

Not tested. `CHGPF`, `ALTER TABLE` and `DROP TABLE` have no documented
handling in the product; plan a resynchronization of the table. An additive
schema change is covered only by an offline verifier on synthetic data, not by a
run against an IBM i. The loader refuses a type it cannot map.

<!-- source: docs/product/compatibility.md, docs/product/sale-mutation-certification.md -->

## Latency and volume

### What latency has been measured?

Tested. In the first 0.3.4 trial with suspended warehouses, three of four
targets went above our 10 s gate, up to 13.641 s. A later 0.3.4 run did one
cold-warehouse and one warm trial per target on the four targets, measured from
the start of the source SQL statement to the first observed row in the Snowflake
mirror. All eight observations were within the 10 s gate: upper bounds from
3.763 s to 8.876 s. These are upper bounds on two synthetic tables, not a P95 and
not a guarantee.

<!-- source: site/recovery-0.3.4.json, site/qualification-0.3.4.json, README.md -->

### What latency should I expect on the current version?

Not tested. Current source version 0.4.9 has no new live qualification. Not measured for that source. The published 0.4.5 prerelease
has dated measurements: its 2026-10-16 closeout records 30 of 97 planned bursts
completed within 120 seconds before the campaign stopped. The ten-second target
was not achieved, and completed 24-hour continuity and receiver rotation were
not proven. The successful final reconciliation is a separate result, not a
P95 or latency guarantee. See the [closeout](../../site/qualification-0.4.5.json).
Default polling is one second; warehouse wake-up, volume and `MERGE` time still
contribute to delay. Measure the version and configuration you deploy.

<!-- source: site/runtime-0.3.5.json, site/qualification-0.4.5.json, docs/product/snowflake-destination.md, README.md -->

### How much data per day can it carry?

Not tested. Not measured as a sustained daily capacity. The older 0.3.x trials used two
synthetic tables; the later 0.4.5 campaign reconciled a million-row fixture and
124 journal images. Neither result establishes a production throughput rate.
Its fixed 97-burst endurance contract did not pass. See the
[version-scoped evidence](../evidence.md) before planning your own load test.

<!-- source: site/recovery-0.3.4.json, site/runtime-0.3.5.json, site/qualification-0.4.5.json, README.md -->

## Outages and recovery

### What happens if the reader process is killed?

Tested. On 0.3.4, we killed a reader on each of four targets, with source
writes acknowledged during the stop. The reader resumed from its checkpoint and
complete typed reads found 110 mirror rows and 148 history images per site, with
unique event IDs. Not tested on 0.3.5.

<!-- source: site/recovery-0.3.4.json, README.md -->

### What happens if the IBM i or the network is down for hours?

Not tested. The reader stops in a named state (for example
`STOPPED_FAIL_CLOSED` for TLS or sign-on problems) and waits for an operator. A
resume is offered only with a valid checkpoint, proven continuity, a recent
catalog probe and no remaining reader Job. A long outage has no published
measurement.

<!-- source: docs/product/operator-guide.md, docs/operations.md -->

### How long must I keep journal receivers?

Required. Receivers must stay online until the checkpoint has passed them.
The reader stops on a missing receiver and a restart cannot recover it. Size the
retention as maximum outage plus maximum lag plus recovery time, with your own
margin. `DLTRCV(*YES)` can delete receivers that were not read yet.

<!-- source: docs/product/compatibility.md -->

### Does it survive a journal receiver change?

Not tested. The reader follows receivers in attach order and stops on a
sequence gap. Receiver rotation is Not tested.

<!-- source: docs/product/compatibility.md, docs/product/native-qualification.md -->

### Can I rebuild after losing the whole installation?

Tested. On 0.3.4, fresh infrastructure and new PostgreSQL volumes on four
targets restored the database, the original keys, raw data, checkpoints and
pipeline identities from backups. We did not run it on 0.3.5, and it started from
a historical baseline rather than an initial copy. Keep the original `SECRET_KEY`
and `TOKEN_PEPPER` outside the cluster: fresh values cannot decrypt a restored
database.

<!-- source: site/recovery-0.3.4.json, docs/product/backup-restore.md, docs/product/operator-guide.md -->

## Where it runs

### What do I need to run it?

Required. An IBM i with journaled tables and TLS access, an AWS or GCP
account with S3 or GCS for raw storage, a Kubernetes cluster or a k3s VM, and a
Snowflake account. The data path is reader, raw storage, loader and Snowflake.
The code has no Kafka dependency and no message broker in that path.

<!-- source: docs/architecture.md, docs/product/compatibility.md, docs/product/install-default.md, pyproject.toml, java/pom.xml -->

### Does it run without Kubernetes?

Not tested. The released install paths need Kubernetes: the cloud VM target
runs k3s on a dedicated VM, and the other target uses an existing EKS or GKE
cluster. `quadringent local` runs one table on a single machine with Docker, with
no cloud account and no Kubernetes ([Try it locally](try-it-local.md)). It needs
images from a release that includes it, and it has not been validated end to end
against an IBM i and Snowflake.

<!-- source: docs/product/install-default.md, docs/product/try-it-local.md, src/quadringent/local/cli.py -->

### Which clouds and targets were tested?

Tested. AWS VM, EKS, GCP VM and GKE, in DEV/INT, with synthetic tables.
Terraform modules exist for AWS and GCP only. Azure and other clouds: Not
supported.

<!-- source: site/runtime-0.3.5.json, site/recovery-0.3.4.json, deploy/terraform/aws/vm/variables.tf, deploy/terraform/gcp/vm/variables.tf -->

### Can it load a destination other than Snowflake?

Not supported. The only loader writes Snowflake history and mirror tables.
The raw batches in your S3 or GCS bucket are JSON Lines files with JSON manifests
that ordinary tools can read, but the repository has no loader for another
warehouse.

<!-- source: docs/product/snowflake-destination.md, docs/product/your-data.md -->

## Costs

### Is there a license fee or a table limit?

Tested. No. The engine, API and cockpit are Apache-2.0, with no activation
key and no commercial table limit. An automated test checks that no code reads a
license key. You pay for your own IBM i, cloud and Snowflake usage. No paid offer
or price is announced.

<!-- source: docs/licensing.md, tests/test_trust_claims.py, LICENSE -->

### What will the cloud infrastructure cost?

Not tested. Not measured. The defaults are a `t3.medium` (AWS) or `e2-medium`
(GCP) VM with a 40 GB disk for the VM target, plus storage and checkpoint
resources. Use your provider's pricing for your region and set billing alerts
before installing.

<!-- source: docs/product/compatibility.md, deploy/terraform/aws/vm/variables.tf, deploy/terraform/gcp/vm/variables.tf -->

### What will Snowflake bill?

Not tested. Not measured; there is no published receipt of credits per day.
The loader uses an `XSMALL` warehouse created with `AUTO_SUSPEND = 60`,
`AUTO_RESUME = TRUE` and `INITIALLY_SUSPENDED = TRUE`. It resumes when the loader
runs a query, so the cost depends on your change rate. Check Snowflake's
documentation for how it bills a resumed warehouse.

<!-- source: docs/product/compatibility.md, src/quadringent_control_plane/v2/services/destinations.py, docs/product/snowflake-destination.md -->

## Security and support

### What does it do on my IBM i?

Tested. Scope: the Java reader code in this repository, checked by an
automated pattern scan. The scan is not a proof. That code installs nothing on the
IBM i. It runs in your cloud account, connects as one user profile over TLS, and
issues `SELECT` statements and calls IBM-supplied table functions and views. It
issues no `INSERT`, `UPDATE`, `DELETE`, DDL or CL command. Two things are outside
the scan. Backlog reads go through a third-party journal-parsing library that
calls IBM journal APIs. And one class built for our qualification runs can write
to a declared test table and run `CHGJRN`; no product job starts it. The CPU and
I/O cost of the reads on your IBM i is Not measured. Details and file references:
[What Quadringent does on your IBM i](ibm-i-footprint.md).

<!-- source: docs/product/ibm-i-footprint.md, tests/test_trust_claims.py -->

### Which authorities does the IBM i profile need?

Not tested. The list in [compatibility](compatibility.md#ibm-i-authorities)
comes from IBM's documentation for each operation. It has not been tested at
runtime with a minimal profile. `*ALLOBJ` is not required by those operations; do
not grant it for convenience. Validate the profile on a non-production IBM i first.

<!-- source: docs/product/compatibility.md, docs/product/ibm-i-footprint.md -->

### Is TLS required?

Required. The chart and the installer refuse plaintext connections to the
IBM i. You supply your CA. `quadringent doctor --check-ibmi <host> --tls` tests
the handshake from your machine.

<!-- source: docs/product/compatibility.md, docs/product/operator-guide.md, SECURITY.md -->

### Does my data go to the project?

Tested. Scope: the code of this repository. It sends no business data to a
project-run endpoint. Automated tests check that telemetry is off by default and
opens no connection while it is off. If you enable it, it sends one daily message
with a version, a tier and a table count, not row data. Your data goes from the
IBM i to your reader, your raw bucket, your loader and your Snowflake account. See
[your data](your-data.md).

<!-- source: docs/product/your-data.md, docs/product/telemetry.md, tests/test_trust_claims.py -->

### Is support available?

Not supported. No paid support is announced. Questions and bugs
go to GitHub issues and are answered on a best-effort basis. Security reports
have a private channel and a five-business-day acknowledgement target. What
remains if the maintainers stop is on the [governance](../governance.md) page. See
[Support](../../SUPPORT.md).

<!-- source: SUPPORT.md, docs/governance.md, docs/licensing.md -->

## Pilots and production

### Can I run it in production?

Maintainer-reported experience. Our maintainer reports repeated tests on production IBM i machines
and tables containing several million rows, in real cluster and VPC endpoint
conditions. This is first-hand experience, not a release-specific measurement,
throughput benchmark or SLA; customer systems and data remain private.

The packaged installation profile still accepts `dev`, `int`, `test` and
`staging`, with production promotion disabled. Those configuration gates do not
mean the tool has never been tested in production. Review the
[production experience and dated evidence](../evidence.md#production-experience)
and use the [pilot guide](positioning-and-offer.md) to validate the selected
release and your own configuration. Start with a proof of concept on a non-production IBM i.
We help with pilots; see the [evidence page](../evidence.md) for its distinct scopes.

<!-- source: chart/templates/_helpers.tpl, README.md, docs/product/positioning-and-offer.md -->

# Questions for a first call

These are the questions a data team usually asks first. Every answer comes from
this repository: the code, the tests and the dated qualification files. Quadringent
is a DEV/INT preview and no production qualification is claimed. Where nothing
was measured, the answer says **Not measured**.

Each answer carries one status:

- **Tested**: checked by a dated qualification file or by an automated test in
  this repository. The scope is given in the answer and applies to nothing else.
- **Required**: the code or the installer needs it. This is a prerequisite, not a
  test result.
- **Not tested**: no qualification covers it. It may work. Nothing here says so.
- **Not supported**: the repository has no code for it, the product refuses it,
  or the project does not offer it.

The answers were checked against source version 0.4.1 on 2026-10-13. The latest
published scoped receipts are [0.3.5](../../site/runtime-0.3.5.json) and
[0.3.4](../../site/recovery-0.3.4.json); each describes only the version it names.
The detailed tables are in [compatibility and prerequisites](compatibility.md).

## Short answers

- **IBM i release:** 7.5 was tested, on one server. Other releases: Not tested.
  [Details](#which-ibm-i-release-was-tested).
- **Primary key:** needed. A table must be journaled with before and after images
  and have a primary key or unique constraint.
  [Details](#which-tables-can-be-replicated).
- **Production:** not supported. DEV/INT preview only.
  [Details](#can-i-run-it-in-production).
- **Latency:** eight 0.3.4 observations on two synthetic tables stayed within 10 s.
  On the current version: Not measured.
  [Details](#what-latency-should-i-expect-on-the-current-version).
- **Cost:** no license fee and no table limit. You pay for your own IBM i, cloud and
  Snowflake usage; the amounts are Not measured.
  [Details](#is-there-a-license-fee-or-a-table-limit).

<!-- source: docs/product/compatibility.md, site/recovery-0.3.4.json, docs/licensing.md -->

## IBM i source

### Which IBM i release was tested?

**Status: Tested.** IBM i 7.5, on one shared qualification server, in a historical
measurement on one table. The published qualification summaries do not name the
IBM i release, so no published receipt ties 7.5 to 0.3.4 or 0.3.5.

<!-- source: docs/product/compatibility.md, docs/decisions/2026-09-23-attached-receiver-tail.md, site/qualification-0.3.4.json -->

### Does it work on IBM i 7.4, 7.6, or 7.3 and earlier?

**Status: Not tested.** No replication test was run on these releases. The
suggested CL syntax was checked on 7.4 and 7.5 only. On 7.4 and 7.3, some of the
Db2 for i services views that the reader queries need specific PTF group levels.
The levels, and the IBM page they come from, are in
[compatibility](compatibility.md).

<!-- source: docs/product/compatibility.md, src/quadringent/table_discovery.py -->

## Tables and data

### Which tables can be replicated?

**Status: Required.** A table must be journaled with before and after images
(`*BOTH`) and have a primary key or a unique constraint. Discovery marks a table
`ready` only then. With `*AFTER` images only, updates and deletes are not
replicated correctly. Tables of one journal use one reader, with at most 32
tables per capture session. Tables from different journals need separate readers.

<!-- source: docs/product/compatibility.md, docs/product/install-client.md, docs/product/operator-guide.md, java/src/main/java/io/quadringent/as400/JournalSession.java, java/src/main/java/io/quadringent/as400/TableDiscovery.java -->

### Can it replicate a table without a key?

**Status: Not tested.** The product allows it only with an explicit
acknowledgement, and then identifies rows by relative record number. `RGZPFM`
or `CLRPFM` on such a table invalidates that identity and needs a
resynchronization. A unique index without a constraint is not detected.

<!-- source: docs/api-v2.md, docs/product/compatibility.md -->

### Which column types have been checked?

**Status: Tested.** Integer, varchar, char, decimal(11,2), date and timestamp(6).
In the 0.3.4 readback all seven business columns of the synthetic qualification
table matched. The test data generator also produces nulls, empty and accented
text, padded CHAR and signed decimals.

<!-- source: site/qualification-0.3.4.json, docs/qualification.md, docs/product/compatibility.md -->

### What about SMALLINT, BIGINT, TIME, GRAPHIC, BINARY, LOB, DECFLOAT and XML?

**Status: Not tested.** The Snowflake mapping lists SMALLINT, BIGINT, TIME,
GRAPHIC, BINARY and LOB types. DECFLOAT and XML are not in it. A type without a
mapping stops with `UnsupportedColumnTypeError` instead of being cast or
truncated. No qualification covers these types end to end.

<!-- source: docs/product/snowflake-destination.md, docs/product/compatibility.md -->

### How are CCSIDs handled?

**Status: Required.** A character column with CCSID 65535 must be reclassified as
binary during discovery, otherwise the column is rejected. Other CCSIDs than the
ones of the test server: Not tested.

<!-- source: docs/product/snowflake-destination.md, docs/product/compatibility.md -->

### Does it work with commitment control?

**Status: Not tested.** Measurements used isolated writes without commitment
control. A rollback entry stops the capture window without advancing the
checkpoint. Uncommitted changes are not filtered by commit cycle.

<!-- source: docs/product/compatibility.md, java/src/main/java/io/quadringent/as400/JournalSession.java -->

## Initial copy and schema changes

### How does the initial load work?

**Status: Tested.** One copy Job per table reads the rows with a `SELECT` over a
read-only JDBC connection and writes them as create events to raw storage. The
cockpit shows **Copying** until the copy completes. A new initial copy rebuilds
the mirror and keeps the earlier history. The scope tested on 0.3.5: two
synthetic tables with 55 rows each, on AWS VM, EKS, GCP VM and GKE. Each target
read back 110 mirror rows and 110 history C images with exact typed values, and
no source write occurred during these trials.

<!-- source: site/runtime-0.3.5.json, java/src/main/java/io/quadringent/as400/ReadOnlyTableSnapshot.java, docs/api-v2.md, docs/product/snowflake-destination.md, docs/product/operator-guide.md -->

### Can the source keep changing during the initial copy?

**Status: Not tested.** The 0.3.5 copy trials made no new source write. The
qualification harness keeps the source quiescent for its snapshot boundary.
Plan the first copy of each table in a quiet window until this is qualified.

<!-- source: site/runtime-0.3.5.json, docs/qualification.md -->

### How long does the copy of a large table take?

**Status: Not tested.** Not measured. The largest published copy is 55 rows per
table. Copy duration, memory use and Snowflake warehouse use for large tables
have no published figure.

<!-- source: site/runtime-0.3.5.json, docs/product/compatibility.md -->

### What happens when I add a column or change a table?

**Status: Not tested.** `CHGPF`, `ALTER TABLE` and `DROP TABLE` have no
documented handling in the product; plan a resynchronization of the table. An
additive schema change is covered only by an offline verifier on synthetic data,
not by a run against an IBM i. The loader refuses a type it cannot map.

<!-- source: docs/product/compatibility.md, docs/product/sale-mutation-certification.md -->

## Latency and volume

### What latency has been measured?

**Status: Tested.** On 0.3.4, one cold-warehouse and one warm trial per target
on four targets, measured from the start of the source SQL statement to the
first observed row in the Snowflake mirror. All eight observations were within
the 10 s gate of the test: upper bounds from 3.763 s to 8.876 s. An earlier
0.3.4 trial with suspended warehouses went above 10 s on three of four targets,
up to 13.641 s. These are upper bounds on two synthetic tables, not a P95 and
not an SLA.

<!-- source: site/recovery-0.3.4.json, site/qualification-0.3.4.json, README.md -->

### What latency should I expect on the current version?

**Status: Not tested.** Not measured. Native CDC writes, reader outage and
latency are not established on 0.3.5, and the 0.4.1 source needs fresh
qualification. Default polling is one second, which targets low latency but does
not guarantee visibility within ten seconds. Warehouse wake-up, volume and
`MERGE` time remain part of the delay.

<!-- source: site/runtime-0.3.5.json, docs/product/snowflake-destination.md, README.md -->

### How much data per day can it carry?

**Status: Not tested.** Not measured. The published trials used two synthetic
tables, 110 rows per target and six source writes. No production-scale capacity
is established, and no throughput figure is published.

<!-- source: site/recovery-0.3.4.json, site/runtime-0.3.5.json, README.md -->

## Outages and recovery

### What happens if the reader process is killed?

**Status: Tested.** On 0.3.4, a reader was killed on each of four targets, with
source writes acknowledged during the stop. The reader resumed from its
checkpoint and complete typed reads found 110 mirror rows and 148 history images
per site, with unique event IDs. This is not established on 0.3.5.

<!-- source: site/recovery-0.3.4.json, README.md -->

### What happens if the IBM i or the network is down for hours?

**Status: Not tested.** The reader stops in a named state (for example
`STOPPED_FAIL_CLOSED` for TLS or sign-on problems) and waits for an operator. A
resume is offered only with a valid checkpoint, proven continuity, a recent
catalog probe and no remaining reader Job. A long outage has no published
measurement.

<!-- source: docs/product/operator-guide.md, docs/operations.md -->

### How long must I keep journal receivers?

**Status: Required.** Receivers must stay online until the checkpoint has passed
them. The reader stops on a missing receiver and a restart cannot recover it.
Size the retention as maximum outage plus maximum lag plus recovery time, with
your own margin. `DLTRCV(*YES)` can delete receivers that were not read yet.

<!-- source: docs/product/compatibility.md -->

### Does it survive a journal receiver change?

**Status: Not tested.** The reader follows receivers in attach order and stops on
a sequence gap. Receiver rotation was not qualified.

<!-- source: docs/product/compatibility.md, docs/product/native-qualification.md -->

### Can I rebuild after losing the whole installation?

**Status: Tested.** On 0.3.4, fresh infrastructure and new PostgreSQL volumes on
four targets restored the database, the original keys, raw data, checkpoints and
pipeline identities from backups. It was not run on 0.3.5, and it started from a
historical baseline rather than an initial copy. Keep the original `SECRET_KEY`
and `TOKEN_PEPPER` outside the cluster: fresh values cannot decrypt a restored
database.

<!-- source: site/recovery-0.3.4.json, docs/product/backup-restore.md, docs/product/operator-guide.md -->

## Where it runs

### What do I need to run it?

**Status: Required.** An IBM i with journaled tables and TLS access, an AWS or GCP
account with S3 or GCS for raw storage, a Kubernetes cluster or a k3s VM, and a
Snowflake account. The data path is reader, raw storage, loader and Snowflake.
The code has no Kafka dependency and no message broker in that path.

<!-- source: docs/architecture.md, docs/product/compatibility.md, docs/product/install-default.md, pyproject.toml, java/pom.xml -->

### Does it run without Kubernetes?

**Status: Not tested.** The released install paths need Kubernetes: the cloud VM
target runs k3s on a dedicated VM, and the other target uses an existing EKS or
GKE cluster. `quadringent local` runs one table on a single machine with Docker,
with no cloud account and no Kubernetes ([Try it locally](try-it-local.md)). It
needs images from a release that includes it, and it has not been validated end
to end against a real IBM i and Snowflake.

<!-- source: docs/product/install-default.md, docs/product/try-it-local.md, src/quadringent/local/cli.py -->

### Which clouds and targets were tested?

**Status: Tested.** AWS VM, EKS, GCP VM and GKE, in DEV/INT, with synthetic
tables. Terraform modules exist for AWS and GCP only. Azure and other clouds:
Not supported.

<!-- source: site/runtime-0.3.5.json, site/recovery-0.3.4.json, deploy/terraform/aws/vm/variables.tf, deploy/terraform/gcp/vm/variables.tf -->

### Can it load a destination other than Snowflake?

**Status: Not supported.** The only loader writes Snowflake history and mirror
tables. The raw batches in your S3 or GCS bucket are JSON Lines files with JSON
manifests that ordinary tools can read, but the repository has no loader for
another warehouse.

<!-- source: docs/product/snowflake-destination.md, docs/product/your-data.md -->

## Costs

### Is there a license fee or a table limit?

**Status: Tested.** No. The engine, API and cockpit are Apache-2.0, with no
activation key and no commercial table limit. An automated test checks that no
code reads a license key. You pay for your own IBM i, cloud and Snowflake usage.
No paid offer or price is announced.

<!-- source: docs/licensing.md, tests/test_trust_claims.py, LICENSE -->

### What will the cloud infrastructure cost?

**Status: Not tested.** Not measured. The defaults are a `t3.medium` (AWS) or
`e2-medium` (GCP) VM with a 40 GB disk for the VM target, plus storage and
checkpoint resources. Use your provider's pricing for your region and set
billing alerts before installing.

<!-- source: docs/product/compatibility.md, deploy/terraform/aws/vm/variables.tf, deploy/terraform/gcp/vm/variables.tf -->

### What will Snowflake bill?

**Status: Not tested.** Not measured; there is no published receipt of credits
per day. The loader uses an `XSMALL` warehouse created with `AUTO_SUSPEND = 60`,
`AUTO_RESUME = TRUE` and `INITIALLY_SUSPENDED = TRUE`. It resumes when the loader
runs a query, so the cost depends on your change rate. Check Snowflake's
documentation for how it bills a resumed warehouse.

<!-- source: docs/product/compatibility.md, src/quadringent_control_plane/v2/services/destinations.py, docs/product/snowflake-destination.md -->

## Security and support

### What does it do on my IBM i?

**Status: Tested.** Scope: the Java reader code in this repository, checked by an
automated pattern scan. The scan is not a proof. That code installs nothing on the
IBM i. It runs in your cloud account, connects as one user profile over TLS, and
issues `SELECT` statements and calls IBM-supplied table functions and views. It
issues no `INSERT`, `UPDATE`, `DELETE`, DDL or CL command. Two things are outside
the scan. Backlog reads go through a third-party journal-parsing library that calls
IBM journal APIs. And one class built for the project's qualification runs can
write to a declared test table and run `CHGJRN`; no product job starts it. Details
and file references: [What Quadringent does on your IBM i](ibm-i-footprint.md).

<!-- source: docs/product/ibm-i-footprint.md, tests/test_trust_claims.py -->

### Which authorities does the IBM i profile need?

**Status: Not tested.** The list in [compatibility](compatibility.md#ibm-i-authorities)
comes from IBM's documentation for each operation. It has not been tested at
runtime with a minimal profile. `*ALLOBJ` is not required by those operations; do
not grant it for convenience. Validate the profile on a DEV/INT system.

<!-- source: docs/product/compatibility.md, docs/product/ibm-i-footprint.md -->

### Is TLS required?

**Status: Required.** The chart and the installer refuse plaintext connections to
the IBM i. You supply your CA. `quadringent doctor --check-ibmi <host> --tls`
tests the handshake from your machine.

<!-- source: docs/product/compatibility.md, docs/product/operator-guide.md, SECURITY.md -->

### Does my data go to the project?

**Status: Tested.** Scope: the code of this repository. It sends no business
data to a project-run endpoint. Automated tests check that telemetry is off by
default and opens no connection while it is off. If you enable it, it sends one
daily message with a version, a tier and a table count, not row data. Your data goes from the IBM i to your
reader, your raw bucket, your loader and your Snowflake account. See
[your data](your-data.md).

<!-- source: docs/product/your-data.md, docs/product/telemetry.md, tests/test_trust_claims.py -->

### Is support available?

**Status: Not supported.** No paid support and no SLA are announced. Questions and
bugs go to GitHub issues and are answered on a best-effort basis. Security
reports have a private channel and a five-business-day acknowledgement target.
What remains if the maintainers stop is on the
[governance](../governance.md) page. See [Support](../../SUPPORT.md).

<!-- source: SUPPORT.md, docs/governance.md, docs/licensing.md -->

## Production readiness

### Can I run it in production?

**Status: Not supported.** No. The chart accepts only the environments `dev`, `int`,
`test` and `staging`, and blocks production promotion. No production, external
pilot or minimal-authority qualification is established. If you want to evaluate
it in DEV/INT, read the [pilot guide](positioning-and-offer.md).

<!-- source: chart/templates/_helpers.tpl, README.md, docs/product/positioning-and-offer.md -->

## Keep reading

- [Compatibility and prerequisites](compatibility.md): every row with its status.
- [How Quadringent compares](comparison.md): a sourced comparison with alternatives.
- [Options to replicate AS/400 (IBM i) data to Snowflake](../articles/as400-to-snowflake-options.md).
- [How IBM i journals work for change data capture](../articles/ibm-i-journals-for-cdc.md).
- [Capture is not delivery: proving IBM i data reached Snowflake](../articles/capture-delivery-reconciliation.md).
- [Verify a release](verify-a-release.md) and [Your data](your-data.md).

<!-- source: README.md -->

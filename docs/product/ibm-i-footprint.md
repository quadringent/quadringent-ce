# What Quadringent does on your IBM i

If you administer an IBM i and have to decide whether to let this software
connect to it, start here. Each statement about the code names the file where you
can check it. Quadringent is a DEV/INT preview: the chart accepts only the
environments `dev`, `int`, `test` and `staging`, and no production qualification
is claimed ([evidence](../evidence.md), [installation status](install-default.md)).
<!-- source: chart/templates/_helpers.tpl -->

## In short

- The reader runs in a container in your cloud account, not on the IBM i.
- It connects to the IBM i as one user profile that you create, over TLS.
- The reader code issues `SELECT` statements and calls IBM-supplied table
  functions and views. It issues no `INSERT`, `UPDATE`, `DELETE`, `MERGE`, DDL or
  CL command. `tests/test_trust_claims.py` fails if a listed pattern of such a
  statement appears; it is a pattern scan, not a proof (see
  [what it never does](#what-it-never-does)).
- Nothing is installed on the IBM i: no program, library, save file or exit
  program.
- The product's reader path never creates or changes journaling (the one
  exception below is a qualification tool, not part of that path). When a table
  is not ready, the cockpit shows CL commands that your administrator can choose
  to run.
- One class in the Java archive can change data and run a `CHGJRN` command. It
  is for our qualification runs and no job in the product starts it. See
  [the one exception](#the-one-exception-the-qualification-driver).
- The CPU and I/O cost of these reads on your IBM i is Not measured.

## Where the reader runs

The reader is a Java process started by a Python process inside a container, in
the Kubernetes cluster or k3s VM that the installer provisions or binds in your
AWS or GCP account. The connection goes from the container to the IBM i. No code
of this repository runs on the IBM i.
<!-- source: docs/architecture.md, docs/product/install-default.md, docker/Dockerfile, src/quadringent/java_worker.py -->

The repository contains no IBM i source or object (RPG, CL, COBOL, display or
physical file source, save file). A test checks the tracked directories for such
files. Journal positions (checkpoints) are stored in DynamoDB (AWS) or in your
GCS bucket (GCP), never on the IBM i.
<!-- source: docs/architecture.md, tests/test_trust_claims.py -->

## How it reads the journal

All reads go through JDBC and the JTOpen driver, plus the journal-retrieval
library below. The statements are in the Java sources listed in the last column.

| What is read | IBM i interface | Where in the code |
|---|---|---|
| Row changes near the journal tail | `QSYS2.DISPLAY_JOURNAL` table function, `JOURNAL_CODES => 'R'` (record-level entries), one file, bounded by receiver and sequence number | `java/src/main/java/io/quadringent/as400/JournalSession.java` |
| Row changes in a larger backlog | The journal-parsing library from the Debezium project (`ibmi-journal-parsing`, version in `java/pom.xml`), which calls IBM's journal retrieval APIs. Its source is not in this repository. | `JournalSession.java`, `java/pom.xml`, [IBM i authorities](compatibility.md#ibm-i-authorities) |
| Journal receiver chain | `QSYS2.JOURNAL_RECEIVER_INFO` | `ReadOnlyReceiverCatalog.java` |
| Identity of a journal receiver | `QSYS2.IFS_OBJECT_STATISTICS` on the `*JRNRCV` path | `ReadOnlyReceiverIdentity.java` |
| Which tables are journaled, with which images, keys, columns and row counts | `QSYS2.JOURNALED_OBJECTS`, `SYSTABLES`, `SYSTABLESTAT`, `SYSCOLUMNS`, `SYSCST`, `SYSKEYCST`, `SYSINDEXES`, `SYSKEYS` | `TableDiscovery.java`, `FleetCatalogProbe.java`, `ReadOnlyReceiverCatalog.java` |
| Initial copy of a table | `SELECT T.*, RRN(T) FROM LIBRARY.TABLE`, JDBC connection set read-only | `ReadOnlyTableSnapshot.java` |
| IBM i release, time zone, clock | `SYSIBMADM.ENV_SYS_INFO`, `QSYS2.SYSTEM_VALUE_INFO` (`QTIMZON`), `SYSIBM.SYSDUMMY1` | `SourceInfo.java`, `JournalTimestamps.java` |
| Counts and metadata for qualification and diagnosis | `DISPLAY_JOURNAL` sequence numbers, entry types and counts (no row image) | `JournalDlScan.java`, `JournalWindowOracle.java` |
| Row images of declared tables, for qualification measurements | `DISPLAY_JOURNAL` including `HEX(ENTRY_DATA)`, the journal row image, which is business data. The class prints only a SHA-256 of it. | `MultiObjectDisplayJournal.java` |

Reads of row images and of table contents carry your business data to the
reader; see [your data](your-data.md). `JournalDlScan`, `JournalWindowOracle` and
`MultiObjectDisplayJournal` are diagnostic and qualification tools: a search over
`src/`, `scripts/`, `chart/`, `docker/` and `deploy/` finds no job or image
entry point that starts them, apart from a script that runs the unit test of
`JournalWindowOracle` (`tests/test_trust_claims.py` checks this).

A Python class, `IbmiJournalReader` (`src/quadringent/ibmi_reader.py`), also
holds journal and catalog queries; all of them are `SELECT` statements. Only two
developer scripts create it, through ODBC (`scripts/as400_journal_probe.py` and
`scripts/as400_reader_benchmark.py`), and neither script is copied into the
capture image.

The capture session is not opened in JDBC read-only mode: `JournalSession.java`
never calls `setReadOnly`. Its protection is the absence of write statements in
the code and the authorities of the profile you give it. The snapshot and
catalog classes do set their connections read-only.
<!-- source: java/src/main/java/io/quadringent/as400/JournalSession.java, java/src/main/java/io/quadringent/as400/ReadOnlyTableSnapshot.java, java/src/main/java/io/quadringent/as400/FleetCatalogProbe.java -->

## Which authorities each component needs

Start from [IBM i authorities](compatibility.md#ibm-i-authorities). That table
lists, per operation, the authority that IBM documents. It has not been tested at
runtime with a minimal profile. Two reads in the code are not in that table:

| Read | Authority |
|---|---|
| `QSYS2.IFS_OBJECT_STATISTICS` (receiver identity, `ReadOnlyReceiverIdentity.java`) | No IBM page checked. Not tested with a minimal profile. |
| `SELECT CURRENT_TIMESTAMP, CURRENT_TIMEZONE FROM SYSIBM.SYSDUMMY1` (clock check, `JournalTimestamps.java`) | No specific authority found. Not tested. |

Which component uses which reads:

| Component | Java class | Reads |
|---|---|---|
| Permanent reader | `PersistentJournalWorker` | Journal entries, receiver chain and identity, clock check |
| Initial copy job | `ReadOnlyTableSnapshot` | `SELECT` on the table |
| Source probe and table discovery jobs | `DiagnosticWorker`, `TableDiscovery`, `SourceInfo` | Sign-on, release, `QTIMZON`, catalog views. No journal entry and no row. |
| Catalog refresh | `FleetCatalogProbe` | Catalog views and `JOURNAL_RECEIVER_INFO`, read-only connection |
| `quadringent doctor` on your machine | none | TCP and TLS handshake only. It never signs on and never sends IBM i credentials. |

<!-- source: scripts/as400_continuous_capture.py, scripts/quadringent_initial_copy_job.py, scripts/quadringent_source_probe_job.py, scripts/quadringent_table_discovery_job.py, src/quadringent_control_plane/fleet_catalog_refresh.py, src/quadringent/diagnostics/network.py -->

Use a dedicated profile with the authorities from the compatibility page and
without `*ALLOBJ`. The compatibility page explains why.

## What Quadringent creates or changes on the IBM i

In the code of the product's reader path: nothing. No statement or command in it
creates, alters or deletes an object, a row or a file on the IBM i, and no code
writes to the IFS.

Journaling is your decision. Table discovery classifies each table and, when a
table is not ready, stores the commands below with it. The cockpit shows them to
you. Quadringent does not run them: no code in this repository sends a CL
command from the product path.
<!-- source: src/quadringent/table_discovery.py, src/quadringent_control_plane/v2/services/tables.py -->

| Situation | Command shown, with your library, file and journal names |
|---|---|
| No journal in the library | `CRTJRNRCV JRNRCV(LIB/QSQJRN0001) TEXT('Quadringent journal receiver')` |
| No journal in the library | `CRTJRN JRN(LIB/QSQJRN) JRNRCV(LIB/QSQJRN0001) MNGRCV(*SYSTEM) DLTRCV(*NO) TEXT('Quadringent journal')` |
| Table not journaled | `STRJRNPF FILE(LIB/FILE) JRN(LIB/QSQJRN) IMAGES(*BOTH) OMTJRNE(*OPNCLO)` |
| Table journaled with after images only | `CHGJRNOBJ OBJ((LIB/FILE *FILE)) ATR(*IMAGES) IMAGES(*BOTH)` |

With `MNGRCV(*SYSTEM)` and `DLTRCV(*NO)`, receivers are not deleted
automatically: they accumulate until you delete them, so disk use grows with
journal activity and the retention policy is yours. The page
[compatibility](compatibility.md#journal-receiver-retention) explains receiver
retention.

## What it never does

Each line below is checked by a pattern scan in `tests/test_trust_claims.py` or
by the `grep` command in the last section. A scan looks for known forms: SQL
verbs and CL command names in any letter case, the JDBC and JTOpen calls that
write, JTOpen classes outside a reviewed list (a wildcard import is a finding),
and IFS classes. It cannot prove that no other form exists, so read the diff of
any new Java file.

- It does not run `INSERT`, `UPDATE`, `DELETE`, `MERGE`, `CREATE`, `DROP` or
  `ALTER` on the IBM i from the Java reader classes, except the class named
  below.
- It does not run a CL command or `QCMDEXC`, and does not use the JTOpen
  command or program call classes, in the Java classes of this repository.
  The journal-parsing library calls IBM journal APIs; that is covered by the
  authorities table, not by this scan.
- It does not write to the IBM i IFS.
- It does not install anything on the IBM i.

What these checks do not cover: the effect of a connection on IBM i job logs,
audit entries and server jobs, and the CPU and I/O cost of the reads. Not
measured: we give no figure for them. Statements issued inside third-party libraries are not
scanned.

## The one exception: the qualification driver

The Java archive built from `java/` contains the class
`QualificationSourceDriver`. It can run `INSERT`, `UPDATE` and `DELETE` on one
declared qualification table, and a `CHGJRN` command through `QSYS2.QCMDEXC`,
for our qualification runs. It takes the host, user and password on
standard input and restricts statements to its declared table and key.
<!-- source: java/src/main/java/io/quadringent/as400/QualificationSourceDriver.java -->

- Its write modes (`exec`, `exec-timed`, `freshness-worker`, `rotate`) are named
  only in `src/quadringent_qualification/`, which is not part of the installed
  Python package and not copied into the capture image.
- The product's jobs, readers and control plane do not reference the class. The
  wheel's `quadringent.qualification` package references it only in the
  read-only modes `dump` and `rowpos`.
- The scan in `tests/test_trust_claims.py` lists this file as the only
  allowed exception and fails if another Java file needs one.

Not tested: that nobody can start the class by hand in a container that holds
the IBM i credentials. A person with that access can already use the profile
directly; the profile's authorities are the limit.
<!-- source: src/quadringent_qualification/real_source.py, src/quadringent/qualification/proof.py, src/quadringent/qualification/collect.py, pyproject.toml, docker/Dockerfile -->

## Ports and TLS

| Port | Use | Source |
|---|---|---|
| 9471 | Database host server over TLS | `src/quadringent/diagnostics/network.py` |
| 9476 | Sign-on server over TLS | same |
| 9475 | Remote command server over TLS. No Java class of this repository sends a CL command; whether the journal-parsing library's IBM API calls use this server is Not tested here, because its source is not in this repository. | same |
| 449 | Server mapper, needed unless the database, sign-on and command ports are all set explicitly | same, `JournalSession.java` |

TLS is on by default, and the product refuses plaintext. The Java reader class
itself would accept plaintext only with two explicit settings together,
`AS400_TLS=false` and `AS400_ALLOW_PLAINTEXT=true`. In the product, three
layers refuse it: the chart does not render when `as400.allowPlaintext` is true
or `as400.tls` is not true, the installer sets `allowPlaintext` to false, and
the control-plane onboarding rejects a source with `tls` other than true or with
`allow_plaintext` true. This matches [SECURITY.md](../../SECURITY.md). The
reader trusts the JVM default trust store, or the PEM CA file you give in
`AS400_TLS_CA_FILE`; the code has no trust-all path and refuses a CA file that
contains a private key. Whether the JTOpen driver also checks the host name on
its own connections: Not tested here. `quadringent doctor --check-ibmi HOST
--tls` performs a handshake that verifies the certificate chain and the host
name.
<!-- source: java/src/main/java/io/quadringent/as400/TlsTrust.java, java/src/main/java/io/quadringent/as400/JournalSession.java, chart/values.yaml, chart/templates/deployment.yaml, src/quadringent/installer/plan.py, src/quadringent_control_plane/onboarding.py, SECURITY.md, src/quadringent/diagnostics/network.py -->

## Where the IBM i credentials are stored

- The IBM i password is read by the Java process from the environment variable
  `ISERIES_PASSWORD`, which Kubernetes fills from a Secret. It is not a command
  argument.
- In the cockpit wizard, the password is encrypted in the control plane's
  database with a Fernet key, then copied into a Kubernetes Secret named
  `qdt-source-` plus the source identifier (key `ISERIES_PASSWORD`) in your
  cluster. The Fernet key is itself a Kubernetes Secret.
- With Helm values, the chart references a Secret that you create
  (`ibmi.passwordSecret`); see [Helm installation](install-client.md).
- The password does not leave your cluster. See [your data](your-data.md) for the
  list of outbound connections.
<!-- source: java/src/main/java/io/quadringent/as400/JournalSession.java, src/quadringent_control_plane/v2/executor/secrets_provisioner.py, src/quadringent_control_plane/v2/crypto.py, chart/templates/control-plane-v2-secrets.yaml, docs/product/install-client.md -->

## How to check this yourself

The first three steps need the source and a Python environment on your own
machine; they do not touch your IBM i.

1. Get the source (`git clone https://github.com/quadringent/quadringent-ce.git`,
   as in [Verify a release](verify-a-release.md)) and set up the Python
   environment described in [CONTRIBUTING.md](../../CONTRIBUTING.md#checks).
2. Read the statements. They are in `java/src/main/java/io/quadringent/as400/`.
   This search lists the places where a statement could change data or call a
   command; only `QualificationSourceDriver.java` should appear:

   ```sh
   grep -nE "executeUpdate|executeBatch|addBatch|\.execute\(|prepareCall|CommandCall|ProgramCall|QCMDEXC|IFS[A-Za-z]|insertRow|updateRow|deleteRow|CONCUR_UPDATABLE|com\.ibm\.as400\.access\.\*" \
     java/src/main/java/io/quadringent/as400/*.java
   ```

3. Run our check. It fails if a Java reader class matches one of the
   scanned patterns, or if this page and the code drift:

   ```sh
   python -m pytest -q tests/test_trust_claims.py
   ```

4. Test reachability and TLS from your network without signing on:

   ```sh
   quadringent doctor --check-ibmi HOST --tls --ibmi-ca-file ca.pem
   ```

5. Trial it on your own terms, and record what your system does:
   - Create a separate user profile for the trial, with only the authorities in
     [compatibility](compatibility.md#ibm-i-authorities) and without `*ALLOBJ`.
   - Start on a non-production partition with a few tables.
   - Review the activity of that profile in your own audit journal (sign-ons,
     and access to the journal and its receivers), and your job logs, over the
     trial period.

Load, job-log volume and audit volume on the IBM i are Not measured: this
repository gives no figure for them.

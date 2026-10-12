# Compatibility and prerequisites

Use this page to check whether your IBM i (formerly AS/400 and iSeries) and
Snowflake setup matches what Quadringent has been tested with, before you
install anything. Each row has a status:

- **Tested**: exercised in a published or recorded DEV/INT qualification, with
  the scope given in the row.
- **Required**: the code or the installer needs it; this is a prerequisite, not
  a test result.
- **Not tested**: no qualification covers it. It may work; nothing here says so.

Sources are the public code at the commit you read, the
[dated qualification summary](../../site/qualification-0.3.4.json) and the
IBM and Snowflake documentation linked below. Nothing on this page is a
production qualification.

## IBM i source

| Item | Status | Detail and source |
|---|---|---|
| IBM i 7.5 | Tested (historical, not tied to 0.3.4 or 0.3.5) | A shared IBM i 7.5 qualification server is recorded for a historical measurement on one table (`docs/decisions/2026-09-23-attached-receiver-tail.md`). The published qualification summaries do not name the IBM i release. |
| IBM i 7.4 and 7.6 | Not tested | Suggested CL syntax was checked on 7.4/7.5 (`src/quadringent/table_discovery.py`); no replication test. On 7.4, IBM lists `JOURNAL_RECEIVER_INFO` from DB2 PTF group SF99704 level 20 and `JOURNALED_OBJECTS` from level 10 ([IBM i Services](https://www.ibm.com/support/pages/node/1119123)). |
| IBM i 7.3 and earlier | Not tested | The same views need SF99703 levels 28 and 22 on 7.3. |
| Journaled tables, `*BOTH` images | Required | Discovery marks a table `ready` only when it is journaled with before and after images (`docs/product/install-client.md`). With `*AFTER` only, updates and deletes are not replicated correctly. |
| Primary key or unique constraint | Required | A primary key or unique constraint is needed. Discovery reads `PRIMARY KEY` and `UNIQUE` constraints (`TableDiscovery.java`). A unique index without a constraint is not detected: Not tested. |
| Tables without a key (RRN) | Not tested | Allowed only with an explicit acknowledgement; any `RGZPFM` or `CLRPFM` then needs a resynchronization. |
| One journal per reader | Required | Tables from different journals need separate readers; one reader per journal, at most 32 tables per capture session (`JournalSession.java`). |
| Column types: integer, varchar, char, decimal(11,2), date, timestamp(6) | Tested | All seven business columns of the qualification table matched in the 0.3.4 readback (`site/qualification-0.3.4.json`); the column types are listed in `docs/qualification.md`, whose generator also produces nulls, empty and accented text, padded CHAR and signed decimals. |
| Other types: SMALLINT, BIGINT, TIME, GRAPHIC, BINARY, LOB, DECFLOAT, XML | Not tested | Mapped or rejected by the loader (`docs/product/snowflake-destination.md`); an unmapped type stops with `UnsupportedColumnTypeError`. |
| CCSID 65535 columns | Required | Must be reclassified as binary at discovery, otherwise the column is rejected. Other CCSIDs than the test server's: Not tested. |
| Writes under commitment control | Not tested | Measurements used isolated writes without commitment control. Rollback entries stop the window without advancing the checkpoint (`JournalSession.java`); uncommitted changes are not filtered by commit cycle. |
| `RGZPFM`, `CLRPFM` | Not tested | Plan a manual resynchronization of the table after either command. The backlog reader stops on file-level journal entries, but the tail reader only reads row entries (`JOURNAL_CODES => 'R'` in `JournalSession.java`), so detection is not guaranteed. |
| `CHGPF`, `ALTER TABLE`, `DROP TABLE` | Not tested | No specific handling is documented; plan a resynchronization. |
| Receiver rotation (`CHGJRN`) | Not tested | The reader follows receivers in attach order and stops on a sequence gap. Rotation was not qualified (`rotation_qualified=false` in the native qualification). |
| TLS host servers 9471, 9475, 9476 | Required | Database, remote command and sign-on servers over TLS; plaintext is refused. The site supplies its CA. `quadringent doctor --check-ibmi <host>` tests TCP reachability, and `--tls` a verified handshake. |
| Server mapper port 449 | Required (unless every service port is set) | JTOpen looks up service ports on 449 unless the database, sign-on and command ports are all configured ([IBM](https://www.ibm.com/support/pages/how-prevent-port-lookup-ibm-toolbox-java)). `quadringent doctor --check-ibmi <host>` tests it over TCP in that case. |
| `QTIMZON` declared to the site | Required | The source time zone is read, never assumed to be UTC (`docs/product/install-client.md`). |

## IBM i authorities

Quadringent only reads on IBM i: the product path runs no INSERT, UPDATE,
DELETE, DDL or CL command (CL commands are suggested for an administrator,
never executed). The list below derives from the operations in the public code
and from IBM's documented authority for each one. It has **not been tested at
runtime with a minimal profile**; validate it on a DEV/INT system.

| Operation (code) | Documented authority | IBM source |
|---|---|---|
| `QSYS2.DISPLAY_JOURNAL`, tail reads (`JournalSession.java`) | `*USE` on the journal and `*EXECUTE` on its library; `*USE` on each receiver read and `*EXECUTE` on its library; `*OBJEXIST` on the journal when no object is named or the object no longer exists. Authority on the named file is not stated on this page; the related API requires `*USE` on it | [DISPLAY_JOURNAL](https://www.ibm.com/docs/en/i/7.5?topic=services-display-journal-table-function) |
| `QjoRetrieveJournalEntries`, backlog reads (journal parsing library) | Journal, receivers and journaled file: `*USE`; their libraries: `*EXECUTE`; `*OBJEXIST` on the journal in the cases IBM lists | [QjoRetrieveJournalEntries](https://www.ibm.com/docs/en/i/7.5?topic=ssw_ibm_i_75/apis/QJORJRNE.htm) |
| `QjoRetrieveJournalInformation` (journal parsing library) | `*OBJOPR` plus a data authority other than `*EXECUTE` on the journal; `*EXECUTE` on its library | [QJORJRNI (7.4)](https://www.ibm.com/docs/ssw_ibm_i_74/apis/QJORJRNI.htm) |
| `QjoRtvJrnReceiverInformation` (journal parsing library) | `*OBJOPR` plus a data authority other than `*EXECUTE` on the receiver and `*EXECUTE` on its library; `*OBJOPR` on the journal and `*EXECUTE` on its library | [QJORRCVI](https://www.ibm.com/docs/en/i/7.5?topic=ssw_ibm_i_75/apis/QJORRCVI.htm) |
| `QDBRTVFD` (file description) | `*OBJOPR` on the file, `*EXECUTE` on its library | [QDBRTVFD](https://www.ibm.com/docs/en/i/7.5?topic=ssw_ibm_i_75/apis/qdbrtvfd.htm) |
| `QSYS2.JOURNAL_RECEIVER_INFO` (`ReadOnlyReceiverCatalog.java`) | `*OBJOPR` plus a data authority on the receiver and `*EXECUTE` on its library; `*OBJOPR` on the journal and `*EXECUTE` on its library | [JOURNAL_RECEIVER_INFO](https://www.ibm.com/docs/en/i/7.5?topic=services-journal-receiver-info-view) |
| `QSYS2.JOURNALED_OBJECTS` (`TableDiscovery.java`) | `*OBJOPR` plus a data authority on the journal, `*EXECUTE` on its library | [JOURNALED_OBJECTS](https://www.ibm.com/docs/en/i/7.5?topic=services-journaled-objects-view) |
| `SELECT` on each table for the initial copy (`ReadOnlyTableSnapshot.java`) | Read access to the table and `*EXECUTE` on its library (`*USE` covers both) | Not verified on an IBM page in this review |
| Catalog views (`QSYS2.SYSTABLES`, `SYSCOLUMNS`, `SYSCST`, `SYSKEYCST`, `SYSTABLESTAT`, `SYSINDEXES`, `SYSKEYS`) and `SYSIBMADM.ENV_SYS_INFO` (`TableDiscovery.java`, `FleetCatalogProbe.java`, `SourceInfo.java`) | No specific authority found in IBM documentation | Not verified |
| `QSYS.QADBKATR` and `QSYS2.SYSTABLES`, read by the journal parsing library to decode row images | No authority found in IBM documentation | Not verified |
| `QSYS2.SYSTEM_VALUE_INFO` for `QTIMZON` | No special authority; only audit system values need `*ALLOBJ` or `*AUDIT` | [SYSTEM_VALUE_INFO](https://www.ibm.com/docs/en/i/7.5?topic=services-system-value-info-view) |

From these pages, *ALLOBJ is not required for the documented operations, and
no special authority (`*AUDIT`, `*JOBCTL`) is listed for them. Do not grant
*ALLOBJ to the reader profile for convenience. Prefer a profile without change
authority on business objects: the capture session itself is not opened in
read-only mode, so least privilege on the profile is the safeguard.

## Journal receiver retention

Receivers must stay attached or online until the checkpoint has passed them;
the reader stops on a missing receiver and a restart cannot recover it. Size
the online retention as:

**retention ≥ maximum outage + maximum lag + recovery time**, plus your own
safety margin, then convert it to receiver storage with your journal's
measured growth rate.

Hypothetical example, not a recommendation: if the reader may stay stopped for
48 h over a weekend, may lag by 1 h, and recovery takes 4 h, retention is at
least 53 h; with a 25 % margin, about 66 h. At an assumed journal growth of
2 GB per hour, that is about 132 GB of receivers kept online. Replace every
number with your own measurements.

`CRTJRN ... MNGRCV(*SYSTEM)` lets the system detach receivers at their
threshold, which needs a threshold other than `*NONE`
([CRTJRN](https://www.ibm.com/docs/en/i/7.5?topic=ssw_ibm_i_75/cl/crtjrn.htm)).
`DLTRCV(*YES)` can delete detached receivers that Quadringent has not read yet;
keep `DLTRCV(*NO)` or make sure your receiver cleanup checks the checkpoint
first.

## Snowflake prerequisites

| Item | Default mode: SQL history and MERGE | Snowpipe Streaming (optional) |
|---|---|---|
| Provisioning administrator | Runs the script generated by the wizard once. It creates a role, a `TYPE = SERVICE` user with an RSA public key, a warehouse, a database and schemas. ACCOUNTADMIN is not required by the statements themselves: they need `CREATE ROLE` and `CREATE USER` (USERADMIN), `CREATE WAREHOUSE` and `CREATE DATABASE` (SYSADMIN), and `MANAGE GRANTS` for future grants (SECURITYADMIN), or one custom role holding these privileges. A split run with the default roles: Not tested. | Same script, plus `CREATE TABLE` on the mirror schema |
| Runtime service role | `QDT_ROLE_*`, key-pair authentication, no password. It owns the history and mirror tables it creates and runs `MERGE`. | Also writes through named channels; no extra objects created by the product |
| Warehouse | `XSMALL`, `AUTO_SUSPEND = 60`, `AUTO_RESUME = TRUE`, `INITIALLY_SUSPENDED = TRUE` (`src/quadringent_control_plane/v2/services/destinations.py`) | Same warehouse for the mirror `MERGE` |
| Stage, pipe, integration | Not used: no stage, pipe or storage integration; the loader reads raw batches from S3 or GCS and runs `MERGE` | No stage or integration |
| Status | Required (script) / Not tested (minimal split roles) | Not tested in the published qualification summaries |

Statement privileges: [CREATE ROLE](https://docs.snowflake.com/en/sql-reference/sql/create-role),
[CREATE USER](https://docs.snowflake.com/en/sql-reference/sql/create-user),
[CREATE WAREHOUSE](https://docs.snowflake.com/en/sql-reference/sql/create-warehouse),
[CREATE DATABASE](https://docs.snowflake.com/en/sql-reference/sql/create-database),
[GRANT](https://docs.snowflake.com/en/sql-reference/sql/grant-privilege) and
[key-pair authentication](https://docs.snowflake.com/en/user-guide/key-pair-auth).

Under verification: with the default schemas, the generated script grants
`CREATE TABLE` on the history schema only, while the loader also creates the
mirror table. If **Verify access** fails on the mirror schema, grant
`CREATE TABLE` on it to the service role. The classic Snowpipe path with an
external stage, storage integration and S3 notifications belongs to the v1 AWS
DEV path in the [Helm guide](install-client.md), not to the default installer.

## Default resources and cost

| Resource | Default | Source |
|---|---|---|
| AWS dedicated VM | EC2 `t3.medium`, 40 GB encrypted root volume, Amazon Linux 2023, k3s | `deploy/terraform/aws/vm/variables.tf` |
| GCP dedicated VM | Compute Engine `e2-medium`, 40 GB `pd-ssd` boot disk, no public IP | `deploy/terraform/gcp/vm/variables.tf` |
| Existing EKS or GKE | No cluster created; pods use existing capacity | [installation](install-default.md) |
| Checkpoints | DynamoDB on demand (AWS) or the raw GCS bucket (GCP) | [installation](install-default.md) |
| Control plane and PostgreSQL | Requests 170m CPU and 480 MiB in total, limits 1.7 CPU and 1.75 GiB; 5 GiB PostgreSQL volume | `chart/values.yaml` |
| Permanent reader | Requests 50m CPU and 512 MiB, limits 1 CPU and 2 GiB | `chart/values.yaml` |
| Readers, loaders and copy Jobs started by the v2 control plane | No requests or limits set | `src/quadringent_control_plane/v2/executor/manifests.py` |
| Snowflake credits per day | Not measured | No public dated receipt |
| Cloud cost per month | Not measured | Use your provider's pricing for your region |

The warehouse resumes when the loader runs a query and suspends after 60
seconds without one. How often it resumes depends on your change rate; that has
not been measured. Set billing alerts and stop rules before installation.

# Evidence

Quadringent is a preview for DEV/INT use. Source version **0.4.2** has had no qualification run yet. The latest observed releases are 0.3.5 (initial copy, 2026-10-12) and 0.3.4 (recovery, 2026-10-12). Production: not qualified. Native CDC writes on 0.3.5 are Not tested.

We use four words for evidence, here and everywhere else: Measured (we have a number, with its scope), Not measured (we have no number), Not tested (we ran nothing) and Not supported (it does not work, or we do not intend it to). Every result stays bound to the version, targets and clock it was observed with. A result on 0.3.4 says nothing about 0.4.2.

## What went wrong, and what we do not know

In the first 0.3.4 cold-warehouse trial, 3 of 4 targets missed our 10 s bar per write. With all four Snowflake loader warehouses suspended before the first write, the worst write took 11.207 s on AWS VM, 13.641 s on GCP VM and 11.802 s on GKE. Only EKS passed, at 8.520 s. A later campaign on the same release (cold and warm, one of each per target) passed on all four targets, at most 8.876 s. Both runs stay on record.

IBM i CPU and I/O cost is Not measured. The footprint page says what the reader does on your IBM i; it does not say how much that costs your system.

Only IBM i 7.5 has been tested, on one shared qualification server, in a historical measurement on one table. The published summaries do not name the release. IBM i 7.4 and 7.6 are Not tested. See [compatibility](product/compatibility.md).

Reconciliation counts events. It does not reconcile business values by key.

Latency beyond the campaigns below is Not measured. We have no P95, no SLA and no universal latency figure. Snowflake cost per 24 h is Not measured, and no invoice was verified.

## Where the tests ran

| Target | Setup | Storage |
|---|---|---|
| AWS VM | EC2 VM with k3s | S3 |
| EKS | Existing EKS cluster | S3 |
| GCP VM | Compute Engine VM with k3s | GCS |
| GKE | Existing GKE cluster | GCS |

Snowflake held history and mirror tables in a dedicated schema, behind a scoped role. IBM i held synthetic journaled tables with before and after images, read over TLS. All tests used synthetic tables on isolated installations. These are maintainer-observed results, not an external certification.

## 0.4.2, current source

Native continuity, reader outage, latency, complete cloud-loss recovery and the operator path are Not tested on 0.4.2. This version binds native capture to the original admitted receiver, refuses automatic adoption of legacy v1 checkpoints, and adds selected replay backup and restore with guarded RAW retention. Keep the previous version and its state until a migration has been verified; see the [native capture guide](product/native-qualification.md).

## 0.3.5, 2026-10-12: initial copy and an EKS cycle

Receipt: [runtime-0.3.5.json](../site/runtime-0.3.5.json). Release notes: [v0.3.5](https://github.com/quadringent/quadringent-ce/releases/tag/v0.3.5).

Fresh initial copy of two synthetic tables, 55 source rows each, from initially empty destination tables. Eight new copy Jobs ran across the four targets. Each target ended with 110 mirror rows and 110 history C images, with exact typed values. No new source INSERT, UPDATE or DELETE was performed, so 0 new source writes.

| Target | Mirror rows | History C images | New copy Jobs | Upgrade path |
|---|---|---|---|---|
| AWS VM | 110 | 110 | 2 | direct Helm only |
| EKS | 110 | 110 | 2 | public installer |
| GCP VM | 110 | 110 | 2 | public installer |
| GKE | 110 | 110 | 2 | public installer |

On EKS, the cycle 0.3.5 to 0.3.4 to 0.3.5, with an application reinstall, kept the same PostgreSQL PVC, keys, pipeline identities, RAW data and external checkpoints. Pipelines stayed paused. Nondefault load settings were not exercised.

On AWS VM the public installer refused an AMI-change replacement; the upgrade we observed used direct Helm. A complete AWS public-installer upgrade is Not tested.

The baseline was restored afterward from typed-verified pre-copy clones. A separate typed read after that restoration was not part of the review. Fresh campaign resources and temporary access were removed, and the cleanup was independently verified. Protected original stopped VMs and disks remain.

Not tested on 0.3.5: native CDC writes, reader outage, cold and warm latency (no new source mutation, no SIGKILL-with-ACK campaign, no latency trial) and complete infrastructure-loss recovery. Reusing the EKS PVC is an application reinstall, not a cloud-loss reconstruction. Source equivalence to 0.3.4 does not turn 0.3.4 observations into 0.3.5 evidence.

## 0.3.4, 2026-10-12 18:13 UTC: recovery campaign

Receipt: [recovery-0.3.4.json](../site/recovery-0.3.4.json).

Fresh infrastructure and new PostgreSQL PVCs on all four targets restored PostgreSQL, original encryption and auth state, RAW data, external checkpoints and configured pipelines. The baseline came from historical archives, so this was not an initial copy from empty on 0.3.4.

Four reader SIGKILL trials, with source writes acknowledged inside the stops, passed native resume and complete typed readback: 110 mirror rows and 148 history images per site, with unique event IDs.

All 8 cold/warm observations met the 10 s gate: one cold and one warm trial per target. A cold warehouse was read as suspended immediately before the SQL statement. The figures are SQL begin to Snowflake mirror visibility, as upper bounds, not P95 or an SLA.

| Target | Mirror rows | History images | Cold upper bound (s) | Warm upper bound (s) | 10 s bar |
|---|---|---|---|---|---|
| AWS VM | 110 | 148 | 6.983 | 3.995 | Passed |
| EKS | 110 | 148 | 7.595 | 3.763 | Passed |
| GCP VM | 110 | 148 | 8.876 | 5.711 | Passed |
| GKE | 110 | 148 | 8.548 | 5.697 | Passed |

An earlier AWS crash window was too short for an ACK. A separately recorded subsequent crash passed. This campaign does not qualify production or an external pilot, and says nothing about native CDC on 0.3.5.

## 0.3.4, 2026-10-12: earlier trial, above the bar

Summary: [qualification-0.3.4.json](../site/qualification-0.3.4.json). Release notes: [v0.3.4](https://github.com/quadringent/quadringent-ce/releases/tag/v0.3.4). We keep its 136-image readbacks and clocks apart from the later 148-image recovery campaign.

The published 0.3.2 artifacts were installed on the four targets, with two synthetic journaled tables and 110 initial typed rows per site. Native INSERT, UPDATE and DELETE, and actual reader termination with writes acknowledged inside the outage, were checked by complete destination reads, without duplicates. Upgrade to 0.3.3, rollback to 0.3.2 and upgrade to 0.3.4 kept pipeline identities, PostgreSQL volumes, original keys and typed history on all four. The 0.3.4 readback matched all 136 history images and 110 mirror rows per site, with all seven business columns matching and 0 duplicate event IDs.

An application reinstall (CLI uninstall, then install) kept PostgreSQL volumes, keys and pipeline identities. Pipelines stayed paused, and renewed CDC was not verified.

The cold-warehouse trial was one trial of six source writes, with all four loader warehouses suspended before the first write and corroborated by loader query history. Readers and loaders were already running. We observed destination visibility with a separate query, so each figure is an upper bound on first visibility, not an ingestion timestamp. The maximum over six writes, in seconds:

| Target | SQL start to visible | Source acknowledgement to visible | Including source-client launch | 10 s bar |
|---|---|---|---|---|
| AWS VM | 11.207 | 11.070 | 16.466 | Above |
| EKS | 8.520 | 8.454 | 13.936 | Passed |
| GCP VM | 13.641 | 13.560 | 18.795 | Above |
| GKE | 11.802 | 11.736 | 16.863 | Above |

Source-client startup took 5.06 s of the launch figure. Each site gave twelve history and mirror signals across the six writes. This is not an application-startup test, not a universal latency guarantee and not a production qualification.

At the time of that summary, reader outage on 0.3.4 was not demonstrated (it was on 0.3.2), nor were a fresh installation and initial copy (on 0.3.2: 2 tables and 110 rows per site), nor complete site loss and reconstruction. The recovery campaign above covers the reader outage and the reconstruction; fresh installation and initial copy from empty on 0.3.4 remain Not tested.

## Earlier published versions

Their clocks are not comparable with the cold-warehouse clock of the first 0.3.4 trial.

| Version | Test | Targets | Result |
|---|---|---|---|
| 0.3.2, 0.3.3, 0.3.2, 0.3.4 | Upgrade and rollback | All four | Pipeline identities, PostgreSQL volumes, original keys and complete typed history retained. Running images matched the admitted release. |
| 0.3.2 | Fresh installation, typed copy, native CDC and reader outage | All four | Two tables and 110 initial rows per site. INSERT, UPDATE, DELETE and acknowledged writes entirely inside actual reader outages checked by complete history and mirror reads, without duplicates. This does not qualify 0.3.4 reader recovery. |

## 0.2.x, October 5 to 6, 2026: artifacts not published

These versions predate this public repository. Their artifacts and evidence are not published here. They ran on isolated personal installations. Their latency clock starts at the IBM i acknowledgement with warehouses already running. They do not guarantee ten seconds for every workload, startup or configuration.

| Version | Test | Targets | Result |
|---|---|---|---|
| 0.2.26 | Autonomous path, source client prepared | Autonomous harness | Not measured. The clock starts when each write is sent to the prepared client. |
| 0.2.24 and 0.2.25 | End-to-end autonomous path | Autonomous harness | Measured, above 10 s: first write visible in the mirror in 12.28 s, then 10.37 s, including Docker and JVM startup for the source request. |
| 0.2.23 | End-to-end autonomous path | Autonomous harness | Incomplete. 154 events and 110 rows reconciled, including after journal rotation; the latency phase failed before any write. |
| 0.2.22 | Native write and full readback | AWS VM, EKS, GCP VM, GKE and a fifth fresh wizard installation | Measured: one write reached all five mirrors in 5.013 to 7.776 s after IBM i acknowledgement, 7.827 s at most including the JDBC statement start. Container startup and source authentication are excluded, so this is not a cold-start measurement. Five complete reads checked 110 typed source rows, retained prior events and showed no duplicates. Historical RAW coverage remains partial on AWS VM and the wizard site. |
| 0.2.22 | Isolated backup restore | GCP VM | Identities, keys and four declared tables retained, with only one installation active during each readback. This differs from the complete 0.2.16 reconstruction. |
| 0.2.19 | Native write after artifact admission | AWS VM, EKS, GCP VM, GKE | Measured: one new native write visible in every mirror in 4.904 to 7.041 s. Eight source, history and mirror reads checked all seven typed columns, prior events and the absence of duplicates. Each authenticated cockpit showed one metric and three logs from the actual Kubernetes loader. This single-write check is separate from the thirty-write 0.2.18 campaign. |
| 0.2.19 | Fresh wizard installation | One site | Activation, sign-in and the IBM i TLS probe checked. Its fresh copy and the autonomous harness are Not tested. |
| 0.2.18 | Thirty native updates over ten minutes | AWS VM, EKS, GCP VM, GKE | Measured: 120 observations from IBM i acknowledgement to Snowflake mirror, 2.317 to 7.992 s. Complete reads checked typed data, retained prior events and showed no duplicates. |
| 0.2.17 | Recopy and historical oracle | AWS VM | Recopy data and the historical oracle checked. Its Job expired before the full API inventory was archived; we claim no strict verification of that inventory. |
| 0.2.16 | Rebuild after complete loss of the four sites | AWS VM, EKS, GCP VM, GKE | PostgreSQL and original keys restored; RAW data and external checkpoints retained without regression. Continuity was read back afterward with 0.2.18 and 0.2.19. |
| 0.2.15, 0.2.13, 0.2.15 | Upgrade and rollback | Affected sites | Data and identities retained. |

An earlier GKE DEV test used synthetic tables and a different loading profile. It measured 6.35 to 31.31 s to the mirror and does not describe the native 0.2.18 campaign.

Version 0.2.5 was exercised in DEV on GKE, EKS, GCP VM and AWS VM: installation, upgrade 0.2.3 to 0.2.5, rollback to 0.2.3, complete loss of the original site and reconstruction of a fresh site from public 0.2.5 artifacts. A 0.2.5 backup and keys exported off-site were restored and checked against the control plane database, including sign-in, synthetic data parity and decryption. This is lifecycle evidence only; it does not qualify 0.2.5 CDC or production stability. PostgreSQL and PVC UIDs were not measured directly on AWS VM, and child and config digests there come from sealed artifacts where no direct runtime read was made.

Release notes define the verified scope of each batch.

## Limits

| Area | Status | Detail |
|---|---|---|
| Production use | Not tested | Production-scale capacity, minimal IBM i authority and external-user validation are all Not tested. |
| 0.4.2 native and cloud operation | Not tested | Native continuity, reader outage, latency, complete cloud-loss recovery and the operator path need fresh qualification. Historical measurements apply only to their original artifacts and scopes. |
| 0.3.5 native CDC writes, reader outage, latency | Not tested | The copy and the EKS cycle involved no new source mutation and no acknowledged write during a reader failure. |
| 0.3.5 complete infrastructure loss | Not tested | EKS reinstall retained the same PostgreSQL PVC. |
| AWS public-installer upgrade on 0.3.5 | Not tested | An AMI-change replacement was refused; the upgrade we observed used direct Helm. Preservation of nondefault EKS load settings was not exercised. |
| Initial copy from empty on 0.3.4 | Not tested | Its later reconstruction restored a historical baseline. The fresh copy on 0.3.5 is separate evidence. |
| Latency | Not measured | Eight 0.3.4 cold and warm upper bounds passed 10 s and the earlier failed trial stays on record. No P95, SLA or universal guarantee. |
| IBM i CPU and I/O cost | Not measured | See the [footprint](product/ibm-i-footprint.md). |
| IBM i releases other than 7.5 | Not tested | See [compatibility](product/compatibility.md). |
| Snowflake cost per 24 h | Not measured | No invoice was verified. |
| Business values by key | Not supported | The destination proof reconciles event counts only. |
| Screens other than desktop | Not tested | The cockpit is designed for desktop workstations, with a 1440 px keyboard-and-mouse reference. |
| Cockpit access | Open by default | The v1 cockpit uses loopback without authentication by default. Read [Security](../SECURITY.md) before exposing it or granting action permissions. |
| Evidence per table | Measured where present | Capture, delivery and reconciliation each need their own current observation. A missing measurement stays visible as Not measured. |

## What the software covers

Java and JTOpen read the journal over TLS, raw files go to S3 or GCS (or to a local data directory in [local mode](product/try-it-local.md)), and a durable checkpoint records the position. Capture and reconciliation target Snowflake. The cockpit (desktop, 1440 px reference), the `/v1` API and connection configuration come with it, along with capability-driven actions, confirmations and an audit log.

The targets observed so far, release by release, are AWS VM, EKS, GCP VM and GKE.

Cost views are optional and are not a bill. Snowflake credits cover 24 h, ending at least 6 h before observation, converted at the site's configured rate. For S3 Standard we show CloudWatch volume and a monthly estimate at constant volume, using the observed public AWS rate. For OpenCost we show the Quadringent namespace allocation, the contextual cluster total and unused resources.

The runtime permits `dev`, `int`, `test` and `staging`. The chart requires a namespace matching `site.namespace` and blocks production promotion; examples use `dev` and `quadringent-demo`.

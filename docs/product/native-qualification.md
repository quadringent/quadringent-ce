# Qualify a native installation

The installed command observes the product's readers and loaders on GKE, EKS
or k3s VMs. It starts no second capture/loader and does not replace the Docker
development harness.

The [October 12, 2026 preview summary](../../site/qualification-0.3.4.json)
records observed artifacts, typed data, version cycles and latency for 0.3.4.
The cold-warehouse 10 s gate failed on three targets. Earlier reader-failure
results apply to 0.3.2; complete site reconstruction and renewed CDC still need
their own current-release evidence.

```sh
quadringent qualification native --config /private/path/site.json \
  --out-dir /private/path/evidence --phase baseline
```

Configuration and referenced files require 0600; output directories require
0700. Each phase exclusively creates <phase>.json with 0600 permissions:
existing evidence is never overwritten. Use a separate directory for each
observation/ACK. Standard output reports only status, path and verdict.
Sanitized failures return nonzero; business values and site identities remain
in private evidence.

## Fresh release admission

admit-release accepts a configuration containing only release. Through an
authenticated gh session, download oci-scan-receipt and oci-admission-metadata
from the **same run and attempt**, before their one-day retention expires.

```json
{
  "release": {
    "mode": "fresh",
    "repository": "example-org/quadringent",
    "visibility": "private",
    "run_id": 123,
    "attempt": 1,
    "source_sha": "<exact 40-character commit SHA>",
    "tag": "v0.3.4",
    "workflow_sha256": "<SHA256 of the reviewed release workflow>",
    "artifact_id": 456,
    "artifact_zip": "/private/path/oci-scan-receipt.zip",
    "metadata_artifact_id": 457,
    "metadata_artifact_zip": "/private/path/oci-admission-metadata.zip",
    "metadata_root": "/private/path/metadata"
  }
}
```

These numbers are structure examples, never evidence. Expected visibility
(private/public) is required and checked without fallback. Admission requires
the reviewed workflow, exact source/tag, all seven canonical push/main CI jobs,
and successful build, scans of six variants/all layers, SBOM, sealing, original
metadata, promotion and signature steps.

Authenticated artifact ID, run, attempt and GitHub ZIP digest bind the downloaded
originals. Exactly 21 OCI files are compared to original passed.json: three
wrappers, three oci-layout files, three indexes, six manifests and six configs.
No layer is reconstructed locally.

```sh
quadringent qualification native --config /private/path/admission.json \
  --out-dir /private/path/admission-evidence --phase admit-release
```

Retain both ZIPs, original metadata, scan receipt and admit-release.json.
Pin the returned admission_sha256, switch release.mode to conserved, add
release.admission and release.admission_sha256, and retain every other release
field. Later phases require this conserved mode, rechecking the pin, ZIPs,
metadata and currently executed images/identities. They do not reauthenticate
expired artifacts or locally revalidate all layers.

Protect the pinned anchor/configuration. Changing the pin is a new trust decision.
A bare complete_seal_verified=true is rejected. Complete scan evidence belongs
to CI; admission alone does not prove that the site was created afterward.

## Native site configuration

Add native using actual installation/pipeline values. Do not copy IBM i or
Snowflake credentials: probes use existing pod connections.

| Fields | Required contract |
|---|---|
| kubeconfig, context, namespace | Absolute regular kubeconfig; explicit context/namespace |
| pipeline, table_id, table | Exact pipeline/table ID and LIBRARY.TABLE |
| reader_deployment, loader_deployment | Exact product Deployment names |
| capture_digest | Admitted capture OCI index |
| loader_digest | Admitted control-plane OCI index used by the native loader |
| columns, pk | Private JSON column types (int/decimal/text/date/timestamp); unique primary key |
| database, schema, history, mirror | Exact Snowflake destination/tables |
| journal_library, journal_name | Actual IBM i journal |
| copy_run_id, copy_boundary | Actual initial copy/boundary bound to Job/evidence key |
| expected_history_mode | Explicit streaming or sql matching runtime snapshots/observations |
| max_seconds, poll_seconds | Collection 1–120 s, interval 1–10 s; defaults 120/1 |
| identity | irsa, vm-metadata, gke-workload-identity or gcp-vm-metadata |

AWS requires expected_role_arn, checked through STS and the actual session provider.
GCP requires expected_gcp_project, expected_gcp_service_account, expected_gcs_bucket
and gcs_read_key; refreshed metadata identity and an actual copy-evidence GCS read.
GCP VMs additionally require expected_gcp_instance_id, expected_gcp_instance_name,
expected_gcp_zone and expected_gcp_machine_type. Instance metadata, scopes and
the exact Kubernetes node are verified; a static key file cannot attest metadata identity.

The Ready pod is linked by UID to ReplicaSet/Deployment and the product container,
not a sidecar. Linux node architecture selects the exact sealed manifest/config.

## Preserve history before a new copy

A new initial copy rebuilds MIRROR while retaining previous HISTORY epochs.
Before requesting it through the product:

```sh
quadringent qualification native --config /private/path/site.json \
  --out-dir /private/path/before-new-copy --phase retain-history
```

This requires an exact SOURCE/MIRROR/HISTORY baseline and retains the Job, copy
objects, journal bytes, independent ROWPOS and admissions. After the new copy,
update actual run IDs/boundary and pin retained_history_manifest plus
retained_history_sha256 at conservation time.

baseline, oracle and snapshot revalidate admissions, Jobs and immutable objects,
then compare complete HISTORY to the union of independent source oracles.
Snowflake rows never construct expected history. Omitted/altered epochs fail.
Bounds are eight epochs and 1,000 events per table. Keep Jobs/objects available;
expiry can block qualification without changing loader retention.

After an upgrade, the current copy Job may belong to an older release. In that
case add top-level initial_copy_release with its separate conserved admission.
Job digest derives from that admission; current reader/loader use release.
Remove it after a new copy made with the current release.

## Phases and immutable references

refs contains only private file paths, except ordered observations and
observation_receipts lists. The command fabricates no source ACK or receipt.

| Phase | References and effect |
|---|---|
| oracle | Immutable Job-bound copy + complete IBM i journal + independent ROWPOS |
| baseline | Collect baseline.oracle.json and SOURCE/HISTORY/MIRROR snapshot |
| retain-history | Freeze exact baseline and independent epoch evidence |
| snapshot | history_oracle; before after resume to read ROWPOS from its checkpoint |
| positions | before; exact source positions from its checkpoint |
| pause | before; observe reader Deployment absence and zero selected pods |
| resume | before and pause_proof for explicit action; observe new Ready reader with same scope/candidate/identity |
| observe | baseline, receipt; first visible HISTORY/MIRROR times from write start and ACK |
| bind-mutations | ack_receipts, history_oracle, pause_proof, resume_proof, before |
| evaluate | before, after, baseline, pause_proof, resume_proof, mutation_receipts, observations, observation_receipts |
| receiver-catalog | Actual admitted reader's original JDBC receiver catalog |
| bind-rotation-mutations | ack_receipts, history_oracle, rotation_proof, before, after, catalog_before, catalog_after |
| evaluate-rotation | before, after, rotation_proof, catalog_before, catalog_after, mutation_receipts |

Externally authorized ACKs require pipeline, table, table_id, namespace and context,
acknowledged=true, affected_rows=1, operation (insert/update/delete),
write_started_utc, write_ack_utc and mutation_identity (full insert/update image,
primary key for delete). Optional expected must equal mutation_identity.
Optional source_positions may bind positions; otherwise the binder requires one
unique independent image. Raw ACKs never include expected_events: the binder
derives them from actual source readback. ACK/observation lists cover the same
mutations in the same order. Deletes require a baseline row, HISTORY d event and
absence of that key from MIRROR.

QualificationSourceDriver exec-timed uses the same TLS/DML restrictions as exec.
Each successful autocommit statement emits SRC_EXEC_ACK with statement_index,
start_utc, ack_utc, elapsed_ns and affected_rows at executeUpdate return.
JVM startup/connection are excluded; no SQL/identity is printed. Preserve earlier
ACKs after a later failure; missing SRC_EXEC_TOTAL prevents batch success.
Bind raw results to scope, mutation and independent journal images before evaluation.

Sequence: fresh admission → conserved pin → baseline → pause → external acknowledged
mutations → resume/observations → independent oracle → after snapshot →
bind-mutations → evaluate. Start observation promptly; a late collection cannot
shorten measured latency. Qualification requires visibility ≤10 s from both
write start and ACK for each mutation.

New installs use loaderHistoryMode=sql, readerPollSeconds=1,
loaderPollSeconds=1 and loaderFlushEachBatch=false. Upgrades preserve effective
tuning. SQL uses a Snowflake warehouse; explicit flush applies to Streaming.
Measure cost and actual latency: these settings alone do not guarantee ten seconds.

## Optional DEV actions and pause semantics

By default pause/resume only observe actions taken separately by the operator.
To execute native API actions, install the api extra and explicitly configure:

```json
{"actions": {"enabled": true, "environment": "dev", "api_config": "/private/path/site-api.json"}}
```

The private API file contains the exact site's url/token; ambient variables do not
override it. This is an operator restriction, not a server environment attestation.
The CLI rereads pipeline/table, issues only native pause/resume routes, rereads the
transition and observes Kubernetes effects. It never uses kubectl scale.
<phase>.action-request.json persists idempotency before POST;
<phase>.action.json distinguishes execution from observation. Missing ACK/readback
fails without automatically repeating a mutation.

Reader absence requires pausing the last active table, or all tables sharing that
journal. Individual table pause stops its Snowflake loader while a shared reader
continues RAW capture for it. Resume consumes retained receipts from the last loader
position. Removed/unstarted tables and tables with unknown prior capture are not
silently added. Test individual pause separately: reader active, paused table absent
from loader, other tables delivered, new RAW retained, then exhaustive source
reconciliation after resume. That scenario does not attest pod absence.

## Verdict limits

Collectors retain native_cdc_qualified=false. evaluate may qualify only its bounded,
observed scenario: actual copy, SOURCE=MIRROR, complete independent HISTORY oracle,
no duplicates, actual reader removal/recreation, checkpoint progress in the same
receiver, ACK mutation coverage through ROWPOS/HISTORY and per-mutation visibility.
Current images/identities are reread.

rotation_qualified=false and maximum_10s_qualified=false remain explicit:
one bounded scenario is neither receiver rotation proof nor a universal SLO.
Limits are 1,000 rows/events, 1–20 mutations, one receiver and one Ready reader/loader.
Missing data/permissions, partial windows, release drift or an exceeded threshold
produce an incomplete verdict. The CLI creates no DML, migrations, infrastructure
or installations. kubectl and, for fresh admission, gh are prerequisites.

## Abrupt reader crash: DEV only

crash requires a qualified native baseline, conserved admission and explicit:

```json
{"actions": {"enabled": true, "environment": "dev", "crash_reader": true}}
```

Existing kubeconfig access must permit pod/Deployment/ReplicaSet/node reads and
pods/exec on the exact reader. The command grants no access, deletes no pod and
changes no replica count.

The capture image must start the Python reader as a direct child of
/usr/bin/tini -g --. Main-process overrides fail. Guards verify executable,
arguments, owner, PID namespace, process/init start and kernel boot identity
without dumping command lines or environment. Persist crash.action-request.json,
reread the same Pod UID/containerID and require one selected pod, including
unready/terminating pods. Open a pidfd, revalidate the incarnation, then send exactly
one SIGKILL to the Python child. Missing pidfd, races, timeout or unwritable intent
stop without PID fallback or automatic retry.

```sh
quadringent qualification native --config /private/path/site.json \
  --out-dir /private/path/crash-evidence --phase crash
```

Within max_seconds (maximum 120 s), require the same pod/Deployment/ReplicaSet,
release and identity, restartCount + 1, a new containerID and kubelet lastState
bound to the old container: signal 9 or exit 137, termination after intent, not
OOMKilled. A preparation marker or Ready pod is insufficient.

Arrange a genuinely acknowledged source mutation during downtime, then collect
independent journal/ROWPOS and the after snapshot. Supply crash_proof, ack_receipts,
history_oracle, before and after. bind-crash-mutations creates mutation_receipts;
evaluate-crash checks checkpoint progress, exhaustive HISTORY with no duplicate,
prior-event retention and SOURCE/MIRROR parity.

ACK start must follow proven termination and finish before the new container starts,
not merely before Ready observation. With second-resolution kubelet times, the
lower bound is the following second. Short/ambiguous windows remain incomplete.
Loader crash and an exact crash between RAW publication/checkpoint are not covered.
crash_recovery_qualified=true remains scenario-specific; native_cdc_qualified=false,
no rotation and no universal ten-second claim. Modified images require rebuild,
scan and fresh admission before native execution.

## Expired initial-copy Jobs

native.initial_copy_job_archive pins an absolute private path and SHA256.
A successful exact GET returning absence is required; permission failure, timeout
or an inaccessible namespace cannot establish expiry. The original full Job may
be revalidated after observed TTL using its own admission; projections cannot
qualify a native primary.

quadringent-job-api-archive-v1 pins api_document and observation_anchor
(each path/sha256), scope, job_uid, observed_utc_pointer and api_sha256_pointer.
Pointers are bounded lists of string keys or strict integer indexes. The original
anchor binds API bytes and an observation between Job completion and TTL.
Revalidate UID, namespace, admitted image, run, boundary and success; any additional
successful archived epoch fails. retain-history freezes an available Job itself.

v2 adds api_document_pointer into the original wrapper bytes without rewritten
extraction. If UTC is in that same wrapper, observation_anchor must equal
api_document and api_sha256_pointer is null; otherwise a distinct original anchor
binds its SHA. Actual batch/v1, namespace, UID, completion and TTL are mandatory.

The archived-primary fallback additionally requires the current admitted reader,
Deployment/pod UIDs and an absent-GET witness bound to that Deployment UID.
Actual bootstrap must include the exact table and a same-receiver position not
after the original boundary. A different receiver needs proven coverage.
Run identity comes from immutable Job/artifacts, never invented bootstrap fields.
Independent copy/journal/ROWPOS, exhaustive HISTORY, mirror and checkpoint checks
remain mandatory. Compare known epochs and present Jobs. A single archived Job
cannot attest an exhaustive historical LIST: archived_jobs_inventory_complete=false.

quadringent-job-api-projection-archive-v1 is a separate limited historical profile.
It pins original public projection bytes and their old anchor, uses observed_utc
from the export, and only compares captured fields, copy bytes and independent
source oracle against full HISTORY. historical_namespace_verified, ttl_observed,
historical_identity_verified and old_jobs_inventory_complete remain false.
retained_history_data_qualified=true never implies native_cdc_qualified=true.

## Receiver rotation oracle

receiver-catalog retains original as400-receiver-catalog-v1 stdout, its SHA256,
the actual JAR SHA and admitted reader UID/image bindings in
ibmi-source-receiver-catalog. Preserve query order; invent no lexical/timestamp order.

Coverage from an old copy boundary to a new receiver checkpoint requires observed
contiguous bounds, first(next)=last(previous)+1, the exact initial receiver and
last attached receiver, with at most eight receivers. A nine-result sentinel fails.
Read each segment separately under the aggregate 1,000-entry budget. Decoded images
and independent ROWPOS must cover exactly the same positions. Retain/revalidate
RAW/ROWPOS bytes, original catalog and bindings.

bind-rotation-mutations binds original ACKs to independent images in the new receiver
after the actual rotation ACK, including every declared column and explicit NULL.
It verifies SEQOPT(*CONT) and exact business mutation replay on the initial source
state. evaluate-rotation requires one actual rotation ACK, later mutation ACKs,
both catalogs and before/after evidence. Journal identity comes from native
configuration and must match the ACK/catalogs. These phases trigger no rotation/DML.

Rotation qualification remains separate from bounded CDC and universal latency.
Pause/resume/crash still cover a same-receiver window, including after a prior rotation.
Sequence reset, missing bounds, omitted chain or foreign scope fails; a new copy
must never hide loss.

The continuous reader also refuses sequence resets: available sequence bounds
and attach order cannot prove that no intermediate receiver was deleted.
It retains the checkpoint and requires operator review; a new copy establishes
a new boundary and does not recover or certify the missing history. Preserve
the original checkpoint and receiver backups before any recovery decision.
This guard is a source correction after v0.3.5; the original v0.3.5 release
artifacts do not contain it.

## SQL history mode

expected_history_mode: sql declares the required runtime mode; it does not change
the runtime. Snapshots/baselines/observations of another mode fail. Streaming retains
the same checks.

SQL parameterized HISTORY MERGE batches are limited to 100 events, followed by
MIRROR MERGE and the durable checkpoint. No SDK channel/commit token is produced.
Independent source oracle, full HISTORY, SOURCE=MIRROR, zero duplicates, scoped
checkpoints and admitted workloads remain required. Local fault tests cover replay
between history and mirror; reader evaluate-crash never attests an unobserved loader crash.

To switch from Streaming, stop the old writer before starting the single SQL loader.
Retain tables, copy run, raw objects and the scope-bound checkpoint. No history
deletion or new copy is required merely for this mode change. Retained epochs keep
their own mode/admission; an old Streaming baseline is not relabeled SQL.
Collect a fresh SQL baseline and verify workloads/exhaustive equality before latency
measurement. SQL targets small flows and adds warehouse MERGE work per batch;
measure wakeup, billing and latency. Mode choice alone proves no cost saving or SLO.

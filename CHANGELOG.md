# Changelog

## Unreleased

Changes not yet in a published release.

- Add a bounded read-only native format identity probe. Its level ID and raw response are metadata observations; copy integration, historical continuity and native admission remain unavailable.
- Keep delivery counts separate from surviving-row commit ages, including after deletes. Loader logs do not establish delivery latency, which stays unmeasured.
- Add SQL authority scaffolding for future native verification, with context generations, fenced requests and immutable historical results. Strict enforcement is disabled by default; native admission, activation and API/cockpit integration remain unavailable.
- Enforce agent tokens' source restrictions on v2 reads, actions, confirmations and paginated lists. Refuse global operations without a safe source scope before providers or cached responses run. This fix is not included in the 0.4.5 images.
- Add bounded native reconciliation helpers: typed row streams, disk-backed comparisons, and read-only IBM i and Snowflake exports. Native provenance, a stable collection boundary and cockpit integration remain required; these helpers do not establish transaction consistency or runtime qualification.
- Export pinned snapshot/RAW originals and explicit native journal positions for those comparisons. Complete inventory, receiver identity and historical record formats still require admission by the controller.
- Add `quadringent local` (init, snowflake-script, check, start, stop, status, reset) to run one table on a single machine with Docker, without a cloud account or Kubernetes, using a local storage backend (`QUADRINGENT_STORAGE_BACKEND=local`, `local://` registry authority). It needs images from a release that includes it and has not been validated end to end against an IBM i and Snowflake. See [Try it locally](docs/product/try-it-local.md).
- Add `quadringent doctor`, which builds one versioned diagnostic report (`quadringent.diagnostic.v1`) as text or JSON. Local checks always run; network calls (`--check-ibmi`, `--tls`, `--control-plane`) run only on request. Exit codes: 0 ready, 1 blocked, 2 usage error, 3 incomplete. `--from-report` and `--save` read and write a report offline.
- Add a shareable export (`quadringent doctor --export FILE.tar.gz`, `--preview` to list it without writing). It is built from allowed fields only, pseudonymizes hosts, accounts and object names, lists every omission with its reason, and sends nothing. The scrubbing is pattern-based: read the archive before you post it.
- Add read-only `GET /v2/diagnostics` and `GET /v2/diagnostics/export` to the control plane, and a Diagnostics screen in the cockpit. Synthetic or historical evidence is never shown as passed, and absent evidence stays unknown.
- Enforce organization scoping of pipelines, tables and sources in the v2 control plane routes.
- Apply the log-route redaction rules to stage details in the pipeline evidence route.
- Harden cockpit actions: a request is "accepted" until a readback confirms the declared state; a timeout or server error is reported as an unknown result with a read-only check, not a second mutation; slow answers for a previous selection are ignored; a confirmation can no longer run on a different source with the same name.
- The install preflight now reports IBM i TCP reachability only (database, sign-on and command ports and, unless every service port is set, the 449 server mapper). `quadringent doctor --tls` adds a verified TLS handshake. Sign-on is never attempted and stays unknown.
- Make the suggested IBM i journal setup valid (no `THRESHOLD(*NONE)` with system-managed receivers, detached receivers kept) and reuse an existing journal of the library instead of assuming `QSQJRN`.
- Grant `CREATE TABLE` on the mirror schema in every Snowflake destination mode, as the loader requires. A static test checks the generated SQL; it is Not tested on Snowflake.
- Add trust pages: [what Quadringent does on your IBM i](docs/product/ibm-i-footprint.md), [your data](docs/product/your-data.md), [verify a release](docs/product/verify-a-release.md), [governance](docs/governance.md) and [support](SUPPORT.md).
- Add a [FAQ](docs/product/faq.md), a sourced [comparison](docs/product/comparison.md) and three [articles](docs/articles/ibm-i-journals-for-cdc.md) on replicating IBM i data.
- Add an [evidence page](docs/evidence.md) that lists every observed result with its version and scope, and use four evidence words (Measured, Not measured, Not tested, Not supported) across the documentation.
- Prepare PyPI package metadata and a package description. The package is not published on PyPI.
- Add a local synthetic laboratory for the operator journey. It is a test tool, outside the distributed wheel.

The control plane image does not yet include the diagnostics module, so
diagnostics routes answer 503 `capability_unavailable` there.

## 0.4.2 — Native capture and recovery preview

- Bind native capture and copy boundaries to the original admitted receiver identity; refuse automatic legacy checkpoint adoption.
- Add selected native replay export, verification and restore with guarded RAW retention.
- Preserve the installed AWS VM image during upgrades and enforce one organisation at control-plane startup.
- Distinguish suspended table delivery from capture state and distribute the synthetic cockpit demo.

This DEV/INT preview changes checkpoint semantics. Native continuity, reader
outage, latency, full cloud-loss recovery and the operator path require fresh
qualification. Historical 0.3.5 and 0.3.4 observations remain bound to their
original artifacts; see the [native capture guide](docs/product/native-qualification.md).

## 0.3.5 — Reliable cockpit controls and administrator guidance

- Resolve connection destinations from pipeline identities; ambiguous links expose no destination control.
- Keep table tabs keyboard-accessible with consistent focus, Tab/Shift+Tab and manual activation.
- Explain first-administrator setup when the installer receives no admin email.
- Report a missing evidence producer version explicitly and link evaluation guides to the public pilot request.

Local synthetic desktop checks cover these changes. Earlier native CDC results
remain bound to their versions; current-release recovery and latency require
their own qualification.

## 0.3.4 — Deliver tables before optional lag measurements

- Reuse committed copy checkpoints instead of repeating snapshot discovery reads.
- Defer lag queries until all tables in the current pass have been delivered.
- Preserve checkpoint compare-and-swap, receipt validation and event deduplication.

Published packages and three signed images were checked against the same commit
and downloaded anonymously on both supported architectures. Four DEV/INT
upgrades retained identities, keys, volumes and complete typed history. The
first cold-warehouse trial delivered every operation but exceeded the 10 s gate
on three targets. See the [qualification summary](site/qualification-0.3.4.json).

## 0.3.3 — Helm 4 profile preservation

- Support Helm 4's removal of the `helm list --all` flag without hiding other errors.
- Preserve the existing CDC profile when applying the admitted release.

Four DEV/INT upgrades from 0.3.2 and subsequent rollbacks retained original keys,
pipeline identities, PostgreSQL volumes and complete typed history.

## 0.3.2 — Table evidence and searchable documentation

- Dated capture, delivery and reconciliation evidence in the table cockpit.
- Read-only evidence API with explicit absence, stale and simulation states.
- Loader evidence bound to the table and last retained delivered batch.
- HTML installation, operations, security and qualification guides.
- Canonical URLs, sitemap and structured metadata for the public site.

Four DEV/INT installations, typed copies, INSERT/UPDATE/DELETE and real reader
outages with source writes inside the outage were observed without duplicates
on October 12, 2026. Retained loader logs alone do not establish complete
reconciliation or a universal latency guarantee.

## 0.3.1 — Initial public preview

- IBM i to Snowflake capture, initial copy, history and mirror delivery.
- English operator cockpit, activation wizard, CLI and installation guides.
- Digest-pinned runtime images and release admission with CI and signature checks.
- AWS/GCP VM and existing EKS/GKE installation paths.

This preview does not establish a universal ten-second latency bound or production
readiness. Historical qualification measurements and their limits are recorded
in the README; current runtime qualification requires evidence from this release.

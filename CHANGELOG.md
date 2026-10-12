# Changelog

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

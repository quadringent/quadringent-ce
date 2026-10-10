# Changelog

## 0.3.2 — Table evidence and searchable documentation

- Dated capture, delivery and reconciliation evidence in the table cockpit.
- Read-only evidence API with explicit absence, stale and simulation states.
- Loader evidence bound to the table and last retained delivered batch.
- HTML installation, operations, security and qualification guides.
- Canonical URLs, sitemap and structured metadata for the public site.

Runtime qualification for this release remains pending. Retained loader logs
do not establish complete reconciliation or a universal latency guarantee.

## 0.3.1 — Initial public preview

- IBM i to Snowflake capture, initial copy, history and mirror delivery.
- English operator cockpit, activation wizard, CLI and installation guides.
- Digest-pinned runtime images and release admission with CI and signature checks.
- AWS/GCP VM and existing EKS/GKE installation paths.

This preview does not establish a universal ten-second latency bound or production
readiness. Historical qualification measurements and their limits are recorded
in the README; current runtime qualification requires evidence from this release.

# Versions and delivery

project.version in pyproject.toml is the only editable version.
python scripts/sync_version.py derives chart version/appVersion, Java and UI
versions and the npm lockfile. CI requires --check. Source 0.4.8 is an Unreleased
candidate; 0.4.5 remains the latest published prerelease. For this candidate:

```sh
python scripts/sync_version.py --check --release v0.4.8
```

/v1/version reads installed package metadata; images read the same pyproject
copied during build. A release tag must match exactly.

## Compatibility and persistent cursors

Before 1.0, interfaces may evolve with documented changes. Patch releases preserve
the v1 contract; additive consumers must retain null and unknown values. A breaking
field or semantic change requires a minor version before 1.0, then a major version,
with migration/coexistence rather than silently changing v1. This policy does not
qualify untested historical instances.

Reader checkpoint identity includes source + journal + storage backend + bucket +
RAW root. Table membership, pauses and pod rollouts do not change it. Independent
RAW roots on the same journal must retain isolated cursors.

To migrate a legacy journal-only cursor, stop the writer, retain the original
checkpoint and immutable RAW receipts/batches, verify their hashes and continuous
coverage from every copy boundary through receiver transitions, then conditionally
initialize the isolated cursor with CAS at the position actually proven. Refuse
changed or conflicting state; retain old reader, table and loader checkpoints.
Reread CAS success before enabling the reader, then reconcile SOURCE/RAW/HISTORY/MIRROR.

An empty receipt cannot replace a missing nonempty batch. Existing RAW with an empty
isolated cursor fails closed. Never jump to the journal head, reset checkpoints or
recopy to hide a gap. A downgrade to a journal-only reader requires a site-specific,
verified restoration plan; it does not prove cursor isolation.

## Initial public source

Review the exact tree, every intended public ref, commit/tag text and release assets.
Keep original development history private. Export reviewed files with
scripts/export_private.py, excluding .git and caches, then initialize a genuinely
new repository. Rewriting/force-pushing an old repository cannot remove PR refs or
cached commits.

```sh
python scripts/check_publication.py --history-ref HEAD \
  --private-denylist-file /outside/repository/identifiers.sha256
python scripts/export_private.py \
  --private-denylist-file /outside/repository/identifiers.sha256 EXPORT
python scripts/check_public_identity.py --root EXPORT \
  --private-policy /outside/repository/policy.json --history-ref HEAD
```

Private identifier policies and unsalted hashes remain outside Git and artifacts.
The hash file contains one SHA256 hex digest per line. The private identity policy
uses identities containing literal values or explicit lexical/substring rules:
lexical matching permits short values with ASCII alphanumeric boundaries; substring
requires at least four characters. Values are escaped, never interpreted as regex.
These checks complement Gitleaks and do not guarantee universal detection.

After git init in the reviewed export, git add --force --all prevents a global
ignore from hiding source instruction files. Compare names and SHA256 for every
exported file against the staged tree before the first neutral commit. Scan the new
reachable history and all refs; no private branch/tag/PR ref may enter it.

Run Gitleaks over the tree, extracted artifacts and all intended public history.
The identity gate reports rules and opaque file/object IDs, never private source
content. PNG metadata inspection validates framing/CRC and bounded compressed text;
pixels are not interpreted as identity text. Review published images visually.
Generic byte scans still apply; encrypted data, steganography and other encodings
are outside any universal absence claim.

The engine, API and cockpit are Apache-2.0. Preserve LICENSE, NOTICE, licenses/
and THIRD_PARTY_NOTICES.md in distributed artifacts. Dependency licenses remain
their own; see [licensing](licensing.md).

## Canonical CI and manual release

release.yml is manually dispatched on an existing matching tag. It reuses the latest
successful canonical CI for the exact SHA: push on main in the same repository,
with all seven jobs successful. A PR run, skipped job, different commit or older
success followed by failure/in-progress does not qualify. Validation stops before
builds and does not silently launch new CI.

The early Trivy gate scans both hash-locked Python dependency sets using an updated
database. Empty/partial inventories, lock mismatches and fixable HIGH/CRITICAL findings
fail. This early gate does not replace full image/layer scans.

Writing jobs require PUBLICATION_APPROVED=true and the publication-approved
environment. No tag push publishes automatically. If required environment reviewers
are unavailable for a private repository, the scoped private build still requires
prior local review and explicit manual dispatch. Configure supported protections
before subsequent public releases. Reset and reread PUBLICATION_APPROVED=false after
delivery, including failure. Helm v4.1.4 matches the chart render reference.

The workflow builds three local OCI indexes without upload and six exact
amd64/arm64 views. Labels must bind source URL, revision and version to the same
commit/tag. Trivy scans all variants, OCI configurations and every layer, including
base layers and later-deleted entries. Private staging preserves entries separately,
without applying whiteouts or extracting links. Secrets at every severity fail before
registry login. Vulnerability SARIF uses HIGH/CRITICAL with
limit-severities-for-sarif: true; arm64 has a separate fixable HIGH/CRITICAL gate.

Six SPDX SBOMs and three vulnerability SARIF reports may be uploaded. Secret JSON,
raw layers and private staging remain private on the runner; Docker build-record
uploads are disabled. Detection rules do not establish universal secret absence.

scripts/release_oci.py seals hashes before GHCR login, rechecks them before
skopeo copy --all --preserve-digests, and verifies resulting index digests.
Promotion reuses scanned bytes without rebuilding. Cosign keyless signatures bind
the published digests to the exact GitHub workflow identity and tag.

## Temporary evidence and installable assets

oci-scan-receipt contains passed.json with relative paths and SHA256 fingerprints.
oci-admission-metadata contains exactly 21 original files: each component's wrapper
index.json and oci-layout, the actual index, and both architectures' manifests/configs.
No layer is exported. Exact bytes are compared before/after export; architecture,
source, SHA and version labels are checked. Limits are 2 MiB/file and 42 MiB total.

Both Actions artifacts retain one day and are separate from release assets. Fresh,
authenticated admission must occur before expiry. It binds artifact ID, ZIP digest,
run and attempt, then compares original metadata to the sealed scan receipt. It does
not revalidate all layers locally. Wait for expiry or remove temporary private evidence
before making its repository public.

The GitHub draft contains 16 assets: chart tgz, wheel, sdist, release-manifest.json,
images.json, six SBOMs, three SARIF reports, and the synthetic demo archive with its
SHA256 file. The chart is a release archive, not an OCI chart.
The CLI installs from the wheel plus the same release's extracted sdist through
--assets-dir. The wheel alone does not contain chart/Terraform files.

Community images use a fresh ghcr.io/<owner>/quadringent-ce package with no inherited
private versions. Check name availability and package visibility independently:
repository privacy does not repair a preexisting package's visibility. The new
repository's GITHUB_TOKEN creates the package; old package permissions are not reused.

A new GitHub origin requires a new canonical commit, version, CI and signatures.
Old labels/SBOM/signatures remain historical, never renamed as new-origin evidence.
The three images, wheel, sdist and chart must come from the same commit. Verify actual
OIDC identity/source labels, exact digests, signature success and rejection of an
incorrect digest or identity before public release.

## Artifact normalization and dependency notices

After python -m build, normalize the exact sdist with
scripts/normalize_release_archive.py before hashes, signatures or upload.
Use the canonical commit timestamp from git show -s --format=%ct HEAD as --epoch
or SOURCE_DATE_EPOCH. Tar uid/gid become zero, uname/gname empty, and PAX headers are
recreated only when required by Unicode names or size. Gzip has no filename and
uses that epoch. Names, bytes and modes must match before atomic replacement.
Links, devices, absolute/traversal paths and duplicate entries fail while preserving
the original archive. Normalization does not replace content/metadata scans.

Cockpit builds collect full production npm/Vite license texts from installed,
lock-matching packages into assets/third-party-notices.txt; missing notices fail.
The file remains accessible in the cockpit image. Java images include exact
source archives and hashes for the five dependent JARs. Python notices come from
installed distributions, including the verifier's Snowflake extra. JTOpen and
affected Debezium portions retain IPL-1.0; they are not relicensed as Apache-2.0.

## Private runner mode

A dedicated Linux ARM64 runner may execute reviewed private CI/release.
PRIVATE_RUNNER_LABELS is a JSON list including self-hosted, Linux, ARM64 and a
repository-specific label. Pin PRIVATE_APPROVED_SHA and PRIVATE_APPROVED_TAG.

When configured, CI accepts only the approved push/main/SHA/workflow identity;
release accepts only manual dispatch on the approved tag, SHA and version.
Other identities skip before allocation, with no paid hosted fallback. needs and
success conditions remain enforced; skipped jobs cannot satisfy admission.

Prefer isolated Docker-in-Docker without the host socket or HOME sharing.
A privileged DinD container shares the Docker Desktop VM kernel; a dedicated Linux
VM improves isolation. Labels are not a security boundary. Independently enforce
the reviewed identity before steps, test rejection on the actual runner, and use
server workflow restrictions where available. Remove the runner before public access.

Check current Actions/Packages quotas and local disk before launching. Change pins
only after reviewing the new source; never to admit an old commit's evidence.

## Pages and consumption proof

Pages is manual and requires canonical CI plus publication gates. During private
review, serve site/ locally: Pages may expose a site even while its source is private.

Before public visibility, audit source/history, build logs, metadata and extracted
wheel/sdist/chart/SBOM, release body and asset names. Validate remotely in the new
private repository. Consumer checks use anonymously downloaded public bytes in two
fresh Python environments and compare modules byte-for-byte to the canonical commit.

Installation/control plane exercises remain separate from IBM i → Snowflake delivery.
Do not claim a universal ten-second bound until measured cold-start and runtime
scenarios support it. See [native qualification](product/native-qualification.md).

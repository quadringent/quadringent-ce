# Governance and continuity

Quadringent is maintained under a pseudonymous project identity.

This page lists what you can verify without knowing who the maintainers are,
how decisions and releases are made, and what remains available to you if the
project stops being maintained. It makes no promise about the future of the
maintainers or of the project.

## What you can verify instead

| What | Where | How |
|---|---|---|
| The container images come from this repository's release workflow | Cosign keyless signatures on the three image digests | [Verify a release](product/verify-a-release.md) |
| What is inside the images | SPDX SBOMs and vulnerability reports attached to each release | [Verify a release](product/verify-a-release.md) |
| Why a design choice was made | Decision records in [`docs/decisions/`](decisions/) | Read them; each states its date, status, measurements and limits |
| Who must approve a release, and what is checked first | [Versions and delivery](releasing.md) | Read the workflow in `.github/workflows/release.yml` next to it |
| What was observed, when, and on which version | Dated JSON files in [`site/`](../site/) (`qualification-0.3.4.json`, `recovery-0.3.4.json`, `runtime-0.3.5.json`) | Read the scope and limitation fields; the README summarizes them |
| What the code does | The source, and the tests in `tests/` | `python -m pytest -q`; see [CONTRIBUTING](../CONTRIBUTING.md) |
| What runs on your IBM i and where data goes | [IBM i footprint](product/ibm-i-footprint.md) and [your data](product/your-data.md) | Read them next to the source they cite |

<!-- source: docs/decisions/2026-09-23-attached-receiver-tail.md -->
<!-- source: docs/decisions/2026-09-23-snowflake-mirror.md -->
<!-- source: site/runtime-0.3.5.json -->
<!-- source: CONTRIBUTING.md -->

Limits of these checks:

- The qualification files are maintainer observations on synthetic data. Their
  underlying evidence is retained privately. They are not external
  certification, and none of them claims production qualification.
- The signatures show which workflow built an image. They do not show that the
  code is correct. They do not cover the wheel, the sdist, the chart archive or
  the SBOMs; see [what is not signed today](product/verify-a-release.md#what-is-not-signed-today).
- Repository settings, such as who may approve a publication environment, are
  not visible from the source tree. The release workflow requires that
  approval; this page cannot show you who holds it.

## How decisions are made

<!-- source: CONTRIBUTING.md -->
<!-- source: docs/decisions/2026-09-23-attached-receiver-tail.md -->

This repository does not define voting, a steering group or a maintainer
roster. What it does define:

- **Changes** are proposed as pull requests that describe the problem, the
  resulting behavior, the checks run and their limits. Behavior changes start
  with a test (CONTRIBUTING).
- **Decision records** in `docs/decisions/` are written when a design choice
  rests on a measurement. Each states a date and status, the measurements, the
  decision, and the limits of the measurement. The two records published so far
  are dated 2026-09-23.
- **Contributions** use Apache-2.0, without a contributor license agreement or
  transfer of copyright (CONTRIBUTING).
- **Compatibility** rules before 1.0 are in [releasing](releasing.md): patch
  releases preserve the v1 contract, and a breaking change requires a minor
  version.

## How releases are made

<!-- source: docs/releasing.md -->
<!-- source: .github/workflows/release.yml -->

[Versions and delivery](releasing.md) is the reference. In short:

1. `project.version` in `pyproject.toml` is the only editable version, and a
   release tag must match it exactly.
2. The release workflow is started by hand on an existing tag. A tag push does
   not publish anything.
3. It reuses the latest successful CI run for the exact commit, with all seven
   jobs successful; a run for a different commit, a skipped job or an older
   success followed by a failure does not count.
4. It scans the locked Python dependencies and every image, builds the images
   once, and promotes the scanned bytes without rebuilding.
5. Jobs that write require `PUBLICATION_APPROVED=true` and the
   `publication-approved` environment. The GitHub release is created as a draft.
6. The images are signed by digest, keyless.

No tag, signature or release is a statement that the software is qualified for
production. See [Verify a release](product/verify-a-release.md) for the checks to
run yourself.

## Continuity

<!-- source: LICENSE -->
<!-- source: docs/licensing.md -->
<!-- source: docs/releasing.md -->
<!-- source: .github/workflows/release.yml -->
<!-- source: docker/Dockerfile -->
<!-- source: docs/product/install-default.md -->

### If the project goes quiet tomorrow

1. Copy the three images you run, by digest, to a registry you control, and
   keep your own copies of the release files you use.
2. Pin the digests of those images and the version you run. A pinned
   installation does not change when the upstream repository does.
3. Run the signature check in [Verify a release](product/verify-a-release.md)
   on those digests now, and keep the output. The release workflow at every tag
   from v0.3.1 to v0.4.1 contains the signing step; whether each of those tags
   was published as a release is not shown by the repository.

The detailed checklist is [below](#if-you-want-to-fork-and-keep-a-version-running).

### License and build definitions

The engine, API and cockpit are licensed under Apache-2.0 ([LICENSE](../LICENSE)).
Rights granted on published versions stay in place. You may maintain a fork or
offer services around it without paying royalties or publishing your changes,
subject to the license and to third-party obligations
([licensing](licensing.md), [third-party notices](../THIRD_PARTY_NOTICES.md)).
Apache-2.0 grants no Quadringent trademark rights.

The build definitions are in the repository:

- the Python package builds with `python -m build` (setuptools, from
  `pyproject.toml`);
- the chart builds with `helm package chart`;
- the three images build from `docker/Dockerfile`, `docker/control-plane.Dockerfile`
  and `docker/verifier.Dockerfile`, whose base images are pinned by digest;
- the release process is the open workflow `.github/workflows/release.yml`, with
  its helper scripts in `scripts/`;
- the tests run without contacting IBM i, AWS or Snowflake (CONTRIBUTING).

No proprietary build service is required by these files. The workflow does run
on GitHub Actions, publishes to the GitHub Container Registry and signs through
the Sigstore public infrastructure. The build inputs are fetched from public
registries and package indexes, such as the base images named in the
Dockerfiles. Building elsewhere than GitHub Actions is Not tested.

### If you want to fork and keep a version running

1. **Keep the artifacts you rely on.** Download the release files you use and
   keep your own copies. Copy the three images, by digest, to a registry you
   control; the installer accepts a mirror that preserves the same digests
   (`--image-repository`, see [installation](product/install-default.md)). Whether
   cosign signatures remain verifiable on a mirror is Not tested.
2. **Pin versions.** The installer and the chart require immutable `sha256:`
   digests, never a moving tag. A pinned installation does not change when the
   upstream repository does.
3. **Fork the repository.** Clone it, or fork it on GitHub. The release workflow
   derives the image path from the repository owner
   (`ghcr.io/<owner>/quadringent-ce`) and signs with the identity of the
   repository that runs it. Releases from your fork are therefore verified with
   your repository's workflow identity, not the upstream one.
4. **Run the checks.** `python -m pytest -q` and the other commands in
   [CONTRIBUTING](../CONTRIBUTING.md) run locally.
5. **Set your own release gate.** The workflow expects `PUBLICATION_APPROVED`
   and the `publication-approved` environment to be configured in the
   repository that runs it. Read [releasing](releasing.md) before your first
   release.
6. **Name your fork differently.** The license gives no right to the project's
   name or marks.

If you use the project's current published versions, note the support
position in [SUPPORT](../SUPPORT.md): security fixes are for the latest minor
release line only.

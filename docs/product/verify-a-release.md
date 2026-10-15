# Verify a release

You can check, on your own machine, that the container images of a Quadringent
release were built and signed by this repository's release workflow. The checks
below also cover what the published reports contain, and the release files that
are not signed today. None of them relies on knowing who the maintainers are.
They do rely on public services (GitHub, the GitHub Container Registry and the
Sigstore public infrastructure), and a passing check has limits; see
[what a passing check does not tell you](#what-a-passing-check-does-not-tell-you).
How releases are made is in [releasing](../releasing.md) and
[governance and continuity](../governance.md).

The tests in `tests/test_release_verification_doc.py` read
`.github/workflows/release.yml` and fail if these items drift from what is
written here: the image path, tags and OCI labels; the three signing commands and their keyless form; the
trigger, the approval environment and condition of the three publishing jobs, and
the version and CI checks of the validate job; the uploaded file patterns; the
Trivy gate on the amd64 view; and the table of unsigned files. Other statements
on this page are not covered by those tests, and the tests do not run `cosign`
against a published release.

## Before you start

<!-- source: .github/workflows/release.yml (release job, draft: true) -->
<!-- source: docs/product/install-default.md (Release assets) -->

You need `cosign`, `jq` and `git`. Steps 5 and 7 also use `helm`, `kubectl` and
`sha256sum` (`shasum -a 256` on macOS). Install them from their own projects;
we do not pin versions of these tools, and which versions work is Not tested.

Releases are listed on the repository's
[Releases page](https://github.com/quadringent/quadringent-ce/releases). The
workflow creates each release as a draft, and a draft is not an available
release until a maintainer publishes it. The source tree can be ahead of the
latest published release, and the files under `site/` name specific versions
(see step 6). Always check the version of the release you are using against the
version in the receipt you are reading.

## What a release contains

<!-- source: .github/workflows/release.yml (jobs images, chart, release) -->
<!-- source: scripts/write_release_manifest.py -->

The release workflow uploads these files to a GitHub release, which it creates
as a draft. `<tag>` is the release tag, for example `v0.4.2`; `<version>` is the
same number without the leading `v`.

| File | What it is |
|---|---|
| `images.json` | The three image references, each as `repository@sha256:digest`. |
| `release-manifest.json` | The same digests, in the form read by `quadringent install --release-manifest`. |
| `quadringent-<version>-py3-none-any.whl` | The Python wheel. |
| `quadringent-<version>.tar.gz` | The Python source distribution (sdist). It carries `chart/` and `deploy/terraform/`. |
| `quadringent-<version>.tgz` | The Helm chart archive (not an OCI chart). |
| `quadringent-<tag>-<component>.spdx.json` | SPDX SBOM of the amd64 view of an image. `<component>` is `capture`, `control-plane` or `verifier`. |
| `quadringent-<tag>-<component>-arm64.spdx.json` | SPDX SBOM of the arm64 view of the same image. |
| `trivy-<component>.sarif` | Vulnerability report (SARIF) for the amd64 view of an image. |
| `quadringent-demo-<version>.tar.gz` and `.tar.gz.sha256` | The synthetic demo archive and its checksum. |

The wheel and sdist file names follow the Python packaging convention for
project `quadringent`; the workflow takes them from `python -m build`. Check the
asset list of the release you are about to use: we describe the
workflow, and an older release may have been produced by an earlier version of
it.

The images are published to `ghcr.io/quadringent/quadringent-ce`. The workflow
tags each image with the release tag and a component suffix:
`<tag>-capture`, `<tag>-control-plane` and `<tag>-verifier`. Registry tags can be
moved; the digests in `images.json` are what you verify and deploy.

## 1. Get the image digests

Download `images.json` and `release-manifest.json` from the release. Read the
digests:

```sh
jq -r '.images | to_entries[] | "\(.key)  \(.value)"' images.json
```

Check that `release-manifest.json` agrees: its `imageDigest` is the capture
image, `controlPlaneImageDigest` the control plane image, and `verifierImageDigest`
(repeated as `observabilityImageDigest`) the verifier image.

```sh
jq . release-manifest.json
```

## 2. Verify each image signature

<!-- source: .github/workflows/release.yml (images job: permissions id-token, step "Sign the published digests") -->

The release workflow signs the three image digests with `cosign sign --yes`, with
no private key: this is keyless signing, using the workflow's GitHub OIDC
identity. The signature therefore binds a digest to the workflow file, the
repository and the tag that produced it.

The identity to expect is the release workflow of this repository, running on a
version tag. The OIDC issuer is GitHub Actions.

```sh
for ref in $(jq -r '.images[]' images.json); do
  cosign verify \
    --certificate-identity-regexp '^https://github\.com/quadringent/quadringent-ce/\.github/workflows/release\.yml@refs/tags/v[0-9]+\.[0-9]+\.[0-9]+$' \
    --certificate-oidc-issuer https://token.actions.githubusercontent.com \
    "$ref" > /dev/null && echo "signature ok  $ref" || echo "SIGNATURE FAILED  $ref"
done
```

For a stricter check of one release, replace the regular expression with
`--certificate-identity` and the exact value
`https://github.com/quadringent/quadringent-ce/.github/workflows/release.yml@refs/tags/<tag>`.
A prerelease tag such as `v0.4.0-rc1` does not match the expression above; use
the exact identity for it.

Also run a negative check once, to see that your setup really rejects what it
should: repeat one command with a wrong repository name in the identity, or with
a digest you changed by one character. Both must fail.

What a successful check shows: the digest was signed during a run of
`release.yml` in `quadringent/quadringent-ce` on a tag. Only the index digest of
each multi-architecture image is signed.

## 3. Check that the image comes from the tag you expect

<!-- source: .github/workflows/release.yml (build steps, labels) -->

The images are built with these OCI labels: `org.opencontainers.image.source`
(the repository URL), `org.opencontainers.image.version` and
`org.opencontainers.image.revision` (the commit). Read them with your registry
tooling and compare the revision with the commit the tag points to:

```sh
git clone https://github.com/quadringent/quadringent-ce.git
git -C quadringent-ce rev-parse '<tag>^{commit}'
```

The release workflow refuses to run unless the tag matches the project version
(`scripts/sync_version.py --check --release`) and CI has passed for that exact
commit (`scripts/check_release_ci.py`). See [releasing](../releasing.md).

You can also rebuild an image from the tag with the Dockerfiles in `docker/` and
compare the contents. Whether a local rebuild gives a byte-identical digest is
Not tested; expect to compare contents, not digests.

## 4. Read the SBOMs and vulnerability reports

<!-- source: .github/workflows/release.yml (SBOM SPDX and Scan Trivy steps) -->

List the packages recorded in an SBOM:

```sh
jq -r '.packages[] | "\(.name) \(.versionInfo)"' quadringent-<tag>-capture.spdx.json | sort | less
```

The SBOMs are generated by Trivy from the same local image views that were
scanned, before the images were pushed. They list what Trivy detects in the
image. They are not attached to the images in the registry and are not signed.

The SARIF reports come from Trivy scans of the amd64 view with severity
HIGH and CRITICAL and `ignore-unfixed` set. A finding in that scan fails the
workflow, and the release job only runs when the image job succeeded. So a
published report should contain no fixable HIGH or CRITICAL vulnerability as of
scan time. What this does not cover:

- vulnerabilities disclosed after the scan date;
- vulnerabilities without an upstream fix, and MEDIUM and LOW severities;
- the arm64 vulnerability scan and the secret scans, which run in the workflow
  but whose reports stay on the runner and are not published.

To get a current view, scan the digest yourself with a scanner you trust, for
example `trivy image <repository>@<digest>`.

## 5. Check that the chart and installer pin those digests

<!-- source: chart/templates/_helpers.tpl -->
<!-- source: chart/templates/control-plane.yaml -->
<!-- source: chart/values.yaml -->
<!-- source: src/quadringent/installer/plan.py -->
<!-- source: src/quadringent/installer/manifest.py -->
<!-- source: src/quadringent/installer/apply.py -->
<!-- source: src/quadringent/installer/cli.py -->

In `chart/values.yaml`, `image.digest` and `controlPlane.image.digest` are empty
and the chart refuses to render without a `sha256:` digest for each. The chart
also ships non-empty default digests for `verification.imageDigest` and
`observability.imageDigest`. These defaults are not tied to a release: they can
differ from the verifier digest in `images.json` (the default
`verification.imageDigest` differs from the `verifier_image_digest` recorded in
`runtime-0.3.5.json`). If you install with Helm directly, set every digest
yourself from `images.json`.

`quadringent install` reads the digests from `release-manifest.json` and rejects
anything that is not `sha256:` followed by 64 hexadecimal characters. It writes
the capture, control plane and `verification.imageDigest` digests into the Helm
values. It does not override `observability.imageDigest`, so the chart default
applies to that image unless you set it.

The installer does not print the generated values, and it does not stop for
confirmation between writing them and running `helm upgrade --install`; its only
prompts are for Terraform applies. So check the digests before you run it:
compare `release-manifest.json` (the only source of the digests the installer
reads) with `images.json`, as in step 1. `--dry-run` writes no file and prints
only the planned steps.

After an install, the generated values are in
`chart-values.generated.yaml` in the installer's working directory, which is
`~/.quadringent/<name>/` unless you changed it. Compare what is deployed with
`images.json`:

```sh
helm get values <release> -n <namespace> | grep -i digest
kubectl get pods -n <namespace> -o jsonpath='{range .items[*].spec.containers[*]}{.image}{"\n"}{end}'
```

The bundled PostgreSQL image is a separate third-party image pinned by digest in
`chart/values.yaml`. It is not built or signed by this release workflow. The
observability image digest is separate from the three digests above; check the
value your chart renders.

## 6. Check the qualification scope

<!-- source: site/runtime-0.3.5.json -->
<!-- source: site/recovery-0.3.4.json -->
<!-- source: site/qualification-0.3.4.json -->

Dated observations are kept as JSON files under `site/`. The historical
`qualification-0.3.4.json` and `recovery-0.3.4.json` name 0.3.4, and
[`runtime-0.3.5.json`](../../site/runtime-0.3.5.json) names 0.3.5.
[`qualification-0.4.5.json`](../../site/qualification-0.4.5.json) names 0.4.5
and `artifact_source_sha` `ad30f16ae9346bd332a482dc1d6de84f2b3e75f1`, with
observations frozen at `2026-10-15T18:08:54.157733+00:00`: initial
one-million-row equality on AWS VM and scoped reader recovery. It is partial:
24-hour continuity, receiver rotation and the final independent oracle remain
unproven, and the 10 s observation target has not passed. It establishes no
P95 or SLA. The current source tree also has Unreleased changes; this receipt
does not qualify the new doctor or local-mode helpers. Match the version,
artifact source and scope, rather than applying a receipt to every current
source file. `release.yml` does not upload a qualification file. If a release
lists one among its assets, it was added outside this workflow: compare it
with the copy in the repository.

Fields differ between receipts. In `runtime-0.3.5.json`, check:

- `version` and `source_commit`: the receipt applies to that version only.
- `artifacts`: image digests observed. Compare them with `images.json`. A
  difference means the receipt is about other bytes.
- `qualification_scope` and `limitations`: what was and was not run.
- `production_qualified`, `external_pilot_validated` and
  `universal_latency_guarantee`: in `runtime-0.3.5.json` all three are `false`.
- `provenance`: the observations were made by the maintainers on synthetic data.
  The underlying evidence is retained privately, so you cannot reproduce it from
  the receipt. It is not an external certification.

What has been measured, and on which release, is on the [evidence page](../evidence.md);
see also [releasing](../releasing.md) and [native qualification](native-qualification.md).

## 7. Check the synthetic demo archive

<!-- source: .github/workflows/release.yml (chart job, demo steps) -->
<!-- source: scripts/verify_demo_release.py -->

The workflow publishes `quadringent-demo-<version>.tar.gz` with a
`.tar.gz.sha256` file whose content is `<sha256>  <file name>`. After
downloading both into one folder:

```sh
sha256sum -c quadringent-demo-<version>.tar.gz.sha256
```

(On macOS: `shasum -a 256 -c quadringent-demo-<version>.tar.gz.sha256`.)

The archive carries `PROVENANCE.json` with the version and source commit. The
checksum file comes from the same release as the archive, so it detects
corruption and mismatched files, not a malicious replacement of both. The demo
archive and its checksum are not signed.

## What is not signed today

<!-- source: .github/workflows/release.yml -->

| File | Signature | Checksum file |
|---|---|---|
| The three container images | Yes, keyless cosign, by digest | Not needed: the digest is the checksum |
| Wheel (`.whl`) | No | No |
| Sdist (`.tar.gz`) | No | No |
| Helm chart archive (`.tgz`) | No | No |
| `images.json`, `release-manifest.json` | No | No |
| SBOMs and SARIF reports | No | No |
| Synthetic demo archive | No | Yes (`.sha256`, unsigned) |

The workflow does not produce a GitHub build-provenance attestation and
disables Docker build provenance (`provenance: false`). It does not sign
SBOMs or attach them to the images.

This means that the wheel, sdist, chart and the two JSON manifests could be
replaced on the release page without any signature failing. What you can do
meanwhile:

1. Anchor on the signed digests. The images are what runs the capture, the
   control plane and the verifier. Install them only by the digests that you
   verified in step 2. If `release-manifest.json` lists any other digest, stop.
2. Compare the Python and chart contents with the tag. Clone the repository,
   check out the tag, and compare the extracted sdist and the wheel's
   `quadringent` package with the files under `src/` byte for byte. This is the
   comparison described in [releasing](../releasing.md) for consumer checks.
   For the chart, run `helm package chart` from the checkout and compare the
   extracted files with the downloaded archive.
3. Or build from the tag yourself. `python -m build` and `helm package chart`
   work from a checkout, and the installer can run from a source checkout
   ([install guide](install-default.md)). Whether your build is byte-identical to
   the published files is Not tested.
4. Record a hash when you first download. Run `sha256sum` on every file you
   fetch and keep the result in your own records. It does not prove the file was
   genuine, but it lets you detect a later change.

## What a passing check does not tell you

- It does not show that the code is free of defects, or that it is safe for your
  IBM i. Read [what Quadringent does on your IBM i](ibm-i-footprint.md) and the
  source.
- It does not show the release is qualified for production; it is not (step 6).
- It does not protect against a compromise of GitHub, of the Sigstore public
  infrastructure or of the release workflow itself. Anyone who can run the release
  workflow of that repository on a tag can produce signatures with the expected
  identity. The rules for that approval are in [releasing](../releasing.md) and
  [governance](../governance.md).

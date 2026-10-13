# Roadmap

<!-- source: README.md, CHANGELOG.md, docs/product/compatibility.md, docs/product/verify-a-release.md, docs/pypi.md, pyproject.toml, docker/control-plane.Dockerfile, tests/test_v2_diagnostics.py -->

This page lists what the maintainers intend to work on, in the order they
currently think it matters. It is a statement of direction, not a commitment.

- There are **no dates**. Nothing here is scheduled and nothing is promised for
  a release.
- Items move, change or are dropped when the evidence changes.
- Quadringent Community is a DEV/INT preview. No item on this page, finished or
  not, is a production qualification. See [Verify a release](docs/product/verify-a-release.md)
  for what a release does and does not show.

## How priorities are chosen

Priority follows evidence from people who run the software:

1. **Compatibility reports.** A report that something worked or failed on a
   given IBM i release, journal setup or table shape moves that row of the
   [compatibility page](docs/product/compatibility.md) from "Not tested" toward
   a tested or documented state. Use the
   [compatibility report form](https://github.com/quadringent/quadringent-ce/issues/new?template=compatibility_report.yml).
2. **Pilot feedback.** What blocks a scoped DEV/INT evaluation, as described in
   the [README](README.md), ranks above what has not been asked for.
3. **Defects in what already exists**, especially anything that would show a
   state as confirmed when the evidence does not support it.

Items nobody has reported on stay where they are.

## Now

Work that is in progress or being prepared at the time of writing.

- **Local mode.** A single-machine mode is in progress. It is not released and no date is given. Current install path: [Install on AWS or GCP](docs/product/install-default.md).
- **Fresh qualification of the 0.4 line.** Native continuity, complete site recovery and the operator path need their own evidence. See the Status section of the [README](README.md).
- **Publish the package on PyPI.** Package metadata is prepared ([PyPI page text](docs/pypi.md)); the package is not published there today.
- **Compatibility reports.** Collect reports for IBM i releases and setups other than the one tested. See the [compatibility page](docs/product/compatibility.md).

## Next

Gaps the documentation and changelog already name as "Not tested", "Not measured" or unsigned.

- **IBM i 7.4 and 7.6.** Replication tests on releases other than 7.5. See [compatibility](docs/product/compatibility.md).
- **Other column types.** SMALLINT, BIGINT, TIME, GRAPHIC, BINARY, LOB, DECFLOAT and XML. See [FAQ](docs/product/faq.md).
- **Commitment control.** Behavior with writes under commitment control. See [compatibility](docs/product/compatibility.md).
- **Journal receiver rotation.** Qualification of `CHGJRN` while capturing. See [native qualification](docs/product/native-qualification.md).
- **Structural changes to a table.** Detection and handling of `RGZPFM`, `CLRPFM` and `ALTER TABLE`, which today need a manual resynchronization. See [compatibility](docs/product/compatibility.md).
- **Signatures beyond the images.** The wheel, sdist, chart archive, manifests and SBOMs are not signed today. See [what is not signed today](docs/product/verify-a-release.md#what-is-not-signed-today).
- **Diagnostics in the control plane image.** The image does not yet include the diagnostics module, so the control plane answers 503 `capability_unavailable` for diagnostics. See the Unreleased notes in the [changelog](CHANGELOG.md) and [the control plane Dockerfile](docker/control-plane.Dockerfile).
- **Narrower Snowflake roles.** Provisioning with split administrator roles instead of one custom role. See [compatibility](docs/product/compatibility.md).

## Later

Possible directions with no work started. They depend on reports and pilot
feedback.

- **Latency and volume measurements** on larger tables and sustained write rates. No latency bound is claimed today. See the [FAQ](docs/product/faq.md).
- **A destination other than Snowflake.** No other loader exists. See the [FAQ](docs/product/faq.md).
- **Clouds other than AWS and GCP.** No Terraform modules exist for them. See the [FAQ](docs/product/faq.md).
- **Provenance attestations for releases.** The release workflow produces none today. See [Verify a release](docs/product/verify-a-release.md).

## What this page does not say

It does not say that any item will be done, that the software is ready for
production, or that a gap is acceptable to leave in your environment. To
suggest a change of order, open a
[question](https://github.com/quadringent/quadringent-ce/issues/new?template=question.yml)
or send a compatibility report. See also [CONTRIBUTING](CONTRIBUTING.md) and
[Support](SUPPORT.md).

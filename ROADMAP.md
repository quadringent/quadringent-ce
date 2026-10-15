# Roadmap

<!-- source: README.md, CHANGELOG.md, docs/product/compatibility.md, docs/product/verify-a-release.md, docs/pypi.md, pyproject.toml, docker/control-plane.Dockerfile, tests/test_v2_diagnostics.py -->

This is what we intend to work on, in the order we currently think it matters. It
is a statement of direction, not a commitment.

- There are no dates. Nothing here is scheduled and nothing is promised for
  a release.
- Items move, change or are dropped when the evidence changes.
- The [evidence page](docs/evidence.md) lists what was observed, and
  [Verify a release](docs/product/verify-a-release.md) says what a release does
  and does not show.

## How priorities are chosen

Priority follows evidence from people who run the software:

1. Compatibility reports. A report that something worked or failed on a
   given IBM i release, journal setup or table shape moves that row of the
   [compatibility page](docs/product/compatibility.md) from "Not tested" toward
   a tested or documented state. Use the
   [compatibility report form](https://github.com/quadringent/quadringent-ce/issues/new?template=compatibility_report.yml).
2. Pilot feedback. What blocks a scoped evaluation, as described in
   the [README](README.md), ranks above what has not been asked for.
3. Defects in what already exists, especially anything that would show a
   state as confirmed when the evidence does not support it.

Items nobody has reported on stay where they are. To suggest a change of order,
open a [question](https://github.com/quadringent/quadringent-ce/issues/new?template=question.yml)
or send a compatibility report. [CONTRIBUTING](CONTRIBUTING.md) and
[Support](SUPPORT.md) say how to help and where to ask.

## Now

Work in progress or being prepared at the time of writing.

- Local mode is in progress: `quadringent local` runs one table on a single machine with Docker ([Try it locally](docs/product/try-it-local.md)). It is not released and has not been validated end to end against an IBM i and Snowflake; no date is given. Until then the install path is [Install on AWS or GCP](docs/product/install-default.md).
- The 0.4 line is next to qualify. Native continuity, complete site recovery and the operator path each need their own evidence ([evidence](docs/evidence.md)).
- Package metadata for PyPI is prepared ([PyPI page text](docs/pypi.md)), but the package is not published there today.
- We are collecting compatibility reports for IBM i releases and setups other than the one tested ([compatibility page](docs/product/compatibility.md)).

## Next

Gaps that the documentation and changelog already mark as "Not tested", "Not measured" or unsigned.

- Replication has only been tested on IBM i 7.5. Releases 7.4 and 7.6 come first ([compatibility](docs/product/compatibility.md)).
- SMALLINT, BIGINT, TIME, GRAPHIC, BINARY, LOB, DECFLOAT and XML columns have no test yet ([FAQ](docs/product/faq.md)).
- Writes under commitment control have not been tested ([compatibility](docs/product/compatibility.md)).
- Qualification of `CHGJRN` while capturing, that is journal receiver rotation, is still to do ([native qualification](docs/product/native-qualification.md)).
- `RGZPFM`, `CLRPFM` and `ALTER TABLE` are neither detected nor handled today; each needs a manual resynchronization ([compatibility](docs/product/compatibility.md)).
- The wheel, sdist, chart archive, manifests and SBOMs are not signed today, only the images are ([what is not signed today](docs/product/verify-a-release.md#what-is-not-signed-today)).
- The control plane image does not yet include the diagnostics module, so the control plane answers 503 `capability_unavailable` for diagnostics (Unreleased notes in the [changelog](CHANGELOG.md) and [the control plane Dockerfile](docker/control-plane.Dockerfile)).
- Provisioning still uses one custom Snowflake role. Split administrator roles would narrow it ([compatibility](docs/product/compatibility.md)).

## Later

Possible directions with no work started. They depend on reports and pilot feedback.

- Latency on larger tables and at sustained write rates is Not measured. Measuring it would be the first step ([FAQ](docs/product/faq.md)).
- No loader exists for a destination other than Snowflake ([FAQ](docs/product/faq.md)).
- No Terraform modules exist for clouds other than AWS and GCP ([FAQ](docs/product/faq.md)).
- The release workflow produces no provenance attestations today ([Verify a release](docs/product/verify-a-release.md)).

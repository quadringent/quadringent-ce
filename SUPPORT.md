# Support

Quadringent Community is an open-source (Apache-2.0) DEV/INT preview and no
production qualification is claimed ([evidence](docs/evidence.md)). We answer on
GitHub, and no paid support is announced.

## Where to go

| You have | Use |
|---|---|
| A bug | [GitHub issues](https://github.com/quadringent/quadringent-ce/issues/new?template=bug_report.yml) (bug report form) |
| A result on your IBM i release, journal setup or Snowflake edition, working or not | The [compatibility report form](https://github.com/quadringent/quadringent-ce/issues/new?template=compatibility_report.yml) |
| A question about behavior or setup | Read the [FAQ](docs/product/faq.md) first, then the [question form](https://github.com/quadringent/quadringent-ce/issues/new?template=question.yml) |
| A security vulnerability | The private channel in [SECURITY.md](SECURITY.md). Never in a public issue. |
| A wish to discuss a pilot | Email [quadringent@proton.me](mailto:quadringent@proton.me?subject=DEV%2FINT%20pilot), or the public pilot interest request described in the [README](README.md) |

<!-- source: .github/ISSUE_TEMPLATE/config.yml -->
<!-- source: .github/ISSUE_TEMPLATE/bug_report.yml -->
<!-- source: .github/ISSUE_TEMPLATE/compatibility_report.yml -->
<!-- source: .github/ISSUE_TEMPLATE/question.yml -->
<!-- source: SECURITY.md -->
<!-- source: README.md -->

Issues and their authors are public. Do not post credentials, access details,
private hostnames, customer data, raw business rows or vulnerability details.

## What to expect

<!-- source: SECURITY.md (Reporting a vulnerability) -->
<!-- source: README.md (Discuss a DEV/INT pilot) -->
<!-- source: docs/licensing.md -->

- Security reports are acknowledged within 5 business days.
- Security fixes are made for the latest minor release line only.
- Issues and questions are answered on a best-effort basis. There is no
  service level agreement and no response-time commitment.
- Pilot discussions go through the project contact above. We never accept a
  request automatically, and there is no response-time commitment.
- Paid support is not announced and there is no price or SLA. The
  [project model](docs/licensing.md) says the same.

## Supported versions

Security fixes apply to the latest minor release line (for example, 0.4.x when
0.4 is the highest minor line published). Find it on the
[Releases](https://github.com/quadringent/quadringent-ce/releases) page.
Older lines get no security fix; upgrade to the latest minor line.

Dated qualification files describe only the version they name. See
[Verify a release](docs/product/verify-a-release.md).

## What to include in a report

<!-- source: src/quadringent/installer/doctor.py -->
<!-- source: src/quadringent/diagnostics/export.py -->

The bug report form asks for the Quadringent version and the installation target
(AWS VM, EKS, GCP VM, GKE or a local lab), the steps with expected and observed
results, and sanitized evidence with the time of the observation. Add the IBM i
version and whether the tables are journaled, if relevant, and a synthetic
reproduction when you can, rather than your own tables.

To share a diagnostic without secrets, use the doctor export:

```sh
quadringent doctor --export diagnostic.tar.gz --preview   # show what would be archived, write nothing
quadringent doctor --export diagnostic.tar.gz             # write the archive
```

Add the options your case needs, for example `--cloud aws`, `--check-ibmi <host>`
or `--control-plane`; see `quadringent doctor --help`. The export is built only
from an allowed list of fields. Hosts, accounts and object names are replaced by
pseudonyms, secret-shaped values are removed, and every omission is listed with
its reason. Nothing is sent anywhere; you decide what to attach. The scrubbing
is pattern-based, so open the archive and read it before you post it. Do not use
`--keep-identifiers` for a public issue: it keeps host, account and object names.

Do not attach logs, values files, certificates or dumps.

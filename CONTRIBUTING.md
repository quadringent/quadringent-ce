# Contributing

Describe the expected behavior and the evidence needed to verify it. For a bug,
provide a synthetic scenario without secrets or customer data. Contributions
use Apache-2.0, like the rest of the project. Contributors retain their
copyright; no separate CLA or transfer of rights is required. Submit only code
you are allowed to distribute under this license and retain third-party notices.

## Ways to help

You do not need to write code to help.

- **Send a compatibility report.** The [compatibility page](docs/product/compatibility.md)
  marks many rows "Not tested": IBM i releases, column types, CCSIDs, commitment
  control. A report on one of them, even a short one that says what worked and
  what failed, is the most useful contribution. Use the
  [compatibility report form](https://github.com/quadringent/quadringent-ce/issues/new?template=compatibility_report.yml).
  `quadringent doctor --export FILE.tar.gz` (add `--preview` to see the content
  first) builds an archive that leaves out secrets and
  pseudonymizes names; read it before you attach it. See [Support](SUPPORT.md).
- **Improve the documentation.** Fix an unclear step, a missing prerequisite or
  a claim you cannot verify. Each page names its sources; keep them accurate.
  A statement about the project must be true in this repository.
- **Pick a good first issue.** Look at the
  [open issues labeled "good first issue"](https://github.com/quadringent/quadringent-ce/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22).
  The list can be empty. If it is, open a
  [question](https://github.com/quadringent/quadringent-ce/issues/new?template=question.yml)
  before starting a large change.
- **Add a test.** A synthetic scenario that fails today and describes the
  expected behavior is welcome even without a fix.

The [roadmap](ROADMAP.md) says what the maintainers expect to work on and how
they choose. Please do not post secrets, customer data or identifying details
about your site in an issue or pull request.

## Checks

Python 3.12+, Node 24 or 26, Java 21, Maven 3.9+, Helm 4.1.4, Git and
Gitleaks 8.30.1. Tests do not contact IBM i, AWS or Snowflake. Docker can provide
the JDK.

~~~sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev,api]'
npm ci --prefix ui
python -m pytest -q
npm test --prefix ui
npm run typecheck --prefix ui
npm run build --prefix ui
python -m ruff check src scripts research tests
python -m mypy
python scripts/check_publication.py
python scripts/sync_version.py --check
sh scripts/test_java_all.sh
~~~

**pytest is the only Python test runner**, including unittest classes.
`unittest discover` omits guarantees. Compare collections with
`python -m pytest --collect-only -q`; CI retains its collection as an artifact.
Subtests are reported separately from collected items.

For a documentation or community-file change, this subset is enough to start
with; CI runs the rest:

~~~sh
python -m pytest -q tests/test_community_files.py tests/test_publication.py
python -m ruff check src scripts research tests
python scripts/check_publication.py
~~~

`python scripts/local_acceptance.py --timeout-seconds 300` also runs historical
supplementary checks. Unverified runtime gates do not qualify a site.

## Principles

Write a test before changing behavior. Preserve measurement, provenance,
freshness, TLS and least privilege. Updating expected copy does not authorize
removing an engine guarantee. Use English for documentation, comments, copy
and code identifiers. Ruff checks unresolved names incrementally; mypy covers
the modules configured in pyproject.toml rather than the entire repository.

Validate the UI at 1440 px using keyboard and mouse: empty, stale and simulated
states, confirmations and capabilities. Browser scripts accept
`QUADRINGENT_PLAYWRIGHT_MODULE` for a local Playwright installation.

A PR describes the problem, resulting behavior, checks run and their limits.
Preserve unrelated changes. Never add real values files, tokens, dumps, site
certificates or production logs. Report vulnerabilities privately according to
[SECURITY.md](SECURITY.md).

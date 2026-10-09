# Contributing

Describe the expected behavior and the evidence needed to verify it. For a bug,
provide a synthetic scenario without secrets or customer data. Contributions
use Apache-2.0, like the rest of the project. Contributors retain their
copyright; no separate CLA or transfer of rights is required. Submit only code
you are allowed to distribute under this license and retain third-party notices.

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

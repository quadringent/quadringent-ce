# Contributing

Describe the expected behavior and the evidence needed to verify it. For a bug,
provide a synthetic scenario without secrets or customer data. Contributions
use Apache-2.0, like the rest of the project. Contributors retain their
copyright; no separate CLA or transfer of rights is required. Submit only code
you are allowed to distribute under this license and retain third-party notices.

## Ways to help

You do not need to write code to help.

- Send a compatibility report. The [compatibility page](docs/product/compatibility.md)
  marks many rows "Not tested": IBM i releases, column types, CCSIDs, commitment
  control. A report on one of them, even a short one that says what worked and
  what failed, is the most useful contribution. Use the
  [compatibility report form](https://github.com/quadringent/quadringent-ce/issues/new?template=compatibility_report.yml).
  `quadringent doctor --export FILE.tar.gz` (add `--preview` to see the content
  first) builds an archive that leaves out secrets and
  pseudonymizes names; read it before you attach it. See [Support](SUPPORT.md).
- Improve the documentation. Fix an unclear step, a missing prerequisite or
  a claim you cannot verify. Each page names its sources; keep them accurate.
  A statement about the project must be true in this repository.
- Pick a good first issue. Look at the
  [open issues labeled "good first issue"](https://github.com/quadringent/quadringent-ce/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22).
  The list can be empty. If it is, open a
  [question](https://github.com/quadringent/quadringent-ce/issues/new?template=question.yml)
  before starting a large change.
- Add a test. A synthetic scenario that fails today and describes the
  expected behavior is welcome even without a fix.

The [roadmap](ROADMAP.md) says what the maintainers expect to work on and how
they choose. Please do not post secrets, customer data or identifying details
about your site in an issue or pull request.

## Writing style

The project makes one claim, that reading a journal is not the same as delivering its data, and backs it with numbers. We write plainly and a little dryly, for IBM i operators who know DSPJRN and receivers and for data engineers who know MERGE and warehouses. We are not enthusiastic, not funny and not formal-legal. These rules apply to the README, the site, the docs and the articles.

1. Say "we", as the maintainers. Use the imperative for instructions. Never write "this project aims to".
2. Show the status once, as "Version 0.4 · Apache-2.0", and link [docs/evidence.md](docs/evidence.md) for what we have measured and what we have not. Do not open a page with a disclaimer. Limits live on the evidence page, in full, and are not repeated as warnings elsewhere.
3. Keep a caveat only if removing it would lead the reader to a wrong decision.
4. Use four evidence words: Measured, Not measured, Not tested, Not supported. Do not use Not established, Not executed, To establish, Partial, Bounded trials or Limited path. Write "Not qualified" only as "Production: not qualified" on the evidence page.
5. Bold at most once per paragraph, for a command, a state or the one thing to remember. Do not write "bullet + bold label + text" lists outside reference tables.
6. Start with the content. Do not open with "This page lists" or "This page describes", and stop when the content stops: no recap, no routine "Keep reading".
7. Keep the uncomfortable facts exact and findable on [docs/evidence.md](docs/evidence.md): 3 of 4 targets missed our 10 s bar in the 0.3.4 cold-warehouse trial (a later run passed on all four), the IBM i CPU and input/output cost is not measured, and only IBM i 7.5 on one shared server has been tested, in a historical measurement on one table that no published summary ties to a release. A specific fact with its version and scope builds trust. A generic caveat on every page wears it down.
8. Prefer the example to the abstract list. Name the stages (journal reading, raw storage, loading, reconciliation) once per page, then show numbers: 120 events read, 118 delivered, reconciliation Not measured.
9. Use IBM i terms where they are exact: journal receivers, `*BOTH` images, `DSPJRN` and `DISPLAY_JOURNAL`, CCSID 65535, `QTIMZON`, function keys. No nostalgia jokes and no green-screen kitsch.
10. Facts do not change when copy does. Move a number, date, scope or limit; never delete or alter it silently. It stays on the page or one link away in [docs/evidence.md](docs/evidence.md). Never invent a story, a customer, a person or a measurement, and never hint at who or where the maintainers are.
11. Show the current source version from `pyproject.toml` and, where a page needs it, the latest published prerelease. Observed releases and their dates belong on the evidence page. Do not leave a stale version anywhere.
12. Write commit messages with the conventional prefix and, when the change is not trivial, a body of one to three lines that says why.
13. Never claim production use, customers, deployments, references, service levels or guarantees, and use no superlatives. Every claim carries its version, date and receipt, or links to [docs/evidence.md](docs/evidence.md). `tests/test_no_overclaim.py` fails on the usual overclaim wording and on a proof line that does not match the receipts.
14. Interface: no coloured rails (left borders or inset bars) on cards, callouts or alerts, and no spaced uppercase labels above headings. State is a word, with a small glyph only in dense tables.
15. Interface: panels are flat and square, with a 1px rule at most, and only around real objects: a screenshot, a form, a table. Use a list or a table, not a grid of identical cards, callout boxes, badges or dividers.
16. Interface: words over decoration. Show the screenshot, the receiver chain or the terminal output instead of describing it, and keep captions to one line.

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

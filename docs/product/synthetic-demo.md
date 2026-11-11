# Try the synthetic demo

The synthetic demo opens the real Quadringent cockpit with generated data, so
you can see connections, tables, an initial copy, an incident and per-table
evidence before you have IBM i or Snowflake access.

It makes no IBM i, Snowflake or cloud connection. Every value is generated in
your browser, and evidence and costs are marked synthetic. The demo is not a
release qualification and proves nothing about a real installation.

## Run it from a clone

Requirements: Git, Node.js 24 or 26 (the UI toolchain), no Python, no cloud
account and no credentials.

~~~sh
git clone https://github.com/quadringent/quadringent-ce.git
cd quadringent-ce
npm ci --prefix ui
node ui/scripts/build-demo.mjs --serve
~~~

Then open <http://127.0.0.1:8870/#/cockpit>. Press Ctrl+C to stop.

The build writes `build/quadringent-demo-<version>/` and a portable
`build/quadringent-demo-<version>.tar.gz` with its `.sha256` file. The archive
only needs Node.js 18 or later:

~~~sh
tar -xzf quadringent-demo-<version>.tar.gz
node quadringent-demo-<version>/serve-demo.mjs
~~~

## A first evidence check

Once the cockpit opens, follow these three tables. These are generated values,
not measurements from an IBM i or Snowflake system.

1. Open Sales → CUSTOMERS → Evidence. Compare 120 captured events with 118
   delivered events. Both are Synthetic; reconciliation is Not measured.
   Reading the journal alone does not confirm that all data reached Snowflake.
2. Open Sales → ORDER_LINES → Evidence. Delivery is Synthetic, stale, with 96
   events. Its observation predates the incident. Open Logs to inspect the
   related synthetic entries instead of treating that old count as current.
3. Open Sales → ARCHIVE_90D → Evidence. No stage has a measurement. A missing
   measurement is not a count of zero and does not confirm delivery.

The check to take into a real evaluation is the same: inspect capture,
delivery and reconciliation separately, including each observation's source
and age. The demo illustrates this workflow without connecting to your systems.

## What you can look at

| Screen | What the demo shows |
|---|---|
| Cockpit home | Two fictional connections, one needing verification |
| Connection | Tables in capture, initial copy, pause and attention states |
| Table → Evidence | Journal capture, Snowflake delivery and reconciliation as separate stages, including missing and stale evidence. On the CUSTOMERS table, capture shows 120 events and delivery 118, both marked Synthetic, and reconciliation is Not measured. |
| Table → Logs | A synthetic incident correlated across log entries |
| Confirmations | Sensitive actions that require approval before they run |

Actions such as pause, resume or restart change only the in-memory demo state
and are lost on reload. Screens that need a running service, such as sign-in
or the v1 views, show a notice instead.

## How the demo stays disconnected

- It is a separate build (`ui/src/demo/`, Vite mode `demo`). The product build
  keeps no demonstration data, and `ui/scripts/verify-build.mjs` still rejects
  it if any appears.
- The page declares `connect-src 'none'`, so the browser refuses every network
  request, and the demo also replaces `fetch`, `EventSource`, `WebSocket` and
  `XMLHttpRequest` with functions that refuse to run.
- The local server listens on loopback only and serves files read-only.
- A banner on every screen states Synthetic demo, the source version, the
  commit and the UTC build time. `PROVENANCE.json` records the same facts.

The demo records the source version and commit of the clone. A clone of `main`
can include unreleased changes even when its version matches a published release.
Check the commit in `PROVENANCE.json` and
[Releases](https://github.com/quadringent/quadringent-ce/releases) for the published
artifacts and their dated qualification.

## Move to your first real table

The demo does not copy your data or test your credentials. For a first
replication, review [compatibility and prerequisites](compatibility.md),
then follow the [installation guide](install-default.md) with a non-production
IBM i and a journaled table with a primary key. Check the
[evidence for the version you select](../evidence.md) before deciding what to
validate on your own setup.

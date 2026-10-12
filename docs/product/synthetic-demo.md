# Try the synthetic demo

The synthetic demo opens the real Quadringent cockpit with generated data, so
you can see connections, tables, an initial copy, an incident and per-table
evidence before you have IBM i or Snowflake access.

**Synthetic demo: no IBM i, Snowflake or cloud connection.** Every value is
generated in your browser, including values labelled measured or observed.
This demo is not a release qualification and proves nothing about a real
installation.

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

## What you can look at

| Screen | What the demo shows |
|---|---|
| Cockpit home | Two fictional connections, one needing verification |
| Connection | Tables in capture, initial copy, pause and attention states |
| Table → Evidence | Journal capture, Snowflake delivery and reconciliation as separate stages, including missing and stale evidence |
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
- A banner on every screen states **Synthetic demo**, the source version, the
  commit and the UTC build time. `PROVENANCE.json` records the same facts.

The demo version is the source version of the clone. It can be newer than the
latest published release; check [Releases](https://github.com/quadringent/quadringent-ce/releases)
for published versions and their dated qualification.

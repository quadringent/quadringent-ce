# Run the local console

There is no cloud mutation. The local same-origin vertical serves a bounded snapshot, REST, SSE and the
cockpit. It does not contact IBM i, cloud storage or Snowflake.

## Prerequisites

Install the repository's Python dependencies, Node.js pinned by `.mise.toml`,
locked UI dependencies and Helm for local chart rendering:

```sh
npm ci --prefix ui
npm run build --prefix ui
```

Run the following from the repository root.

## Terminal 1 — create bounded evidence

```sh
PYTHONPATH=src python3 scripts/emit_console_snapshot_dev.py --out /tmp/quadringent-console.json
```

## Terminal 2 — serve the control plane

```sh
PYTHONPATH=src python3 scripts/quadringent_control_plane.py \
  --source historical:local-proof:file:///tmp/quadringent-console.json \
  --ui-dist ui/dist
```

The snapshot helper exercises the raw-first loop with a simulated journal in a
temporary directory. It writes the requested file. The server binds to
`127.0.0.1:8844`; open `http://127.0.0.1:8844/`.

`historical:local-proof` is intentional: this is bounded local evidence rather
than a current IBM i worker heartbeat. Read routes include `/v1/overview`,
`/v1/pipelines`, pipeline detail and `/v1/events`.
The `#/setup` route evaluates onboarding through
`POST /v1/onboarding/evaluate`, accepting no secrets and performing no
IBM i, S3 or Snowflake mutation.

The optional Vite development server uses port 5180 with `strictPort`.
It fails if occupied. Its v2 cockpit fixture is a labeled demonstration.
The production build is same-origin and contains no fixture fallback.

## What the evidence establishes

The operator projection preserves simulation and staleness. Overview, Pipelines,
detail, Incidents and Usage use the same REST/SSE contract. SSE revision changes
reload a snapshot without inventing intermediate measurements.

Snowflake delivery remains unproved until reconciliation is available.
Missing destination evidence remains **Destination not observed** and limits the
verdict. A table's existence alone is insufficient. A delivery proof must bind
`load_checkpoint`, `apply_checkpoint` and reconciled counts to capture.

Changing `historical` to `live` in a command creates no live evidence.
Live status requires a real worker, fresh observations, complete coverage and
all required stage signals. Destination staleness becomes `unknown`;
mismatched reconciliation becomes `degraded`.

A real S3 source can be supplied using an authorized read-only identity:

```sh
PYTHONPATH=src python3 scripts/quadringent_control_plane.py \
  --source live:dev-sale:s3://<authorized-bucket>/<combined-snapshot-key> \
  --environment dev
```

This variant reads S3 and requires the AWS Python dependencies. It does not
produce the proof or load data. The explicit environment preserves its DEV
identity. See [autonomous Snowflake](autonomous-snowflake-dev.md) for real
destination verification.

## Deterministic verification

```sh
PYTHONPATH=src python3 scripts/verify_quadringent_vertical.py --ui-dist ui/dist
```

The verifier imports runtime modules, renders Helm, rebuilds Vite in an isolated
directory from `/src/main.tsx`, compares executable assets, starts a bounded
local server, and checks the executed router, REST routes and SSE shape.
It removes its temporary directory. Each check prints PASS/FAIL; exit 0
requires all prerequisites and checks. String markers in JavaScript alone are
not proof of running behavior.

This verifies the local contract. Cloud deployment, IBM i access, Snowflake
loading and soak require separate execution and evidence.

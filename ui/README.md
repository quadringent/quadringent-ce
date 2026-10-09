# Quadringent operator interface

The cockpit presents IBM i-to-Snowflake evidence and available operations.
Enabled capture never implicitly becomes confirmed delivery. v1 and v2 use
distinct service contracts while preserving provenance and unknown observations.

## Develop and verify

```sh
npm ci
npm run dev
npm test
npm run typecheck
npm run build
```

The suite covers render helpers, routing, shell/screens, strict contracts,
clients, SSE/controller state and accessibility without a persistent server.

Hash routes include v1 overview/pipelines/detail/live/incidents/usage/setup,
v2 `#/cockpit`, connection/table detail and confirmations,
`#/wizard/activate|source|snowflake|tables`, and login.
Compatibility aliases remain readable; generate canonical routes with the router.

The v2 local development bridge supplies deterministic synthetic cockpit and
wizard fixtures. It does not establish a real installation.
Production builds have no fixture fallback; `scripts/verify-build.mjs`
rejects packaged fixture assets.

For v1 local evidence, use the
[local console runbook](../docs/product/quadringent-local-console.md).
Vite proxies the local API as configured; same-origin production uses
the served control plane.

## Architecture and operator contract

Clients in `src/data/` own REST/SSE requests. Controllers keep session,
revision, cache and connection state separate. Pure helpers in `src/domain/`
resolve proof limits before rendering. `src/screens/` composes those
contracts; reusable copy belongs in `src/domain/operator.ts`.

Missing, inaccessible and confirmed-empty states remain distinct.
Evidence tabs do not invent records. A source-to-target row count alone
cannot establish business parity. Controls exist only when the service reports
availability; sensitive actions require confirmation and readback.

Use [the visual system](../docs/design-system.md): self-hosted IBM Plex Sans/Mono,
quiet ruled panels, explicit status words, source provenance and keyboard focus.
Blue indicates activity/interaction, green observed measurement, amber a proof
limit and red an incident. English numbers and UTC 24-hour timestamps retain
measurement precision. Respect reduced motion.

## Delivery gates

Run tests, typecheck and production build, then from the repository root:

```sh
PYTHONPATH=src python3 scripts/verify_quadringent_vertical.py --ui-dist ui/dist
```

Verify rendered behavior at 1440 px with keyboard/mouse, both themes, critical
wizard steps, confirmations, missing/error states and table navigation.
Build success alone is not browser proof.

The optional diagnostic regression uses an existing local service and installed
Playwright/Chromium:

```sh
QUADRINGENT_BROWSER_URL=http://127.0.0.1:8853/ \
QUADRINGENT_PIPELINE_ID=review node ui/scripts/verify-diagnostic-browser.mjs
```

Substitute the actually exposed simulated pipeline.
`QUADRINGENT_PLAYWRIGHT_MODULE` can provide an ESM module URL;
`QUADRINGENT_BROWSER_EXECUTABLE` can select existing Chromium.
Only credential-free loopback HTTP URLs without query are accepted.
The check covers keyboard diagnostic access, focus/visibility, unchanged hash,
overflow, measurements navigation and relevant console/page/network errors.

Local fixtures and browser checks do not prove IBM i connectivity, actual
Snowflake delivery, a deployed cloud target or production qualification.

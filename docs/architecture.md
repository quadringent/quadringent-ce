# Architecture

The Java reader reads the IBM i journal over TLS. The Python runtime controls
windows, publishes raw data to S3, then advances the DynamoDB checkpoint under
the capture contract. Snowflake loaders and probes provide evidence separate
from a successful source read.

~~~mermaid
flowchart LR
  I[IBM i / journal] -->|TLS| C[Java reader + Python runtime]
  C -->|HTTPS| S[S3 / raw data and evidence]
  C -->|HTTPS| D[DynamoDB / checkpoint]
  S -->|Snowpipe or bounded loader| W[Snowflake]
  V[Verifier] --> W
  V --> S
  S --> P[Python control plane]
  CW[CloudWatch + public AWS rate] --> F[Cost collector]
  OC[OpenCost / allocation and assets] --> F
  F -->|private local file| P
  P -->|API v1 + SSE| U[React cockpit]
  P -->|create / get / patch Jobs| K[Kubernetes]
~~~

Business phase and `run.state` are separate. A historical table can remain
after the reader stops. Missing evidence never becomes zero.

| Directory | Responsibility |
|---|---|
| `src/quadringent/` | Capture, checkpoints, loaders, measurements and contracts |
| `src/quadringent_control_plane/` | API, projection, capabilities, intents and audit |
| `java/` | Reader and JDBC/JTOpen probes |
| `ui/` | Cockpit; operator copy in `domain/operator.ts` |
| `chart/`, `docker/` | Bounded deployment and reduced image surface |
| `infra-values/` | Synthetic examples to adapt in a private site repository |
| `research/` | Comparisons and experimental captures, outside the product wheel |
| `tests/` | Product and tooling guarantees, collected by pytest |
| `site/`, `docs/` | Presentation and documentation |

Frozen report generators, experimental manifests and private reports are
excluded. The original private repository retains them. AGENTS.md contains
contribution guidance.

`SiteConfig` validates a site's scope. The multi-site registry uses
`contextvars` to isolate an explicitly selected context. The CLI server serves
the site declared by its environment; registering multiple declarations does
not automatically provision multi-site runtimes.

Cost collection is independent of capture. The control plane receives no
billing permissions: it projects a dated local observation bound to the
declared site, bucket, region and namespace. A cost failure does not alter
capture state; failure of one cost source does not hide the other.
See [FinOps](finops.md).

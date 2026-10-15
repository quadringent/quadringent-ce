# Architecture

The Java reader reads the IBM i (formerly AS/400 and iSeries) journal of Db2 for i
tables over TLS. The Python runtime controls windows, publishes raw batches to
S3 or GCS, then advances the checkpoint in DynamoDB (AWS) or GCS (GCP) under the
capture contract. Snowflake loaders and the verifier provide evidence separate
from a successful source read.

![Quadringent architecture: the reader reads the IBM i journal over TLS and writes raw batches and checkpoints; loaders apply batches to Snowflake history and mirror tables; the verifier reads back Snowflake and raw storage; the control plane serves the cockpit and manages Kubernetes Jobs.](../site/assets/architecture.svg)

| From | To | Transport | Purpose |
|---|---|---|---|
| Reader | IBM i journal | TLS (JDBC, sign-on, command host servers) | Read journal entries and catalog |
| Reader and capture | S3 or GCS | HTTPS | Raw batches and receipts |
| Reader and capture | DynamoDB or GCS | HTTPS | Durable checkpoint |
| Loader | Snowflake | HTTPS | Apply batches to history and mirror tables |
| Verifier | Snowflake, raw storage | HTTPS | Bounded readback evidence |
| Control plane | Raw storage | HTTPS | Read permitted evidence keys |
| Control plane | Kubernetes API | HTTPS | Create, get and patch reader and loader Jobs |
| Cockpit | Control plane | HTTPS, SSE | API v1 and API v2 |
| Optional cost collector | Control plane | Private local file | CloudWatch, public AWS rate and OpenCost observations |

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

For a first evaluation, review the [installation prerequisites](product/install-default.md)
and [pilot preparation](product/positioning-and-offer.md). A running reader alone does not prove delivery.

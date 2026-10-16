# Install Quadringent with Helm

This expert path deploys the chart manually. For orchestrated Terraform and
Helm installation use [the default installer](install-default.md).
Both use the same chart. This guide targets a non-production site.
Repository examples contain synthetic identities.

## Prepare the site

The site owner must provide:

- Reachable IBM i, journaled tables and an authorized catalog/table/journal reader;
  journal library, receivers, image format and sufficient recovery retention.
- The IBM i IANA time zone from `QTIMZON`, declared in
  `ibmi.sourceTimeZone` / `AS400_SOURCE_TIME_ZONE`. UTC is never assumed.
- An independently verified IBM i CA and TLS on the site's declared ports.
  JTOpen defaults are database 9471, command 9475 and sign-on 9476.
- Kubernetes, Helm 3, a dedicated namespace and a suitable StorageClass,
  with access to the authorized image registry.
- S3 and DynamoDB with scoped IAM/IRSA/OIDC identities, or GCS with workload
  identity and generation-conditional checkpoint writes.
- A non-production Snowflake warehouse, database, schema, storage integration,
  external stages and destination tables. Snowpipe notifications are configured
  separately. The control plane receives no account-wide billing grant.

GCS uses `QUADRINGENT_STORAGE_BACKEND=gcs` and the Python `gcs` extra:
raw batches in `AS400_RAW_BUCKET`, checkpoints and source gates in
`AS400_CHECKPOINT_BUCKET`. Runtime application-default credentials need read,
create and list access, with no object-delete permission. Snowflake needs a GCS
storage integration and external stage. The v1 S3 console/proof/notification
examples remain AWS-specific. See [autonomous Snowflake](autonomous-snowflake-dev.md).

Review IAM templates for the actual site; do not apply synthetic policies as-is.
The offline native collection component is not an installed admission controller.
If explicitly integrated at a site, its S3 identity needs `GetObject` and
`PutObject` on that organization's `native-collections-v1/<org-sha256>/objects/*`,
plus `AbortMultipartUpload` there to clean up interrupted large-file transfers.
Bucket-level `ListBucket` must also allow the initial `HeadObject` check to
distinguish an absent archive object from a denied read.
Read access to the corresponding `native-preparations-v1` archive and list/read
access to the table's journal prefix are separate scoped permissions. Collection
alone grants no native qualification or stable-cut proof.

## Prepare values and secrets

Copy `infra-values/values-int.yaml` outside the clone into a private file.
Adapt site identity, cloud configuration, IBM i and workload identities.
The Helm namespace must exactly equal `site.namespace`. Allowed environments
are `dev`, `int`, `test` and `staging`;
`productionPromotionAllowed` remains `false`.

Use three images built from the same validated source and admitted by the
release procedure. Configure repositories and immutable digests for
`image`, `controlPlane.image`, `verification.imageDigest` and any enabled
observability job. Include the authorized repository in
`controlPlane.image.allowedRepositories`. Example repositories/digests do
not establish that an image exists.

| Secret reference | Content |
|---|---|
| `ibmi.passwordSecret.name/key` | Reader password |
| `as400.tlsCaSecret.name/key` | Verified PEM CA bundle |
| `image.pullSecret` | Private registry credentials |
| `controlPlane.auth.proxySecret.secretName/secretKey` | Authentication proxy secret, when enabled |
| Verifier secret references | Site-declared Snowflake authentication |

Prepare secrets through the site's secret mechanism. Keep plaintext out of Git,
values and process arguments. Mount the CA read-only at `as400.tlsCaFile`.
Fleet Job templates must preserve that volume/mount; see
`infra-values/job-template-int.json` and [TLS](../../docker/certs/README.md).

Community Edition requires no license key or commercial table-count limit.
Safety, declared capacity, readiness and continuity checks still apply.
Remove obsolete `controlPlane.licenseSecret` and `QUADRINGENT_LICENSE_KEY`
settings. The chart does not delete an existing Secret.

## Enable the v2 control plane

The copied INT example keeps v2 and PostgreSQL disabled. The `/v2` setup,
source and table routes below require v2. Save this additional overlay as
`/private/path/v2.yaml`, after adapting the base site values:

```yaml
replicaCount: 0
pilot:
  enabled: false
consoleSnapshot:
  enabled: false
observability:
  enabled: false
fleetObserve:
  enabled: false
controlPlane:
  enabled: true
  host: "127.0.0.1"
  launch:
    enabled: false
  v2:
    enabled: true
    orgId: ""
postgres:
  enabled: true
```

Bundled PostgreSQL requires persistent storage through `postgres.storage`.
For an external database, replace the overlay's `postgres` section and add
`externalDatabase` as follows. Create the referenced Secret through the site's
secret mechanism; its `url` key holds a SQLAlchemy `postgresql+psycopg://` DSN
for a database reachable from the Pod, with permission to run schema migrations:

```yaml
postgres:
  enabled: false
externalDatabase:
  url: ""
  existingSecret: site-managed-db
  existingSecretKey: url
```

Use one database per installation. Empty `controlPlane.v2.orgId` selects
`site.id`; for an existing database, keep its original organization ID.
Startup applies migrations and refuses a different or multiple organizations.
The chart supplies `QUADRINGENT_V2_DATABASE_URL`, `QUADRINGENT_V2_SECRET_KEY`
and `QUADRINGENT_V2_TOKEN_PEPPER`. Helm creates
`<release>-quadringent-v2-secrets` (`SECRET_KEY`, `TOKEN_PEPPER`) on first install
and reuses both keys when its live lookup finds them. Preserve that Secret
with the database and, for bundled PostgreSQL, its credentials Secret and PVC.
Replacing the key breaks decryption; replacing the pepper invalidates tokens
and sessions. Offline rendering generates temporary Secret values; keep its
output private and install with Helm. Before upgrading, verify that both keys
are present and Helm has permission to read the existing Secret.

## Render and install

The overlay keeps capture, pilot, v1 fleet launch and scheduled observation
disabled. The v2 executor can still start workloads through authorized actions.
The configured proof source must belong to the authorized site; absent objects
remain absent evidence. Helm does not provision cloud or Snowflake resources.

```sh
umask 077
helm lint chart -f /private/path/site.yaml -f /private/path/v2.yaml --namespace quadringent-demo
helm template quadringent chart -f /private/path/site.yaml -f /private/path/v2.yaml \
  --namespace quadringent-demo > /private/path/rendered.yaml
# After reviewing the rendering and receiving site authorization:
helm upgrade --install quadringent chart -f /private/path/site.yaml -f /private/path/v2.yaml --namespace quadringent-demo
kubectl -n quadringent-demo rollout status deployment/quadringent-quadringent-control-plane
kubectl -n quadringent-demo port-forward deployment/quadringent-quadringent-control-plane 8844:8844
```

Adapt the namespace to the values. The tunnel binds locally.
Check `/healthz`, `/v1/version`, `/v1/overview` and `/v2/healthz`.
v1 relays `/v2/*` and `/mcp` to v2 on loopback port 8845; only port 8844
needs forwarding. The chart creates no control-plane Service or Ingress.
Keep the tunnel private. External exposure requires the site's reviewed HTTPS
proxy and network restrictions. A non-loopback `controlPlane.host` requires
`controlPlane.auth.enabled`, trusted user/group headers, authorized
`operatorGroups`/`adminGroups` and the `auth.proxySecret` reference. The proxy
must replace client headers and supply `x-quadringent-proxy-secret`; Helm does
not provision or isolate it. See [security and exposure limits](../../SECURITY.md).
Ready Pods do not prove replication.

v2 requires authentication. For a new installation, prepare a private
`/private/path/first-admin.json` containing the administrator's `email`, then
request bootstrap through the local tunnel:

```sh
umask 077
curl --fail-with-body --silent --show-error \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: first-admin' \
  --data-binary @/private/path/first-admin.json \
  http://localhost:8844/v2/setup/first-admin > /private/path/first-admin-response.json
```

Protect the response's `after.activation_token`; it is emitted once and expires
after 24 hours. Use it at `http://localhost:8844/#/wizard/activate?token=<token>`,
set the password and sign in before accessing protected routes. Session cookies
are Secure: use Chromium/Firefox at `localhost` for the private tunnel; Safari
may prevent login. For an existing administrator, sign in instead of repeating
bootstrap. Account recovery and agent-token scopes are in [API v2](../api-v2.md).

v1 connection persistence requires `--state-dir` or `--fleet-state-dir`.
The chart wires it through fleet mode with a PVC; read-only mode without state
does not expose `/v1/connections`. Enabling a fleet requires its own validated
runtime prerequisites.

The [values reference](chart-values.md) is generated from
`chart/values.schema.json`. Helm checks types, enumerations, digest patterns,
tuning bounds and unknown properties before rendering. Template guards also
validate relationships between fields and give specific English error messages.

## Discover and prepare tables

`POST /v2/sources/{id}/tables/refresh` reads the IBM i catalog
(`QSYS2.SYSTABLES`, `SYSTABLESTAT`, `JOURNALED_OBJECTS`, `SYSKEYCST`).

| Readiness | Meaning |
|---|---|
| `ready` | Journaled with `*BOTH` images and a primary or unique key |
| `not_journaled` | Needs journaling; reuse a suitable journal or prepare one |
| `images_incomplete` | Before images are missing; updates/deletes cannot be replicated correctly |
| `no_key` | RRN is possible only with explicit acknowledgement of reorganization risk |

An authorized IBM i administrator executes any suggested CL commands. The API
never executes them. For a synthetic example:

```text
CRTJRNRCV JRNRCV(SALES/QSQJRN0001) TEXT('Quadringent journal receiver')
CRTJRN JRN(SALES/QSQJRN) JRNRCV(SALES/QSQJRN0001) MNGRCV(*SYSTEM) DLTRCV(*NO) TEXT('Quadringent journal')
STRJRNPF FILE(SALES/ORDHDR) JRN(SALES/QSQJRN) IMAGES(*BOTH) OMTJRNE(*OPNCLO)
CHGJRNOBJ OBJ((SALES/ORDHDR *FILE)) ATR(*IMAGES) IMAGES(*BOTH)
```

`MNGRCV(*SYSTEM)` needs a receiver threshold other than `*NONE`
([CRTJRN](https://www.ibm.com/docs/en/i/7.5?topic=ssw_ibm_i_75/cl/crtjrn.htm)). The example
omits `THRESHOLD`, so the IBM default of 1500000 KB applies, and keeps detached
receivers (`DLTRCV(*NO)`); size both with [compatibility and prerequisites](compatibility.md).
When the library already has a journal, the suggestion reuses it.

For RRN, use `PATCH /v2/tables/{id}` with `acknowledge_rrn: true`.
`RGZPFM` or `CLRPFM` requires resynchronization. Tables from different
journals require separate readers; the UI warns about that grouping.

## Declare and commission

A v1 connection creation response of `201 declared_not_in_service` records
configuration; it does not test connectivity or start ingestion.
Commissioning requires observed catalog/sidecar state, a compatible Job template,
durable checkpoints and proved continuity. Render fleet launch only after those
prerequisites are verified:

```sh
helm template quadringent chart -f /private/path/site.yaml -f /private/path/v2.yaml --namespace quadringent-demo \
  --set controlPlane.launch.enabled=true \
  --set-file controlPlane.launch.jobTemplate=/private/path/job-template.json \
  --set-file controlPlane.launch.fleetCatalog=/private/path/fleet-catalog.json \
  --set-file controlPlane.launch.fleetSidecar=/private/path/fleet-sidecar.json
```

Review before applying. Preserve the fleet-state PVC. Choose v1 fleet or v2
pipeline commissioning for each journal and keep a single reader; do not run
the permanent Deployment and fleet pilot together.
Preflight can write storage probe objects and requires site authorization.

`controlPlane.launch.enabled` controls v1 fleet Jobs; v2 has its own Kubernetes
executor. Before starting a v2 pipeline, verify the capture ServiceAccount,
cloud permissions and destination prerequisites. Setup alone declares no source
or pipeline. Sensitive v2 actions (`remove`, `restart_initial_copy`, `replay`,
organization-wide pause/resume) require a bound, approved `confirmation_token`;
without it the API returns `409 pending_confirmation_required`. Follow the
[confirmation flow](../api-v2.md#confirmations-and-audit) before execution.

## Prove and remove

For a declared table/window scope, retain source/catalog/TLS proof, receiver
continuity, start/end positions, durable checkpoints, reread raw bytes,
Snowflake key/operation/value reconciliation, insert/update/delete coverage,
freshness and checkpoint recovery after an authorized controlled stop.
Counts, zero lag, HTTP success and green buttons are insufficient.

See [FinOps](../finops.md) for the independent cost collector and
[operations](../operations.md) for recovery. No configured collector means
unmeasured costs.

Before uninstalling, stop readers and verify no runtime Job remains active.
Runtime Jobs may not be Helm-owned. Preserve state, intentions and audit before
any approved PVC deletion. Storage objects/versions, checkpoints, Snowflake,
notifications, IAM and site secrets remain separately managed. Their deletion
is a distinct data-owner decision. Record the first unmet prerequisite and
the evidence obtained at each stage.

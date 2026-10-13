# Control plane v2 API

The current API is served under /v2. Its live OpenAPI 3.1 document at
GET /v2/openapi.json is the authoritative request/response schema.
This guide describes identity, write semantics, orchestration and evidence
boundaries. See [API v1](api.md) for projection and legacy operator routes.

GET /v2/pipelines/{id}/evidence requires read access and returns separate
capture, delivery and reconciliation measurements with dates and provenance.
Missing measurements are null; declared pipeline state is never delivery
evidence. Loader logs describe only the last retained delivered batch.
Validated console projections can also publish reconciliation for their
measured scope. Simulation, stale evidence and unavailable providers remain
explicit. Row values and raw pod log messages are excluded.

## Private access and startup

The chart runs v1 (8844) and v2 (8845) in the same control plane pod.
Both listen on loopback. With controlPlane.v2.enabled, v1 relays /v2/*
and /mcp to the configured v2 upstream, preserving request bodies, relevant
headers, cookies, tokens and SSE. v2 authenticates its own requests.

```sh
kubectl port-forward deployment/<release>-quadringent-control-plane 8844:8844
pip install -e ".[api]"
```

The native entrypoint requires the PostgreSQL DSN, Fernet encryption key
and token pepper. Supply them through private environment/Secret references,
not committed files. Each installation uses one organization and its own
database. Current source refuses startup if the database contains a different
organization or multiple organizations; this check does not rename or remove
organizations. Schema migrations run before the check.
Restore the matching organization configuration or use a separate database.
This guard is absent from the published 0.3.5 artifacts.
Direct application construction is useful for isolated tests:

```python
from quadringent_control_plane.v2 import db
from quadringent_control_plane.v2.app import create_v2_app
from quadringent_control_plane.v2.crypto import SecretBox

db.run_migrations(dsn)
app = create_v2_app(
    engine=db.create_engine_for(dsn),
    secret_box=SecretBox.from_environment(),
    org_id="default",
    token_pepper=server_pepper,
    require_authentication=True,
)
```

Alembic migrations live in v2/migrations/ and run idempotently to head.
Revision IDs must fit PostgreSQL's 32-character alembic_version column.
Schema history includes identity/audit, discovery, observation, scope-pause
markers, unified leases, one-time-secret sanitization and destination scope.
Use the migration chain in source rather than manually creating selected tables.

## Data model and state

Sources declare display_name, ibmi_host and ibmi_user; the encrypted password
is represented publicly only by secret_set. Destinations persist the account,
service identity, encrypted RSA key, setup script, declared database/schema and
verification_state. Tables include library/name, discovered columns, journal,
images, readiness, key strategy and discovery timestamp. Pipelines bind a table
and a destination, with separate declared and observed state. Organization-scoped
idempotency records bind a request to its actor and retain a sanitized response.

```text
not_started --start--> copying --bootstrap_completed--> live
copying|live --pause--> paused
paused --resume_copying|resume_live--> copying|live
copying|live|paused --attention--> attention
attention --resume--> copying
copying|live|paused|attention --remove--> stopped
copying|live|paused|attention --restart_initial_copy--> copying
```

stopped is terminal. Unlisted transitions raise ForbiddenTransitionError;
unknown states raise UnknownStateError. A declared transition is not proof
that Snowflake received the corresponding data.

## Identity

Identity is resolved in this order:

1. Authorization: Bearer qdt_<rd|op|ad>_<b62> agent token. Only its SHA256
   hash plus server pepper is stored. Scopes are read, operate and admin;
   source_restriction may narrow access. Expiration is required unless
   never_expires: true is explicitly declared. Rotation invalidates the old
   value; revocation is permanent.
2. User session from POST /v2/auth/login. Passwords are scrypt-hashed.
   The signed cookie is HttpOnly, SameSite=Strict and Secure in the native
   entrypoint. Roles are admin and reader; reader never exceeds read scope.
3. Explicitly configured trusted v1 authentication proxy.

The native entrypoint sets require_authentication=True. Unauthenticated
protected routes return 401 invalid_request. Direct create_v2_app calls default
to False for isolated development; that mode permits an implicit loopback admin.

Public bootstrap/authentication routes are healthz, openapi.json, docs/redoc,
setup/first-admin, user activation, auth/login and auth/logout. auth/me requires
an identity and returns subject, role, actor_kind and email (null for agents).

The first admin uses a single-use activation link with a 24-hour TTL. Subsequent
users are invited by an admin. Secure loopback cookies work with Chromium/Firefox
at localhost; Safari may prevent login through a plain local tunnel. Use the
site's reviewed HTTPS setup for external access.

### Optional OIDC

OIDC routes are mounted only when oidc_config and oidc_http_client are provided.
GET /v2/auth/oidc/login starts Authorization Code + PKCE S256; the signed
HttpOnly state cookie carries state, nonce and code_verifier. The callback
verifies cookie signature/expiry, matching state, the token exchange, RS256/JWKS,
issuer, audience, nonce and required claims.

A linked subject resolves its existing user. An invited email may be linked
without changing its role. Otherwise a new activated reader is created.
OIDC never creates an admin. The successful callback sets the same session
cookie as password login. Invalid or incomplete claims fail closed.

## Writes, idempotency and secrets

Every POST/PATCH/DELETE requires Idempotency-Key (maximum 128 characters).
Within one organization, the same actor and identical request replay the
previous status and sanitized metadata without repeating an effect.
A different actor or body returns 409 idempotency_key_conflict. Keys expire
after 24 hours; reuse replaces only that actor's expired row. Organizations
have independent namespaces.

```json
{"before": null, "after": {"...": "..."}, "verify": {"method": "GET", "path": "/v2/sources/..."}, "dry_run": null}
```

With dry_run: true, after remains null and dry_run contains the plan.
No effect is persisted. The first successful response alone may contain
private_key_pem, token, activation_token or secret. These fields are recursively
removed from stored idempotency responses and audit before/after, including
reads of restored historical records. Save the first emission privately;
GET or replay cannot recover it.

A lost agent token requires explicit admin rotation with a new idempotency
key. A destination's setup script remains readable because it contains only
the public key; its private RSA key is not recoverable through GET.

### First-admin activation recovery

POST /v2/users/{id}/activation/reissue requires an authenticated admin
operator. The user must be the organization's unactivated admin, with no
active admin in that organization. The transaction preserves the user,
invalidates old links and stores only the new token hash. The token appears
once in after.activation_token (201), expires after 24 hours, and is not
redisplayed on replay. No password is reset and no active admin is reactivated.

An operator already holding kubectl exec access can obtain a short-lived
native token without displaying it:

```sh
quadringent agent-token --name <site> --namespace <namespace> \
  --label bootstrap-recovery --scope admin --days 1 \
  --url <private-url> --write-config <private-0600-file>
quadringent users --config <private-0600-file> list
quadringent users --config <private-0600-file> \
  reissue-activation <user_id> --idempotency-key <new-key>
```

The explicit configuration takes precedence over ambient variables. Capture
the final one-time response in private storage and revoke temporary access
after recovery.

### Older databases

Migration 0015_one_time_secrets sanitizes historical idempotency and audit JSON
while preserving encrypted keys, authentication hashes, identifiers and action
metadata. If an old database has exactly one organization, old keys can be
associated with it. Ambiguous keys return 409 even after expiry: inspect the
previous action before issuing another request.

Stop old control plane processes before upgrading; a migration lock does not
prevent an older binary from writing unsanitized responses. Apply migrations
before serving a restored database. Old backups remain sensitive and are not
purged by migration. Downgrades that reintroduce the vulnerable store are refused.

## Confirmations and audit

remove, restart_initial_copy and replay are sensitive pipeline actions.
Without an approved confirmation_token they create a pending confirmation
(TTL 24 hours) and return 409 pending_confirmation_required. pause_all and
resume_all at organization scope also require confirmation.

Approval is possible through an authenticated operate identity, a single-use
signed approval link, or a sufficiently scoped agent token with the exact
action_ref in its declared pre_authorized_actions. The approved confirmation
becomes used after its bound action executes; it cannot approve another action
or execute twice. Replay always requires approval; there is no configurable
cost threshold in this implementation.

Audit records are queryable by actor_kind, action, resource_type and resource_id.
Writes declaring AuditContext are recorded on success and failure, once per
idempotent action. X-MCP-Client populates mcp_client. Secret fields are removed
before storage and read; organization isolation applies to every read.

## Endpoint reference

| Method | Route | Scope | Behavior |
|---|---|---|---|
| GET | /v2/healthz | public | Health, not delivery proof |
| GET | /v2/sources | read | Source list; next_cursor currently null |
| POST | /v2/sources | operate | Declare encrypted IBM i credentials; dry-run validation |
| GET | /v2/sources/{id} | read | Source detail |
| POST | /v2/sources/{id}/test | operate | Injected real probe; unknown if no probe |
| POST | /v2/sources/{id}/actions/{pause,resume} | operate | Scoped pipeline action |
| GET | /v2/sources/{id}/tables | read | search, library, readiness filters |
| POST | /v2/sources/{id}/tables/refresh | operate | Injected discovery; 503 if unavailable |
| PATCH | /v2/tables/{id} | operate | primary, unique_index or acknowledged rrn key |
| POST | /v2/tables/{id}/pipeline | operate | Create/start pipeline for declared destination |
| GET/POST | /v2/destinations | read/operate | List/create RSA service identity and SQL |
| GET | /v2/destinations/{id} | read | Declared scope and verification state |
| GET | /v2/destinations/{id}/setup-script | read | Public-key SQL; no private key |
| POST | /v2/destinations/{id}/verify | operate | Verify persisted scope and actual privileges |
| POST | /v2/destinations/{id}/actions/{pause,resume} | operate | Scoped pipeline action |
| GET | /v2/pipelines | read | limit/cursor, state/source_id/destination_id filters |
| GET | /v2/pipelines/{id} | read | Declared state plus observation |
| GET | /v2/pipelines/{id}/metrics | read | window=1h or 24h |
| GET | /v2/pipelines/{id}/logs | read | since, level, correlate_incident; redacted |
| POST | /v2/pipelines/{id}/actions/{pause,resume,remove,restart_initial_copy,replay} | operate | Executor; sensitive actions confirmed |
| POST | /v2/actions/{pause_all,resume_all} | operate | Organization action; always confirmed |
| GET | /v2/confirmations | read | state filter, default pending |
| GET | /v2/confirmations/{id} | read | Confirmation detail |
| POST | /v2/confirmations/{id}/approve | operate or signed link | Consume approval authority |
| POST | /v2/confirmations/{id}/reject | operate | Reject |
| GET/POST | /v2/agent-tokens | admin | List/create; token emitted once |
| POST | /v2/agent-tokens/{id}/rotate | admin | Replace token value |
| DELETE | /v2/agent-tokens/{id} | admin | Revoke |
| POST | /v2/setup/first-admin | public bootstrap | 409 if an admin is already active |
| GET/POST | /v2/users | admin | List/invite; activation emitted once |
| GET | /v2/users/{id} | admin | User detail |
| POST | /v2/users/activate | activation token | token and password |
| POST | /v2/users/{id}/activate | activation token | activation_token and password |
| POST | /v2/users/{id}/activation/reissue | admin | First unactivated admin only |
| POST | /v2/auth/login | public | Establish session |
| POST | /v2/auth/logout | public | Clear session |
| GET | /v2/auth/me | read | Current identity; 401 if absent |
| GET | /v2/audit | admin | Filtered organization audit |
| GET | /v2/events | read | SSE with Last-Event-ID or last_event_id |
| GET/POST | /v2/webhooks | read/admin | List/create; secret emitted once |
| DELETE | /v2/webhooks/{id} | admin | Remove |
| POST | /v2/webhooks/{id}/redeliver/{event_id} | operate | Explicit idempotent redelivery |
| GET | /v2/costs | read | scope=connection or table, id, window |
| GET | /v2/openapi.json | public | Complete OpenAPI 3.1 schemas |

Unknown IDs return 404; invalid state/window/level/scope filters return 400.
The error catalog includes invalid_request, idempotency_key_conflict,
not_found, insufficient_role, capability_unavailable, action_in_progress,
pending_confirmation_required, wrong_confirmation, wrong_environment,
store_unavailable, executor_unavailable and internal_error.

## Discovery and orchestration

Discovery returns journal metadata and readiness:

| Readiness | Condition | Proposed operator CL commands |
|---|---|---|
| ready | Journal active, *BOTH images, primary/unique key | None |
| not_journaled | No active journal | STRJRNPF, or CRTJRNRCV + CRTJRN + STRJRNPF |
| images_incomplete | Only *AFTER images | CHGJRNOBJ ... ATR(*IMAGES) IMAGES(*BOTH) |
| no_key | Journal and images ready, no unique key | Explicit RRN choice and resynchronization acknowledgment |

RRN requires acknowledge_rrn: true. RGZPFM/CLRPFM invalidates physical-position
identity and requires resynchronization. Proposed CL commands are not executed
by discovery. The real source probe also resolves QTIMZON to a capture time zone.

Pipeline creation resolves an explicit destination_id or the organization's
single destination. Multiple destinations require an explicit choice.
KubernetesPipelineExecutor maintains one reader Deployment per source/journal
and one initial-copy Job per table. The background reconciler observes Job
success for copying → live, or failure for attention. These control plane
states do not by themselves prove destination rows. See [orchestration](orchestration.md)
for boundaries, receiver continuity and referenced Secrets.

Bounded replay is implemented by the executor but is not wired into the replay
HTTP action. A callable API route does not imply every executor capability is
available. Missing executors fail explicitly rather than silently succeeding.

Scope pause records operator intent and actually applies to eligible pipelines.
Scope resume does not restart a table paused individually. after.pipelines
reports applied/skipped; dry_run.pipelines reports would_transition/skipped.
Without an executor, a scope with pipelines returns 503 executor_unavailable;
an empty scope is a safe no-op.

## Snowflake destination scope

POST /v2/destinations accepts destination_database (default QUADRINGENT) and
optional destination_schema. With no explicit schema, history uses RAW and mirror
uses CURATED in the persisted database. An explicit schema contains both.
Identifiers start with a letter or underscore, continue with letters/digits/_/$,
are limited to 63 characters, and are uppercased before RSA generation.

The generated SQL creates a TYPE=SERVICE user with RSA authentication and scoped
USAGE/CREATE TABLE; the reader role receives SELECT. It does not alter human
accounts or MFA policies. Creating a destination declares SQL but does not apply it
or verify ingestion. The verifier uses the persisted scope and cannot accept a
client-supplied success or replacement scope. It reports connection, role, warehouse,
database, schema and loading checks independently, including a temporary CREATE TABLE
probe followed by removal. No verifier means verified: unknown.

Loaders use the linked destination, never global fallback values to override it.
They receive QUADRINGENT_DESTINATION_DATABASE, QUADRINGENT_DESTINATION_SCHEMA
and QUADRINGENT_MIRROR_SCHEMA. Migration 0016 preserves historical destinations
with the default database and nullable schema.

Before upgrading old loaders, stop them and inventory their data/checkpoints.
If old global settings wrote elsewhere, do not resume into empty targets using
existing checkpoints. Restore into the declared scope and perform a controlled
new initial copy, then reconcile rows. No data is moved automatically.

The reconciler stops loaders with a mismatching database/history/mirror scope
and refuses resume. Remove that stopped Deployment after preparing the new copy.
An immutable loader-scopes/*.json raw-storage marker binds each table/run checkpoint
to its account and HISTORY/MIRROR targets before Snowflake connection. Missing
provenance with an existing checkpoint, unreadable markers or a different target
blocks startup. A new copy uses another checkpoint; old cursors are never reset.
Back up markers with raw objects and checkpoints; older backups without provenance
need a new controlled copy.

## Observations, logs and costs

Absent providers return null measurements with reasons, empty logs or status: absent.
Pipeline observations contain observed_state, lag_seconds, throughput_rows_per_second,
rows_source, rows_destination, last_arrival_at, collected_at and absent_reasons.
Declared state remains separate.

ProjectionRepositoryObservationAdapter reads v1 evidence documents and their
timestamped lag series. StorageBackendObservationAdapter derives throughput
only from two checkpoints in the same receiver; rotation or a backward sequence
leaves it absent. A checkpoint is not an absolute source/destination row count.
CompositeObservationProvider combines fields and delegates history to projection.

KubernetesLogSource uses exact pipeline labels and bounded reads. Shared journal
readers are not unambiguously selected by pipeline labels. KubernetesLoaderTelemetry
selects exact table log lines, bounded to the last 2,000 pod lines. A 24-hour window
may therefore be incomplete. Only an actual journal batch followed by MERGE creates
a delivery latency point. A snapshot, including an empty one, can provide dated
history/mirror mutation ages, but no journal-delivery point.

At rest, the loader does not continuously query Snowflake. Last measured ages retain
collected_at and disappear after one hour without measurement; they do not establish
current source freshness. lag_seconds remains absent at rest. Throughput requires
two actual deliveries.

Metrics return window, points (at, lag_seconds, throughput_rows_per_second),
provenance, freshness and collected_at. Logs filter info/warning/error and optional
incident correlation. Central redaction masks credential-like key/value pairs and
serialized multi-field row payloads regardless of the injected provider.

Costs preserve measured/estimated/absent, explicit currency and basis. The v1
warehouse adapter is connection-scoped, never table-scoped. Snowflake credit samples
need a declared price per credit to produce money; provisional samples are estimated,
finalized samples measured. No sample or price means absent, never zero.
FallbackCostsProvider selects the first nonabsent result.

ObservationRefreshService persists last observed state and emits state-change events,
plus alert.fired/resolved on incident transitions. The scheduler is disabled by default;
enable_observation_scheduler=True enables it. Unified leases provide generation fencing
shared with the reconciler. No observed state overwrites declared intent.

## Events and signed webhooks

GET /v2/events resumes from an opaque integer cursor using Last-Event-ID or
last_event_id. Types include pipeline.state_changed, action.pending_confirmation,
action.completed, alert.fired and alert.resolved. Events prompt HTTP rereads;
they do not carry full resource state. Persisted events are polled, not LISTEN/NOTIFY.

A webhook's secret is emitted once, then encrypted. The signature is
X-Quadringent-Signature: t=<unix>,v1=<hex>, HMAC over timestamp.body, with a
five-minute tolerance. A unique webhook_id/event_id pair prevents automatic replay.
Explicit redeliver permits a new idempotent attempt. Delivery uses bounded exponential
backoff (five attempts) and disables repeatedly failing webhooks; successful delivery
resets the failure counter. WebhookDeliveryWorker runs outside HTTP requests and must
be explicitly wired; registering a webhook alone is not delivery proof.

## MCP and CLI

/mcp uses streamable HTTP in the same FastAPI app. Tools call REST through
internal ASGI transport and preserve Bearer identity, dry-run, confirmations,
idempotency and audit. No business logic is duplicated. X-MCP-Client records
clientInfo. With required authentication, missing agent identity fails identically
to the equivalent REST route.

Tools cover list/test/pause/resume sources, verify/pause/resume destinations,
list/refresh/choose-key tables, list/get/pause/resume pipelines, confirmed
restart_initial_copy/replay_journal_range/remove_table, confirmed pause_all/resume_all,
pending confirmations and audit. The MCP get_costs tool reports absent when its
service is not wired. An agent cannot approve its own sensitive action without
the exact declared preauthorization.

Resources are quadringent://docs/api (OpenAPI), quadringent://docs/errors,
quadringent://docs/llms.txt and quadringent://state/overview. The short guide is also
served at GET /llms.txt. See [agent integration](agents.md) for transports.

Client CLI commands emit JSON:
{"result": <API response>, "idempotency_key": "..."}.
An explicit --config private 0600 file overrides ambient configuration;
otherwise QUADRINGENT_URL/QUADRINGENT_TOKEN precede ~/.quadringent/cli.json.
Unsafe configuration permissions fail closed.

```text
quadringent sources       list | get | test | pause | resume
quadringent destinations  list | get | pause | resume
quadringent tables        list | refresh | choose-key
quadringent pipelines     list | get | pause | resume | restart-initial-copy | replay | remove
quadringent actions       pause-all | resume-all
quadringent confirmations list | approve | reject
quadringent tokens        list | create | rotate | revoke
quadringent users         list | invite | reissue-activation
quadringent audit         tail
quadringent events        stream
quadringent webhooks      list | create | delete
quadringent mcp --stdio
```

Writes accept --dry-run and --idempotency-key (generated and displayed if omitted).
Sensitive writes accept --confirmation-token. Preview, approve through a separate
authorized identity, then replay the bound action with its approved token.

| Exit | Meaning |
|---|---|
| 0 | Success |
| 2 | invalid_request |
| 3 | not_found |
| 4 | insufficient_role / wrong_confirmation / wrong_environment |
| 5 | idempotency_key_conflict |
| 6 | pending_confirmation_required |
| 7 | capability_unavailable / action_in_progress |
| 8 | store_unavailable / executor_unavailable |
| 9 | Network failure |
| 10 | Missing or unsafe client configuration |
| 1 | Unclassified failure |

mcp --stdio exposes the same tools but calls the configured remote /v2 API over
HTTP. httpx/mcp are imported lazily so base installation commands work without
the api extra.

## Cryptographic configuration

QUADRINGENT_V2_SECRET_KEY (or _FILE) encrypts IBM i passwords, Snowflake private
keys and webhook secrets with Fernet. Missing native key configuration fails
closed; no ephemeral production key is created.

QUADRINGENT_V2_TOKEN_PEPPER (or _FILE) protects agent/activation token hashes and
HMAC session/approval signatures. A direct development app may use a per-process
ephemeral pepper, invalidating sessions after restart; native deployment must use
a persistent declared pepper. Agent tokens have 32 bytes of entropy; password
hashing uses scrypt. Preserve both encryption key and pepper during backup/restore.

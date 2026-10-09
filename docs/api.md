# HTTP API `/v1`

Local cockpit API, UTF-8 JSON. See [security](../SECURITY.md) for identity.
GET routes also accept HEAD. Simple errors use
`{"error":{"code":"..."}}`; evaluation refusals may return a structured verdict.
HTTP 200 never guarantees complete replication.

| Method | Route | Response |
|---|---|---|
| GET | `/healthz` | 200 without data, exempt from authentication |
| GET | `/v1/version` | `version`, `api` |
| GET | `/v1/overview` | Revision, summary, pipelines |
| GET | `/v1/pipelines` | `revision`, `pipelines` |
| GET | `/v1/pipelines/{id}` | `revision`, `pipeline`; 404 when absent |
| GET | `/v1/events` | SSE revision notifications |
| GET | `/v1/onboarding/defaults` | `defaults`, public `site` identity |
| POST | `/v1/onboarding/evaluate` | Declaration verdict, never a network probe |
| GET | `/v1/connections` | Persisted declarations, when a store is configured |
| POST | `/v1/connections` | 201 and `connection`, when a store is configured |
| POST | `/v1/pipelines/{id}/actions/{action}` | Action receipt or refusal |

## Reading and refresh

Projections provide an ETag. `If-None-Match` allows a 304 with no body.
Mutation responses are `no-store`. The projection reports status, freshness,
provenance, counters and evidence; `null` remains unknown.
`observability.checks` contains measurements. `costs` may be absent or contain
a JSON `null` amount even when a price is configured.

`infrastructure_costs` is optional. Without admissible evidence:
`{"status":"unavailable","reason":"cost_evidence_unavailable"}`.
Otherwise it contains `status: "available"`, `collected_at`, `namespace`
and two blocks:

- `storage`: `status: "measured"`, `observed_at`, `bytes`,
  `price_per_gib_month`, `monthly_run_rate`, `currency: "USD"`,
  `basis: "aws_public_standard_first_tier"`;
- `cluster`: `status: "measured"`, `start`, `end`, `allocated_amount`,
  `cluster_amount`, nullable `idle_amount`, configured `currency`,
  `basis: "opencost_no_idle_share"`.

Each block can independently be `{"status":"unavailable"}`. Amounts and bytes
are decimal strings; `"0"` is a measurement. Monthly S3 run rate, daily cluster
allocation and Snowflake credits are not a combined total.
See [FinOps](finops.md) for freshness and scope.

SSE uses `stream.cursor`, `projection.updated`, `projection.reset` according
to retention, with revision `id` and `data: {"revision":N}`. The client then
reloads the HTTP projection; the stream contains no pipelines.
`Last-Event-ID` accepts a nonnegative integer; invalid values yield 400.
Keepalive comments establish no new evidence.

## Declaring a connection

Read `/v1/onboarding/defaults` first. This synthetic example matches the README
local site; browser-declared steps prove no access:

~~~json
{
  "display_name": "Local example",
  "step": "destination",
  "ibmi_host": "ibmi.example.invalid",
  "ibmi_user": "CDCUSER",
  "tls": true,
  "allow_plaintext": false,
  "secret_ref_name": "quadringent-ibmi",
  "secret_ref_key": "password",
  "schema": "SALES",
  "table": "SALE",
  "journal_library": "DEMOLIB",
  "journal_name": "DEMOJRN",
  "snowflake_database": "EXAMPLE_RAW",
  "snowflake_schema": "IBMI_TEST",
  "snowflake_stage": "IBMI_TEST_SALE_EXTERNAL_STAGE"
}
~~~

The stage name is derived from configuration: use `site.snowflake_stage`,
never a guessed name. For multiple tables, send `tables` instead of `table`.
The response contains `connection_id`, timestamp, declared fields and
`lifecycle_state: "declared_not_in_service"`. There is no implicit activation
or connection DELETE route. Repeating POST can create another declaration;
read the list after a lost response.

Codes: 400 invalid body/blocked verdict, 401 missing identity, 403 rejected
group/origin, 404 unavailable route without a store, 503 unavailable store.

## Actions

Closed action set: `prepare`, `start`, `pause`, `resume`, `refresh`.
Read `fleet_runtime.capabilities` when present, otherwise `fleet.capabilities`.
Only `state: "available"` permits a button. The server revalidates before effects.

~~~json
{"fleet_id":"example-corp-test","environment":"test","confirmation":"RESUME EXAMPLE-CORP TEST"}
~~~

Fleet identity and environment come from the site. Exact confirmation:
`ACTION UPPERCASE_SITE_ID FLEET_ENVIRONMENT`; `refresh` requires `null`.
The body has exactly these three fields. The receipt is a Python/JSON object,
represented in the UI by `PipelineActionReceipt`, not a Python class with that
name. Fields are `id`, `action`, `fleet_id`, `environment`, `created_at`,
`state`, and `stages.intent`, `stages.execution`, `stages.observed_effect`.
Each stage has `state`, `code`, `message`. Evaluate all three stages.

Typical codes: 200 receipt, 400 invalid body, 403 incorrect confirmation or
environment, 404 unknown connection/action, 409 concurrency/conflict,
503 unavailable executor/capability or failed audit, 500 invalid internal
result. GET on an action route yields 405.

POST requires `application/json`, Content-Length and an allowed origin.
There is no permissive CORS. Origin guards supplement authentication.
See [audit and recovery](operations.md) before retrying an uncertain result.
Bodies are bounded; unknown fields cannot extend the contract.

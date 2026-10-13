# Local laboratory

A deterministic, synthetic test harness that runs the real frontend and the
real v2 control plane on one machine, with nothing real behind them. It
exists to prove behavior that is hard to reproduce on a real site: refused
credentials, stale evidence, a lost response, a user without rights.

The laboratory is a test tool. It is not in the distributed wheel
(`src/quadringent_lab` is outside `[tool.setuptools.packages.find]`), and its
router is mounted only by the laboratory gateway, never by the product
application. It is not a qualification and certifies nothing about a site.

## What is real and what is replaced

| Real | Replaced, only at the product's injectable boundaries |
| --- | --- |
| `ui/dist` built by `npm --prefix ui run build`, served from the same origin | IBM i network/TLS/authentication probe (`source_probe`) |
| `create_v2_app` routes and services over a throwaway SQLite file | Table discovery (`table_discovery_client`) |
| Sessions, scopes, idempotency, confirmations, audit, redaction | Snowflake verification (`destination_verifier`) |
| Two organizations, `lab` and `lab-other`, on one engine | Pipeline execution (`pipeline_executor`), observation and evidence (`pipeline_observation_provider.evidence`), logs (`log_source`), costs (`costs_provider`) |

No browser response is mocked. Transport faults (a delayed or withheld
response) are injected by the laboratory gateway, in front of the real
application.

Everything is synthetic. Records carry the provenance `lab_fixture` and
evidence is of kind `simulation` by default. A few scenarios inject `live`
(marked below) to exercise the green path of the diagnostics; that is a
laboratory-injected condition, disclosed in the evidence detail, never a
measurement.

## Commands

```sh
npm --prefix ui run build                                  # the laboratory serves ui/dist
python scripts/quadringent_lab.py list                     # the scenario catalog
python scripts/quadringent_lab.py start --scenario environment_ready --port 8877
python scripts/quadringent_lab.py start --port 8877 --detach   # background; returns once ready
python scripts/quadringent_lab.py stop --port 8877         # or Ctrl+C in the foreground
python scripts/quadringent_lab.py status --port 8877

python -m pytest -q tests/test_lab*.py                     # backend assertions, no network
export QUADRINGENT_PLAYWRIGHT_MODULE=/path/to/playwright-core/index.mjs
node ui/scripts/lab-browser-scenarios.mjs --screenshots /tmp/lab-shots   # --skip-slow, --only 11,13, --attach URL
```

Safety properties, each covered by `tests/test_lab_infrastructure.py`:

- binds 127.0.0.1 only and refuses a non-loopback `Host` header;
- the process refuses every non-loopback connection and name resolution, runs
  with its own `HOME`, and drops AWS, Snowflake, Kubernetes, IBM i and
  `QUADRINGENT_*` variables from its environment;
- everything lives in one temporary directory marked `.quadringent-lab`,
  removed on exit; cleanup refuses to delete a directory without the marker.

## Identities

All fictional; the laboratory password is a laboratory-only passphrase that
protects nothing (`quadringent_lab/fixtures.py`).

| Identity | Organization | Role |
| --- | --- | --- |
| `operator@lab.invalid` | `lab` | admin (may operate and administer) |
| `reader@lab.invalid` | `lab` | reader (read scope only) |
| `admin@lab-other.invalid` | `lab-other` | admin of the other organization |

The main organization is served at `/v2`. The other organization is served by
the same engine at `/lab-other/v2` (API only). Identifiers are deterministic
(`src_lab_primary`, `pl_lab_orders`, `pl_lab_customers`, ...) so URLs and
sessions survive a scenario change or a restart.

## Lab-only router

Under `/lab` (never in the product): `GET /lab/identity`, `/lab/state`,
`/lab/scenarios`, `/lab/ledger`; `PUT /lab/scenario {"name": ...}` (resets the
database and injects the scenario), `PUT /lab/conditions`, `PUT|DELETE
/lab/faults`, `POST /lab/reset`, and `POST /lab/shutdown` only when started
with `--allow-shutdown`. The ledger records what crossed each replaced
boundary (never a secret), which is how a test proves an effect happened
exactly once or not at all.

## Matrix

Backend assertions run the real routes through the gateway
(`tests/test_lab_scenarios.py`). Browser checks render the real pages in
Chrome at 1440x900 in the light and dark colour schemes, with keyboard use
and a console/network check, and save screenshots
(`ui/scripts/lab-browser-scenarios.mjs`). The "visible state" column is the
expected state; it is asserted in the browser only where a browser check is
listed. Diagnostics assertions use `GET /v2/diagnostics/report` and
`/v2/diagnostics/report/export`.

| # | Scenario | Injected condition | Backend result | Visible state | Action allowed or forbidden | Assertion |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `environment_ready` (injected live) | Probe ok, destination verifier ok, tables ready, evidence injected as live (laboratory condition). | Diagnostics overall status ready; every required result passed. | Diagnostics screen shows ready; evidence labelled live. | All operator actions. | `test_scenario_01_environment_ready`; browser check 1 |
| 2 | `tool_missing` | A tool such as java or kubectl is not installed on the operator machine. | Not a server condition: the control plane never inspects local tools. | Reported by the local `quadringent doctor` report. | Fix the machine, then rerun the doctor. | CLI tests (`tests/test_doctor*.py`) |
| 3 | `tcp_unreachable` | Source probe reports the network step failed. | POST sources/test returns reachable=false, network failed, TLS and authentication not attempted. | Network step failed; source authentication stays unknown. | Test again after fixing the route; table discovery is not offered as proven. | `test_scenario_03_tcp_unreachable` |
| 4 | `tls_invalid` | Source probe reports an untrusted certificate chain. | reachable=false, tls.ok=false, trust=unknown with the measured fingerprint; nothing is persisted. | TLS step failed with a fingerprint to compare; source TLS stays unknown. | Pin the certificate explicitly; never trusted silently. | `test_scenario_04_tls_invalid` |
| 5 | `auth_denied` | Source probe reports TLS ok and authentication refused. | reachable=false, authentication.ok=false; version and fingerprint are not persisted. | Authentication step failed; source authentication stays unknown. | Correct the credentials and test again. | `test_scenario_05_auth_denied` |
| 6 | `journal_absent` | Discovery returns a table that is not journaled. | Table readiness not_journaled with CL commands to fix it. | Table shown as not ready with its fix commands on the Tables screen; diagnostics only cover tables that feed a pipeline. | Read-only; the service does not gate the start of a pipeline (see limits). | `test_scenario_06_journal_absent` |
| 7 | `journal_config_incompatible` | Discovery returns a journaled table with after-images only. | Table readiness images_incomplete with the CHGJRNOBJ fix command. | Table shown as not ready (before images missing) on the Tables screen; not in diagnostics until selected. | Read-only; the service does not gate the start of a pipeline (see limits). | `test_scenario_07_journal_config_incompatible` |
| 8 | `no_usable_key` | Discovery returns a journaled table with no primary key or unique index. | Readiness no_key; choosing RRN without acknowledgement is refused; with acknowledgement it becomes ready. | Table needs a key decision before it is ready. | Choose a key or explicitly acknowledge RRN identification. | `test_scenario_08_no_usable_key` |
| 9 | `destination_privileges` | Destination verifier reports the role cannot create tables. | Verify returns verified=false with the failing step; state becomes failed. | Destination shown as failed with the step that failed. | Fix grants and verify again; the service does not gate the start of a pipeline (see limits). | `test_scenario_09_destination_privileges` |
| 10 | `control_plane_down` | The lab process is stopped: the port is closed. | No answer: connection refused. | Cockpit shows `Service unavailable`. | Refresh retries; no action is offered as done. | `test_scenario_10_control_plane_down`; browser check 10 |
| 11 | `read_without_delivery` (injected live) | Evidence: reading observed (injected live), delivery and reconciliation absent. | Capture reading passed; delivery and reconciliation unknown; overall incomplete. | Delivery and reconciliation shown as not measured. | All operator actions; no claim of end-to-end delivery. | `test_scenario_11_read_without_delivery`; browser check 11 |
| 12 | `evidence_absent` | The observation provider returns no evidence. | Evidence kind absent; every stage absent; diagnostics unknown, never passed. | `No evidence collected`; nothing inferred from the declared state. | All operator actions. | `test_scenario_12_evidence_absent`; browser check 12 |
| 13 | `evidence_stale` (injected live) | Evidence: delivery and reconciliation observed three hours ago (injected live). | Delivery stage stale; diagnostics delivery failed with a since timestamp; overall blocked. | Delivery shown as stale with its observation date. | All operator actions; refresh does not hide staleness. | `test_scenario_13_evidence_stale`; browser check 13 |
| 14 | `late_old_pipeline_response` | The evidence read of pipeline A is delayed 3 s at the transport level. | Both responses complete; each payload names its own pipeline. | After navigating to pipeline B, the late answer of A never replaces B on screen. | Navigation stays possible while a read is pending. | `test_scenario_14_late_old_pipeline_response`; browser check 14 |
| 15 | `action_confirmed_reread` | Executor accepts; a sensitive action goes through the confirmation flow. | 409 pending confirmation, approve, execute, then GET returns the new state; executor ran once. | Pending confirmation listed; after execution the new state is read back. | Approve or reject the pending confirmation; no unconfirmed sensitive action. | `test_scenario_15_action_confirmed_reread`; browser check 15 |
| 16 | `timeout_effect_unknown` | The pause request executes, but its response is withheld for 4 s (client times out). | Client timeout; GET shows the effect applied; replay with the same key runs the executor once. | Result reported as unknown until the state is read back; no second mutation. | Read back the state; replay only with the same idempotency key. | `test_scenario_16_timeout_effect_unknown`; browser check 16 |
| 17 | `reader_without_rights` | A reader signs in (read scope only). | Reads allowed; actions, confirmations and audit refused with 403 insufficient_role. | Read-only cockpit; mutations are refused by the service. | Read only. | `test_scenario_17_reader_without_rights` |
| 18 | `cross_organization` | Two organizations share one engine; the other organization's user reads. | Destinations, users, confirmations isolated; other organization's session refused. See limits for sources/pipelines. | Nothing of the other organization is visible. | Read own organization only. | `test_scenario_18_cross_organization` |
| 19 | `export_canaries` | Seed carries fictional canaries in host, account, secrets, logs, evidence and table comment. | Diagnostic export contains none of the canaries; the preview lists what was omitted or pseudonymized. | Export preview shown before download. | Download only after the preview. | `test_scenario_19_export_canaries`; browser check 19 |
| 20 | `resume_after_refresh` | A pending confirmation exists; the browser is reloaded and the service restarted over the same file. | Pending confirmation and pipeline state survive a runtime restart. | After reload the same screen and the pending confirmation are shown again. | Continue the pending confirmation. | `test_scenario_20_resume_after_refresh`; browser check 20 |

Scenarios 3 to 5 are also represented by the local `quadringent doctor`
report, owned by the CLI tests. Scenario 2 cannot exist server side: the
control plane never inspects local tools (the test asserts its diagnostics
contain no `local_tools` layer).

Scenario 0, `baseline`, is the default start state and is not part of the
matrix: simulation evidence, nothing verified, nothing live.

Scenario 10 is checked in the browser by starting a separate laboratory
process, stopping it, and refreshing: the port is really closed. Scenario 16
withholds the pause response for 33 s in the browser (longer than the UI's 30 s
action timeout), so it is skipped by `--skip-slow`.

## Limits and defects observed

These are what the laboratory showed about the product, not about the
laboratory. They are reported, not worked around.

1. **Organization isolation is incomplete.** With two organizations on one
   engine, destinations, users, confirmations and audit are isolated, and a
   session never crosses organizations. Sources, tables and pipelines are not:
   the `pipelines` table has no `org_id` and `PipelinesService`, `TablesService`
   and `SourcesService` do not filter by organization. The other organization's
   user lists the main organization's pipelines and sources (the cockpit home
   shows both connections) and can run actions on its pipelines. Starting a
   pipeline without `destination_id` also considers destinations of every
   organization. Four tests are marked `xfail(strict=True)` and flip to failures
   once this is fixed. `/v2/diagnostics/report` is scoped by organization and is
   asserted strictly.
2. **No readiness gate on start.** `POST /v2/tables/{id}/pipeline` starts a
   pipeline for a table that is not journaled, and the executor is invoked.
   Scenarios 6 to 9 therefore describe what the UI must withhold, not what the
   service refuses.
3. **Evidence detail is not redacted.** `GET /v2/pipelines/{id}/evidence`
   returns the provider's `detail` text unchanged; a provider that includes a
   secret-shaped value or a row would expose it. The diagnostic export removes
   free-text evidence and is asserted free of canaries; the logs route redacts.
4. **Journal mismatch** (several tables in different journals) is a property of
   a selection (`classify_selection`), not of one refreshed table, so scenario
   7 uses the stored `images_incomplete` state.

Not covered: real IBM i, Snowflake, Kubernetes or cloud behavior; SSE event
delivery under faults; mobile layouts; any claim about a real site.

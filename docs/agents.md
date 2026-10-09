# Connect an agent

Quadringent exposes the same control-plane tools through MCP and its CLI.
See [API v2](api-v2.md) for the complete contract. Use a token issued for the
installation and the narrowest required scope.

## Transports

Clients with Streamable HTTP support can connect directly to `/mcp`:

```json
{
  "mcpServers": {
    "quadringent": {
      "url": "https://control-plane.example.test/mcp",
      "headers": {"Authorization": "Bearer qdt_op_..."}
    }
  }
}
```

Client configuration syntax varies. For a client that launches local stdio
servers, install the `api` extra and use the CLI bridge:

```json
{
  "mcpServers": {
    "quadringent": {
      "command": "quadringent",
      "args": ["mcp", "--stdio"],
      "env": {
        "QUADRINGENT_URL": "https://control-plane.example.test",
        "QUADRINGENT_TOKEN": "qdt_op_..."
      }
    }
  }
}
```

Keep real tokens in the client's protected secret configuration. The examples
are placeholders. Both transports expose the same tools and resources.

## Agent tokens

Tokens authenticate agents independently of human sessions. Scopes are cumulative:

| Scope | Access |
|---|---|
| `read` | Read sources, tables, pipelines, confirmations, costs and audit where permitted |
| `operate` | Also perform ordinary operations and request sensitive operations |
| `admin` | Also manage users, agent tokens and webhooks |

A `source_restriction` limits access to the listed sources. Access outside that
restriction returns `403 wrong_environment`. An administrator can explicitly
configure `pre_authorized_actions`; the agent cannot extend that list.

An existing authorized administrator can create a token:

```sh
quadringent tokens create --name agent-onboarding --scope operate \
  --source-restriction src_abc123 --never-expires
```

The plaintext value is displayed once. Subsequent reads return metadata, not the
token. Prefer a bounded expiry for temporary qualification work.

## Discovery and operation

Start with `list_sources`, `test_source` and `list_tables`. Table readiness
distinguishes `ready`, `not_journaled`, `images_incomplete` and `no_key`.
Refresh a stale catalog with `refresh_tables`, then choose an explicit key with
`choose_table_key`. Inspect pipeline state and observed evidence separately.

Pipeline, source and destination pause/resume tools are available according to
scope and service capability. A pause of one pipeline may leave its shared reader
running for other tables. Inspect the effective scope before operating.

Use `dry_run=true` first for a mutation. Its `would_transition` or
`would_apply` response describes the plan without persisting its effect.
`get_costs` returns `{"status":"absent"}` when no v2 cost service is wired;
absence is not zero spending.

## Sensitive actions

`restart_initial_copy`, `replay_journal_range`, `remove_table`, `pause_all`
and `resume_all` require a valid confirmation. A request without one returns:

```json
{
  "status": "pending_confirmation",
  "confirmation_id": "conf_xyz789",
  "approve_url": "/v2/confirmations/conf_xyz789/approve"
}
```

The pending response does not execute the operation. An authorized human approves
the confirmation in the cockpit or with:

```sh
quadringent confirmations approve conf_xyz789
```

The agent then repeats the original operation with
`confirmation_token="conf_xyz789"` (CLI: `--confirmation-token conf_xyz789`).
Successful execution consumes the confirmation. Approval binds the exact action
and target; it does not authorize a different request.

An agent may approve its own request only when an administrator explicitly
pre-authorized that exact `action_ref` on its token. HTTP exposure of a replay
action does not establish that a replay executor is wired for the installation;
check the action's runtime availability.

## Audit

MCP calls record `actor_kind`, token identity, `mcp_client` client metadata,
`dry_run`, status and relevant before/after state in `audit_records`.
Direct REST/CLI calls have no MCP client metadata. A human approval and the
subsequent agent execution retain their distinct actors.

```sh
quadringent audit tail --actor-kind agent --limit 50
```

Access remains subject to the endpoint's scope checks. The complete confirmation
sequence is covered by `tests/test_v2_mcp_e2e_onboarding.py`.

## Installation recovery token

Administrators with Kubernetes `pods/exec` access can issue a token from the
control-plane Pod for recovery or qualification:

```sh
quadringent agent-token --name example --namespace example-dev \
  --label qualification --scope operate --days 7 \
  --write-config ~/.config/quadringent/agent.json
```

That Kubernetes permission already exposes the database, encryption key and
token pepper. Reserve it for installation administrators. The command permits
1–90 days (default 30), writes the token only to a `0600` file, records
`agent_token.create` under actor `kubectl-exec`, and produces a normally
revocable token.

# Security and exposure limits

## Authentication

By default the v1 cockpit listens on `127.0.0.1:8844` and **has no application
authentication of its own**. The v2 control plane requires an application
session: the first administrator activates the account with a one-time link,
then signs in. The chart adds neither a Service nor an Ingress for the cockpit.
Keep port-forwarding private; do not expose v1 through a simple public tunnel.
Kubernetes RBAC protects opening a port-forward, not requests from someone
who can already reach the local port.

Optional authentication delegates identity to a trusted OIDC proxy. Configure
`controlPlane.auth.enabled`, `userHeader`, `groupsHeader`,
`operatorGroups`/`adminGroups` and the `proxySecret` reference. The proxy must
overwrite incoming headers and add `x-quadringent-proxy-secret`. An identity
without an operator group can read but cannot write. Provide the shared secret
through `QUADRINGENT_AUTH_PROXY_SECRET`, never through a process argument.

The backend permits a network bind only with authentication configured.
This configuration neither creates the proxy nor proves its isolation.
**Without a shared secret and network restrictions, forged headers can
impersonate an identity.** The site deployment must enforce these protections;
Helm does not create them automatically.

## Flows and network

| Origin → destination | Port / transport | Purpose |
|---|---|---|
| Browser → loopback or proxy | Local 8844; site-specific proxy HTTPS | UI, API, SSE |
| Reader/probes → IBM i | Configured TLS ports, usually 9471/9475/9476 | JDBC, sign-on, command |
| Capture → S3, DynamoDB, STS | HTTPS 443 | Raw data, evidence, checkpoint, AWS identity |
| Verifier → Snowflake, CloudWatch, S3 | HTTPS 443 | Bounded measurements and evidence publication |
| Cost collector → CloudWatch and public AWS catalog | HTTPS 443 | S3 Standard volume and regional rate |
| Cost collector → OpenCost | Configured service port, usually 9003 | Namespace allocation and cluster assets |
| Control plane → S3 | HTTPS 443 | Read permitted evidence keys |
| Control plane → Kubernetes API | Cluster HTTPS | Jobs `create/get/patch` |
| Jobs / name resolution | DNS TCP/UDP 53, according to the cluster | Service resolution |

The chart provides optional NetworkPolicies, disabled by default
(`networkPolicy.enabled=false`). The general policy selects release pods,
limits ingress to the control plane port when listening outside loopback,
and allows all egress. With bundled PostgreSQL, an additional policy limits
PostgreSQL ingress to the control plane and, when backup is enabled, the
backup pod from the same release.

These rules require a CNI that enforces NetworkPolicies; they do not establish
complete network isolation. DNS, private endpoints, proxies and egress filters
are site-specific. The owner validates these rules before shared exposure.
Telemetry is disabled by default; its opt-in and payload are documented in
the [telemetry guide](docs/product/telemetry.md).

## TLS, identities and secrets

IBM i TLS is mandatory; plaintext is rejected. The site supplies its CA through
a read-only Secret mount; images contain no customer certificate authority.
Verify the fingerprint, chain, validity and expected names out of band.
A `subPath` mount requires a controlled restart to apply certificate rotation.

Required secrets are listed in [Installation](docs/product/install-client.md).
Passwords, license tokens, private keys and cloud credentials must never enter
Git, URLs, process arguments or logs. Secret references are metadata; they
do not prove that a secret exists. The community edition uses no commercial
license token.

Bounded exception: the initial activation link contains a token in
`#/wizard/activate?token=…`. The fragment is not sent to the server in the HTTP
request, but the link remains sensitive in the clipboard, local history and
installer output. The browser removes the token from the address bar after
reading it. Protect installation output and the site's private directory;
never attach the link to a ticket.

Secrets provided once are excluded from persisted idempotent responses and
audit records. A replay always checks current identity and permissions; it
does not retrieve a lost key or link. Reissuing the initial link is limited to
the first administrator, while still inactive, through an authorized admin
operator. See the [API contract](docs/api-v2.md).

When upgrading an old database, stop old control plane processes before
applying `0015_one_time_secrets`. This migration removes secrets from historical
JSON without changing encrypted secrets or authentication hashes. It does not
purge backups, WAL or previously exported copies. Keep those media protected
as sensitive data. Migrate a restored old backup before serving the API.
Rollback to an old binary capable of writing these secrets again is excluded.

The v1 fleet role permits namespace Jobs `create`, `get` and `patch`; its client
limits PATCH to `spec.suspend`. Enabling the v2 control plane adds two roles:
diagnostic Jobs and Secrets, pod and pod-log reads, and reader/loader Deployment
creation, updates and deletion. The v2 role can also create and modify source
and destination Secrets.

These permissions cover the **entire namespace**. Reserve a dedicated
Quadringent namespace and restrict access to the control plane service account.
Per-table filtering of API logs does not narrow its Kubernetes identity's read
permissions. Cloud permissions for the control plane, capture and verifier
are separate. Cost Explorer is not used.

The cost collector is a separate process installed with the Python package.
It uses an AWS identity allowed `cloudwatch:GetMetricStatistics` and a
site-provided OpenCost endpoint. This integration creates no extra role,
scheduler or permission. The control plane reads only its local file.
The atomic file is private (0600); collection failures expose neither provider
responses nor credentials. See [FinOps](docs/finops.md) for the scope.

Job templates remain trusted artifacts administered by the site. Application
validation does not replace cluster admission policies.

## Data, retention and GDPR

Business rows pass through S3 and Snowflake and may contain personal data.
The data controller decides minimization, legal basis, regions, encryption/KMS,
access, retention and erasure procedures. Quadringent does not automatically
anonymize replicated rows.

Define S3 retention rules covering object versions and incomplete uploads,
log and backup retention, and Snowflake policy, including Time Travel and
Fail-safe. A source delete does not immediately erase raw data or backups.
`helm uninstall` does not remove those copies. Erasure requires an inventory
and owner authorization, with separate evidence for each storage system.

## Reporting a vulnerability

Private vulnerability reporting is enabled for this repository:
[report a vulnerability privately](https://github.com/quadringent/quadringent-ce/security/advisories/new)
(a GitHub account is required). Never describe a vulnerability, exploit,
secret or data in a public issue, discussion or pilot request.

If you cannot use GitHub, email quadringent@proton.me to ask for a private
channel, without vulnerability details, exploit code, secrets or data in that
first message. No response time is promised.

## Release checks

Source, history and release artifacts undergo Gitleaks and private-identity
checks before public exposure. Private evidence and site credentials are not
distributed. Pages publication requires a separate explicit action.

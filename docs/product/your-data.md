# Your data: where it goes and how to leave

Your data travels from the IBM i, through a reader and a bucket in your cloud
account, to your Snowflake account. The sections below follow that path, then
list every outbound connection the code in this repository makes, where the
secrets live, and how to stop, uninstall and keep what you have. Quadringent is a
DEV/INT preview: the chart accepts only the environments `dev`, `int`, `test` and
`staging`, and no production qualification is claimed ([evidence](../evidence.md),
[installation status](install-default.md)).
<!-- source: chart/templates/_helpers.tpl, chart/values.yaml -->

What the software does on the IBM i itself is on
[What Quadringent does on your IBM i](ibm-i-footprint.md). To confirm that what
you run matches the published code, see [Verify a release](verify-a-release.md).

## The data path

```text
IBM i  ->  reader in your cloud  ->  raw storage in your account  ->  loader  ->  Snowflake in your account
```

1. A Java process in a container reads journal entries from the IBM i and, for
   the first copy, table rows, over TLS. Row images and table contents
   are your business data. A checkpoint is the journal position that the reader
   has stored safely.
2. The reader writes batches as JSON Lines files with
   a JSON manifest (format `as400-raw-v1`, with the SHA-256 of the payload) to an
   S3 bucket (AWS) or a GCS bucket (GCP), then advances the checkpoint in
   DynamoDB (AWS) or in the same GCS bucket (GCP). The batch is durable before
   the checkpoint moves.
3. Loaders running in your cluster read the raw
   batches and write history and mirror tables in the Snowflake account you
   declare, with a service user that authenticates with a key pair.
4. A verifier reads back Snowflake and raw storage and records
   bounded measurements. See [delivery evidence](quadringent-destination-proof-contract.md).

<!-- source: docs/architecture.md, java/src/main/java/io/quadringent/as400/RawCaptureWriter.java, docs/product/snowflake-destination.md, docs/product/compatibility.md -->

The reader, the loaders and the control plane run in the Kubernetes cluster or
k3s VM in your AWS or GCP account. The control plane keeps its configuration, its
audit records and encrypted connection secrets in a PostgreSQL database that, by
default, runs in that cluster; an external database is an expert option
(`src/quadringent_control_plane/v2/schema.py`, [installation](install-default.md)).

The raw bucket created by the AWS module uses SSE-KMS (server-side encryption with a KMS key),
blocks public access and enables versioning by default; the GCS bucket created
by the GCP module enforces public access prevention and uniform bucket-level
access. A bucket that you reuse keeps its owner's configuration, which the
installer does not change. Encryption of Kubernetes Secrets at rest depends on
your cluster; this repository does not configure it.
<!-- source: deploy/terraform/aws/base/main.tf, deploy/terraform/gcp/base/main.tf -->

## What leaves your accounts

The running software sends no data to a project-run endpoint unless you turn on
the opt-in telemetry; it is off by default. Downloading a release or pulling an
image contacts the registry or mirror that you choose, which is a separate
matter from the data path.

- `controlPlane.telemetry.enabled` is `false` in the chart. With it unset, the
  chart does not even set the `QUADRINGENT_TELEMETRY` variable.
- In the code, `TelemetryConfig.enabled` defaults to `False`, and it becomes
  `True` only when `QUADRINGENT_TELEMETRY` (or the `--telemetry` option) is one
  of `1`, `true`, `on`, `yes`. Without it `maybe_start` returns nothing and no
  thread is started.
- When enabled, one HTTPS POST per day with these fields only: schema version,
  a SHA-256 hash of a random local identifier, the product version, the tier
  (always `community`), the number of tables in the fleet catalog file (0 when
  none is configured, which is the case in a default cockpit install) and a
  timestamp. If you
  give no endpoint, the code uses `https://telemetry.quadringent.io/v1/ping`; you
  can set your own HTTPS collector instead. An endpoint that is not HTTPS is
  replaced by the default one.
- The exact payload and its limits are in [telemetry](telemetry.md).
- Other files with "telemetry" in their name (`slo_telemetry.py`,
  `loader_telemetry.py`) read your own cluster and write local observations;
  they do not use the telemetry endpoint.

<!-- source: src/quadringent_control_plane/telemetry.py, src/quadringent_control_plane/cli.py, chart/values.yaml, chart/templates/control-plane.yaml, docs/product/telemetry.md -->

Data that you send yourself is your choice: a diagnostic export written by
`quadringent doctor --export` stays on your disk with private permissions, and
the command sends nothing
([operator guide](operator-guide.md#2-preflight-and-diagnosis)).

## Outbound connections

These are the connections that the code makes, from where, and whether they are
always on. DNS and in-cluster traffic are not listed.

| From | To | When | Purpose | Source |
|---|---|---|---|---|
| Reader, probes, jobs | Your IBM i, TLS ports 9471, 9475, 9476 (and 449 unless every port is set) | Always, once a source is declared | Journal, catalog and table reads | `src/quadringent/diagnostics/network.py` |
| Reader and capture | S3, DynamoDB and STS (AWS) or GCS (GCP), in your account | Always | Raw batches, evidence, checkpoint | `docs/architecture.md`, `SECURITY.md` |
| Loader and verifier | Snowflake account that you declare, HTTPS 443 | Always, once a destination is declared | History and mirror writes, readback | `docs/architecture.md`, `src/quadringent_control_plane/v2/services/destination_verifier.py` |
| Control plane | Kubernetes API of your cluster | Always | Jobs, and with the v2 control plane also Deployments, Secrets, pods and pod logs, across the whole release namespace (access list in `SECURITY.md`) | `docs/architecture.md`, `SECURITY.md` |
| Control plane | Raw storage | Always | Read permitted evidence keys | `SECURITY.md` |
| Control plane | The `http`, `https`, `file` or `s3` document origins that the operator configures (console, proof and fleet documents) | When such an origin is configured | Read JSON documents shown in the cockpit | `src/quadringent_control_plane/repository.py` |
| `quadringent` commands, doctor and the MCP bridge on your machine | The control-plane API URL that you give | When you run them | Operate your own control plane | `src/quadringent/installer/api_client.py` |
| Installer on your machine | Cloud APIs through Terraform, Helm and kubectl | At install and upgrade | Provision and deploy | `docs/product/install-default.md` |
| Your cluster or VM | Container registry that hosts the images (`--image-repository` selects your own mirror); for a VM, the download of k3s (stated for the GCP VM) | At install and pod start | Pull digest-pinned images, install k3s | `docs/product/install-default.md` |
| Control plane | The telemetry endpoint | Only if you enable telemetry | Adoption count | `src/quadringent_control_plane/telemetry.py` |
| Alert transport | The webhook URL or SNS topic you configure | Only if configured | Alert metadata, no business rows | `src/quadringent/alert_transport.py` |
| Cost collector (optional command) | CloudWatch, the public AWS price list, and OpenCost if you give its URL | Only when you run it | Cost observations | `docs/architecture.md`, `src/quadringent/cost_collect.py` |

Webhooks: the control plane stores webhook declarations and defines a signed
delivery worker, but this repository does not contain an HTTP client wired to
that worker (`WebhookHttpClient` is an interface only). Delivery to a declared
URL is therefore Not tested and is not listed above.
<!-- source: src/quadringent_control_plane/v2/services/webhooks.py -->

What is machine-checked: `tests/test_trust_claims.py` lists the files of this
repository that open an HTTP connection through the standard library or `httpx`,
and the host names written as URLs in the Python code, and fails when a new one
appears without a decision. It does not scan the connections that third-party
libraries make on their own (the Snowflake connector and SDKs, the cloud SDKs,
JTOpen), and this list does not enumerate them. The chart's NetworkPolicy is off by default and, when on, allows
all egress; filter egress at your cluster if you need a closed list
([security](../../SECURITY.md)).

## Where the secrets live

| Secret | Where | Source |
|---|---|---|
| IBM i password | Encrypted (Fernet) in the control plane database; copied to a Kubernetes Secret `qdt-source-` plus the source id, injected as the environment variable `ISERIES_PASSWORD`. With Helm values: a Secret you create. | `src/quadringent_control_plane/v2/executor/secrets_provisioner.py`, `docs/product/install-client.md` |
| IBM i CA certificate (public, not secret) | A Kubernetes Secret mounted read-only, if you pin a private CA | `src/quadringent_control_plane/v2/executor/manifests.py` |
| Snowflake private key, user, role, account | Encrypted (Fernet) in the control plane database; copied to a Kubernetes Secret `qdt-destination-` plus the destination id. Authentication is by key pair, no password. | `src/quadringent_control_plane/v2/executor/secrets_provisioner.py`, `docs/product/compatibility.md` |
| Fernet key (the symmetric key that encrypts the stored secrets) and token pepper (a server-side secret for agent tokens) | A Kubernetes Secret generated once by the chart and kept across upgrades | `chart/templates/control-plane-v2-secrets.yaml` |
| Cloud access | An identity bound to the workload (instance profile, IRSA, the AWS mechanism for pods, or Workload Identity, the GCP equivalent); the Terraform modules create no access key | `docs/product/install-default.md`, `deploy/terraform/aws/base/main.tf`, `deploy/terraform/gcp/base/main.tf` |
| Telemetry install identifier | A random value in a local file with mode 0600, only if telemetry is enabled | `src/quadringent_control_plane/telemetry.py` |

The secrets are not written to logs by the provisioner, and the IBM i password is
never a command argument. If you lose the Fernet key, the stored secrets cannot
be decrypted; keep it with your backups
([backup and restore](backup-restore.md)).

## How to stop

1. In the cockpit, pause the tables and confirm that no reader Job remains
   ([operator guide, section 8](operator-guide.md#8-stop-and-uninstall)). A pause
   keeps the durable checkpoint.
2. To cut access at the source, disable the user profile on the IBM i. The
   reader classifies a disabled profile as an authentication failure, blocks
   further sign-on attempts and waits for an operator reset.
3. To stop the loaders from writing, revoke or disable the Snowflake service
   user.

<!-- source: docs/product/operator-guide.md, src/quadringent/java_worker.py -->

## How to uninstall

`quadringent uninstall` removes only the Helm release. Terraform resources, raw
data, checkpoints, identities and your Snowflake objects remain until you decide
to remove them. Removal of the Terraform resources is a separate, reviewed
`terraform destroy`, after you have retained the evidence and backups that you
need. The steps are in [uninstall](install-default.md#uninstall) and, for Helm
sites, in [Prove and remove](install-client.md#prove-and-remove).

Secrets also remain in the cluster after the Helm release is removed. This
repository has no command that deletes them:

- `qdt-source-<id>` holds the IBM i password, one Secret per source. The control
  plane creates it through the Kubernetes API without an owner reference and the
  code has no delete path for it.
- `qdt-destination-<id>` holds the Snowflake private key, one Secret per
  destination, created the same way.
- `<release>-v2-secrets` holds the Fernet key and the token pepper, and the
  PostgreSQL credential Secret holds the database password. Both carry the
  Helm annotation `helm.sh/resource-policy: keep`, so Helm leaves them.
- The PostgreSQL volume claim holds the encrypted copies of the connection
  secrets. Whether it survives depends on your storage class and on how you
  remove the namespace; Not tested.

Delete these objects yourself with `kubectl delete` once you no longer need the
backups they unlock, and disable the IBM i profile and revoke the Snowflake key
as described under "How to stop".
<!-- source: chart/templates/control-plane-v2-secrets.yaml, chart/templates/postgres.yaml, src/quadringent_control_plane/v2/executor/secrets_provisioner.py, docs/product/install-default.md -->

## How to keep your data

- Snowflake: the history and mirror tables are tables in your account. They
  stay after uninstall and need no Quadringent component to be read. Column
  types and technical columns are listed in
  [Snowflake history and mirror](snowflake-destination.md).
- Raw storage: the batches are JSON Lines files with JSON manifests in your
  bucket and can be read with ordinary tools. The manifest holds the event count
  and the payload SHA-256.
- Configuration: pipelines, tables and audit records are in the PostgreSQL
  database; the backup procedure is in [backup and restore](backup-restore.md).

<!-- source: docs/product/install-default.md, docs/product/snowflake-destination.md, java/src/main/java/io/quadringent/as400/RawCaptureWriter.java -->

## Why the license lets you keep running it

The CDC engine, the API, the cockpit, the chart and the documentation are
licensed under [Apache-2.0](../../LICENSE).

- Apache-2.0 allows use, modification and redistribution, including commercial
  redistribution. Versions already released under Apache-2.0 keep the rights
  granted.
- The licensing page states that there is no activation key, commercial table
  limit or expiry. The code does not read a license key (a test checks this),
  and the chart rejects an obsolete license key setting instead of checking one.
- Apache-2.0 gives no trademark rights.
- Third-party components keep their own licenses
  ([notices](../../THIRD_PARTY_NOTICES.md)).

The project model is in [Community edition and project model](../licensing.md).
<!-- source: LICENSE, docs/licensing.md, chart/templates/control-plane.yaml, THIRD_PARTY_NOTICES.md -->

What would be left if we stopped publishing is on the
[governance](../governance.md) page.

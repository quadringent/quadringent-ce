# Operator guide

A short path through a DEV/INT installation, from the first install to a
controlled stop. Each step links to the guide that holds the details. Nothing
here qualifies production; a ready pod, a reachable API or an accepted action
does not prove delivery.

## 1. First installation

Check [compatibility and prerequisites](compatibility.md) first. Download the
wheel, sdist and release manifest from the same release, verify their hashes,
then run the installer with `--dry-run` and review the plan, cloud identity and
kubectl context before applying. See [installation](install-default.md).
With `--admin-email`, the installer prints a single-use activation link: open
it through the private tunnel it describes and choose the administrator
password.

## 2. Preflight and diagnosis

`quadringent doctor` checks what this machine can see and says what blocks:

~~~sh
quadringent doctor --cloud aws --target vm
quadringent doctor --check-ibmi <host> --tls --ibmi-ca-file <ca.pem>
quadringent doctor --control-plane --json
~~~

Local tools and credential configuration always run; network calls happen
only with `--check-ibmi` (TCP to the database, sign-on and command servers and,
unless every port is set explicitly, the 449 server mapper), `--tls` (a TLS
handshake that always verifies the certificate and host name) and
`--control-plane` (reads `GET /v2/diagnostics` through the tunnel). The doctor
never signs on to IBM i, never connects to Snowflake and never changes anything.

| State | Meaning |
|---|---|
| passed | Observed in this run, within the limits printed next to it |
| failed | Established problem, with the next action to take |
| unknown | Not observed: configuration present is not authentication, absent evidence is not success |
| skipped | Not run, for example the control plane without `--control-plane` |

Synthetic or historical evidence is never `passed`. Exit codes: 0 ready,
1 blocked, 2 usage error, 3 incomplete; without `--control-plane` a report is
at best incomplete. `--from-report FILE` re-reads a report saved with
`--save FILE` offline. `--export FILE.tar.gz --preview` shows what a shareable
archive would hold; without `--preview` it writes it with private permissions.
Hosts, accounts and object names are pseudonymized, secrets are removed and
nothing is sent. The cockpit shows the same results under **Diagnostics**.

In the wizard, **Test** probes the IBM i source when the installation has a
source probe; otherwise it says that only password storage was verified. On the
Snowflake step, run the generated script with an administrator role, then
**Verify access**, which creates and removes a test table and may resume the
warehouse. Fix the failing step rather than retrying. The chart preflight can
write storage probe objects and needs site authorization
([Helm guide](install-client.md#declare-and-commission)).

## 3. First copy

Refresh the table catalog in the wizard. Only tables shown as `ready`
(journaled with `*BOTH` images and a primary or unique key) replicate updates
and deletes correctly; the wizard shows the CL commands an IBM i administrator
can run for the others ([table readiness](install-client.md#discover-and-prepare-tables)).
Start with two authorized tables. The cockpit shows **Copying** until the
initial copy completes; capture enabled does not mean Snowflake is up to date.

## 4. Read the evidence

Open a table, then **Evidence**. Journal capture, Snowflake delivery and
destination reconciliation are separate stages, each with its own state:

| State | Meaning |
|---|---|
| Observed | A current observation from this installation, with its time |
| Stale | The last observation is old; the current state is unknown |
| Synthetic or Historical | Demonstration data or an archived observation, never a current measurement |
| Not measured | Nothing was measured; no time or count is shown |

Provenance, collection time and producer version come from the service. A
producer version that the service does not report stays "Not reported by the
service". See the [evidence contract](quadringent-destination-proof-contract.md).

## 5. Outage

When a table shows **Attention**, open **Logs** and filter on incident-correlated
entries. Read the reader state before acting: `STOPPED_FAIL_CLOSED` points to
TLS or the IBM i account, `STOPPED_PROOF_CHAIN` to incomplete continuity,
`STOPPED_CONFIG_BLOCKED` to configuration or clock. The
[on-call runbook](oncall-runbook.md) lists the first reads for each case.
Keep one reader per journal; never start a second reader to "help".

## 6. Recovery

Pause and resume keep the durable checkpoint. A resume is offered only when the
service reports it as available: valid checkpoint, proven continuity, a recent
catalog probe and no remaining reader Job. After a resume, wait for new
delivery evidence; a relaunched reader is not yet a delivery. Sensitive actions
(remove a table, restart the initial copy, replay a range) need an approved
confirmation and show their effect read back afterwards. See
[operations](../operations.md).

## 7. Backup and restore

Before any upgrade or rebuild, keep the release, digests and effective values,
preserve the original `SECRET_KEY` and `TOKEN_PEPPER` values outside the
cluster, and enable the PostgreSQL backup CronJob, which checks each archive
before reporting success. Restore into a new isolated database; fresh secret
values cannot decrypt a restored database. See
[backup, restore and rollback](backup-restore.md).

## 8. Stop and uninstall

Pause the tables, confirm that no reader Job remains, then run
`quadringent uninstall`, which removes only the Helm release. Raw data,
checkpoints, Snowflake objects, identities and Terraform resources remain until
their owner decides to delete them. See [uninstall](install-default.md#uninstall).

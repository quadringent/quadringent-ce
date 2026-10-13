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

## 2. Preflight

`quadringent install ... --check-ibmi <host>` tests TCP reachability of the
IBM i host servers (9471, 9476, 9475) without changing anything; it does not
perform a TLS handshake. In the
wizard, **Test** probes the IBM i source when the installation has a source
probe; otherwise it says that only password storage was verified. On the
Snowflake step, run the generated script with an administrator role, then
**Verify access**, which creates and removes a test table and may resume the
warehouse. Fix the failing step rather than retrying. The chart preflight can write storage
probe objects and needs site authorization
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

This export is available in source builds; published v0.4.2 packages do not
include it. An authenticated organization administrator can select **Download diagnostic**
in the cockpit. The JSON file contains a dated local database snapshot,
resource counts and declared/cached states with temporary resource aliases.
It excludes names, endpoints, credentials, SQL, row payloads and raw logs.
The stored state-change time is not a recent measurement; delivery, metrics
and costs are not collected. The export triggers no live provider checks.
Keep the file private and review it before sharing; nothing is sent automatically.

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

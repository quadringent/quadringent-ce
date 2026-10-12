# Backup, restore and version rollback

This guide covers **control-plane v2 PostgreSQL**. CDC objects, checkpoints,
fleet-state PVCs and Snowflake data need separate retention and consistency
procedures. API administration does not confer Kubernetes, database or object
storage permissions. Qualify in isolated, authorized DEV resources.

## Preserve recovery material

Keep the admitted release, commit, chart checksum, three OCI digests, effective
Helm values, secret references and pre-upgrade backup privately. Both the
candidate and previous images must be available in the authorized registry.

Preserve the **original values** of `SECRET_KEY` and `TOKEN_PEPPER` from
`<chart-fullname>-v2-secrets`, alongside database and external-connection
secret references. Fernet protects connection credentials; the pepper is needed
for existing tokens. The chart uses `lookup` and
`helm.sh/resource-policy: keep` in the same namespace. A new release or
namespace must receive the original values through the site's secret channel
**before v2 starts**. Fresh values cannot decrypt the restored database.
A SQL dump does not replace this separate preservation.

Use PostgreSQL clients compatible with the server, Helm, kubectl and an
authenticated AWS or Google client. Supply passwords through a private
`PGPASSFILE` in mode `0600`; avoid password-bearing command arguments and
`set -x`. Keep dumps and receipts private.

## Backup and integrity

For embedded PostgreSQL, enable `postgres.backup.enabled` in reviewed values.
The `<postgres-service>-backup` CronJob uses the pinned PostgreSQL image for
`pg_dump --format=custom`, then fully decodes the archive with
`pg_restore --file=/dev/null`. A trusted init container writes a local
receipt bound to the exact size and SHA256.

If `pg_restore` is unavailable in the uploader, it requires that receipt.
After upload, it rereads the stored bytes and compares length and SHA256 before
reporting success. Its S3/GCS identity needs both write and read access to
`postgres.backup.destinationPrefix`.

With network policy enabled, the backup ingress rule selects the same release's
exact name, instance and component labels on the PostgreSQL port. Disabling
backup removes that rule.

The `quadringent.pg-backup-validation.v1` receipt identifies
`pg_restore-full-sql-decode`. It proves trusted local decoding, **not an
independently authenticated receipt or a successful restoration**. It remains
on the Job's temporary shared volume; preserve it privately before Job cleanup
if needed. The script does not upload a separate receipt object. Job retention
and object-backup retention are distinct.

Manual backup of an authorized database:

```sh
set -eu
umask 077
export PGHOST='<authorized-source-host>' PGPORT='5432'
export PGUSER='<backup-user>' PGDATABASE='<source-database>'
export PGPASSFILE='/private/path/pgpass-source'
pg_dump --format=custom --no-owner --no-privileges --file /private/path/preupgrade.dump
pg_restore --file=/dev/null /private/path/preupgrade.dump
shasum -a 256 /private/path/preupgrade.dump
```

Download an existing backup using one storage backend:

```sh
aws s3 cp 's3://<bucket>/<prefix>/<object>.dump' /private/path/restoration.dump
# Or:
gcloud storage cp 'gs://<bucket>/<prefix>/<object>.dump' /private/path/restoration.dump
shasum -a 256 /private/path/restoration.dump
pg_restore --file=/dev/null /private/path/restoration.dump
```

Use `umask 077` before downloading. Compare against the original protected
upload hash or receipt. A newly calculated hash alone cannot prove origin.
Reject empty, truncated, undecodable or mismatched archives.

## Restore into a new isolated database

Use a separately authorized restoration server without active workload access.
Verify host and database before executing:

```sh
set -eu
umask 077
export PGHOST='<isolated-restore-host>' PGPORT='5432'
export PGUSER='<restore-administrator>'
export PGPASSFILE='/private/path/pgpass-restore'
export PGDATABASE='quadringent_restore_dev'
createdb --maintenance-db=postgres "$PGDATABASE"
pg_restore --exit-on-error --single-transaction --no-owner --no-privileges \
  --dbname "$PGDATABASE" /private/path/restoration.dump
psql --no-psqlrc --set=ON_ERROR_STOP=1 --command='SELECT current_database();'
```

`createdb` must fail if the target exists. Do not drop or empty it to bypass
that failure, and do not run `--clean` against an active database.
Verify expected tables, accounts, reference counts, values and decryption with
the original secrets. Successful `pg_restore` alone does not qualify the
product.

Start the control plane at the **backup's product version** against this isolated
database, original keys/pepper, and disabled or strictly isolated integrations.
For an external database use `postgres.enabled=false`,
`externalDatabase.existingSecret` and `existingSecretKey`. Prepare its DSN
secret through the site's secret channel. Avoid a second active fleet.

## Upgrade and rollback

v2 startup automatically runs locked migrations. Test compatibility on an
isolated copy before upgrading. Stop writes during the approved window and
create a pre-upgrade backup. Archive the previous chart and effective values.
Pin `image.digest`, `controlPlane.image.digest` and
`verification.imageDigest` to the admitted release's digests.

The chart preserves existing PostgreSQL StatefulSet PVC-template metadata with
`lookup`. Helm's identity must read that StatefulSet. A denied read blocks
rendering. Offline `helm template` cannot attest preservation of cluster state.
Do not bypass a refusal with `--force`, StatefulSet deletion or PVC recreation.

After checking chart and image references:

```sh
helm upgrade --install '<release>' /private/path/quadringent-0.3.4.tgz \
  --kube-context '<authorized-context>' --namespace '<namespace>' \
  --values /private/path/site-candidate-pinned.yaml --wait --timeout 5m
```

Helm success or `--wait` establishes no CDC or data guarantee. Read back product
version, running digests, API state and business data using the site's
qualification procedure.

If migrations support the previous version, apply its chart and admitted pinned
values, then verify operation. Otherwise keep writes stopped, restore the
pre-upgrade dump into **another isolated database**, preserve the original
keys/pepper, test the old version, and obtain approval for switching the site.
Reconcile writes made after the dump before resuming. `helm rollback` changes
Kubernetes manifests; it does not reverse PostgreSQL migrations or data.
Security migration 0015 requires a compatible corrected application version.

## Evidence limits

Earlier DEV control-plane lifecycle exercises covered GKE, EKS and both cloud
VM targets with synthetic data, login and decryption checks. Direct StatefulSet
and PVC UID observations covered GKE, EKS and GCP VM; AWS VM UID preservation
was not directly measured. Some child-image selections were inferred from
sealed artifacts. Those historical exercises do not qualify this release's CDC
or production stability.

A disposable local PostgreSQL check restored 50 matching synthetic rows and
rejected a truncated archive retaining the PGDMP header. It validates that local
archive path, not this exact release on a cloud target. See
[native qualification](native-qualification.md) and [installation](install-default.md).

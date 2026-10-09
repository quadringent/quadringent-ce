# GCP storage and identity

This module prepares storage and runtime identity without creating a VM or
cluster. The installer uses it before GCP VM or GKE attachment.

It creates a GCS bucket with uniform access, enforced public-access prevention
and optional versioning, plus a runtime service account with bucket-scoped
objectCreator/objectViewer roles. Conditional replacement is limited to
checkpoints/ and source-gates/; raw batches remain write-once.
It grants no project-wide storage role.

The same bucket serves AS400_RAW_BUCKET and AS400_CHECKPOINT_BUCKET under
distinct prefixes. Generation-conditional writes protect durable state.
The account ID condenses longer names with a stable hash to respect GCP's
30-character limit.

Required inputs: project_id, name and region. Defaults:
bucket_versioning=true, labels={}, existing_bucket_name="".
Existing bucket reuse remains explicit and requires plan review.
Outputs: raw_bucket_name, service_account_email, service_account_name.

```sh
terraform init -backend=false
terraform validate
terraform fmt -check
```

Actual apply requires site authority and budget.
See [installation](../../../../docs/product/install-default.md).

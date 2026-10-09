# AWS storage and identity

The installer applies this module before AWS VM or EKS attachment. It creates
no compute:

- Private raw S3 bucket with complete public-access blocking, SSE-KMS,
  optional versioning and optional lifecycle expiration.
- PAY_PER_REQUEST DynamoDB checkpoints with encryption and optional PITR.
- IAM policy limited to this bucket/table, without broad S3/DynamoDB access
  or data-delete permission.
- EC2-trusted runtime role and instance profile. EKS uses a separate
  OIDC-trusted role with the exported policy.

Resources carry purpose=quadringent. Required inputs are name and region.
Defaults: bucket_versioning=true, lifecycle_expiration_days=0 (disabled),
checkpoint_table_point_in_time_recovery=true, tags={}.
Existing bucket/checkpoint inputs support declared reuse; review the plan
rather than treating existing data as disposable.

Outputs expose raw bucket, checkpoint table, runtime policy/role/instance profile
and account identity. See variables.tf and outputs.tf for exact names/validation.
No account is hardcoded.

Local checks:

```sh
terraform init -backend=false
terraform validate
terraform fmt -check
```

Actual plan/apply requires the site's authorized identity and budget.
See [installation](../../../../docs/product/install-default.md).

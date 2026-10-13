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
Positive expiration requires an explicit canonical `raw_prefix_root`; only
objects below `<raw_prefix_root>/` expire. The fixed `receiver-authority-v1/`
prefix, including copy intents, must remain disjoint. The installer supplies
the same approved RAW prefix as its Helm values without enabling expiration.
Existing bucket/checkpoint inputs support declared reuse; review the plan
rather than treating existing data as disposable.

The shared runtime policy denies authority deletion (including versions) and
PUT requests without exact `If-None-Match: *`. Current registry writes use
single conditional PUTs; no multipart exemption is granted. This narrows the
runtime role, not other bucket owners' identities. Existing bucket lifecycle
and policies remain owner-managed and require independent retention review.
These controls do not replace an authority/copy-intent backup and restore
procedure or separate permissions for control plane, reader, copy and loader.
See [AWS conditional-write enforcement](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes-enforce.html).

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

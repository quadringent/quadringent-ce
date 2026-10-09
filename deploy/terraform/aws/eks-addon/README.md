# Attach an existing EKS cluster

This module creates scoped IRSA roles using the base runtime policy.
It neither creates nor modifies the EKS cluster.

Trust binds the supplied OIDC provider, exact namespace/ServiceAccount subject
and sts.amazonaws.com audience. The site must provide an existing EKS cluster
with its IAM OIDC provider and an already prepared runtime_policy_arn.

Required inputs: name, runtime_policy_arn, oidc_provider_arn and
oidc_provider_url (without https://). Defaults are namespace quadringent,
service_account_name quadringent-control-plane,
capture_service_account_name quadringent-capture and tags={}.
Capture and control-plane identities remain distinct.

Use the exported role references in the matching chart ServiceAccounts.
Review variables.tf and outputs.tf for exact validation and output names.

```sh
terraform init -backend=false
terraform validate
terraform fmt -check
```

See [installation](../../../../docs/product/install-default.md).

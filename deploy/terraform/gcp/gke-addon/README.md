# Attach an existing GKE cluster

This module binds declared Kubernetes ServiceAccounts to the bucket-scoped
Google runtime account through Workload Identity. It creates no cluster and
does not enable Workload Identity on one.

Bindings grant roles/iam.workloadIdentityUser to the exact
serviceAccount:<project>.svc.id.goog[<namespace>/<service_account>] members.
Prepare the GCP base account and an existing GKE cluster with Workload Identity
enabled first.

Required inputs: project_id, name and service_account_email. Defaults:
namespace quadringent, kubernetes_service_account_name quadringent-control-plane,
capture_kubernetes_service_account_name quadringent-capture.
Preserve the declared capture/control-plane identities.

Use the exported annotation on the matching chart ServiceAccounts:
iam.gke.io/gcp-service-account=<service_account_email>.
The IAM binding and Kubernetes annotation are both required.
Review variables.tf and outputs.tf for the exact input/output contract.

```sh
terraform init -backend=false
terraform validate
terraform fmt -check
```

See [installation](../../../../docs/product/install-default.md).

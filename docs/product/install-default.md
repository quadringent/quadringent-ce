# Install Quadringent with the default installer

```sh
quadringent install --cloud aws|gcp --target vm|cluster
```

The installer provisions a dedicated k3s VM or uses an existing Kubernetes
cluster. The [expert guide](install-client.md) covers sites managing their own
Helm values. See [backup and restore](backup-restore.md) before upgrading or
rebuilding a site.

Current source is **0.4.0, a DEV/INT preview candidate** with native receiver
bindings and selected replay recovery. Use only available, admitted release
assets; this candidate has no native continuity or full-site recovery verdict.
The published [0.3.5 scoped receipt](../../site/runtime-0.3.5.json) covers initial
copy and an EKS application cycle. The
[0.3.4 recovery receipt](../../site/recovery-0.3.4.json) remains historical. A ready
pod or accessible API does not prove IBM i → Snowflake delivery, CDC continuity,
a maximum latency of 10 seconds, or production readiness.

Native capture now requires its original admitted receiver binding; legacy v1
checkpoints cannot be adopted automatically. Keep the previous admitted version
and checkpoint state available. In-place migration and rollback across this
boundary need separate qualification; see the [native capture guide](native-qualification.md).

Historical 0.2.5 control plane exercises covered GKE, EKS, AWS VM and GCP VM:
installation, 0.2.3 → 0.2.5 → 0.2.3, isolated backup/key export, complete removal
of the original site, fresh reconstruction and restore. Login, synthetic data
parity and decryption were checked. These bounded results do not qualify 0.4.0
or every configuration; AWS VM PostgreSQL/PVC UIDs were not measured directly,
and image-child selection was sometimes inferred from the sealed manifest.

## Release assets

Download the wheel, sdist and release manifest from the **same admitted
release**. The wheel installs the CLI; the extracted sdist supplies chart/
and deploy/terraform/. The three OCI images are separate, immutable digest
references. Verify all published hashes before installation. An absent page
or a draft is not an available release.

```sh
python3 -m venv /private/path/quadringent-venv
/private/path/quadringent-venv/bin/python -m pip install quadringent-0.4.0-py3-none-any.whl
tar -xzf quadringent-0.4.0.tar.gz -C /private/path
/private/path/quadringent-venv/bin/quadringent install \
  --cloud gcp --target cluster --region europe-west1 --project my-project \
  --name my-site --release-manifest /private/path/release-manifest.json \
  --assets-dir /private/path/quadringent-0.4.0 --dry-run
```

Review the plan, cloud identity and kubectl context before removing
--dry-run. Missing chart or Terraform assets fail closed. A source checkout
may omit --assets-dir. Use verification.imageDigest in chart values.

The community OCI repository is ghcr.io/<owner>/quadringent-ce; the manifest
is authoritative. --image-repository may select a private mirror preserving
the same digests. deploy/release-manifest.example.json contains fake digests
for offline tests only.

For private images, --image-pull-secret names a Kubernetes Secret already
present in the target namespace. The CLI neither creates it nor receives
registry credentials. The chart binds it to the default capture ServiceAccount,
so later readers, loaders and Jobs can pull images. If serviceAccount.create=false,
configure the supplied ServiceAccount yourself. Public images need no pull secret.

## Prerequisites

Check [compatibility and prerequisites](compatibility.md) for the IBM i,
journaling, authority and Snowflake requirements first.

terraform, helm and kubectl must be on PATH. AWS VMs also require aws and
session-manager-plugin; GCP requires gcloud. The installer checks credential
presence without displaying values. `--check-ibmi <host>` optionally tests TCP
reachability on 9471, 9476 and 9475; it does not perform a TLS handshake.

Required target inputs are checked before terraform apply:

| Target | Inputs |
|---|---|
| AWS VM | Existing --vpc-id and --subnet-id; optional --aws-profile and --vm-instance-type |
| AWS cluster | Existing EKS cluster/context and its --eks-oidc-provider-arn / --eks-oidc-provider-url |
| GCP VM | --project, existing --gcp-network / --gcp-subnetwork; optional --gcp-zone |
| GCP cluster | --project and an existing GKE cluster/context |

An existing cluster needs a StorageClass that provisions PostgreSQL PVCs.
The chart uses the default class unless postgres.storage.storageClassName
selects another. On EKS, ensure Amazon EBS CSI and its IAM permissions work:

```sh
aws eks describe-addon --cluster-name my-cluster \
  --addon-name aws-ebs-csi-driver \
  --query 'addon.{status:status,issues:health.issues}'
kubectl get storageclass
kubectl -n kube-system get deployment ebs-csi-controller
kubectl -n kube-system get daemonset ebs-csi-node
```

The add-on must be ACTIVE, controller/node pods ready, and the EBS StorageClass
must use ebs.csi.aws.com. CREATE_FAILED or a Pending PVC is not a functional
installation. If the add-on owns its ServiceAccount, use
eksctl create iamserviceaccount --role-only to create only the IAM role.
See [AWS EBS CSI](https://docs.aws.amazon.com/eks/latest/userguide/ebs-csi.html)
and [eksctl IAM service accounts](https://docs.aws.amazon.com/eks/latest/eksctl/iamserviceaccounts.html).

## Installation behavior

1. Copy each Terraform module into the site's private workspace,
   ~/.quadringent/<name>/terraform/<module>/; repository checkouts never
   contain the site's Terraform state.
2. Save a terraform plan -out=tfplan, show additions/changes/deletions,
   require interactive confirmation unless --yes, and refuse deletions
   unless --allow-destroy. Apply that exact saved plan and reread
   terraform output -json.
3. Create the raw bucket, checkpoint storage and a scoped cloud identity.
   --existing-bucket and AWS --existing-checkpoint-table switch to data
   sources: reused resources retain their owner's versioning, encryption
   and lifecycle configuration.
4. Provision the VM or bind the existing cluster identity. Actual Terraform
   outputs populate Helm values; missing identities fail closed. Dry-run
   documentation identities are never used by a real installation.
5. Render the chart, then helm upgrade --install. Preserve an existing
   release's effective CDC profile rather than silently resetting tuning.
6. Wait for the control plane rollout. If --admin-email is supplied, request
   POST /v2/setup/first-admin from inside the control-plane-v2 pod through
   kubectl exec. Display the actual single-use activation link and private
   tunnel instructions. Failure or an unready control plane returns a failure.

The installation is idempotent. Replaying verifies prior effects but never
redisplays a one-time secret. If the first link is lost, use the explicit,
authenticated first-admin reissue procedure in [API v2](../api-v2.md).
The installer does not declare an IBM i source; use the activation wizard.

## Examples

```sh
# Offline preview: no commands executed and no files written.
quadringent install --cloud aws --target vm --region eu-west-3 --name demo-int --dry-run

# AWS VM: supply admitted release assets and existing network identifiers.
quadringent install --cloud aws --target vm --region eu-west-3 --name demo-int \
  --vpc-id vpc-0123456789abcdef0 --subnet-id subnet-0123456789abcdef0 \
  --release-manifest /private/path/release-manifest.json --aws-profile demo-int \
  --vm-instance-type m7i-flex.large --image-pull-secret my-private-registry

# Existing EKS cluster.
quadringent install --cloud aws --target cluster --region eu-west-3 --name demo-int \
  --eks-oidc-provider-arn arn:aws:iam::000000000000:oidc-provider/oidc.eks.eu-west-3.amazonaws.com/id/EXAMPLE \
  --eks-oidc-provider-url oidc.eks.eu-west-3.amazonaws.com/id/EXAMPLE \
  --release-manifest /private/path/release-manifest.json

# Existing GKE cluster and raw bucket.
quadringent install --cloud gcp --target cluster --region europe-west1 --name demo-int \
  --project my-project --existing-bucket my-existing-bucket \
  --release-manifest /private/path/release-manifest.json --dry-run

# Dedicated GCP VM.
quadringent install --cloud gcp --target vm --region europe-west1 --name demo-int \
  --project my-project --gcp-network my-vpc --gcp-subnetwork my-subnet \
  --release-manifest /private/path/release-manifest.json --dry-run

quadringent vm-tunnel --name demo-int
quadringent status --name demo-int
quadringent uninstall --name demo-int
```

--yes is for automation that has already reviewed the plan. It does not
authorize destruction without --allow-destroy.

## Cloud identity and private access

AWS VMs use the foundation's instance profile and SSM. The installer retrieves
a private 0600 k3s kubeconfig and opens a temporary SSM tunnel. GCP VMs use
SSH IAP and a temporary SSH tunnel; they attach the foundation's bucket-scoped
service account and use Compute Engine metadata (gcpIdentityMode: vm-metadata),
without a GKE Workload Identity annotation. All pods can reach this node
identity: keep the VM dedicated to trusted workloads.

A GCP VM has no public IP by default. Its subnet needs outbound connectivity
to download k3s and images, for example a customer-managed Cloud NAT. IAP
does not supply outbound Internet access and the module does not create NAT.
TF_VAR_assign_public_ip=true explicitly requests an ephemeral public IP;
review its cost and the customer's network firewall. SSH remains IAP-limited
by default. See the [GCP VM module](../../deploy/terraform/gcp/vm/README.md).

Existing EKS clusters use IRSA; GKE clusters bind the Kubernetes ServiceAccount
to the scoped GCP service account with roles/iam.workloadIdentityUser.
Resolved identities populate controlPlane.serviceAccount.roleArn or
controlPlane.serviceAccount.gcpServiceAccount, respectively.

The UI remains private. Open the displayed tunnel; quadringent vm-tunnel
reuses the recorded AWS profile or GCP VM context, then displays the
kubectl port-forward command to run in another terminal. A single v1 port
(8844) relays /v2/* and /mcp to the second container (8845). Use the displayed
activation URL at /#/wizard/activate?token=… and protect the private kubeconfig.

Session cookies use Secure. Use Chromium or Firefox with
http://localhost:8844 for the local tunnel; Safari loopback handling can
prevent login. External access requires the site's own reviewed HTTPS setup.

## Resources and cost boundaries

| Resource | AWS | GCP |
|---|---|---|
| Raw storage | S3 storage and requests | GCS storage and operations |
| Checkpoints | DynamoDB PAY_PER_REQUEST | Same GCS bucket |
| Dedicated VM | Configurable x86_64 EC2, default t3.medium, encrypted EBS root | Default e2-medium, 40 GB boot disk, no public IP by default |
| Existing cluster | No cluster creation; pods consume its capacity and may trigger autoscaling | Same |

Configure billing alerts separately. Dry-run executes no cloud commands.
Terraform planning does not provision resources; it still requires access
to read the selected account. Stopped workloads may leave billable storage,
VM disks, networking and cluster resources.

## Uninstall

quadringent uninstall removes only the Helm release. For a VM, it resolves
the recorded site and uses SSM/IAP; missing site state refuses access rather
than operating on the ambient cluster.

Terraform resources, raw data, checkpoints and identities remain. To remove
them, the data owner must separately review terraform destroy in the private
site modules (VM first, then foundation), confirm no reader is active and
retain required evidence/backups. See [expert uninstall](install-client.md).

## Current chart boundaries

The installer enables v1, v2 and embedded PostgreSQL on all four targets.
With site.connectionDeclared=false and replicaCount=0, no reader renders
and IBM i/Snowflake fields remain empty. Site and storage identity remain
required; referenced pending Secret names are object names, not credentials.

AWS identity and GCP identity fields are mutually exclusive. AWS account/region
fields remain empty for GCS. The embedded PostgreSQL StatefulSet uses a
digest-pinned image, PVC, preserved credential Secret and restricted network
access; an external database is an expert option.

controlPlane.launch.enabled remains false until real jobTemplate and fleetCatalog
inputs exist. Creating infrastructure and an accessible control plane proves
installation only. Initial copy, mirror reconciliation, native reader recovery
and measured latency require the separate [qualification](native-qualification.md).

### Dry-run sample — GCP, existing cluster and bucket

```
quadringent install --cloud gcp --target cluster --region europe-west1 --name demo-int --project example-gcp-project --existing-bucket preexisting-gcs-bucket --dry-run
```

```
Installation Quadringent — cloud=gcp target=cluster region=europe-west1 name=demo-int
Working directory: /home/operator/.quadringent/demo-int

1. Copy the Terraform module of the foundation (bucket, checkpoints, identity) in the site's private workspace
   copy deploy/terraform/gcp/base -> /home/operator/.quadringent/demo-int/terraform/base
2. Generate Terraform variables of the foundation (bucket, checkpoints, identity) — bucket 'preexisting-gcs-bucket' reused, no creation
   write /home/operator/.quadringent/demo-int/terraform/base/site.auto.tfvars.json
3. Initialize the Terraform module of the foundation (bucket, checkpoints, identity)
   $ terraform init -input=false  (cwd=/home/operator/.quadringent/demo-int/terraform/base)
4. Calculate the Terraform plan of the foundation (bucket, checkpoints, identity)
   $ terraform plan -input=false -out=tfplan  (cwd=/home/operator/.quadringent/demo-int/terraform/base)
5. Summarize the plan of the foundation (bucket, checkpoints, identity) and request confirmation (refused without confirmation or --yes; any destruction requires --allow-destroy)
   $ terraform show -json tfplan  (cwd=/home/operator/.quadringent/demo-int/terraform/base)
6. Apply the Terraform plan of the foundation (bucket, checkpoints, identity) — bucket 'preexisting-gcs-bucket' reused, no creation
   $ terraform apply -input=false tfplan  (cwd=/home/operator/.quadringent/demo-int/terraform/base)
7. Read the Terraform outputs of the foundation (bucket, checkpoints, identity) (actual identity, gap 3)
   $ terraform output -json  (cwd=/home/operator/.quadringent/demo-int/terraform/base)
8. Copy the Terraform module of the identity binding (IRSA/Workload Identity) in the site's private workspace
   copy deploy/terraform/gcp/gke-addon -> /home/operator/.quadringent/demo-int/terraform/addon
9. Generate Terraform variables of the identity binding (IRSA/Workload Identity)
   write /home/operator/.quadringent/demo-int/terraform/addon/site.auto.tfvars.json
10. Initialize the Terraform module of the identity binding (IRSA/Workload Identity)
   $ terraform init -input=false  (cwd=/home/operator/.quadringent/demo-int/terraform/addon)
11. Calculate the Terraform plan of the identity binding (IRSA/Workload Identity)
   $ terraform plan -input=false -out=tfplan  (cwd=/home/operator/.quadringent/demo-int/terraform/addon)
12. Summarize the plan of the identity binding (IRSA/Workload Identity) and request confirmation (refused without confirmation or --yes; any destruction requires --allow-destroy)
   $ terraform show -json tfplan  (cwd=/home/operator/.quadringent/demo-int/terraform/addon)
13. Apply the Terraform plan of the identity binding (IRSA/Workload Identity)
   $ terraform apply -input=false tfplan  (cwd=/home/operator/.quadringent/demo-int/terraform/addon)
14. Read the Terraform outputs of the identity binding (IRSA/Workload Identity) (actual identity, gap 3)
   $ terraform output -json  (cwd=/home/operator/.quadringent/demo-int/terraform/addon)
15. Verify access to the existing cluster (kubeconfig already configured by the site)
   $ kubectl cluster-info
16. Generate Helm values (new site; when executing, preserve the existing release's CDC profile)
   write /home/operator/.quadringent/demo-int/chart-values.generated.yaml
17. Validate chart rendering (helm template)
   $ helm template demo-int chart --namespace quadringent -f /home/operator/.quadringent/demo-int/chart-values.generated.yaml
18. Install the Quadringent chart
   $ helm upgrade --install demo-int chart --namespace quadringent --create-namespace -f /home/operator/.quadringent/demo-int/chart-values.generated.yaml
19. Wait for the control plane deployment (kubectl rollout status)
   $ kubectl -n quadringent rollout status deployment/demo-int-quadringent-control-plane --timeout=180s
20. Retrieve the first-admin activation token (kubectl exec, POST /v2/setup/first-admin from inside the pod)
   $ kubectl -n quadringent exec deployment/demo-int-quadringent-control-plane -c control-plane-v2 -- python -c <POST http://127.0.0.1:8845/v2/setup/first-admin>

(--dry-run: no command executed, no file written)
```

### Dry-run sample — AWS, existing cluster

```
quadringent install --cloud aws --target cluster --region eu-west-3 --name demo-int --dry-run
```

```
Installation Quadringent — cloud=aws target=cluster region=eu-west-3 name=demo-int
Working directory: /home/operator/.quadringent/demo-int

1. Copy the Terraform module of the foundation (bucket, checkpoints, identity) in the site's private workspace
   copy deploy/terraform/aws/base -> /home/operator/.quadringent/demo-int/terraform/base
2. Generate Terraform variables of the foundation (bucket, checkpoints, identity)
   write /home/operator/.quadringent/demo-int/terraform/base/site.auto.tfvars.json
3. Initialize the Terraform module of the foundation (bucket, checkpoints, identity)
   $ terraform init -input=false  (cwd=/home/operator/.quadringent/demo-int/terraform/base)
4. Calculate the Terraform plan of the foundation (bucket, checkpoints, identity)
   $ terraform plan -input=false -out=tfplan  (cwd=/home/operator/.quadringent/demo-int/terraform/base)
5. Summarize the plan of the foundation (bucket, checkpoints, identity) and request confirmation (refused without confirmation or --yes; any destruction requires --allow-destroy)
   $ terraform show -json tfplan  (cwd=/home/operator/.quadringent/demo-int/terraform/base)
6. Apply the Terraform plan of the foundation (bucket, checkpoints, identity)
   $ terraform apply -input=false tfplan  (cwd=/home/operator/.quadringent/demo-int/terraform/base)
7. Read the Terraform outputs of the foundation (bucket, checkpoints, identity) (actual identity, gap 3)
   $ terraform output -json  (cwd=/home/operator/.quadringent/demo-int/terraform/base)
8. Copy the Terraform module of the identity binding (IRSA/Workload Identity) in the site's private workspace
   copy deploy/terraform/aws/eks-addon -> /home/operator/.quadringent/demo-int/terraform/addon
9. Generate Terraform variables of the identity binding (IRSA/Workload Identity)
   write /home/operator/.quadringent/demo-int/terraform/addon/site.auto.tfvars.json
10. Initialize the Terraform module of the identity binding (IRSA/Workload Identity)
   $ terraform init -input=false  (cwd=/home/operator/.quadringent/demo-int/terraform/addon)
11. Calculate the Terraform plan of the identity binding (IRSA/Workload Identity)
   $ terraform plan -input=false -out=tfplan  (cwd=/home/operator/.quadringent/demo-int/terraform/addon)
12. Summarize the plan of the identity binding (IRSA/Workload Identity) and request confirmation (refused without confirmation or --yes; any destruction requires --allow-destroy)
   $ terraform show -json tfplan  (cwd=/home/operator/.quadringent/demo-int/terraform/addon)
13. Apply the Terraform plan of the identity binding (IRSA/Workload Identity)
   $ terraform apply -input=false tfplan  (cwd=/home/operator/.quadringent/demo-int/terraform/addon)
14. Read the Terraform outputs of the identity binding (IRSA/Workload Identity) (actual identity, gap 3)
   $ terraform output -json  (cwd=/home/operator/.quadringent/demo-int/terraform/addon)
15. Verify access to the existing cluster (kubeconfig already configured by the site)
   $ kubectl cluster-info
16. Generate Helm values (new site; when executing, preserve the existing release's CDC profile)
   write /home/operator/.quadringent/demo-int/chart-values.generated.yaml
17. Validate chart rendering (helm template)
   $ helm template demo-int chart --namespace quadringent -f /home/operator/.quadringent/demo-int/chart-values.generated.yaml
18. Install the Quadringent chart
   $ helm upgrade --install demo-int chart --namespace quadringent --create-namespace -f /home/operator/.quadringent/demo-int/chart-values.generated.yaml
19. Wait for the control plane deployment (kubectl rollout status)
   $ kubectl -n quadringent rollout status deployment/demo-int-quadringent-control-plane --timeout=180s
20. Retrieve the first-admin activation token (kubectl exec, POST /v2/setup/first-admin from inside the pod)
   $ kubectl -n quadringent exec deployment/demo-int-quadringent-control-plane -c control-plane-v2 -- python -c <POST http://127.0.0.1:8845/v2/setup/first-admin>

(--dry-run: no command executed, no file written)
```

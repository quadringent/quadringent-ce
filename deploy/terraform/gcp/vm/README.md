# GCP VM target

This module creates an Ubuntu 24.04 LTS Compute Engine VM with k3s in an
**existing** network/subnetwork. Startup disables Traefik. Shielded VM secure
boot, vTPM and integrity monitoring are enabled.

Default machine_type is e2-medium, state disk 40 GB, k3s channel stable and
assign_public_ip=false. The SSH firewall targets only the VM tag, permits
IAP TCP forwarding from 35.235.240.0/20, and adds only explicitly declared
allowed_ssh_ranges (empty by default).

The base module's service account uses cloud-platform OAuth scope; effective
access remains bounded by its IAM roles. k3s Pods can use the node metadata
identity, so the VM must be dedicated to Quadringent.

## Outbound access and administration

Installing k3s/images requires outbound Internet access. Without public IP,
provide an existing route such as Cloud NAT; this module creates no NAT.
IAP administrative forwarding does not provide workload outbound access.

An isolated test VM without NAT can explicitly request an ephemeral public IP:

```sh
export TF_VAR_assign_public_ip=true
export TF_VAR_machine_type=e2-standard-2
export TF_VAR_boot_disk_size_gb=20
```

Review the actual Terraform plan: installer dry run does not calculate those
environment overrides. Public IP adds no administrative firewall port.
VM, disk and address may incur charges until cleaned up.

```sh
quadringent vm-tunnel --name <site>
```

Run the printed kubectl port-forward in a second terminal and open
http://localhost:8844. IAP uses SSH port 22, then local forwarding to k3s;
no public 6443/8844 rule is required. The caller needs
roles/iap.tunnelResourceAccessor for the instance.

The installer retrieves /etc/rancher/k3s/k3s.yaml through
gcloud compute ssh --tunnel-through-iap and writes a protected kubeconfig.
Required inputs: project_id, region, zone, name, network, subnetwork and
service_account_email.

```sh
terraform init -backend=false
terraform validate
terraform fmt -check
```

Control-plane health and GCS identity checks alone do not prove IBM i copy,
Snowflake delivery or restoration.
See [installation](../../../../docs/product/install-default.md).

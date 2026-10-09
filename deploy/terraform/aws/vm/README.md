# AWS VM target

This module creates EC2 with k3s in an **existing** VPC/subnet.
It creates no network. Prefer a private subnet with the required outbound route.

The instance uses Amazon Linux 2023 from the declared public AMI parameter,
IMDSv2, encrypted EBS and the base module's instance profile.
It attaches AmazonSSMManagedInstanceCore to that profile's role.
Cloud-init installs k3s with Traefik disabled.

Default architecture is x86_64, instance type t3.medium, root disk 40 GB,
k3s channel stable and no SSH key. arm64 requires compatible instance/images.
SSH/UI CIDR lists are empty by default: there is no public administrative ingress.
Outbound registry, Snowflake, IBM i and SSM access remains necessary.

A newer resolved AMI may propose replacement. Review the plan and preserve
k3s/control-plane state before authorizing destruction; the installer refuses
destructive changes by default.

## Access

```sh
quadringent vm-tunnel --name <site>
```

Run the printed kubectl port-forward in a second terminal, then open
http://127.0.0.1:8844. SSM needs outbound endpoints, NAT or the network's
declared route. The CLI retrieves /etc/rancher/k3s/k3s.yaml using SSM and writes
a private 0600 kubeconfig. Direct restricted CIDRs are an explicit site choice.

Required inputs: name, vpc_id, subnet_id, instance_profile_name and
instance_role_name. See variables.tf for architecture/size bounds and optional
allowed_ssh_cidrs, allowed_ui_cidrs and ui_port.

```sh
terraform init -backend=false
terraform validate
terraform fmt -check
```

See [installation](../../../../docs/product/install-default.md).

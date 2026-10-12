"""Exécution réelle de l'installation (hors ``--dry-run``).

``plan.py`` reste l'aperçu textuel imprimé par ``--dry-run`` : entièrement
statique, construit sans toucher au disque ni au réseau. L'exécution réelle
ne peut pas être aussi statique — les sorties Terraform du socle ne sont
connues qu'après l'avoir réellement appliqué (gap (3)), et chaque module
Terraform doit tourner dans une copie privée de l'espace de travail du site,
jamais dans l'arbre du dépôt (gap (1)). Ce module porte donc sa propre
orchestration, avec le même ``CommandRunner`` injectable que le reste de
l'installateur (aucune commande externe n'est jamais exécutée pendant la
suite de tests : ``RecordingRunner`` script les réponses).
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
from typing import Callable, Mapping, TextIO

from .plan import (
    InstallInputs,
    _addon_module_dir,
    _base_module_dir,
    _module_workdir,
    _source_path,
    _tfvars_json_dump,
    _vm_module_dir,
    _yaml_dump,
    activation_message,
    addon_tfvars_for,
    base_tfvars_for,
    build_chart_values,
    fetch_first_admin_activation_token,
    vm_tfvars_for,
)
from .manifest import ReleaseManifest
from .runner import CommandRunner
from .vm_access import VmAccessError, connect_aws_vm, connect_gcp_vm

ConfirmFn = Callable[[str], bool]


def _default_confirm(prompt: str) -> bool:
    try:
        answer = input(f"{prompt} [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes", "o", "oui")


class TerraformApplyRefused(RuntimeError):
    """Le plan Terraform a été refusé : destruction sans --allow-destroy,
    ou confirmation interactive déclinée. Jamais une exception silencieuse —
    toujours un message actionnable pour l'opérateur."""


def _summarize_plan(show_json_stdout: str) -> tuple[int, int, int]:
    """Compte les ajouts/modifications/suppressions d'un ``terraform show
    -json <planfile>``. Une action de remplacement (``delete, create``) est
    comptée à la fois en suppression et en création — elle détruit une
    ressource existante."""

    try:
        payload = json.loads(show_json_stdout or "{}")
    except json.JSONDecodeError:
        return (0, 0, 0)
    creates = updates = deletes = 0
    for change in payload.get("resource_changes", []):
        actions = change.get("change", {}).get("actions", [])
        if "create" in actions:
            creates += 1
        if "update" in actions:
            updates += 1
        if "delete" in actions:
            deletes += 1
    return (creates, updates, deletes)


def apply_terraform_module(
    *,
    module_label: str,
    source_dir: str,
    module_workdir: Path,
    tfvars: dict,
    runner: CommandRunner,
    stdout: TextIO,
    env: Mapping[str, str] | None = None,
    yes: bool = False,
    allow_destroy: bool = False,
    confirm: ConfirmFn | None = None,
) -> dict:
    """Copie ``source_dir`` (module du dépôt) dans ``module_workdir``
    (espace de travail privé du site, gap (1) : l'état Terraform ne doit
    jamais vivre dans l'arbre du dépôt), écrit les variables en
    ``site.auto.tfvars.json`` (auto-chargé, pas de ``-var-file``), calcule un
    plan sauvegardé, en affiche le résumé, refuse toute destruction sans
    ``--allow-destroy`` et exige une confirmation sauf ``--yes`` (gap (5)),
    applique le plan sauvegardé (jamais un ``apply -auto-approve`` en
    aveugle sur des variables non revues), puis relit les sorties réelles
    (gap (3)).

    Lève :class:`TerraformApplyRefused` si le plan est refusé — jamais un
    ``apply`` silencieux sur une destruction ou sans confirmation.
    """

    confirm_fn = confirm or _default_confirm
    module_workdir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_dir, module_workdir, dirs_exist_ok=True)
    tfvars_path = module_workdir / "site.auto.tfvars.json"
    tfvars_path.write_text(_tfvars_json_dump(tfvars), encoding="utf-8")

    cwd = str(module_workdir)
    print(f"-> Initialize the Terraform module {module_label}", file=stdout)
    result = runner.run(("terraform", "init", "-input=false"), cwd=cwd, env=env)
    if not result.ok:
        print(result.stdout, file=stdout)
        print(result.stderr, file=stdout)
        raise TerraformApplyRefused(f"terraform init failed for module {module_label}")

    print(f"-> Calculate the Terraform plan {module_label}", file=stdout)
    result = runner.run(("terraform", "plan", "-input=false", "-out=tfplan"), cwd=cwd, env=env)
    if not result.ok:
        print(result.stdout, file=stdout)
        print(result.stderr, file=stdout)
        raise TerraformApplyRefused(f"terraform plan failed for module {module_label}")

    result = runner.run(("terraform", "show", "-json", "tfplan"), cwd=cwd, env=env)
    if not result.ok:
        raise TerraformApplyRefused(f"terraform show -json failed for module {module_label}")
    creates, updates, deletes = _summarize_plan(result.stdout)
    print(
        f"   Plan {module_label} : {creates} addition(s), {updates} change(s), {deletes} deletion(s)",
        file=stdout,
    )
    if deletes > 0 and not allow_destroy:
        raise TerraformApplyRefused(
            f"the plan {module_label} destroys {deletes} resource(s): run again with --allow-destroy to "
            "confirm explicitly, or correct the variables if this is unintended"
        )
    if not yes and not confirm_fn(f"Apply the Terraform plan {module_label} ?"):
        raise TerraformApplyRefused(f"applying module {module_label} cancelled (no confirmation)")

    print(f"-> Apply the Terraform plan {module_label}", file=stdout)
    result = runner.run(("terraform", "apply", "-input=false", "tfplan"), cwd=cwd, env=env)
    if not result.ok:
        print(result.stdout, file=stdout)
        print(result.stderr, file=stdout)
        raise TerraformApplyRefused(f"terraform apply failed for module {module_label}")

    result = runner.run(("terraform", "output", "-json"), cwd=cwd, env=env)
    if not result.ok:
        raise TerraformApplyRefused(f"terraform output -json failed for module {module_label}")
    try:
        raw_outputs = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as error:
        raise TerraformApplyRefused(f"unreadable Terraform outputs for module {module_label}") from error
    return {key: value.get("value") for key, value in raw_outputs.items()}


def _control_plane_identity(inputs: InstallInputs, base_outputs: dict, addon_outputs: dict) -> tuple[str, str | None]:
    """Résout l'identité réelle du control plane depuis les sorties
    Terraform effectivement lues (gap (3)) — jamais un exemple fictif dans
    une vraie installation : lève si l'information attendue est absente.
    """

    if inputs.cloud == "aws":
        role_arn = (addon_outputs.get("role_arn") if inputs.target == "cluster" else None) or base_outputs.get(
            "runtime_role_arn"
        )
        if not role_arn:
            raise TerraformApplyRefused(
                "IAM identity missing from Terraform outputs (role_arn from aws/eks-addon or "
                "runtime_role_arn from aws/base): nothing to publish in controlPlane.serviceAccount.roleArn"
            )
        return role_arn, None
    gcp_service_account = (
        addon_outputs.get("service_account_email") if inputs.target == "cluster" else None
    ) or base_outputs.get("service_account_email")
    if not gcp_service_account:
        raise TerraformApplyRefused(
            "GCP service account missing from Terraform outputs (service_account_email from "
            "gcp/gke-addon or gcp/base): nothing to publish in controlPlane.serviceAccount.gcpServiceAccount"
        )
    return "", gcp_service_account


def execute_install(
    inputs: InstallInputs,
    manifest: ReleaseManifest,
    workdir: Path,
    runner: CommandRunner,
    stdout: TextIO,
    *,
    yes: bool = False,
    allow_destroy: bool = False,
    confirm: ConfirmFn | None = None,
    vm_connector=connect_aws_vm,
    gcp_vm_connector=connect_gcp_vm,
    assets_dir: Path | None = None,
) -> int:
    """Exécute réellement l'installation (jamais appelé en ``--dry-run``).
    Retourne le code de sortie du CLI (0 = succès)."""

    if inputs.cloud == "aws" and inputs.target == "cluster" and not (
        inputs.eks_oidc_provider_arn and inputs.eks_oidc_provider_url
    ):
        print("EKS: --eks-oidc-provider-arn and --eks-oidc-provider-url are required", file=stdout)
        return 2
    if inputs.cloud == "aws" and inputs.target == "vm" and not (inputs.vpc_id and inputs.subnet_id):
        print("AWS VM: --vpc-id and --subnet-id are required", file=stdout)
        return 2
    if inputs.cloud == "gcp" and inputs.target == "vm" and not (inputs.gcp_network and inputs.gcp_subnetwork):
        print("GCP VM: --gcp-network and --gcp-subnetwork are required", file=stdout)
        return 2

    env: dict[str, str] | None = None
    if inputs.cloud == "aws":
        # The requested installation region wins over a profile's default
        # region, including for Terraform, AWS CLI, and kubectl exec plugins.
        env = {"AWS_REGION": inputs.region, "AWS_DEFAULT_REGION": inputs.region}
        if inputs.aws_profile:
            env["AWS_PROFILE"] = inputs.aws_profile

    base_workdir = _module_workdir(workdir, "base")
    try:
        base_outputs = apply_terraform_module(
            module_label="of the foundation (bucket, checkpoints, identity)",
            source_dir=_source_path(_base_module_dir(inputs.cloud), assets_dir),
            module_workdir=base_workdir,
            tfvars=base_tfvars_for(inputs),
            runner=runner,
            stdout=stdout,
            env=env,
            yes=yes,
            allow_destroy=allow_destroy,
            confirm=confirm,
        )
    except TerraformApplyRefused as error:
        print(f"Terraform foundation failed: {error}", file=stdout)
        return 1
    account_id = base_outputs.get("account_id") if inputs.cloud == "aws" else None
    if inputs.cloud == "aws" and (not isinstance(account_id, str) or not (account_id.isdigit() and len(account_id) == 12)):
        print("Terraform foundation failed: AWS account_id output absent or invalid", file=stdout)
        return 1
    if inputs.cloud == "aws" and inputs.target == "cluster":
        assert inputs.eks_oidc_provider_arn is not None
        if inputs.eks_oidc_provider_arn.split("::", 1)[1].split(":", 1)[0] != account_id:
            print("EKS: the OIDC provider does not belong to the foundation's AWS account", file=stdout)
            return 1

    addon_outputs: dict = {}
    if inputs.target == "vm":
        vm_workdir = _module_workdir(workdir, "vm")
        try:
            vm_outputs = apply_terraform_module(
                module_label="of the k3s VM",
                source_dir=_source_path(_vm_module_dir(inputs.cloud), assets_dir),
                module_workdir=vm_workdir,
                tfvars=vm_tfvars_for(inputs, base_outputs),
                runner=runner,
                stdout=stdout,
                env=env,
                yes=yes,
                allow_destroy=allow_destroy,
                confirm=confirm,
            )
        except TerraformApplyRefused as error:
            print(f"Terraform VM failed: {error}", file=stdout)
            return 1
        print("-> Open a temporary tunnel to k3s", file=stdout)
        try:
            if inputs.cloud == "aws":
                connection = vm_connector(vm_outputs.get("instance_id"), workdir, runner, env or {})
            else:
                expected_zone = inputs.gcp_zone or f"{inputs.region}-b"
                if vm_outputs.get("zone") != expected_zone:
                    raise VmAccessError("the GCP VM zone differs from the requested zone")
                assert inputs.project is not None
                connection = gcp_vm_connector(
                    vm_outputs.get("instance_name"), expected_zone, inputs.project, workdir, runner, env or {},
                )
            with connection as vm_env:
                result = runner.run(("kubectl", "cluster-info"), env=vm_env)
                if not result.ok:
                    print("k3s access via the private tunnel failed", file=stdout)
                    return 1
                return _deploy_chart(inputs, manifest, workdir, runner, stdout, base_outputs, {}, account_id, vm_env,
                                     assets_dir=assets_dir)
        except VmAccessError as error:
            print(f"VM access failed: {error}", file=stdout)
            return 1
    else:
        addon_workdir = _module_workdir(workdir, "addon")
        try:
            addon_outputs = apply_terraform_module(
                module_label="of the identity binding (IRSA/Workload Identity)",
                source_dir=_source_path(_addon_module_dir(inputs.cloud), assets_dir),
                module_workdir=addon_workdir,
                tfvars=addon_tfvars_for(inputs, base_outputs),
                runner=runner,
                stdout=stdout,
                env=env,
                yes=yes,
                allow_destroy=allow_destroy,
                confirm=confirm,
            )
        except TerraformApplyRefused as error:
            print(f"Terraform identity binding failed: {error}", file=stdout)
            return 1
        print("-> Verify access to the existing cluster", file=stdout)
        result = runner.run(("kubectl", "cluster-info"), env=env)
        if not result.ok:
            print(result.stdout, file=stdout)
            print(result.stderr, file=stdout)
            print("Access to the existing cluster failed", file=stdout)
            return 1

    return _deploy_chart(inputs, manifest, workdir, runner, stdout, base_outputs, addon_outputs, account_id, env,
                         assets_dir=assets_dir)


def _preserve_existing_loader_profile(
    inputs: InstallInputs, values: dict, runner: CommandRunner, env: Mapping[str, str] | None,
) -> None:
    """Relire les defaults de la release installée avant de générer son upgrade."""
    list_command = ("helm", "list", "--namespace", inputs.namespace, "--filter", f"^{inputs.name}$",
                    "--all", "--output", "json")
    listed = runner.run(list_command, env=env)
    if not listed.ok and "unknown flag: --all" in listed.stderr:
        # Helm 4 includes every status by default and removed --all.
        listed = runner.run(tuple(value for value in list_command if value != "--all"), env=env)
    if not listed.ok:
        raise ValueError("Cannot read Helm releases")
    try:
        releases = json.loads(listed.stdout)
    except json.JSONDecodeError as error:
        raise ValueError("Invalid Helm list") from error
    if not isinstance(releases, list) or any(not isinstance(r, dict) or r.get("name") != inputs.name for r in releases):
        raise ValueError("Helm list outside the scope")
    if not releases:
        return
    if len(releases) != 1:
        raise ValueError("ambiguous Helm release")
    existing = runner.run(("helm", "get", "values", inputs.name, "--namespace", inputs.namespace,
                           "--all", "--output", "json"), env=env)
    if not existing.ok:
        raise ValueError("Cannot read the effective Helm profile")
    try:
        profile = json.loads(existing.stdout)["controlPlane"]["v2"]
        mode = profile["loaderHistoryMode"]
        reader = profile["readerPollSeconds"]
        loader = profile["loaderPollSeconds"]
        flush = profile["loaderFlushEachBatch"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise ValueError("effective profile absent or invalid; explicit migration required") from error
    if (not isinstance(mode, str) or mode not in {"sql", "streaming"} or type(flush) is not bool
            or any(type(interval) not in (int, float) or not 0 < interval <= 60 for interval in (reader, loader))):
        raise ValueError("Invalid effective profile")
    values["controlPlane"]["v2"].update({"loaderHistoryMode": mode, "readerPollSeconds": reader,
                                      "loaderPollSeconds": loader, "loaderFlushEachBatch": flush})


def _deploy_chart(
    inputs: InstallInputs,
    manifest: ReleaseManifest,
    workdir: Path,
    runner: CommandRunner,
    stdout: TextIO,
    base_outputs: dict,
    addon_outputs: dict,
    account_id: str | None,
    env: Mapping[str, str] | None,
    *,
    assets_dir: Path | None = None,
) -> int:

    try:
        role_arn, gcp_service_account = _control_plane_identity(inputs, base_outputs, addon_outputs)
    except TerraformApplyRefused as error:
        print(f"Identity resolution failed: {error}", file=stdout)
        return 1

    chart_values = build_chart_values(
        inputs, manifest, control_plane_role_arn=role_arn,
        control_plane_gcp_service_account=gcp_service_account, aws_account_id=account_id,
    )
    try:
        _preserve_existing_loader_profile(inputs, chart_values, runner, env)
    except ValueError as error:
        print(f"CDC profile preservation failed: {error}", file=stdout)
        return 1
    values_path = workdir / "chart-values.generated.yaml"
    values_path.write_text(_yaml_dump(chart_values), encoding="utf-8")
    print("-> Generate Helm values (storage, resolved identity, control plane)", file=stdout)

    print("-> Validate chart rendering (helm template)", file=stdout)
    result = runner.run(
        ("helm", "template", inputs.name, _source_path("chart", assets_dir), "--namespace", inputs.namespace, "-f", str(values_path)),
        env=env,
    )
    if not result.ok:
        print(result.stdout, file=stdout)
        print(result.stderr, file=stdout)
        print("Chart rendering failed", file=stdout)
        return 1

    print("-> Install the Quadringent chart", file=stdout)
    result = runner.run(
        (
            "helm", "upgrade", "--install", inputs.name, _source_path("chart", assets_dir), "--namespace", inputs.namespace,
            "--create-namespace", "-f", str(values_path),
        ),
        env=env,
    )
    if not result.ok:
        print(result.stdout, file=stdout)
        print(result.stderr, file=stdout)
        print("Chart installation failed", file=stdout)
        return 1

    v2_enabled = bool(chart_values.get("controlPlane", {}).get("v2", {}).get("enabled", False))
    activation_token = None
    if v2_enabled:
        control_plane_deployment = f"deployment/{inputs.name}-quadringent-control-plane"
        print("-> Wait for the control plane deployment (kubectl rollout status)", file=stdout)
        result = runner.run(
            ("kubectl", "-n", inputs.namespace, "rollout", "status", control_plane_deployment, "--timeout=180s"),
            env=env,
        )
        if not result.ok:
            print(result.stdout, file=stdout)
            print(result.stderr, file=stdout)
            print(
                "Failed: the control plane is not ready (see `kubectl -n "
                f"{inputs.namespace} get pods`). Fix the cause, then rerun `quadringent install`, "
                "which is idempotent.",
                file=stdout,
            )
            return 1
        print("-> Retrieve the first-admin activation token", file=stdout)
        activation_token = fetch_first_admin_activation_token(inputs, runner, v2_enabled=True, env=env)

    print("", file=stdout)
    print(activation_message(inputs, activation_token=activation_token, workdir=workdir), file=stdout)
    return 0

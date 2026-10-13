"""CLI ``quadringent`` : installateur mode par défaut (chantier 6).

Sous-commandes : ``install``, ``uninstall``, ``status``. Toute commande
externe passe par un ``CommandRunner`` injectable — voir ``runner.py``. Le
CLI réel (``main``) utilise ``SubprocessRunner`` ; les tests injectent
``RecordingRunner`` via ``run(argv, runner=..., stdout=...)``.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import time
from typing import Sequence, TextIO

from ..local.cli import add_local_subcommand, run_local_command
from .apply import execute_install
from .doctor import add_doctor_subcommand, run_doctor
from .manifest import InvalidReleaseManifest, ReleaseManifest
from .mcp_bridge import add_mcp_subcommand, run_mcp_command
from .plan import (
    agent_token_script,
    InstallInputs,
    InvalidInstallInputs,
    _addon_module_dir,
    _base_module_dir,
    _vm_module_dir,
    build_plan,
)
from .preflight import run_preflight
from .runner import CommandRunner, SubprocessRunner
from .v2_commands import add_v2_subcommands, run_v2_command
from .vm_access import VmAccessError, connect_aws_vm, connect_gcp_vm

DEFAULT_MANIFEST_PATH = Path("deploy/release-manifest.example.json")
STATE_FILENAME = "install-state.json"


def _default_workdir(name: str) -> Path:
    return Path.home() / ".quadringent" / name


def _write_state(workdir: Path, inputs: InstallInputs, status: str) -> None:
    workdir.mkdir(parents=True, exist_ok=True)
    state = {
        "cloud": inputs.cloud,
        "target": inputs.target,
        "region": inputs.region,
        "name": inputs.name,
        "namespace": inputs.namespace,
        "status": status,
    }
    if inputs.aws_profile:
        state["aws_profile"] = inputs.aws_profile
    if inputs.project:
        state["project"] = inputs.project
    (workdir / STATE_FILENAME).write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _read_state(workdir: Path) -> dict | None:
    state_path = workdir / STATE_FILENAME
    if not state_path.exists():
        return None
    return json.loads(state_path.read_text(encoding="utf-8"))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quadringent", description="Quadringent installer (default mode).")
    subparsers = parser.add_subparsers(dest="command", required=True)

    install = subparsers.add_parser("install", help="Create a Quadringent site (infrastructure + chart).")
    install.add_argument("--cloud", required=True, choices=("aws", "gcp"))
    install.add_argument("--target", required=True, choices=("vm", "cluster"))
    install.add_argument("--region", required=True)
    install.add_argument("--name", required=True)
    install.add_argument("--namespace", default="quadringent")
    install.add_argument("--workdir", default=None, help="Private working directory (default: ~/.quadringent/<name>)")
    install.add_argument("--release-manifest", default=str(DEFAULT_MANIFEST_PATH))
    install.add_argument(
        "--assets-dir", default=None,
        help="Root of the extracted release sdist (contains chart/ and deploy/terraform/; default: current directory)",
    )
    install.add_argument("--image-repository", default=None, help="Override the image repository in the release manifest")
    install.add_argument("--check-ibmi", default=None, metavar="HOST", help="Test IBM i TCP reachability (9471/9476/9475 and the 449 server mapper) before installation; TLS and sign-on are not tested")
    install.add_argument(
        "--existing-bucket", default=None, metavar="NAME",
        help="Reuse an existing S3/GCS bucket (deploy/terraform/<cloud>/base, existing_bucket_name)",
    )
    install.add_argument(
        "--existing-checkpoint-table", default=None, metavar="NAME",
        help="AWS only: reuse an existing DynamoDB checkpoint table (existing_checkpoint_table_name)",
    )
    install.add_argument(
        "--admin-email", default=None,
        help="First-admin email: installation displays its single-use activation link",
    )
    install.add_argument("--project", default=None, help="GCP only, required: actual GCP project (project_id)")
    install.add_argument(
        "--aws-profile", default=None, metavar="NAME",
        help="AWS only, optional: local credential profile (AWS_PROFILE, passed to terraform/aws/kubectl)",
    )
    install.add_argument("--vpc-id", default=None, help="AWS VM: existing VPC")
    install.add_argument("--subnet-id", default=None, help="AWS VM: existing subnet")
    install.add_argument("--vm-instance-type", default=None, help="AWS VM: x86_64 EC2 type (default t3.medium)")
    install.add_argument("--gcp-network", default=None, help="GCP VM: existing VPC network")
    install.add_argument("--gcp-subnetwork", default=None, help="GCP VM: existing subnet")
    install.add_argument("--gcp-zone", default=None, help="GCP VM: zone (default <region>-b)")
    install.add_argument("--image-pull-secret", default=None, help="Existing Kubernetes Secret for pulling images from a private registry")
    install.add_argument("--eks-oidc-provider-arn", default=None, help="EKS: IAM ARN of the existing OIDC provider")
    install.add_argument("--eks-oidc-provider-url", default=None, help="EKS: OIDC provider URL without https://")
    install.add_argument("--dry-run", action="store_true", help="Display the complete plan without executing any command")
    install.add_argument("--skip-preflight", action="store_true")
    install.add_argument(
        "--yes", action="store_true",
        help="Do not request confirmation before applying each Terraform plan (a summary is always displayed)",
    )
    install.add_argument(
        "--allow-destroy", action="store_true",
        help="Allow a Terraform plan that destroys existing resources (refused by default)",
    )

    uninstall = subparsers.add_parser("uninstall", help="Remove the Helm release (does not delete Terraform infrastructure).")
    uninstall.add_argument("--name", required=True)
    uninstall.add_argument("--namespace", default="quadringent")
    uninstall.add_argument("--workdir", default=None)
    uninstall.add_argument("--aws-profile", default=None, help="AWS profile if different from the installation profile")
    uninstall.add_argument("--dry-run", action="store_true")

    tunnel = subparsers.add_parser("vm-tunnel", help="Keep the private tunnel to the AWS or GCP k3s VM open.")
    tunnel.add_argument("--name", required=True)
    tunnel.add_argument("--workdir", default=None)
    tunnel.add_argument("--aws-profile", default=None)

    agent = subparsers.add_parser(
        "agent-token",
        help="Issue a bounded agent token (kubectl exec) and write it to a 0600 file, never to the screen.",
    )
    agent.add_argument("--name", required=True)
    agent.add_argument("--namespace", default="quadringent")
    agent.add_argument("--label", required=True, help="Token name (visible in the audit)")
    agent.add_argument("--scope", default="operate", choices=("read", "operate", "admin"))
    agent.add_argument("--days", type=int, default=30, help="Validity period (1 to 90 days)")
    agent.add_argument("--url", default="http://localhost:8844", help="Control plane URL (after tunnel)")
    agent.add_argument("--write-config", required=True, help="CLI/agent configuration file (created with 0600 permissions)")

    add_doctor_subcommand(subparsers)

    # One IBM i table to Snowflake on one machine with Docker (local.cli).
    add_local_subcommand(subparsers)

    status = subparsers.add_parser("status", help="Display the state of the latest known plan for this site.")
    status.add_argument("--name", required=True)
    status.add_argument("--workdir", default=None)

    # Client /v2 (contrat §5, tâche 19) : sources, destinations, tables,
    # pipelines, confirmations, tokens, users, audit, events, webhooks —
    # toutes sorties en JSON, voir v2_commands.py. QUADRINGENT_URL/
    # QUADRINGENT_TOKEN (ou ~/.quadringent/cli.json) configurent la cible.
    add_v2_subcommands(subparsers)

    # Pont MCP stdio<->streamable HTTP distant (contrat §4/§5, tâche 19).
    add_mcp_subcommand(subparsers)

    from quadringent.qualification.cli import add_parser
    add_parser(subparsers)
    from quadringent.recovery_cli import add_recovery_subcommand
    add_recovery_subcommand(subparsers)
    return parser


def _resolve_workdir(name: str, override: str | None) -> Path:
    return Path(override) if override else _default_workdir(name)


def _run_install(args: argparse.Namespace, runner: CommandRunner, stdout: TextIO) -> int:
    try:
        inputs = InstallInputs(
            cloud=args.cloud,
            target=args.target,
            region=args.region,
            name=args.name,
            namespace=args.namespace,
            existing_bucket=args.existing_bucket,
            existing_checkpoint_table=args.existing_checkpoint_table,
            project=args.project,
            aws_profile=args.aws_profile,
            vpc_id=args.vpc_id,
            subnet_id=args.subnet_id,
            vm_instance_type=args.vm_instance_type,
            gcp_network=args.gcp_network,
            gcp_subnetwork=args.gcp_subnetwork,
            gcp_zone=args.gcp_zone,
            image_pull_secret=args.image_pull_secret,
            eks_oidc_provider_arn=args.eks_oidc_provider_arn,
            eks_oidc_provider_url=args.eks_oidc_provider_url,
            admin_email=args.admin_email,
        )
    except InvalidInstallInputs as error:
        print(f"Invalid inputs: {error}", file=stdout)
        return 2

    try:
        manifest = ReleaseManifest.from_file(Path(args.release_manifest), repository_override=args.image_repository)
    except InvalidReleaseManifest as error:
        print(f"Invalid release manifest: {error}", file=stdout)
        return 2

    workdir = _resolve_workdir(inputs.name, args.workdir)
    assets_dir = Path(args.assets_dir or ".").expanduser().resolve()
    module_dir = _vm_module_dir(inputs.cloud) if inputs.target == "vm" else _addon_module_dir(inputs.cloud)
    required_assets = (
        assets_dir / "chart" / "Chart.yaml",
        assets_dir / _base_module_dir(inputs.cloud) / "main.tf",
        assets_dir / module_dir / "main.tf",
    )
    missing_assets = [str(path) for path in required_assets if not path.is_file()]
    if missing_assets:
        print(
            "Installation assets not found: " + ", ".join(missing_assets) + ". "
            "Extract the quadringent-<version>.tar.gz sdist from the same release and pass --assets-dir <extracted-root>.",
            file=stdout,
        )
        return 2
    # Conserver l'aperçu historique depuis le checkout ; une racine explicite
    # issue d'une release fournit des chemins absolus utilisables ailleurs.
    source_root = assets_dir if args.assets_dir is not None else None

    if args.dry_run:
        plan = build_plan(inputs, workdir, manifest, assets_dir=source_root)
        print(plan.render_text(), file=stdout)
        print("", file=stdout)
        print("(--dry-run: no command executed, no file written)", file=stdout)
        return 0

    if not args.skip_preflight:
        results = run_preflight(inputs, runner, check_ibmi_host=args.check_ibmi)
        failed = [result for result in results if not result.ok]
        for result in results:
            marker = "OK " if result.ok else "KO "
            print(f"[{marker}] {result.name} : {result.detail}", file=stdout)
        if failed:
            print("", file=stdout)
            print("Preflight failed: fix the failed checks before running install again.", file=stdout)
            _write_state(workdir, inputs, "preflight_failed")
            return 1

    workdir.mkdir(parents=True, exist_ok=True)
    # L'exécution réelle a sa propre orchestration (apply.py) : les sorties
    # Terraform du socle ne sont connues qu'une fois réellement appliquées
    # (gap (3)) et chaque module tourne dans une copie privée de l'espace de
    # travail du site (gap (1)) — `plan.steps` n'est qu'un aperçu textuel
    # statique, imprimé par --dry-run, jamais rejoué tel quel ici.
    code = execute_install(
        inputs, manifest, workdir, runner, stdout,
        yes=args.yes, allow_destroy=args.allow_destroy,
        assets_dir=source_root,
    )
    _write_state(workdir, inputs, "installed" if code == 0 else "failed")
    return code


def _vm_connection(state: dict, workdir: Path, runner: CommandRunner, env: dict[str, str], outputs: dict):
    if state.get("cloud") == "aws":
        return connect_aws_vm(outputs["instance_id"]["value"], workdir, runner, env)
    if state.get("cloud") == "gcp":
        return connect_gcp_vm(
            outputs["instance_name"]["value"], outputs["zone"]["value"], state["project"],
            workdir, runner, env,
        )
    raise VmAccessError("unknown VM cloud")


def _run_uninstall(args: argparse.Namespace, runner: CommandRunner, stdout: TextIO) -> int:
    workdir = _resolve_workdir(args.name, args.workdir)
    argv = ("helm", "uninstall", args.name, "--namespace", args.namespace)
    if args.dry_run:
        print(f"$ {' '.join(argv)}", file=stdout)
        print(
            "Reminder: Terraform resources (bucket, checkpoint table, identities, VM) are not "
            "deleted by this command — see docs/product/install-default.md § Uninstall.",
            file=stdout,
        )
        return 0
    state = _read_state(workdir)
    if not state or state.get("name") != args.name or state.get("namespace") != args.namespace:
        print("Unknown installation or different namespace: uninstall refused.", file=stdout)
        return 2
    env: dict[str, str] = {}
    if state.get("cloud") == "aws":
        env = {"AWS_REGION": state["region"], "AWS_DEFAULT_REGION": state["region"]}
        profile = args.aws_profile or state.get("aws_profile")
        if profile:
            env["AWS_PROFILE"] = profile
    if state.get("target") == "vm":
        result = runner.run(("terraform", "output", "-json"), cwd=str(workdir / "terraform" / "vm"), env=env)
        if not result.ok:
            print("VM Terraform outputs unavailable: uninstall refused.", file=stdout)
            return 1
        try:
            outputs = json.loads(result.stdout)
            with _vm_connection(state, workdir, runner, env, outputs) as vm_env:
                result = runner.run(argv, env=vm_env)
        except (KeyError, TypeError, ValueError, VmAccessError) as error:
            print(f"Cannot access the VM: {error}", file=stdout)
            return 1
    else:
        result = runner.run(argv, env=env)
    print(result.stdout, file=stdout)
    if not result.ok:
        print(result.stderr, file=stdout)
        return 1
    state["status"] = "uninstalled"
    (workdir / STATE_FILENAME).write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


def _run_status(args: argparse.Namespace, stdout: TextIO) -> int:
    workdir = _resolve_workdir(args.name, args.workdir)
    state = _read_state(workdir)
    if state is None:
        print(f"No known state for {args.name!r} in {workdir}.", file=stdout)
        return 1
    print(json.dumps(state, indent=2, ensure_ascii=False), file=stdout)
    return 0


def _run_vm_tunnel(args: argparse.Namespace, runner: CommandRunner, stdout: TextIO) -> int:
    workdir = _resolve_workdir(args.name, args.workdir)
    state = _read_state(workdir)
    if not state or state.get("cloud") not in ("aws", "gcp") or state.get("target") != "vm":
        print("No known VM installation in this working directory.", file=stdout)
        return 2
    env: dict[str, str] = {}
    if state["cloud"] == "aws":
        env = {"AWS_REGION": state["region"], "AWS_DEFAULT_REGION": state["region"]}
        profile = args.aws_profile or state.get("aws_profile")
        if profile:
            env["AWS_PROFILE"] = profile
    result = runner.run(("terraform", "output", "-json"), cwd=str(workdir / "terraform" / "vm"), env=env)
    if not result.ok:
        print("VM Terraform outputs unavailable.", file=stdout)
        return 1
    try:
        outputs = json.loads(result.stdout)
        with _vm_connection(state, workdir, runner, env, outputs) as vm_env:
            kubeconfig = shlex.quote(vm_env["KUBECONFIG"])
            namespace = shlex.quote(str(state["namespace"]))
            release = shlex.quote(str(state["name"]))
            mode = "SSM" if state["cloud"] == "aws" else "IAP"
            print(f"Tunnel {mode} to k3s is active. In another terminal:", file=stdout, flush=True)
            print(
                f"KUBECONFIG={kubeconfig} kubectl -n {namespace} port-forward "
                f"deployment/{release}-quadringent-control-plane 8844:8844",
                file=stdout, flush=True,
            )
            print(f"Then open http://127.0.0.1:8844; Ctrl+C closes the {mode}.", file=stdout, flush=True)
            while True:
                time.sleep(1)
    except (KeyError, TypeError, ValueError, VmAccessError) as error:
        print(f"VM tunnel unavailable: {error}", file=stdout)
        return 1
    except KeyboardInterrupt:
        mode = "SSM" if state["cloud"] == "aws" else "IAP"
        print(f"Tunnel {mode} closed.", file=stdout)
        return 0


def _write_private_agent_config(path: Path, *, url: str, token: str) -> None:
    """Remplace la configuration après écriture privée, synchronisation et fermeture."""
    if path.is_symlink():
        raise OSError("the destination must not be a symbolic link")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1  # Le flux possède désormais le descripteur.
            json.dump({"url": url, "token": token}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        # Revalider après l'écriture ; replace ne suit jamais le lien final.
        if path.is_symlink():
            raise OSError("the destination must not be a symbolic link")
        os.replace(temporary_path, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)


def _run_agent_token(args: argparse.Namespace, runner: CommandRunner, stdout: TextIO) -> int:
    path = Path(args.write_config)
    if path.is_symlink():
        print("Configuration refused: the destination is a symbolic link.", file=stdout)
        return 2
    try:
        script = agent_token_script(args.label, args.scope, days=args.days)
    except InvalidInstallInputs as error:
        print(f"Invalid inputs: {error}", file=stdout)
        return 2
    result = runner.run((
        "kubectl", "-n", args.namespace, "exec", f"deployment/{args.name}-quadringent-control-plane",
        "-c", "control-plane-v2", "--",
        # -P : ne pas préfixer le répertoire courant (/app contient un
        # quadringent_control_plane.py qui masquerait le paquet).
        "python", "-P", "-c", script,
    ))
    if not result.ok:
        print("Token issuance failed (kubectl exec):", file=stdout)
        print(result.stderr[-2000:], file=stdout)
        return 1
    try:
        body = json.loads(result.stdout)
        token_id, token = body["id"], body["token"]
    except (ValueError, KeyError, TypeError):
        print("Unexpected control plane response: token not recorded", file=stdout)
        return 1
    try:
        _write_private_agent_config(path, url=args.url, token=token)
    except OSError:
        print("Cannot write configuration: previous file preserved, token not displayed.", file=stdout)
        return 1
    print(json.dumps({"token_id": token_id, "scope": args.scope, "days": args.days, "config": str(path)}), file=stdout)
    return 0


def run(argv: Sequence[str], *, runner: CommandRunner | None = None, stdout: TextIO | None = None) -> int:
    """Point d'entrée testable : injecte le runner et le flux de sortie."""

    parser = _build_parser()
    args = parser.parse_args(argv)
    runner = runner if runner is not None else SubprocessRunner()
    stdout = stdout if stdout is not None else sys.stdout

    if args.command == "qualification":
        from quadringent.qualification.cli import run_native
        return run_native(args.config,args.out_dir,args.phase,stdout=stdout)
    if args.command == "recovery":
        from quadringent.recovery_cli import run_recovery_command
        return run_recovery_command(args, stdout=stdout)
    if args.command == "install":
        return _run_install(args, runner, stdout)
    if args.command == "uninstall":
        return _run_uninstall(args, runner, stdout)
    if args.command == "agent-token":
        return _run_agent_token(args, runner, stdout)
    if args.command == "status":
        return _run_status(args, stdout)
    if args.command == "doctor":
        return run_doctor(args, runner, stdout)
    if args.command == "local":
        return run_local_command(args, runner, stdout)
    if args.command == "vm-tunnel":
        return _run_vm_tunnel(args, runner, stdout)
    if args.command == "mcp":
        return run_mcp_command(args, stdout=stdout)
    if hasattr(args, "handler"):
        # Sous-commandes /v2 (sources, destinations, tables, pipelines,
        # actions, confirmations, tokens, users, audit, events, webhooks) —
        # voir v2_commands.py::add_v2_subcommands.
        return run_v2_command(args, stdout=stdout)
    return 2


def main() -> None:
    sys.exit(run(sys.argv[1:]))

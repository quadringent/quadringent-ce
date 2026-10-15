"""Contrôles avant installation : outils, identifiants, joignabilité IBM i.

Aucune valeur d'identifiant n'est jamais affichée : seule sa présence est
vérifiée (variable d'environnement non vide, ou fichier de configuration
présent). The optional IBM i probe tests TCP reachability only (database,
sign-on, command and, on the default JTOpen path, the server mapper); TLS
and sign-on are tested by ``quadringent doctor --tls`` and the wizard.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import socket

from ..diagnostics.network import ibmi_ports, probe_tcp
from .plan import REQUIRED_TOOLS, InstallInputs
from .runner import CommandRunner

AWS_CREDENTIAL_ENV_VARS = ("AWS_ACCESS_KEY_ID", "AWS_PROFILE", "AWS_ROLE_ARN")
GCP_CREDENTIAL_ENV_VARS = ("GOOGLE_APPLICATION_CREDENTIALS", "CLOUDSDK_CORE_ACCOUNT")

IBMI_PORTS = ibmi_ports()


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


def check_tools(runner: CommandRunner, tools: tuple[str, ...] = REQUIRED_TOOLS) -> list[CheckResult]:
    results = []
    for tool in tools:
        found = runner.which(tool)
        results.append(CheckResult(f"tool {tool}", found is not None, found or "not found on PATH"))
    return results


def check_cloud_credentials(cloud: str, environ: dict[str, str] | None = None) -> CheckResult:
    environ = dict(os.environ) if environ is None else environ
    names = AWS_CREDENTIAL_ENV_VARS if cloud == "aws" else GCP_CREDENTIAL_ENV_VARS
    present = [name for name in names if environ.get(name)]
    if present:
        # Jamais la valeur : seulement la variable détectée.
        return CheckResult("cloud credentials", True, f"detected via {', '.join(present)}")
    return CheckResult(
        "cloud credentials",
        False,
        f"none of the variables {', '.join(names)} is configured (credentials not detected, values never displayed)",
    )


def check_ibmi_reachability(
    host: str,
    *,
    ports: dict[str, int] | None = None,
    timeout_seconds: float = 3.0,
    socket_factory=socket.create_connection,
) -> list[CheckResult]:
    """TCP reachability of the IBM i ports on the JTOpen path; never TLS or sign-on."""

    results = []
    for label, port in (ports or IBMI_PORTS).items():
        probe = probe_tcp(host, port, label, timeout_seconds=timeout_seconds, connector=socket_factory)
        detail = (
            "TCP reachable; TLS and sign-on not tested (quadringent doctor --tls)"
            if probe.status == "passed" else f"TCP {probe.evidence['error']}; {probe.next_action}"
        )
        results.append(CheckResult(f"IBM i {label} ({port}/tcp)", probe.status == "passed", detail))
    return results


def run_preflight(
    inputs: InstallInputs,
    runner: CommandRunner,
    *,
    check_ibmi_host: str | None = None,
    environ: dict[str, str] | None = None,
) -> list[CheckResult]:
    results = check_tools(runner)
    if inputs.cloud == "aws":
        results.extend(check_tools(runner, ("aws",)))
        if inputs.target == "vm":
            results.extend(check_tools(runner, ("session-manager-plugin",)))
    else:
        results.extend(check_tools(runner, ("gcloud",)))
    effective_environ = dict(os.environ if environ is None else environ)
    if inputs.cloud == "aws" and inputs.aws_profile:
        effective_environ["AWS_PROFILE"] = inputs.aws_profile
    credentials = check_cloud_credentials(inputs.cloud, effective_environ)
    if inputs.cloud == "gcp" and runner.which("gcloud") is not None:
        # Terraform accepte ADC ou GOOGLE_APPLICATION_CREDENTIALS, mais SSH
        # sur IAP utilise aussi l'identité CLI. Vérifier les deux sans jamais
        # écrire le compte ou le jeton dans la sortie du pré-vol.
        cli = runner.run(("gcloud", "auth", "list", "--filter=status:ACTIVE", "--format=value(account)"))
        cli_ok = cli.ok and bool(cli.stdout.strip())
        adc_ok = bool(effective_environ.get("GOOGLE_APPLICATION_CREDENTIALS"))
        if not adc_ok:
            adc = runner.run(("gcloud", "auth", "application-default", "print-access-token"))
            adc_ok = adc.ok and bool(adc.stdout.strip())
        credentials = CheckResult(
            "cloud credentials", cli_ok and adc_ok,
            "gcloud CLI and ADC available" if cli_ok and adc_ok else "gcloud CLI or ADC unavailable",
        )
    results.append(credentials)
    if check_ibmi_host:
        results.extend(check_ibmi_reachability(check_ibmi_host))
    return results

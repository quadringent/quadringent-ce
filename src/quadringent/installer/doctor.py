"""``quadringent doctor``: check prerequisites and explain what blocks.

Local checks always run. Network calls happen only on request and are
bounded: ``--check-ibmi`` (TCP), ``--tls`` (verified TLS handshake) and
``--control-plane`` (``GET /v2/diagnostics``). The doctor never signs on to
IBM i, never connects to Snowflake and never changes anything.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import sys
from typing import Callable, Mapping, TextIO

from ..diagnostics.export import ExportPathError, ExportTooLargeError, build_export, write_archive
from ..diagnostics.local import cloud_credentials_result, tool_results
from ..diagnostics.model import (
    EXIT_USAGE,
    DiagnosticContractError,
    DiagnosticReport,
    DiagnosticResult,
    Subject,
    parse_report,
    utc_now,
)
from ..diagnostics.network import authentication_not_observed, ibmi_ports, probe_tcp, probe_tls
from ..diagnostics.render import render_text
from .api_client import DEFAULT_CONFIG_PATH, ConfigError, load_config
from .plan import REQUIRED_TOOLS
from .runner import CommandRunner

HELP = """Check prerequisites and explain what blocks an installation.

Exit codes: 0 ready, 1 blocked (a check failed), 2 usage error,
3 incomplete (a required check is unknown or was not run).
Network calls only happen with --check-ibmi, --tls or --control-plane."""

Fetch = Callable[[str, Mapping[str, object] | None, float], tuple[int, object]]


def add_doctor_subcommand(subparsers) -> None:
    parser = subparsers.add_parser("doctor", help="Check prerequisites and explain what blocks.",
                                   description=HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true", help="Print the quadringent.diagnostic.v1 JSON report")
    parser.add_argument("--cloud", choices=("aws", "gcp"), help="Also check the tools and credential configuration for this cloud")
    parser.add_argument("--target", choices=("vm", "cluster"), default="cluster")
    parser.add_argument("--check-ibmi", metavar="HOST", help="Test TCP reachability of the IBM i ports (network call)")
    parser.add_argument("--tls", action="store_true", help="With --check-ibmi: verified TLS handshake on the TLS ports (network call)")
    parser.add_argument("--ibmi-ca-file", metavar="PATH", help="CA bundle trusted for the IBM i certificate")
    parser.add_argument("--database-port", type=int, help="Explicit JTOpen database port (AS400_DATABASE_PORT)")
    parser.add_argument("--signon-port", type=int, help="Explicit JTOpen sign-on port (AS400_SIGNON_PORT)")
    parser.add_argument("--command-port", type=int, help="Explicit JTOpen command port (AS400_COMMAND_PORT)")
    parser.add_argument("--control-plane", action="store_true",
                        help="Read GET /v2/diagnostics from QUADRINGENT_URL or ~/.quadringent/cli.json (network call)")
    parser.add_argument("--pipeline-id", help="With --control-plane: only this pipeline")
    parser.add_argument("--timeout", type=float, default=5.0, help="Seconds per network call (at most 10)")
    parser.add_argument("--from-report", metavar="FILE", help="Offline: read a saved JSON report instead of checking")
    parser.add_argument("--save", metavar="FILE", help="Write the JSON report to a new private file")
    parser.add_argument("--export", metavar="FILE.tar.gz", help="Write a pseudonymized diagnostic archive")
    parser.add_argument("--preview", action="store_true", help="With --export: print what the archive would contain, write nothing")
    parser.add_argument("--keep-identifiers", action="store_true", help="With --export: keep host, account and object names")


def _version() -> tuple[str, str]:
    try:
        from importlib.metadata import PackageNotFoundError, version
        return version("quadringent"), "installed"
    except PackageNotFoundError:
        return "unknown", "checkout"


def _default_fetch(base_url: str, token: str | None) -> Fetch:
    def fetch(path, params, timeout):
        import httpx
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        with httpx.Client(base_url=base_url, headers=headers, timeout=timeout) as client:
            response = client.get(path, params=params)
            try:
                return response.status_code, response.json()
            except ValueError:
                return response.status_code, None
    return fetch


def _control_plane_failure(summary: str, action: str, *, error: str, status: str = "failed") -> DiagnosticResult:
    return DiagnosticResult(
        code="control_plane.api", layer="control_plane", status=status, required=True, observed_at=utc_now(),
        provenance="doctor:control-plane", subject=Subject("control_plane", "v2"), summary=summary,
        next_action=action, limits="Capture, delivery and evidence could not be read from the control plane.",
        evidence={"error": error},
    )


def _control_plane_results(fetch: Fetch, pipeline_id: str | None, timeout: float) -> list[DiagnosticResult]:
    try:
        status, body = fetch("/v2/diagnostics", {"pipeline_id": pipeline_id} if pipeline_id else None,
                             max(0.5, min(timeout, 10.0)))
    except (OSError, socket.timeout) as error:
        reason = "timed out" if isinstance(error, (socket.timeout, TimeoutError)) else "connection refused"
        return [_control_plane_failure(f"The control plane did not answer: {reason}.",
                                       "Open the private tunnel or port-forward, check QUADRINGENT_URL, then run again.",
                                       error=reason)]
    except Exception as error:  # httpx errors: never print their raw text
        name = type(error).__name__.lower()
        reason = "timed out" if "timeout" in name else "connection failed"
        return [_control_plane_failure(f"The control plane did not answer: {reason}.",
                                       "Open the private tunnel or port-forward, check QUADRINGENT_URL, then run again.",
                                       error=reason)]
    if status == 401:
        return [_control_plane_failure("The control plane refused the token (401).",
                                       "Issue a token with quadringent agent-token --scope read and set QUADRINGENT_TOKEN.",
                                       error="status 401")]
    if status == 403:
        return [_control_plane_failure("The token has no permission to read diagnostics (403).",
                                       "Use a token with the read scope or higher.", error="status 403")]
    if status == 404:
        return [_control_plane_failure("The control plane does not know this pipeline or has no diagnostics route (404).",
                                       "Check --pipeline-id, or upgrade the control plane to a version with /v2/diagnostics.",
                                       error="status 404")]
    if status == 429:
        return [_control_plane_failure("The control plane is limiting requests (429).",
                                       "Wait before running the doctor again; do not loop.", error="status 429",
                                       status="unknown")]
    if status >= 500:
        return [_control_plane_failure(f"The control plane is unavailable ({status}).",
                                       "Check the control plane pod and its database, then run again.",
                                       error=f"status {status}")]
    try:
        return list(parse_report(body).results)
    except DiagnosticContractError:
        return [_control_plane_failure("The control plane answered with a report outside quadringent.diagnostic.v1.",
                                       "Upgrade the doctor and the control plane to matching versions.",
                                       error="contract mismatch")]


def build_local_report(args, runner: CommandRunner, *, environ: Mapping[str, str], fetch: Fetch | None,
                       connector=None, handshake=None) -> DiagnosticReport:
    results: list[DiagnosticResult] = []
    results += tool_results(runner.which, REQUIRED_TOOLS)
    if args.cloud == "aws":
        results += tool_results(runner.which, ("aws",) + (("session-manager-plugin",) if args.target == "vm" else ()))
    elif args.cloud == "gcp":
        results += tool_results(runner.which, ("gcloud",))
    if args.cloud:
        results.append(cloud_credentials_result(args.cloud, environ))
    if args.check_ibmi:
        ports = ibmi_ports(database=args.database_port, signon=args.signon_port, command=args.command_port)
        for label, port in ports.items():
            kwargs = {"connector": connector} if connector else {}
            results.append(probe_tcp(args.check_ibmi, port, label, timeout_seconds=args.timeout, **kwargs))
        for label, port in ports.items():
            if label == "server_mapper":
                continue
            kwargs = {"handshake": handshake} if handshake else {}
            results.append(probe_tls(args.check_ibmi, port, label, attempt=args.tls, ca_file=args.ibmi_ca_file,
                                     timeout_seconds=args.timeout, **kwargs))
        results.append(authentication_not_observed("host", args.check_ibmi))
    if args.control_plane and fetch is not None:
        results += _control_plane_results(fetch, args.pipeline_id, args.timeout)
    elif not args.control_plane:
        results.append(DiagnosticResult(
            code="control_plane.api", layer="control_plane", status="skipped", required=True, observed_at=utc_now(),
            provenance="doctor:not-requested", subject=Subject("control_plane", "v2"),
            summary="The control plane was not read; capture, delivery and evidence are not observed.",
            next_action="Run quadringent doctor --control-plane once the tunnel to the control plane is open.",
            limits="Local checks only.",
        ))
    version, distribution = _version()
    return DiagnosticReport(results=tuple(results), generated_at=utc_now(), scope="local", synthetic=False,
                            software={"product": "quadringent", "version": version, "distribution": distribution})


def _write_private(path: Path, content: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)


def run_doctor(args, runner: CommandRunner, stdout: TextIO, *, environ: Mapping[str, str] | None = None,
               fetch: Fetch | None = None, connector=None, handshake=None, config_path: Path | None = None) -> int:
    environ = dict(os.environ) if environ is None else environ
    if args.preview and not args.export:
        print("--preview requires --export FILE.tar.gz", file=stdout)
        return EXIT_USAGE
    if args.from_report:
        try:
            report = parse_report(json.loads(Path(args.from_report).read_text(encoding="utf-8")))
        except (OSError, ValueError, DiagnosticContractError) as error:
            print(f"Cannot read the saved report: {type(error).__name__}: {str(error)[:200]}", file=stdout)
            return EXIT_USAGE
    else:
        if args.control_plane and fetch is None:
            try:
                config = load_config(env=dict(environ), config_path=config_path or DEFAULT_CONFIG_PATH)
            except ConfigError as error:
                print(f"Control plane configuration: {error}", file=stdout)
                return EXIT_USAGE
            fetch = _default_fetch(config.base_url, config.token)
        report = build_local_report(args, runner, environ=environ, fetch=fetch, connector=connector,
                                    handshake=handshake)
    if args.save:
        try:
            _write_private(Path(args.save), json.dumps(report.to_dict(), indent=2) + "\n")
        except OSError as error:
            print(f"Report not saved ({type(error).__name__}): choose a new file in an existing folder.", file=stdout)
            return EXIT_USAGE
    if args.export:
        try:
            bundle = build_export(report, keep_identifiers=args.keep_identifiers)
            if args.preview:
                print(json.dumps(bundle.preview, indent=2), file=stdout)
                return report.exit_code()
            write_archive(bundle, Path(args.export))
        except (ExportPathError, ExportTooLargeError) as error:
            print(f"Export refused: {error}", file=stdout)
            return EXIT_USAGE
        print(f"Export written to {args.export} (private, pseudonymized; nothing was sent).", file=stdout)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2), file=stdout)
    elif not args.export:
        stdout.write(render_text(report))
    return report.exit_code()


def main() -> None:  # pragma: no cover - convenience entry for python -m
    from .cli import run
    sys.exit(run(["doctor", *sys.argv[1:]]))

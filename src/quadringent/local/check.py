"""``quadringent local check``: a quadringent.diagnostic.v1 report for one local instance.

Without options nothing signs on to the IBM i and nothing connects to Snowflake:
sign-on, table discovery and the Snowflake check each run only when asked for.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable

from ..diagnostics.local import tool_results
from ..diagnostics.model import DiagnosticReport, DiagnosticResult, Subject, utc_now
from ..diagnostics.network import SERVER_MAPPER_PORT, authentication_not_observed, probe_tcp, probe_tls
from ..installer.runner import CommandRunner
from . import dockerplan as plan
from .config import ConfigError, LocalConfig, LocalPaths, check_layout
from .discovery import JobOutputError, assess_table, find_table, parse_discovery, parse_probe, result_line
from .images import package_version_string
from .orchestrator import RESULT_PREFIX, Docker, StartError, failure_reason

PLAINTEXT_PORTS = {"database": 8471, "signon": 8476, "command": 8475}
TLS_PORTS = {"database": 9471, "signon": 9476, "command": 9475}
MAX_TEXT = 480


@dataclass(frozen=True)
class CheckOptions:
    signon: bool = False
    discover: bool = False
    snowflake: bool = False
    timeout: float = 5.0


def _result(code: str, layer: str, status: str, summary: str, next_action: str, limits: str, *, required: bool = True,
            provenance: str = "local:check", subject: Subject | None = None, evidence: dict | None = None) -> DiagnosticResult:
    return DiagnosticResult(
        code=code, layer=layer, status=status, required=required, observed_at=utc_now(), provenance=provenance,
        subject=subject, summary=summary[:MAX_TEXT], next_action=next_action[:MAX_TEXT], limits=limits[:MAX_TEXT],
        evidence=evidence or {},
    )


_LOCAL_TLS_NEXT = ("Run quadringent local init again with --ibmi-ca-file PEM (the CA that signs the IBM i "
                   "certificate), or ask the IBM i administrator for a certificate trusted for this host name. "
                   "Never disable certificate verification.")


def _local_next_action(result: DiagnosticResult) -> DiagnosticResult:
    """The shared probes speak to the managed install; local mode has its own commands."""
    if result.layer == "tls" and result.status == "failed" and result.evidence.get("error") != "CA file unreadable":
        return replace(result, next_action=_LOCAL_TLS_NEXT)
    if "quadringent doctor" in result.next_action or "activation wizard" in result.next_action:
        return replace(result, next_action="Fix the reported layer, then run quadringent local check again.")
    return result


def ibmi_ports(config: LocalConfig) -> dict[str, int]:
    """Ports JTOpen uses: the secure or plain defaults, each replaceable; 449 only while one is looked up."""
    defaults = TLS_PORTS if config.ibmi.tls else PLAINTEXT_PORTS
    overrides = {"database": config.ibmi.database_port, "signon": config.ibmi.signon_port,
                 "command": config.ibmi.command_port}
    ports = {name: overrides[name] or default for name, default in defaults.items()}
    if any(value is None for value in overrides.values()):
        ports["server_mapper"] = SERVER_MAPPER_PORT
    return ports


def _not_run(code: str, layer: str, flag: str, what: str, subject: Subject) -> DiagnosticResult:
    return _result(
        code, layer, "skipped", f"{what} was not checked.",
        f"Run quadringent local check {flag} once the previous checks pass.",
        "Not requested: this check contacts a remote system.", provenance="local:not-requested", subject=subject,
    )


class LocalChecker:
    def __init__(self, config: LocalConfig, paths: LocalPaths, runner: CommandRunner, *, uid: int, gid: int,
                 connector: Callable | None = None, handshake: Callable | None = None) -> None:
        self.config, self.paths, self.runner = config, paths, runner
        self.docker = Docker(runner, config, paths, uid=uid, gid=gid)
        self.connector, self.handshake = connector, handshake
        self.cl_commands: list[tuple[str, tuple[str, ...]]] = []

    def build(self, options: CheckOptions) -> DiagnosticReport:
        results: list[DiagnosticResult] = []
        results += self._local(options)
        layout_ok = not any(item.code == "local_config.instance" and item.status == "failed" for item in results)
        results += self._network(options)
        docker_ready = self._docker_ready(results)
        results += self._remote(options, docker_ready, layout_ok)
        return DiagnosticReport(results=tuple(results), generated_at=utc_now(), scope="local", synthetic=False,
                                software={"product": "quadringent", "version": package_version_string(),
                                          "distribution": "local-mode"})

    # -- local

    def _local(self, options: CheckOptions) -> list[DiagnosticResult]:
        results = tool_results(self.runner.which, ("docker",))
        try:
            check_layout(self.paths, data=not self.config.uses_volume)
            held = (f"configuration and secrets have private permissions (data in the Docker volume "
                    f"{self.config.volume_name})" if self.config.uses_volume
                    else "configuration, secrets and data directory have private permissions")
            results.append(_result(
                "local_config.instance", "local_config", "passed",
                f"Instance {self.config.name!r}: {held}.",
                "No action.", "Permissions and file types only; secret values are not read.",
                subject=Subject("source", self.config.name)))
        except ConfigError as error:
            results.append(_result(
                "local_config.instance", "local_config", "failed", f"Instance files are unsafe: {error}.",
                "Fix the permission or remove the link, or run quadringent local init again for a new name.",
                "Permissions and file types only.", subject=Subject("source", self.config.name)))
        if not self.config.ibmi.tls:
            results.append(_result(
                "tls.plaintext_accepted", "tls", "skipped",
                "WARNING: plaintext IBM i connection (--allow-plaintext-ibmi): the password and the data are not encrypted.",
                "Prefer TLS: ask the IBM i administrator to enable the secure host servers.",
                "TLS is not tested in plaintext mode.", required=False, subject=Subject("host", self.config.ibmi.host)))
        return results

    def _docker_ready(self, results: list[DiagnosticResult]) -> bool:
        if self.runner.which("docker") is None:
            return False
        try:
            self.docker.ready()
        except StartError:
            results.append(_result(
                "local_tools.docker_daemon", "local_tools", "failed", "The Docker daemon does not answer.",
                "Start Docker (Docker Desktop or the docker service) and run the check again.",
                "Server version query only."))
            return False
        results.append(_result(
            "local_tools.docker_daemon", "local_tools", "passed", "The Docker daemon answers.", "No action.",
            "Server version query only."))
        for role, image in (("capture", self.config.capture_image), ("loader", self.config.loader_image)):
            present = self.runner.run(("docker", "image", "inspect", image)).ok
            results.append(_result(
                f"local_config.image_{role}", "local_config", "passed" if present else "skipped",
                f"The {role} image is {'present' if present else 'not present'} locally.",
                "No action." if present else "quadringent local start pulls it; or run docker pull yourself.",
                "Local image cache only.", required=False, subject=Subject("tool", image[:190])))
        return True

    # -- network

    def _network(self, options: CheckOptions) -> list[DiagnosticResult]:
        host = self.config.ibmi.host
        ports = ibmi_ports(self.config)
        results = []
        for label, port in ports.items():
            kwargs = {"connector": self.connector} if self.connector else {}
            results.append(probe_tcp(host, port, label, timeout_seconds=options.timeout, **kwargs))
        if self.config.ibmi.tls:
            ca_file = str(self.paths.ca_file) if self.config.ibmi.ca_file else None
            for label, port in ports.items():
                if label == "server_mapper":
                    continue
                kwargs = {"handshake": self.handshake} if self.handshake else {}
                results.append(probe_tls(host, port, label, attempt=True, ca_file=ca_file,
                                         timeout_seconds=options.timeout, **kwargs))
        return [_local_next_action(result) for result in results]

    # -- remote systems

    def _remote(self, options: CheckOptions, docker_ready: bool, layout_ok: bool = True) -> list[DiagnosticResult]:
        config = self.config
        host_subject = Subject("host", config.ibmi.host)
        table_subject = Subject("table", f"{config.table_schema}/{config.table_name}")
        destination_subject = Subject("destination", config.snowflake.account)
        results: list[DiagnosticResult] = []
        want_discover = options.discover or (options.signon and not config.ibmi.tls)
        if not (options.signon or want_discover or options.snowflake):
            return [authentication_not_observed("host", config.ibmi.host),
                    _not_run("source_prerequisites.table", "source_prerequisites", "--discover", "The table",
                             table_subject),
                    _not_run("destination.snowflake", "destination", "--snowflake", "The Snowflake destination",
                             destination_subject)]
        if not layout_ok:
            # The containers mount the secret files and sign on: never with files that are not private.
            return [_result(
                "local_tools.docker_runtime", "local_tools", "failed",
                "The remote checks mount the secret files and the instance files are unsafe.",
                "Fix the instance files reported above, then run the check again.", "Nothing was contacted.")]
        if not docker_ready:
            blocked = _result(
                "local_tools.docker_runtime", "local_tools", "failed",
                "The remote checks run in containers and Docker is not ready.", "Install and start Docker.",
                "Nothing was contacted.")
            return [blocked]
        authenticated: bool | None = None
        if options.signon and config.ibmi.tls:
            probe_result, authenticated = self._probe(host_subject)
            results.append(probe_result)
        if want_discover and authenticated is False:
            # The sign-on was refused one step earlier: a second sign-on could disable the profile.
            results.append(_result(
                "source_prerequisites.table", "source_prerequisites", "skipped",
                "The table was not checked: the IBM i sign-on was refused and nothing is retried.",
                "Fix the user or password, then run the check again once.",
                "Discovery signs on again, so it does not run after a refused sign-on.",
                provenance="local:not-run", subject=table_subject))
        elif want_discover:
            discovery, signed_on = self._discover(table_subject)
            results.append(discovery)
            authenticated = signed_on if authenticated is None else (authenticated and signed_on)
        if authenticated is True:
            results.append(_result(
                "authentication.ibmi_signon", "authentication", "passed", "IBM i sign-on succeeded.", "No action.",
                "One sign-on from a container with the configured user; privileges beyond the catalog are not tested.",
                provenance="container:check", subject=host_subject))
        elif authenticated is False:
            results.append(_result(
                "authentication.ibmi_signon", "authentication", "failed",
                "IBM i sign-on did not succeed. Nothing is retried.",
                "Fix the user or password with the IBM i administrator, then run the check again once.",
                "A repeated wrong password can disable the IBM i profile.", provenance="container:check",
                subject=host_subject))
        else:
            results.append(authentication_not_observed("host", config.ibmi.host))
        if not want_discover:
            results.append(_not_run("source_prerequisites.table", "source_prerequisites", "--discover", "The table",
                                    table_subject))
        if options.snowflake:
            results.append(self._snowflake(destination_subject))
        else:
            results.append(_not_run("destination.snowflake", "destination", "--snowflake",
                                    "The Snowflake destination", destination_subject))
        return results

    def _probe(self, subject: Subject) -> tuple[DiagnosticResult, bool | None]:
        result = self.docker.run(plan.probe_spec(self.config, self.paths, port=self.config.ibmi.signon_port))
        try:
            probe = parse_probe(result.stdout)
        except JobOutputError:
            return (_result("source_prerequisites.probe", "source_prerequisites", "failed",
                            f"The source probe printed no result: {failure_reason(result)}.",
                            "Check the capture image and the secret files, then run the check again.",
                            "The probe runs in a container.", provenance="container:probe", subject=subject), None)
        zone = self.config.source_time_zone or probe.detected_timezone
        if not probe.network_ok:
            failure = probe.network_detail
        elif not probe.tls_ok or probe.tls_trust == "unknown":
            failure = probe.tls_detail
        elif not probe.auth_ok:
            failure = probe.auth_detail
        else:
            failure = None
        if failure is not None:
            status, summary = "failed", f"Sign-on probe failed: {failure}."
            next_action = "Fix the reported layer, then run the check again once."
        elif zone is None:
            status = "failed"
            summary = f"Sign-on probe succeeded (IBM i {probe.ibmi_version or 'version unknown'}) but QTIMZON gives no capture time zone."
            next_action = "Pass --source-time-zone at init."
        else:
            status = "passed"
            summary = f"Sign-on probe succeeded: IBM i {probe.ibmi_version or 'version unknown'}, capture time zone {zone}."
            next_action = "No action."
        authenticated = probe.auth_ok if probe.authentication_attempted else None
        return (_result("source_prerequisites.probe", "source_prerequisites", status, summary, next_action,
                        "Network, TLS, sign-on and two system values; no business row.", provenance="container:probe",
                        subject=subject, evidence={"qtimzon": probe.qtimzon, "detected_timezone": probe.detected_timezone}),
                authenticated)

    def _discover(self, subject: Subject) -> tuple[DiagnosticResult, bool | None]:
        result = self.docker.run(plan.discover_spec(self.config, self.paths))
        try:
            tables = parse_discovery(result.stdout)
        except JobOutputError:
            return (_result("source_prerequisites.table", "source_prerequisites", "failed",
                            f"Table discovery printed no result: {failure_reason(result)}.",
                            "Check the user, the library name and the capture image, then run the check again once.",
                            "The discovery runs in a container.", provenance="container:discover", subject=subject),
                    False if not result.ok else None)
        table = find_table(tables, self.config.table_schema, self.config.table_name)
        if table is None:
            return (_result("source_prerequisites.table", "source_prerequisites", "failed",
                            "The table was not found, or the user cannot see it.",
                            "Check the library and table names and the authority of the IBM i user.",
                            "Catalog search only.", provenance="container:discover", subject=subject), True)
        assessment = assess_table(table)
        if assessment.ready:
            return (_result("source_prerequisites.table", "source_prerequisites", "passed",
                            f"Ready: journaled with *BOTH in {assessment.journal_library}/{assessment.journal_name}, "
                            f"key {', '.join(assessment.key_columns)}, {len(assessment.columns)} supported columns.",
                            "No action.", "Catalog only; row contents and journal access are checked by start.",
                            provenance="container:discover", subject=subject), True)
        commands = assessment.cl_commands()
        self.cl_commands.append((subject.ref, commands))
        evidence = {f"cl_{index}": command[:480] for index, command in enumerate(commands[:5], 1)}
        return (_result("source_prerequisites.table", "source_prerequisites", "failed",
                        " ".join(assessment.reasons),
                        "Ask the IBM i administrator to review and run the CL commands printed below." if commands
                        else "Choose another table or fix the table definition.",
                        "Catalog only; this tool never changes the IBM i.", provenance="container:discover",
                        subject=subject, evidence=evidence), True)

    def _snowflake(self, subject: Subject) -> DiagnosticResult:
        result = self.docker.run(plan.verify_spec(self.config, self.paths))
        try:
            document = result_line(result.stdout, RESULT_PREFIX)
        except JobOutputError:
            return _result("destination.snowflake", "destination", "failed",
                           f"The Snowflake check printed no result: {failure_reason(result)}.",
                           "Check that the administrator ran the setup script, then run the check again.",
                           "The check runs in a container.", provenance="container:verify", subject=subject)
        checks = document.get("checks") if isinstance(document.get("checks"), dict) else {}
        failed = sorted(name for name, value in checks.items() if isinstance(value, dict) and value.get("ok") is False)
        ok = document.get("ok") is True and not failed
        return _result(
            "destination.snowflake", "destination", "passed" if ok else "failed",
            "Snowflake accepts the key pair; role, warehouse, database, schemas and table creation are verified."
            if ok else "Snowflake check failed" + (f": {', '.join(failed)}" if failed else "") + ".",
            "No action." if ok else "Ask the Snowflake administrator to run snowflake-setup.sql (quadringent local "
            "snowflake-script prints it), then run the check again.",
            "Creates and drops one scratch table to prove privileges; no data is read.",
            provenance="container:verify", subject=subject)

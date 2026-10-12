"""IBM i network probes for ``quadringent doctor``.

Each level has its own result: a TCP connection proves reachability only, a
TLS handshake (always with certificate and host name verification) proves
the certificate chain only, and sign-on is never attempted here. JTOpen looks
up service ports through the server mapper (449) unless every service port
is configured explicitly (see JournalSession.configureServicePorts and IBM's
"How to prevent port lookup" note), so 449 is probed only in that case.
"""

from __future__ import annotations

import hashlib
import socket
import ssl
from typing import Callable

from .model import DiagnosticResult, Subject, utc_now

SERVER_MAPPER_PORT = 449
DEFAULT_TLS_PORTS = {"database": 9471, "signon": 9476, "command": 9475}
MAX_TIMEOUT_SECONDS = 10.0


def ibmi_ports(*, database: int | None = None, signon: int | None = None, command: int | None = None) -> dict[str, int]:
    """Ports on the path JTOpen actually uses for the reader configuration."""
    overrides = {"database": database, "signon": signon, "command": command}
    ports = {name: overrides[name] or default for name, default in DEFAULT_TLS_PORTS.items()}
    if any(value is None for value in overrides.values()):
        ports["server_mapper"] = SERVER_MAPPER_PORT
    return ports


def _bounded(timeout_seconds: float) -> float:
    return max(0.5, min(float(timeout_seconds), MAX_TIMEOUT_SECONDS))


def _network_error(error: BaseException) -> str:
    if isinstance(error, socket.gaierror):
        return "host name not resolved"
    if isinstance(error, (socket.timeout, TimeoutError)):
        return "timed out"
    if isinstance(error, ConnectionRefusedError):
        return "connection refused"
    if isinstance(error, ConnectionResetError):
        return "connection reset"
    return "network error"


def probe_tcp(
    host: str,
    port: int,
    label: str,
    *,
    timeout_seconds: float = 3.0,
    connector: Callable[..., object] = socket.create_connection,
) -> DiagnosticResult:
    subject = Subject("host", host, f"{label} {port}/tcp")
    required = True
    try:
        connection = connector((host, port), timeout=_bounded(timeout_seconds))
        close = getattr(connection, "close", None)
        if callable(close):
            close()
    except OSError as error:
        reason = _network_error(error)
        return DiagnosticResult(
            code=f"network.tcp.{label}", layer="network", status="failed", required=required,
            observed_at=utc_now(), provenance="probe:tcp", subject=subject,
            summary=f"TCP connection to port {port} failed: {reason}.",
            next_action=_tcp_next_action(label, port, reason),
            limits="Observed from this machine only; the reader runs elsewhere and may see another network path.",
            evidence={"port": port, "error": reason},
        )
    return DiagnosticResult(
        code=f"network.tcp.{label}", layer="network", status="passed", required=required,
        observed_at=utc_now(), provenance="probe:tcp", subject=subject,
        summary=f"TCP connection to port {port} accepted.",
        next_action="No action for reachability. Check TLS next.",
        limits="TCP reachability only: TLS validity and IBM i sign-on are not tested by this check. "
               "Observed from this machine only.",
        evidence={"port": port},
    )


def _tcp_next_action(label: str, port: int, reason: str) -> str:
    if reason == "host name not resolved":
        return "Check the IBM i host name and the DNS used by this machine and by the reader."
    if label == "server_mapper":
        return (f"Allow port {port} from the reader network, or configure every service port explicitly "
                "so JTOpen does not need the server mapper.")
    return (f"Ask the network or IBM i administrator to allow port {port} from the reader network "
            "and to check that the host server is started with TLS.")


def _verified_context(ca_file: str | None) -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=ca_file)
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def _handshake(context: ssl.SSLContext, host: str, port: int, timeout: float) -> bytes:
    with socket.create_connection((host, port), timeout=timeout) as raw:
        with context.wrap_socket(raw, server_hostname=host) as tls:
            return tls.getpeercert(binary_form=True) or b""


def _tls_error(error: BaseException) -> str:
    if isinstance(error, ssl.SSLCertVerificationError):
        message = str(getattr(error, "verify_message", "") or error).lower()
        if "hostname" in message or "ip address mismatch" in message:
            return "certificate does not match the host name"
        if "expired" in message:
            return "certificate expired"
        return "certificate not trusted"
    if isinstance(error, ssl.SSLError):
        return "TLS handshake refused"
    return _network_error(error)


def probe_tls(
    host: str,
    port: int,
    label: str,
    *,
    attempt: bool = True,
    ca_file: str | None = None,
    timeout_seconds: float = 5.0,
    handshake: Callable[[ssl.SSLContext, str, int, float], bytes] = _handshake,
) -> DiagnosticResult:
    subject = Subject("host", host, f"{label} {port}/tls")
    if not attempt:
        return DiagnosticResult(
            code=f"tls.{label}", layer="tls", status="unknown", required=True,
            observed_at=utc_now(), provenance="doctor:not-requested", subject=subject,
            summary="TLS was not tested.",
            next_action="Run quadringent doctor --check-ibmi HOST --tls, optionally with --ibmi-ca-file.",
            limits="No TLS handshake was attempted.",
            evidence={"port": port},
        )
    try:
        context = _verified_context(ca_file)
    except (OSError, ssl.SSLError):
        return DiagnosticResult(
            code=f"tls.{label}", layer="tls", status="failed", required=True,
            observed_at=utc_now(), provenance="local:ca-file", subject=subject,
            summary="The CA file given with --ibmi-ca-file cannot be read as PEM certificates.",
            next_action="Check the path and the PEM content of the CA file on this machine, then run again.",
            limits="No TLS handshake was attempted.",
            evidence={"port": port, "error": "CA file unreadable", "custom_ca": True},
        )
    try:
        certificate = handshake(context, host, port, _bounded(timeout_seconds))
    except (OSError, ssl.SSLError) as error:
        reason = _tls_error(error)
        return DiagnosticResult(
            code=f"tls.{label}", layer="tls", status="failed", required=True,
            observed_at=utc_now(), provenance="probe:tls", subject=subject,
            summary=f"TLS handshake on port {port} failed: {reason}.",
            next_action=(
                "Provide the IBM i certificate authority with --ibmi-ca-file (and to the site as the CA Secret), "
                "or ask the IBM i administrator to assign a certificate trusted for this host name. "
                "Never disable certificate verification."
            ),
            limits="Certificate verification and host name checking were enabled.",
            evidence={"port": port, "error": reason, "custom_ca": ca_file is not None},
        )
    fingerprint = hashlib.sha256(certificate).hexdigest()
    return DiagnosticResult(
        code=f"tls.{label}", layer="tls", status="passed", required=True,
        observed_at=utc_now(), provenance="probe:tls", subject=subject,
        summary=f"TLS handshake on port {port} succeeded with a verified certificate.",
        next_action="Compare the fingerprint with the one shown by the IBM i administrator.",
        limits="Certificate chain and host name only: IBM i sign-on is not tested by this check.",
        evidence={"port": port, "sha256_fingerprint": fingerprint, "custom_ca": ca_file is not None},
    )


def authentication_not_observed(kind: str, ref: str = "ibmi") -> DiagnosticResult:
    """``quadringent doctor`` never signs on: credentials stay with the site."""
    return DiagnosticResult(
        code="authentication.ibmi_signon", layer="authentication", status="unknown", required=True,
        observed_at=utc_now(), provenance="doctor:not-tested", subject=Subject(kind, ref),
        summary="IBM i sign-on was not tested; configured credentials are not proof of authentication.",
        next_action="Run Test in the activation wizard, or read the control plane diagnostic with --control-plane.",
        limits="The doctor never sends IBM i credentials.",
    )

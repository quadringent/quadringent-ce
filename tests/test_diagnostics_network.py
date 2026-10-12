"""IBM i network diagnostics: TCP, TLS and sign-on stay distinct results."""

from __future__ import annotations

import datetime as dt
import ipaddress
import socket
import ssl
import threading

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from quadringent.diagnostics.network import (
    SERVER_MAPPER_PORT,
    authentication_not_observed,
    ibmi_ports,
    probe_tcp,
    probe_tls,
)

CANARY = "canary-password-NOT-REAL-7731"


def test_default_jtopen_path_needs_the_server_mapper_and_the_three_tls_servers():
    ports = ibmi_ports()
    assert ports == {"database": 9471, "signon": 9476, "command": 9475, "server_mapper": SERVER_MAPPER_PORT}


def test_explicit_service_ports_remove_the_server_mapper_lookup():
    assert "server_mapper" not in ibmi_ports(database=19471, signon=19476, command=19475)
    assert ibmi_ports(database=19471)["server_mapper"] == SERVER_MAPPER_PORT


def test_tcp_success_never_claims_tls_or_authentication():
    class Connection:
        def close(self):
            pass

    result = probe_tcp("ibmi.synthetic.invalid", 9471, "database", connector=lambda *a, **k: Connection())
    assert result.status == "passed" and result.layer == "network"
    assert "TLS" in result.limits and "sign-on" in result.limits
    assert result.code == "network.tcp.database"


@pytest.mark.parametrize("error,expected", [
    (ConnectionRefusedError(61, "Connection refused"), "connection refused"),
    (socket.timeout("timed out"), "timed out"),
    (socket.gaierror(8, "nodename nor servname provided"), "host name not resolved"),
    (OSError(65, f"No route to host {CANARY}"), "network error"),
])
def test_tcp_failures_are_normalized_without_raw_messages(error, expected):
    def connector(*_args, **_kwargs):
        raise error

    result = probe_tcp("ibmi.synthetic.invalid", 9476, "signon", connector=connector)
    assert result.status == "failed"
    assert result.evidence["error"] == expected
    assert CANARY not in str(result.to_dict())


def test_probe_timeouts_are_bounded():
    seen = {}

    def connector(address, timeout):
        seen["timeout"] = timeout
        raise socket.timeout()

    probe_tcp("ibmi.synthetic.invalid", 9471, "database", timeout_seconds=600, connector=connector)
    assert seen["timeout"] <= 10


def test_tls_not_requested_stays_unknown():
    result = probe_tls("ibmi.synthetic.invalid", 9471, "database", attempt=False)
    assert result.status == "unknown" and result.layer == "tls"


def test_authentication_is_never_inferred_from_configuration():
    result = authentication_not_observed("source")
    assert result.status == "unknown" and result.layer == "authentication"
    assert "not" in result.summary.lower()


def _self_signed(tmp_path):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "synthetic-ibmi.invalid")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return cert_path, key_path


@pytest.fixture()
def local_tls_server(tmp_path):
    cert_path, key_path = _self_signed(tmp_path)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    stop = threading.Event()

    def serve():
        listener.settimeout(0.2)
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except (socket.timeout, OSError):
                continue
            try:
                with context.wrap_socket(connection, server_side=True) as tls:
                    tls.recv(1)
            except (ssl.SSLError, OSError):
                pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield port, cert_path
    stop.set()
    thread.join(timeout=2)
    listener.close()


def test_tcp_reachable_but_untrusted_certificate_fails_tls_on_a_real_handshake(local_tls_server):
    port, _ = local_tls_server
    assert probe_tcp("127.0.0.1", port, "database").status == "passed"
    result = probe_tls("127.0.0.1", port, "database")
    assert result.status == "failed"
    assert result.evidence["error"] == "certificate not trusted"


def test_trusted_certificate_passes_tls_and_reports_its_fingerprint(local_tls_server):
    port, cert_path = local_tls_server
    result = probe_tls("127.0.0.1", port, "database", ca_file=str(cert_path))
    assert result.status == "passed"
    assert len(result.evidence["sha256_fingerprint"]) == 64
    assert "sign-on" in result.limits


def test_tls_probe_always_verifies_certificates_and_host_names():
    captured = {}

    def wrapper(context, host, port, timeout):
        captured["mode"], captured["hostname"] = context.verify_mode, context.check_hostname
        raise ssl.SSLCertVerificationError("certificate verify failed")

    probe_tls("ibmi.synthetic.invalid", 9471, "database", handshake=wrapper)
    assert captured == {"mode": ssl.CERT_REQUIRED, "hostname": True}

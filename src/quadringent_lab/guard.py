"""Isolation guards: no real credentials, no external connection.

``isolate_environment`` gives the laboratory process its own HOME and removes
cloud/warehouse credentials from the environment. ``NetworkGuard`` refuses
every socket connection and name resolution that is not loopback.
"""

from __future__ import annotations

import ipaddress
import os
from pathlib import Path
import socket
from typing import MutableMapping

_SENSITIVE_PREFIXES = (
    "AWS_",
    "AZURE_",
    "GOOGLE_",
    "GCP_",
    "GCLOUD_",
    "CLOUDSDK_",
    "SNOWFLAKE_",
    "KUBERNETES_",
    "KUBECONFIG",
    "AS400_",
    "IBMI_",
    "ISERIES_",
    "QUADRINGENT_",
)
_LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1", ""})


def isolate_environment(environ: MutableMapping[str, str], workdir: Path) -> list[str]:
    """Point HOME at the laboratory directory and drop credential variables.

    Returns the names of the variables that were removed (values are never
    reported).
    """

    removed = [name for name in list(environ) if name.upper().startswith(_SENSITIVE_PREFIXES)]
    for name in removed:
        del environ[name]
    home = workdir / "home"
    home.mkdir(parents=True, exist_ok=True)
    environ["HOME"] = str(home)
    environ["XDG_CONFIG_HOME"] = str(home / ".config")
    environ["XDG_CACHE_HOME"] = str(home / ".cache")
    environ["NO_PROXY"] = "*"
    environ["no_proxy"] = "*"
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        environ.pop(name, None)
    return sorted(removed)


class NetworkBlockedError(OSError):
    """An attempt to leave the loopback interface was refused."""


def _is_loopback_host(host: object) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", "ignore")
    if host is None or host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(str(host).strip("[]")).is_loopback
    except ValueError:
        return False


class NetworkGuard:
    """Context manager (or explicit install/uninstall) refusing external traffic."""

    def __init__(self) -> None:
        self._originals: dict[str, object] = {}

    def install(self) -> "NetworkGuard":
        if self._originals:
            return self
        original_connect = socket.socket.connect
        original_connect_ex = socket.socket.connect_ex
        original_getaddrinfo = socket.getaddrinfo

        def check_address(sock: socket.socket, address: object) -> None:
            if sock.family == getattr(socket, "AF_UNIX", None):
                return
            host = address[0] if isinstance(address, tuple) and address else address
            if not _is_loopback_host(host):
                raise NetworkBlockedError(f"laboratory network guard: external address refused ({host!r})")

        def guarded_connect(self_socket, address):  # noqa: ANN001
            check_address(self_socket, address)
            return original_connect(self_socket, address)

        def guarded_connect_ex(self_socket, address):  # noqa: ANN001
            check_address(self_socket, address)
            return original_connect_ex(self_socket, address)

        def guarded_getaddrinfo(host, *args, **kwargs):  # noqa: ANN001
            if not _is_loopback_host(host):
                raise NetworkBlockedError(f"laboratory network guard: name resolution refused ({host!r})")
            return original_getaddrinfo(host, *args, **kwargs)

        self._originals = {
            "connect": original_connect,
            "connect_ex": original_connect_ex,
            "getaddrinfo": original_getaddrinfo,
        }
        socket.socket.connect = guarded_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = guarded_connect_ex  # type: ignore[method-assign]
        socket.getaddrinfo = guarded_getaddrinfo  # type: ignore[assignment]
        return self

    def uninstall(self) -> None:
        if not self._originals:
            return
        socket.socket.connect = self._originals["connect"]  # type: ignore[method-assign]
        socket.socket.connect_ex = self._originals["connect_ex"]  # type: ignore[method-assign]
        socket.getaddrinfo = self._originals["getaddrinfo"]  # type: ignore[assignment]
        self._originals = {}

    def __enter__(self) -> "NetworkGuard":
        return self.install()

    def __exit__(self, *_exc: object) -> None:
        self.uninstall()


def home_is_isolated(workdir: Path) -> bool:
    return Path(os.environ.get("HOME", "")).resolve() == (workdir / "home").resolve()

"""Process management for the laboratory: workdir, registry, uvicorn, cleanup.

Everything the laboratory writes lives in one temporary directory marked with
``.quadringent-lab``; cleanup refuses to delete anything without that marker.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.request
from typing import Callable

import uvicorn

from . import DEFAULT_PORT, LAB_MARKER_FILE
from .gateway import LabGateway, default_ui_dist
from .guard import NetworkGuard, isolate_environment
from .runtime import LabRuntime
from .scenarios import DEFAULT_SCENARIO

WORKDIR_PREFIX = "quadringent-lab-"
HOST = "127.0.0.1"


def temp_root() -> Path:
    return Path(tempfile.gettempdir()).resolve()


def registry_dir() -> Path:
    return temp_root() / "quadringent-lab-registry"


def create_workdir() -> Path:
    path = Path(tempfile.mkdtemp(prefix=WORKDIR_PREFIX, dir=temp_root()))
    (path / LAB_MARKER_FILE).write_text("quadringent laboratory working directory\n", encoding="utf-8")
    return path


def is_lab_workdir(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return (
        resolved.parent == temp_root()
        and resolved.name.startswith(WORKDIR_PREFIX)
        and (resolved / LAB_MARKER_FILE).is_file()
    )


def remove_workdir(path: Path) -> bool:
    """Delete a laboratory working directory; refuse anything else."""

    if not is_lab_workdir(path):
        return False
    shutil.rmtree(path, ignore_errors=True)
    return True


def bind_socket(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((HOST, port))
    return sock


class _LabServer(uvicorn.Server):
    """uvicorn server that does not re-raise SIGINT/SIGTERM after shutdown.

    uvicorn restores the default handler and re-sends the signal once it has
    stopped, which would kill the process before cleanup could remove the
    working directory. ``serve`` installs its own handlers instead.
    """

    @contextlib.contextmanager
    def capture_signals(self):  # type: ignore[override]
        yield


def _build_server(gateway: LabGateway, sock: socket.socket, log_level: str) -> uvicorn.Server:
    config = uvicorn.Config(gateway, log_level=log_level, access_log=False, lifespan="on")
    return _LabServer(config)


def registry_path(port: int) -> Path:
    return registry_dir() / f"{port}.json"


def write_registry(port: int, workdir: Path) -> None:
    registry_dir().mkdir(parents=True, exist_ok=True)
    registry_path(port).write_text(
        json.dumps({"pid": os.getpid(), "port": port, "workdir": str(workdir), "started_at": time.time()}),
        encoding="utf-8",
    )


def read_registry(port: int) -> dict | None:
    try:
        return json.loads(registry_path(port).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def serve(
    *,
    scenario: str = DEFAULT_SCENARIO,
    port: int = DEFAULT_PORT,
    ui_dist: Path | None = None,
    allow_shutdown: bool = False,
    announce: Callable[[str], None] = print,
    log_level: str = "warning",
) -> int:
    """Run the laboratory in the foreground until SIGINT/SIGTERM (or shutdown)."""

    sock = bind_socket(port)
    actual_port = sock.getsockname()[1]
    workdir = create_workdir()
    guard = NetworkGuard()
    runtime: LabRuntime | None = None
    try:
        isolate_environment(os.environ, workdir)
        guard.install()
        server_holder: dict[str, uvicorn.Server] = {}

        def request_exit() -> None:
            server = server_holder.get("server")
            if server is not None:
                server.should_exit = True

        runtime = LabRuntime(
            workdir,
            scenario=scenario,
            ui_dist=ui_dist if ui_dist is not None else default_ui_dist(),
            shutdown=request_exit if allow_shutdown else None,
            port=actual_port,
        )
        gateway = LabGateway(runtime)
        server = _build_server(gateway, sock, log_level)
        server_holder["server"] = server
        write_registry(actual_port, workdir)
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda *_args: request_exit())

        async def main() -> None:
            task = asyncio.create_task(server.serve(sockets=[sock]))
            while not server.started and not task.done():
                await asyncio.sleep(0.05)
            if server.started:
                announce(f"READY http://{HOST}:{actual_port}/ pid={os.getpid()} scenario={runtime.state.scenario}")
            await task

        asyncio.run(main())
        return 0
    finally:
        guard.uninstall()
        try:
            sock.close()
        except OSError:
            pass
        if runtime is not None:
            runtime.close()
        remove_workdir(workdir)
        _drop_registry(actual_port)


def _drop_registry(port: int) -> None:
    for leftover in (registry_path(port), registry_dir() / f"{port}.log"):
        try:
            leftover.unlink()
        except OSError:
            pass
    try:
        registry_dir().rmdir()  # only succeeds when no other laboratory is registered
    except OSError:
        pass


def lab_identity(port: int, timeout: float = 2.0) -> dict | None:
    """Ask a loopback port whether a laboratory answers there."""

    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/lab/identity", timeout=timeout) as response:
            payload = json.loads(response.read())
    except (OSError, ValueError, urllib.error.URLError):
        return None
    return payload if isinstance(payload, dict) and payload.get("lab") is True else None


def stop(port: int, timeout: float = 15.0) -> str:
    """Stop the laboratory listening on ``port``. Returns a human-readable result."""

    record = read_registry(port)
    identity = lab_identity(port)
    if record is None and identity is None:
        return f"no laboratory is registered or answering on port {port}"
    pid = (identity or record or {}).get("pid")
    if not isinstance(pid, int):
        return "registry entry has no process id"
    if identity is not None and record is not None and identity.get("pid") != record.get("pid"):
        return "refusing to stop: registry and running process disagree"
    if identity is None:
        # Stale registry entry: only clean what is provably the laboratory's.
        workdir = Path(str(record.get("workdir", "")))
        remove_workdir(workdir)
        _drop_registry(port)
        return f"stale entry for port {port} removed (the process was already gone)"
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if lab_identity(port, timeout=0.5) is None:
            break
        time.sleep(0.2)
    else:
        return f"laboratory pid {pid} did not stop within {timeout:.0f}s"
    workdir = Path(str((record or {}).get("workdir", identity.get("workdir", ""))))
    if workdir.exists():
        time.sleep(0.5)
        remove_workdir(workdir)
    _drop_registry(port)
    return f"laboratory on port {port} stopped (pid {pid})"


class ThreadedLab:
    """Run a gateway on a real loopback port in a thread (used by tests)."""

    def __init__(self, gateway: LabGateway) -> None:
        self.gateway = gateway
        self.sock = bind_socket(0)
        self.port = self.sock.getsockname()[1]
        self.server = _build_server(gateway, self.sock, "error")
        self._thread = threading.Thread(target=self._run, daemon=True)

    @property
    def url(self) -> str:
        return f"http://{HOST}:{self.port}"

    def _run(self) -> None:
        asyncio.run(self.server.serve(sockets=[self.sock]))

    def start(self) -> "ThreadedLab":
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        if not self.server.started:
            raise RuntimeError("laboratory server did not start")
        return self

    def stop(self) -> None:
        self.server.should_exit = True
        self._thread.join(timeout=10)
        try:
            self.sock.close()
        except OSError:
            pass

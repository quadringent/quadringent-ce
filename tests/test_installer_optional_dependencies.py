"""Missing API extras fail before requests, without breaking the base CLI."""

from __future__ import annotations

import argparse
import builtins
import io
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from quadringent.installer import mcp_bridge
from quadringent.installer.api_client import ClientConfig, EXIT_CONFIG_ERROR
from quadringent.installer.v2_commands import run_v2_command


def _without_api_extra(*argv: str) -> subprocess.CompletedProcess[str]:
    # Keep base dependencies available, but make API extras unavailable to the CLI.
    env = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        "QUADRINGENT_URL": "http://127.0.0.1:1",
        "QUADRINGENT_TOKEN": "synthetic-token-not-for-output",
    }
    script = """
import importlib.abc
import socket
import sys
class NoApiExtra(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname.split(".")[0] in {"httpx", "mcp", "fastapi"}:
            raise ModuleNotFoundError(f"No module named '{fullname}'", name=fullname)
sys.meta_path.insert(0, NoApiExtra())
def no_connection(*args, **kwargs):
    raise AssertionError("No network connection is allowed")
socket.socket.connect = no_connection
from quadringent.installer.cli import run
raise SystemExit(run(sys.argv[1:]))
"""
    return subprocess.run(
        [sys.executable, "-B", "-c", script, *argv],
        env=env, capture_output=True, text=True, timeout=20, check=False,
    )


@pytest.mark.parametrize("argv", [("sources", "list"), ("events", "stream"), ("mcp", "--stdio")])
def test_missing_api_extra_is_an_actionable_json_error(argv: tuple[str, ...]) -> None:
    result = _without_api_extra(*argv)
    assert result.returncode == EXIT_CONFIG_ERROR, result.stderr
    assert result.stderr == ""
    error = json.loads(result.stdout)["error"]
    assert error["code"] == "config_error"
    assert "api extra" in error["message"]
    assert ".[api]" in error["message"]
    assert "same release" in error["message"]
    assert "synthetic-token-not-for-output" not in result.stdout
    assert "127.0.0.1" not in result.stdout


def test_base_cli_help_does_not_require_api_extra() -> None:
    result = _without_api_extra("--help")
    assert result.returncode == 0, result.stderr
    assert "install" in result.stdout
    assert "Traceback" not in result.stderr


def test_base_installer_dry_run_does_not_require_api_extra() -> None:
    root = Path(__file__).resolve().parents[1]
    result = _without_api_extra(
        "install", "--cloud", "aws", "--target", "vm", "--region", "eu-west-3",
        "--name", "demo-int", "--dry-run", "--assets-dir", str(root),
        "--release-manifest", str(root / "deploy/release-manifest.example.json"),
    )
    assert result.returncode == 0, result.stderr
    assert "--dry-run: no command executed" in result.stdout


def _missing_import(monkeypatch, *, requested: str, missing: str) -> None:
    original_import = builtins.__import__

    def import_module(name, *args, **kwargs):
        if name == requested:
            raise ModuleNotFoundError(f"No module named '{missing}'", name=missing)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_module)


@pytest.mark.parametrize(
    ("requested", "missing"),
    [("mcp.server.mcpserver", "mcp"), ("quadringent_control_plane.v2.mcp_server", "fastapi")],
)
def test_mcp_partial_api_install_reports_missing_direct_dependency(monkeypatch, requested, missing) -> None:
    monkeypatch.setattr(mcp_bridge, "load_config", lambda: ClientConfig("http://127.0.0.1:1", "private-token"))
    _missing_import(monkeypatch, requested=requested, missing=missing)
    out = io.StringIO()
    assert mcp_bridge.run_mcp_command(argparse.Namespace(), stdout=out) == EXIT_CONFIG_ERROR
    assert json.loads(out.getvalue())["error"]["code"] == "config_error"
    assert "api extra" in out.getvalue()
    assert "private-token" not in out.getvalue()


@pytest.mark.parametrize("command", ["v2", "mcp"])
def test_unrelated_import_errors_are_not_misreported_as_missing_extra(monkeypatch, command) -> None:
    config = ClientConfig("http://127.0.0.1:1", None)
    monkeypatch.setattr(mcp_bridge, "load_config", lambda: config)
    _missing_import(monkeypatch, requested="httpx", missing="unrelated_dependency")
    out = io.StringIO()
    with pytest.raises(ModuleNotFoundError, match="unrelated_dependency"):
        if command == "v2":
            run_v2_command(argparse.Namespace(), stdout=out, config=config)
        else:
            mcp_bridge.run_mcp_command(argparse.Namespace(), stdout=out)
    assert out.getvalue() == ""


def test_v2_network_failure_keeps_its_existing_exit_code() -> None:
    httpx = pytest.importorskip("httpx")

    def unavailable(request):
        raise httpx.ConnectError("synthetic unreachable server", request=request)

    out = io.StringIO()
    code = run_v2_command(
        argparse.Namespace(handler=lambda client, args: client.get("/v2/sources")),
        stdout=out, config=ClientConfig("http://testserver", None),
        transport=httpx.MockTransport(unavailable),
    )
    assert code == 9
    assert json.loads(out.getvalue())["result"]["error"]["code"] == "network_error"


def test_v2_handler_errors_are_not_caught_as_startup_errors() -> None:
    from quadringent.installer.api_client import ConfigError

    httpx = pytest.importorskip("httpx")

    def broken_handler(client, args):
        raise ConfigError("synthetic handler bug")

    def no_request(request):
        raise AssertionError("The handler must fail before any request")

    out = io.StringIO()
    with pytest.raises(ConfigError, match="synthetic handler bug"):
        run_v2_command(
            argparse.Namespace(handler=broken_handler), stdout=out,
            config=ClientConfig("http://127.0.0.1:1", None),
            transport=httpx.MockTransport(no_request),
        )
    assert out.getvalue() == ""

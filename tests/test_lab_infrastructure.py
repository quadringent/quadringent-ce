"""Laboratory infrastructure: isolation, packaging, router confinement, CLI.

These tests prove the laboratory cannot reach the outside world, is not part
of the distributed product, and cleans up after itself.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import tomllib

import pytest

from quadringent_lab import LAB_MARKER_FILE, fixtures
from quadringent_lab.guard import NetworkBlockedError, NetworkGuard, isolate_environment
from quadringent_lab.scenarios import BY_NAME, SCENARIOS, get_scenario
from quadringent_lab import server as lab_server

from test_lab_support import REPO_ROOT, build_lab


def test_scenario_matrix_has_the_twenty_numbered_scenarios():
    numbers = [scenario.number for scenario in SCENARIOS if scenario.number > 0]
    assert numbers == list(range(1, 21))
    assert len({scenario.name for scenario in SCENARIOS}) == len(SCENARIOS)
    assert get_scenario(7) is get_scenario("journal_config_incompatible")


def test_matrix_document_lists_every_scenario_and_its_assertion():
    document = (REPO_ROOT / "docs" / "product" / "local-lab.md").read_text(encoding="utf-8")
    for scenario in SCENARIOS:
        if scenario.number == 0:
            continue
        assert f"`{scenario.name}`" in document, scenario.name
        if "backend" in scenario.coverage:
            assert scenario.backend_test in document, scenario.backend_test
    source = (REPO_ROOT / "tests" / "test_lab_scenarios.py").read_text(encoding="utf-8")
    browser = (REPO_ROOT / "ui" / "scripts" / "lab-browser-scenarios.mjs").read_text(encoding="utf-8")
    for scenario in SCENARIOS:
        if "backend" in scenario.coverage:
            assert re.search(rf"def {scenario.backend_test}\(", source), scenario.backend_test
        if "browser" in scenario.coverage:
            assert (
                f"number: {scenario.number}, name: '{scenario.name}'" in browser
                or f"scenario {scenario.number:02d} {scenario.name}" in browser
            ), scenario.browser_check


def test_lab_package_is_not_distributed():
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    include = config["tool"]["setuptools"]["packages"]["find"]["include"]
    import fnmatch

    assert not any(fnmatch.fnmatchcase("quadringent_lab", pattern) for pattern in include)
    assert not any(fnmatch.fnmatchcase("quadringent_lab.router", pattern) for pattern in include)


def test_product_app_never_mounts_the_lab_router(tmp_path):
    lab = build_lab(tmp_path, "baseline")
    for app in lab.runtime.apps.values():
        paths = set(app.openapi()["paths"])
        assert not any(path.startswith("/lab") for path in paths)
    product_sources = "\n".join(
        path.read_text(encoding="utf-8") for path in (REPO_ROOT / "src" / "quadringent_control_plane").rglob("*.py")
    )
    assert "quadringent_lab" not in product_sources
    # The lab router answers only through the laboratory gateway.
    assert lab.admin().get("/lab/identity").json()["lab"] is True


def test_gateway_refuses_non_loopback_host(tmp_path):
    from fastapi.testclient import TestClient

    lab = build_lab(tmp_path, "baseline")
    foreign = TestClient(lab.gateway, base_url="http://lab.example.test")
    assert foreign.get("/v2/healthz").status_code == 421
    assert foreign.get("/lab/state").status_code == 421


def test_network_guard_refuses_external_addresses_and_allows_loopback():
    with NetworkGuard():
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as outward:
            with pytest.raises(NetworkBlockedError):
                outward.connect(("93.184.216.34", 80))
        with pytest.raises(NetworkBlockedError):
            socket.getaddrinfo("example.com", 443)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        try:
            with socket.create_connection(listener.getsockname(), timeout=2):
                pass
        finally:
            listener.close()
    # Uninstalled: the guard no longer refuses (an unroutable address fails later, not by the guard).
    assert socket.getaddrinfo("127.0.0.1", 80)


def test_environment_isolation_drops_credentials_and_redirects_home(tmp_path):
    environ = {
        "HOME": "/Users/someone",
        "AWS_ACCESS_KEY_ID": "x",
        "SNOWFLAKE_PASSWORD": "x",
        "KUBECONFIG": "/x",
        "QUADRINGENT_V2_SECRET_KEY": "x",
        "HTTPS_PROXY": "http://proxy",
        "PATH": os.environ.get("PATH", ""),
    }
    removed = isolate_environment(environ, tmp_path)
    assert removed == ["AWS_ACCESS_KEY_ID", "KUBECONFIG", "QUADRINGENT_V2_SECRET_KEY", "SNOWFLAKE_PASSWORD"]
    assert environ["HOME"] == str(tmp_path / "home")
    assert "HTTPS_PROXY" not in environ and environ["PATH"]


def test_workdir_cleanup_refuses_anything_without_the_marker(tmp_path):
    foreign = Path(lab_server.temp_root()) / "quadringent-lab-not-ours-test"
    foreign.mkdir(exist_ok=True)
    try:
        (foreign / "keep.txt").write_text("x")
        assert lab_server.remove_workdir(foreign) is False  # prefix matches, marker missing
        assert (foreign / "keep.txt").exists()
        assert lab_server.remove_workdir(tmp_path) is False  # not under the temp root with the prefix
    finally:
        for child in foreign.iterdir():
            child.unlink()
        foreign.rmdir()
    owned = lab_server.create_workdir()
    assert (owned / LAB_MARKER_FILE).is_file()
    assert lab_server.remove_workdir(owned) is True and not owned.exists()


def test_seed_is_deterministic(tmp_path):
    first_dir = tmp_path / "a"
    first_dir.mkdir()
    first = build_lab(first_dir, "baseline")
    second_dir = tmp_path / "b"
    second_dir.mkdir()
    second = build_lab(second_dir, "baseline")
    assert first.runtime.public_state()["seed"] == second.runtime.public_state()["seed"]
    assert first.runtime.public_state()["seed"]["pipeline_ids"] == {
        "ORDERS": fixtures.PIPELINE_ORDERS,
        "CUSTOMERS": fixtures.PIPELINE_CUSTOMERS,
    }


def test_every_scenario_activates_on_one_runtime(tmp_path):
    lab = build_lab(tmp_path, "baseline")
    for scenario in SCENARIOS:
        lab.runtime.activate(scenario.name)
        assert lab.runtime.state.scenario == scenario.name
        assert lab.admin().get("/v2/healthz").status_code == 200
    assert BY_NAME["export_canaries"].conditions == {"canaries": True}


def test_lab_router_selects_scenarios_and_rejects_bad_input(tmp_path):
    lab = build_lab(tmp_path, "baseline")
    admin = lab.admin()
    changed = admin.put("/lab/scenario", json={"name": 13})
    assert changed.status_code == 200 and changed.json()["state"]["scenario"] == "evidence_stale"
    assert admin.put("/lab/scenario", json={"name": "nope"}).status_code == 404
    assert admin.put("/lab/conditions", json={"evidence": "bogus"}).status_code == 400
    assert admin.put("/lab/conditions", json={"unknown": 1}).status_code == 400
    assert admin.put("/lab/faults", json={"id": "x", "mode": "delay_before", "path": "("}).status_code == 400
    assert admin.post("/lab/shutdown").status_code == 403  # disabled unless explicitly allowed
    # Sessions survive a scenario change: identifiers are deterministic and cookies are stateless.
    operator = lab.session()
    admin.put("/lab/scenario", json={"name": "baseline"})
    assert operator.get("/v2/pipelines").status_code == 200


def test_cli_start_stop_real_process(tmp_path):
    script = REPO_ROOT / "scripts" / "quadringent_lab.py"
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = {key: value for key, value in os.environ.items() if not key.startswith(("AWS_", "SNOWFLAKE_"))}
    started = subprocess.run(
        [sys.executable, str(script), "start", "--scenario", "evidence_absent", "--port", str(port), "--detach"],
        capture_output=True, text=True, timeout=60, env=env,
    )
    try:
        assert started.returncode == 0, started.stderr
        assert "READY" in started.stdout
        identity = lab_server.lab_identity(port)
        assert identity is not None and identity["scenario"] == "evidence_absent"
        workdir = Path(identity["workdir"])
        assert workdir.is_dir() and lab_server.is_lab_workdir(workdir)
        import httpx

        assert httpx.get(f"http://127.0.0.1:{port}/v2/healthz", timeout=5).json() == {"status": "ok"}
        assert httpx.get(f"http://127.0.0.1:{port}/v1/overview", timeout=5).status_code == 404
    finally:
        stopped = subprocess.run(
            [sys.executable, str(script), "stop", "--port", str(port)], capture_output=True, text=True, timeout=60, env=env
        )
    assert stopped.returncode == 0, stopped.stdout + stopped.stderr
    deadline = time.monotonic() + 5
    while lab_server.lab_identity(port, timeout=0.3) is not None and time.monotonic() < deadline:
        time.sleep(0.1)
    assert lab_server.lab_identity(port, timeout=0.3) is None
    assert not workdir.exists()

"""Un daemon Docker bloqué ne doit pas immobiliser toute la qualification."""
import subprocess

import pytest

import conftest


def test_start_timeout_attempts_cleanup_of_only_the_owned_container(monkeypatch):
    calls = []
    monkeypatch.setattr(conftest, "_docker_binary", lambda: "docker")
    monkeypatch.setattr(conftest, "_free_tcp_port", lambda: 23456)

    def run(command, **options):
        calls.append((command, options))
        if command[1] == "run":
            raise subprocess.TimeoutExpired(command, options["timeout"])
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(conftest.subprocess, "run", run)
    fixture = conftest.postgres_dsn.__wrapped__()
    with pytest.raises(subprocess.TimeoutExpired):
        next(fixture)
    start, cleanup = calls
    owned = start[0][start[0].index("--name") + 1]
    assert owned.startswith("quadringent-v2-test-pg-")
    assert start[1]["timeout"] == 60
    assert cleanup[0] == ["docker", "rm", "-f", owned]
    assert cleanup[1]["timeout"] == 15

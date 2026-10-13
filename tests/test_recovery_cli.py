from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from quadringent import recovery_cli


AUTHORITY = "s3://recovery-bucket/receiver-authority-v1"


def selection_file(tmp_path: Path, **changes) -> Path:
    record = {
        "format_version": "quadringent-native-recovery-selection-v1",
        "authority": AUTHORITY,
        "raw_prefixes": ["native/JLIB/JRN"],
        "checkpoint_keys": ["source:JLIB:JRN"],
    }
    record.update(changes)
    path = tmp_path / "selection.json"
    path.write_text(json.dumps(record))
    return path


def test_verify_is_available_without_cloud_configuration(monkeypatch, tmp_path):
    calls = []
    digest = "a" * 64
    monkeypatch.setattr(recovery_cli, "verify_recovery_bundle", lambda directory, **kwargs: calls.append(
        (directory, kwargs)) or {"selection": {"raw_prefixes": ["native/JLIB/JRN"], "checkpoint_keys": ["source"]}})
    monkeypatch.setattr(recovery_cli.StorageBackend, "from_environment", lambda environ: pytest.fail("cloud accessed"))
    output = io.StringIO()
    assert recovery_cli.run(["verify", "--directory", str(tmp_path), "--expected-sha256", digest],
                            environ={}, stdout=output) == 0
    assert calls == [(tmp_path, {"expected_sha256": digest})]
    assert digest in output.getvalue()
    assert "does not validate IBM i" in output.getvalue()


@pytest.mark.parametrize("operation", ["export", "restore"])
def test_writers_attestation_is_required_before_cloud_access(monkeypatch, tmp_path, operation):
    monkeypatch.setattr(recovery_cli.StorageBackend, "from_environment", lambda environ: pytest.fail("cloud accessed"))
    argv = [operation, "--selection", str(selection_file(tmp_path)), "--directory", str(tmp_path / "bundle")]
    if operation == "restore":
        argv += ["--expected-sha256", "b" * 64]
    errors = io.StringIO()
    assert recovery_cli.run(argv, environ={}, stderr=errors) == 2
    assert "--writers-stopped" in errors.getvalue()


@pytest.mark.parametrize("changes", [
    {"raw_prefixes": "native"}, {"checkpoint_keys": []}, {"authority": "s3://other/path"},
    {"raw_prefixes": ["receiver-authority-v1/child"]}, {"extra": "field"},
])
def test_invalid_selection_is_refused_before_cloud_access(monkeypatch, tmp_path, changes):
    monkeypatch.setattr(recovery_cli.StorageBackend, "from_environment", lambda environ: pytest.fail("cloud accessed"))
    errors = io.StringIO()
    assert recovery_cli.run(["export", "--selection", str(selection_file(tmp_path, **changes)),
                             "--directory", str(tmp_path / "bundle"), "--writers-stopped"],
                            environ={}, stderr=errors) == 2


def test_duplicate_selection_fields_are_not_accepted(monkeypatch, tmp_path):
    path = selection_file(tmp_path)
    content = path.read_text()
    path.write_text(content[:-1] + ',"authority":"' + AUTHORITY + '"}')
    monkeypatch.setattr(recovery_cli.StorageBackend, "from_environment", lambda environ: pytest.fail("cloud accessed"))
    assert recovery_cli.run(["export", "--selection", str(path), "--directory", str(tmp_path / "bundle"),
                             "--writers-stopped"], environ={}, stderr=io.StringIO()) == 2


def test_selection_read_is_bounded(monkeypatch, tmp_path):
    path = tmp_path / "huge.json"
    path.write_bytes(b" " * 65537)
    monkeypatch.setattr(recovery_cli.StorageBackend, "from_environment", lambda environ: pytest.fail("cloud accessed"))
    assert recovery_cli.run(["export", "--selection", str(path), "--directory", str(tmp_path / "bundle"),
                            "--writers-stopped"], environ={}, stderr=io.StringIO()) == 2


@pytest.mark.parametrize("field,count", [("raw_prefixes", 65), ("checkpoint_keys", 257)])
def test_selection_count_is_checked_before_backend_access(monkeypatch, tmp_path, field, count):
    path = selection_file(tmp_path, **{field: [f"label-{index}" for index in range(count)]})
    monkeypatch.setattr(recovery_cli.StorageBackend, "from_environment", lambda environ: pytest.fail("cloud accessed"))
    assert recovery_cli.run(["export", "--selection", str(path), "--directory", str(tmp_path / "bundle"),
                             "--writers-stopped"], environ={}, stderr=io.StringIO()) == 2


def test_selection_special_file_is_refused_without_waiting_for_a_writer(monkeypatch, tmp_path):
    path = tmp_path / "selection.fifo"
    os.mkfifo(path)
    monkeypatch.setattr(recovery_cli.StorageBackend, "from_environment", lambda environ: pytest.fail("cloud accessed"))
    assert recovery_cli.run(["export", "--selection", str(path), "--directory", str(tmp_path / "bundle"),
                             "--writers-stopped"], environ={}, stderr=io.StringIO()) == 2


def test_export_uses_only_explicit_selection_and_keeps_contents_out_of_output(monkeypatch, tmp_path):
    backend = object()
    calls = []
    monkeypatch.setattr(recovery_cli.StorageBackend, "from_environment", lambda environ: backend)
    monkeypatch.setattr(recovery_cli, "export_recovery_bundle", lambda path, **kwargs: calls.append((path, kwargs))
                        or {"manifest_sha256": "c" * 64, "private_content": "DO-NOT-PRINT"})
    output = io.StringIO()
    assert recovery_cli.run(["export", "--selection", str(selection_file(tmp_path)), "--directory",
                             str(tmp_path / "bundle"), "--writers-stopped"], environ={}, stdout=output) == 0
    assert calls[0][1]["storage"] is backend
    assert calls[0][1]["selection"].authority == AUTHORITY
    assert calls[0][1]["writers_suspended"] is True
    assert "c" * 64 in output.getvalue()
    assert "DO-NOT-PRINT" not in output.getvalue()
    assert "PostgreSQL" in output.getvalue()


@pytest.mark.parametrize("error_type", [RuntimeError, ValueError, FileNotFoundError])
def test_cloud_errors_do_not_echo_credentials(monkeypatch, tmp_path, error_type):
    def denied(environ):
        raise error_type("secret credential content")

    monkeypatch.setattr(recovery_cli.StorageBackend, "from_environment", denied)
    output, errors = io.StringIO(), io.StringIO()
    assert recovery_cli.run(["export", "--selection", str(selection_file(tmp_path)), "--directory",
                             str(tmp_path / "bundle"), "--writers-stopped"],
                            environ={}, stdout=output, stderr=errors) == 2
    assert "secret credential content" not in output.getvalue() + errors.getvalue()
    assert error_type.__name__ in errors.getvalue()


@pytest.mark.parametrize("digest", ["", "a" * 63, "A" * 64, "invalid"])
def test_restore_digest_is_validated_before_backend_creation(monkeypatch, tmp_path, digest):
    monkeypatch.setattr(recovery_cli.StorageBackend, "from_environment", lambda environ: pytest.fail("cloud accessed"))
    assert recovery_cli.run(["restore", "--selection", str(selection_file(tmp_path)), "--directory",
                             str(tmp_path / "bundle"), "--writers-stopped", "--expected-sha256", digest],
                            environ={}, stderr=io.StringIO()) == 2


def test_installer_exposes_recovery_help_without_configuration():
    from quadringent.installer.cli import run

    with pytest.raises(SystemExit) as exit_info:
        run(["recovery", "--help"], stdout=io.StringIO())
    assert exit_info.value.code == 0


def test_installer_routes_export_verify_and_restore_without_automatic_resume(monkeypatch, tmp_path):
    from quadringent.installer.cli import run
    from test_recovery_bundle import LocalStorage, SELECTION, inventory, populated

    source, _, _ = populated(tmp_path)
    target = LocalStorage(tmp_path / "restored")
    backends = iter((source, target))
    monkeypatch.setattr(recovery_cli.StorageBackend, "from_environment", lambda environ: next(backends))
    path = selection_file(tmp_path, authority=SELECTION.authority, raw_prefixes=list(SELECTION.raw_prefixes),
                          checkpoint_keys=list(SELECTION.checkpoint_keys))
    directory = tmp_path / "cli-bundle"
    output = io.StringIO()
    assert run(["recovery", "export", "--selection", str(path), "--directory", str(directory),
                "--writers-stopped"], stdout=output) == 0
    manifest_sha = output.getvalue().split("manifest SHA256: ")[1].splitlines()[0]
    assert run(["recovery", "verify", "--directory", str(directory), "--expected-sha256", manifest_sha],
               stdout=io.StringIO()) == 0
    assert run(["recovery", "restore", "--selection", str(path), "--directory", str(directory),
                "--writers-stopped", "--expected-sha256", manifest_sha], stdout=io.StringIO()) == 0
    assert inventory(target) == inventory(source)
    assert all(target.checkpoint_store(key).load_bound() == source.checkpoint_store(key).load_bound()
               for key in SELECTION.checkpoint_keys)
    assert "does not validate IBM i or resume writers" in output.getvalue()

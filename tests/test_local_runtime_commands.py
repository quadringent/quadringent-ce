"""Command surface of ``local_runtime``: result lines, secret handling and exec wrappers."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

import quadringent_control_plane  # noqa: F401

from local_support import LABEL, NO_MOUNTINFO, local_env, make_data_dir
from quadringent.receiver_identity import ReceiverIdentity
from quadringent.storage_backend import StorageBackend
from quadringent_control_plane.v2.executor import local_runtime as runtime
from quadringent_control_plane.v2.executor.boundary import JournalBoundary

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 13, 9, 30, tzinfo=timezone.utc)
IDENTITY = ReceiverIdentity("TESTLIB", "TESTJRN", "TESTLIB", "RCV0001", "native-test-v1", "generation-a")
MODULE = "quadringent_control_plane.v2.executor.local_runtime"


def result_lines(text):
    return [line for line in text.splitlines() if line.startswith(runtime.RESULT_PREFIX)]


def run_module(*args, env=None, stdin=None):
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("AS400_", "QUADRINGENT_", "ISERIES_", "SNOWFLAKE_", "PYTHON"))}
    environment["PYTHONPATH"] = str(ROOT / "src")
    environment.update(env or {})
    return subprocess.run([sys.executable, "-m", MODULE, *args], cwd=ROOT, env=environment, input=stdin,
                          capture_output=True, text=True, timeout=60)


# -- secret files -------------------------------------------------------------------------------------


def test_secret_files_are_regular_bounded_and_not_symlinks(tmp_path):
    good = tmp_path / "pw"
    good.write_text("p4ss\n")
    assert runtime.read_secret_file(str(good), strip_final_newline=True) == "p4ss"
    assert runtime.read_secret_file(str(good)) == "p4ss\n"
    link = tmp_path / "link"
    link.symlink_to(good)
    empty = tmp_path / "empty"
    empty.write_text(" \n")
    big = tmp_path / "big"
    big.write_bytes(b"x" * (runtime.MAX_SECRET_BYTES + 1))
    binary = tmp_path / "binary"
    binary.write_bytes(b"\xff\xfe")
    for bad in (link, tmp_path, empty, big, binary, tmp_path / "absent"):
        with pytest.raises(runtime.LocalRuntimeError):
            runtime.read_secret_file(str(bad))


def test_redaction_removes_every_known_secret_and_bounds_the_line(tmp_path):
    secret = tmp_path / "pw"
    secret.write_text("hunter2-value\n")
    runtime.read_secret_file(str(secret), strip_final_newline=True)
    line = runtime.redact("failed\nwith hunter2-value inside " + "x" * 1000)
    assert "hunter2-value" not in line and "\n" not in line and len(line) <= runtime.MAX_ERROR_CHARS
    assert runtime.redact("a secret-two here", ["secret-two"]) == "a [redacted] here"


# -- fs-probe -----------------------------------------------------------------------------------------


def test_fs_probe_prints_one_result_line(tmp_path):
    data = make_data_dir(tmp_path)
    result = run_module("fs-probe", "--data-dir", str(data))
    assert result.returncode == 0, result.stderr
    (line,) = result_lines(result.stdout)
    assert len(result.stdout.strip().splitlines()) == 1
    payload = json.loads(line[len(runtime.RESULT_PREFIX):])
    assert payload["ok"] is True and "hard_link" in payload["checks"]


def test_fs_probe_failure_exits_non_zero_with_the_checks_and_a_redacted_reason(tmp_path):
    (tmp_path / "bare").mkdir()
    result = run_module("fs-probe", "--data-dir", str(tmp_path / "bare"))
    assert result.returncode == 1
    payload = json.loads(result_lines(result.stdout)[0][len(runtime.RESULT_PREFIX):])
    assert payload["ok"] is False and payload["checks"]["marker"]["ok"] is False
    assert result.stderr.startswith(runtime.ERROR_PREFIX) and len(result.stderr.strip().splitlines()) == 1


def test_fs_probe_reads_the_data_directory_from_the_environment(tmp_path):
    data = make_data_dir(tmp_path)
    assert run_module("fs-probe", env={"AS400_LOCAL_DATA_DIR": str(data)}).returncode == 0
    refused = run_module("fs-probe")
    assert refused.returncode == 1 and "AS400_LOCAL_DATA_DIR" in refused.stderr


# -- boundary / admit / copy through main --------------------------------------------------------------


def test_boundary_command_emits_the_observed_boundary(tmp_path, monkeypatch, capsys):
    boundary = JournalBoundary("TESTLIB", "RCV0001", 4200, NOW, receiver_identity=IDENTITY)
    monkeypatch.setattr(runtime, "read_boundary", lambda environ, **kwargs: boundary)
    assert runtime.main(["boundary"]) == 0
    out = capsys.readouterr().out
    (line,) = result_lines(out)
    assert runtime.observed_boundary_from_dict(json.loads(line[len(runtime.RESULT_PREFIX):])) == boundary


def test_admit_command_reads_the_request_from_stdin_and_a_file(tmp_path, monkeypatch, capsys):
    data = make_data_dir(tmp_path)
    for key, value in local_env(data).items():
        monkeypatch.setenv(key, value)
    boundary = JournalBoundary("TESTLIB", "RCV0001", 4200, NOW, receiver_identity=IDENTITY)
    request = {"source_id": "src-1", "pipeline_id": "pipe-1", "table_id": "tbl-orders",
               "journal_library": "TESTLIB", "journal_name": "TESTJRN", "raw_prefix": "raw/example",
               "boundary": runtime.observed_boundary_to_dict(boundary)}
    document = tmp_path / "request.json"
    document.write_text(json.dumps(request))
    assert runtime.main(["admit", "--request-file", str(document)]) == 0
    first = json.loads(result_lines(capsys.readouterr().out)[0][len(runtime.RESULT_PREFIX):])
    assert set(first) == {"binding", "boundary", "run_id", "evidence_key", "raw_prefix_root", "stream_key",
                          "table_id", "pipeline_id"}
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(read=lambda: json.dumps(dict(request, run_id=first["run_id"]))))
    assert runtime.main(["admit", "--request-file", "-"]) == 0
    again = json.loads(result_lines(capsys.readouterr().out)[0][len(runtime.RESULT_PREFIX):])
    assert again == first
    assert runtime.main(["admit"]) == 1
    assert runtime.ERROR_PREFIX in capsys.readouterr().err


def test_a_non_local_backend_is_refused_before_any_work(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("QUADRINGENT_STORAGE_BACKEND", "aws")
    monkeypatch.setenv("AS400_RAW_BUCKET", "bucket-a")
    assert runtime.main(["admit", "--request-json", "{}"]) == 1
    assert "must be local" in capsys.readouterr().err


def test_unexpected_errors_expose_only_the_exception_type(monkeypatch, capsys):
    def explode(args):
        raise KeyError("password=hunter2-value")

    monkeypatch.setitem(runtime.COMMANDS, "fs-probe", explode)
    assert runtime.main(["fs-probe"]) == 1
    captured = capsys.readouterr()
    assert "KeyError" in captured.err and "hunter2" not in captured.err and captured.out == ""


# -- snowflake-verify ---------------------------------------------------------------------------------


class FakeVerifier:
    def __init__(self, ok=True):
        self.ok = ok
        self.requests = []

    def verify(self, request):
        from quadringent_control_plane.v2.services.destination_verifier import (
            DestinationVerificationResult, VerificationOutcome)

        self.requests.append(request)
        good, bad = VerificationOutcome(True, "fine"), VerificationOutcome(False, "refused")
        return DestinationVerificationResult(good, good, good, good, good, good if self.ok else bad)


@pytest.fixture()
def snowflake_env(tmp_path, monkeypatch):
    key = tmp_path / "key.p8"
    key.write_text("-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n")
    for name, value in {"SNOWFLAKE_ACCOUNT": "EXAMPLEORG-EXAMPLEACCOUNT", "SNOWFLAKE_USER": "SVC_LOCAL",
                        "SNOWFLAKE_ROLE": "QDT_ROLE_LOCAL", "QUADRINGENT_DESTINATION_DATABASE": "QUADRINGENT",
                        "QUADRINGENT_DESTINATION_SCHEMA": "CURATED",
                        runtime.ENV_SNOWFLAKE_KEY_FILE: str(key)}.items():
        monkeypatch.setenv(name, value)
    return key


@pytest.mark.parametrize("ok", [True, False])
def test_snowflake_verify_reports_checks_without_the_key(snowflake_env, ok):
    verifier = FakeVerifier(ok)
    result, code = runtime.command_snowflake_verify(SimpleNamespace(key_file=None), verifier=verifier)
    assert (result["ok"], code) == (ok, 0 if ok else 1)
    assert set(result["checks"]) == {"connection", "role", "warehouse", "database", "schema", "load_privileges"}
    assert "BEGIN PRIVATE KEY" not in json.dumps(result)
    request = verifier.requests[0]
    assert (request.service_role, request.warehouse, request.destination_schema) == (
        "QDT_ROLE_LOCAL", "QDT_WH_LOCAL", "CURATED")
    assert request.private_key_pem.startswith("-----BEGIN PRIVATE KEY-----")


def test_snowflake_verify_refuses_a_role_outside_the_convention(snowflake_env, monkeypatch):
    monkeypatch.setenv("SNOWFLAKE_ROLE", "SYSADMIN")
    with pytest.raises(runtime.LocalRuntimeError):
        runtime.command_snowflake_verify(SimpleNamespace(key_file=None), verifier=FakeVerifier())


# -- exec wrappers -------------------------------------------------------------------------------------


STUB = """
import json, os, sys
print(json.dumps({
    "argv": sys.argv[1:],
    "password": os.environ.get("ISERIES_PASSWORD"),
    "key": os.environ.get("SNOWFLAKE_PRIVATE_KEY_PEM"),
    "safe_path": sys.flags.safe_path,
    "umask": oct(os.umask(0o077)),
    "has_file_vars": [name for name in os.environ if name.endswith("_FILE") and "LOCAL" in name],
}))
"""


@pytest.mark.parametrize("wrapper,secret_env,flag,expected_safe", [
    ("exec-capture", "password", "--password-file", False),
    ("exec-loader", "key", "--key-file", True),
])
def test_exec_wrappers_hand_the_secret_to_the_child_environment_only(tmp_path, wrapper, secret_env, flag, expected_safe):
    stub = tmp_path / "stub.py"
    stub.write_text(STUB)
    secret = tmp_path / "secret"
    secret.write_text("top-secret-value\n" if wrapper == "exec-capture" else "-----KEY-----\n")
    result = run_module(wrapper, "--script", str(stub), flag, str(secret), "--", "--max-polls", "1", "extra")
    assert result.returncode == 0, result.stderr
    child = json.loads(result.stdout)
    expected = "top-secret-value" if wrapper == "exec-capture" else "-----KEY-----\n"
    assert child[secret_env] == expected
    assert child["argv"] == ["--max-polls", "1", "extra"]
    assert child["safe_path"] == (1 if expected_safe else 0)
    assert child["umask"] == "0o77"
    assert "top-secret-value" not in result.stderr and expected.strip() not in " ".join(child["argv"])
    other = "key" if secret_env == "password" else "password"
    assert child[other] is None


def test_exec_wrapper_failures_never_start_the_child(tmp_path):
    stub = tmp_path / "stub.py"
    stub.write_text(STUB)
    missing = run_module("exec-capture", "--script", str(stub), "--password-file", str(tmp_path / "absent"))
    assert missing.returncode == 1 and missing.stdout == "" and "secret file" in missing.stderr
    relative = tmp_path / "pw"
    relative.write_text("x")
    refused = run_module("exec-capture", "--script", "stub.py", "--password-file", str(relative))
    assert refused.returncode == 1 and "absolute" in refused.stderr


def test_exec_uses_the_image_scripts_by_default(tmp_path, monkeypatch):
    secret = tmp_path / "pw"
    secret.write_text("x\n")
    calls = []
    monkeypatch.setattr(runtime, "exec_with_secret", lambda script, argv, **kwargs: calls.append((script, kwargs)))
    runtime.command_exec_capture(SimpleNamespace(script=None, password_file=str(secret), child_arguments=[]))
    runtime.command_exec_loader(SimpleNamespace(script=None, key_file=str(secret), child_arguments=["--once"]))
    assert calls[0][0] == "/app/as400_continuous_capture.py"
    assert calls[1][0] == "/app/quadringent_destination_loader.py"
    assert calls[1][1]["python_flags"] == ("-P",)


def test_admit_command_accepts_the_boundary_result_and_identity_flags(tmp_path, monkeypatch, capsys):
    data = make_data_dir(tmp_path)
    for key, value in {**local_env(data), "AS400_JOURNAL_LIBRARY": "TESTLIB", "AS400_JOURNAL_NAME": "TESTJRN"}.items():
        monkeypatch.setenv(key, value)
    boundary = JournalBoundary("TESTLIB", "RCV0001", 4200, NOW, receiver_identity=IDENTITY)
    flags = ["admit", "--boundary-json", json.dumps(runtime.observed_boundary_to_dict(boundary)),
             "--source-id", "src-1", "--pipeline-id", "pipe-1", "--table-id", "tbl-orders",
             "--raw-prefix-root", "raw/example"]
    assert runtime.main(flags) == 0
    result = json.loads(result_lines(capsys.readouterr().out)[0][len(runtime.RESULT_PREFIX):])
    assert result["raw_prefix_root"] == "raw/example" and result["table_id"] == "tbl-orders"
    assert runtime.main([item for item in flags if item != "--table-id" and item != "tbl-orders"]) == 1
    assert "--table-id is required" in capsys.readouterr().err
    assert runtime.main([*flags, "--request-json", "{}"]) == 1


def test_exec_capture_arguments_after_the_separator_are_passed_through(monkeypatch):
    seen = []
    monkeypatch.setattr(runtime, "command_exec_capture", lambda args: seen.append(args.child_arguments) or ({}, 0))
    monkeypatch.setitem(runtime.COMMANDS, "exec-capture", runtime.command_exec_capture)
    assert runtime.main(["exec-capture", "--", "--console-snapshot-path", "/data/console/capture.json"]) == 0
    assert runtime._child_arguments(seen[0]) == ["--console-snapshot-path", "/data/console/capture.json"]
    assert runtime.main(["exec-capture", "--script", "/app/x.py", "--", "--host", "h"]) == 0
    assert runtime._child_arguments(seen[1]) == ["--host", "h"]


def test_copy_environment_matches_the_cluster_copy_job_environment():
    """``copy_job_environment`` mirrors ``build_initial_copy_job``: same variable names, same values."""
    sys.path.insert(1, str(ROOT / "scripts"))
    from test_v2_executor_manifest_script_contracts import _container_env, _copy_spec
    from quadringent_control_plane.v2.executor.manifests import build_initial_copy_job
    from test_v2_capture_binding_handoff import boundary as bound_boundary

    boundary = bound_boundary(4250)
    spec = _copy_spec(boundary=boundary)
    cluster = _container_env(build_initial_copy_job(spec))
    base = {key: cluster[key] for key in ("AS400_RAW_BUCKET", "AS400_SOURCE_TIME_ZONE", "ISERIES_HOST", "ISERIES_USER",
                                         "QUADRINGENT_STORAGE_BACKEND")}
    admission = {"raw_prefix_root": spec.raw_prefix, "run_id": spec.run_id, "evidence_key": spec.evidence_key,
                 "pipeline_id": spec.pipeline_id, "table_id": spec.table_id}
    local = runtime.copy_job_environment(base, admission, boundary, boundary.capture_binding,
                                         snapshot_dir="/data/scratch/run", schema=spec.schema_name,
                                         table=spec.table_name)
    assert set(local) == set(cluster)
    differing = {key for key in cluster if local[key] != cluster[key]}
    assert differing == {"QUADRINGENT_STORAGE_BACKEND", "AS400_SNAPSHOT_OUTPUT_DIR"}
    assert local["QUADRINGENT_STORAGE_BACKEND"] == "local"

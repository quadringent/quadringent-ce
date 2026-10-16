"""A native observation must finish, validate and retain its exact original."""
import hashlib
import os
import stat
import sys
import time

import pytest

from quadringent.qualification.verification_process import (
    DocumentLimits, ProcessCollectionError, collect_process_document,
)

PAYLOAD = b'{"format_version":"fixture-observation-v1","value":"spaces  "}\n'


def child(script):
    return (sys.executable, "-I", "-c", script)


def validate(value):
    if value != {"format_version": "fixture-observation-v1", "value": "spaces  "}:
        raise ValueError("private-provider-message")


def collect(tmp_path, script, **kwargs):
    return collect_process_document(
        child(script), directory=tmp_path / "observation", input_bytes=b"",
        environment={}, validate=validate, **kwargs,
    )


def test_exact_observation_is_durable_after_exit_zero_and_validation(tmp_path):
    result = collect(tmp_path, f"import sys; sys.stdout.buffer.write({PAYLOAD!r})")
    assert result.path.read_bytes() == PAYLOAD
    assert result.sha256 == hashlib.sha256(PAYLOAD).hexdigest()
    assert result.byte_count == len(PAYLOAD) and result.return_code == 0
    assert result.document == {"format_version": "fixture-observation-v1", "value": "spaces  "}
    assert stat.S_IMODE(result.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(result.path.parent.stat().st_mode) == 0o700
    assert list(result.path.parent.iterdir()) == [result.path]


@pytest.mark.parametrize("payload", [
    b'{"format_version":"fixture-observation-v1","value":"spaces  ","value":"spaces  "}',
    b'{"format_version":"fixture-observation-v1","value":NaN}',
    b'{"format_version":"fixture-observation-v1","value":Infinity}',
    PAYLOAD + b"{}\n", b"[]", b"null", b"", b"\xff",
    b'{"format_version":"fixture-observation-v1","value":"different"}',
])
def test_malformed_duplicate_foreign_or_multiple_documents_are_rejected(tmp_path, payload):
    with pytest.raises(ProcessCollectionError) as error:
        collect(tmp_path, f"import sys; sys.stdout.buffer.write({payload!r})")
    assert "private-provider-message" not in str(error.value)
    assert not list((tmp_path / "observation").iterdir())


def test_valid_json_does_not_admit_failed_process(tmp_path):
    with pytest.raises(ProcessCollectionError, match="process_exit"):
        collect(tmp_path, f"import sys; sys.stdout.buffer.write({PAYLOAD!r}); sys.exit(7)")
    assert not list((tmp_path / "observation").iterdir())


def test_valid_json_does_not_bypass_hard_deadline(tmp_path):
    start = time.monotonic()
    with pytest.raises(ProcessCollectionError, match="process_deadline"):
        collect(tmp_path, f"import sys,time; sys.stdout.buffer.write({PAYLOAD!r}); "
                "sys.stdout.flush(); time.sleep(30)", limits=DocumentLimits(max_seconds=1))
    assert time.monotonic() - start < 5
    assert not list((tmp_path / "observation").iterdir())


def test_document_byte_budget_kills_large_producer(tmp_path):
    with pytest.raises(ProcessCollectionError, match="process_byte_budget"):
        collect(tmp_path, "import sys; sys.stdout.buffer.write(b'x'*1000000)",
                limits=DocumentLimits(max_bytes=100))
    assert not list((tmp_path / "observation").iterdir())


def test_secret_stdin_and_stderr_are_never_retained_or_inherited(tmp_path, monkeypatch):
    monkeypatch.setenv("UNRELATED_TOKEN", "must-not-forward")
    secret = b"fixture-secret\n" * 1000
    script = ("import sys,os,hashlib; assert 'UNRELATED_TOKEN' not in os.environ; "
              f"assert hashlib.sha256(sys.stdin.buffer.read()).hexdigest()=={hashlib.sha256(secret).hexdigest()!r}; "
              "sys.stderr.buffer.write(b'fixture-secret'*100000); "
              f"sys.stdout.buffer.write({PAYLOAD!r})")
    result = collect_process_document(child(script), directory=tmp_path / "observation",
                                      input_bytes=secret, environment={}, validate=validate)
    assert result.path.read_bytes() == PAYLOAD and "fixture-secret" not in repr(result)
    assert list(result.path.parent.iterdir()) == [result.path]


def test_original_replaced_by_validator_is_refused(tmp_path):
    def replacing(value):
        path = tmp_path / "observation" / "original.json"
        path.unlink()
        path.write_bytes(PAYLOAD)
        os.chmod(path, 0o600)
    with pytest.raises(ProcessCollectionError, match="original_changed"):
        collect_process_document(child(f"import sys; sys.stdout.buffer.write({PAYLOAD!r})"),
                                 directory=tmp_path / "observation", input_bytes=b"",
                                 environment={}, validate=replacing)


def test_success_stops_descendants_that_closed_their_inherited_pipes(tmp_path):
    marker = tmp_path / "escaped-descendant"
    script = ("import os,sys,time\n"
              "if os.fork()==0:\n"
              " os.close(0); os.close(1); os.close(2)\n"
              " time.sleep(0.4)\n"
              f" open({str(marker)!r},'w').write('still-running')\n"
              " open('original.json','ab').write(b' ')\n"
              " os._exit(0)\n"
              f"sys.stdout.buffer.write({PAYLOAD!r})\n")
    result = collect(tmp_path, script)
    time.sleep(0.6)
    assert not marker.exists()
    assert result.path.read_bytes() == PAYLOAD


@pytest.mark.parametrize("changes", [{"max_bytes": True}, {"max_bytes": 0},
                                     {"max_bytes": 4*1024*1024+1},
                                     {"max_seconds": 0}, {"max_seconds": True},
                                     {"max_seconds": 901}])
def test_document_limits_are_exact_bounded_integers(changes):
    with pytest.raises(ValueError):
        DocumentLimits(**changes)

import hashlib
import io
import os
import stat
import sys
import time

import pytest

from quadringent.qualification.verification_process import (
    ProcessCollectionError, collect_process_stream,
)
from quadringent.qualification.verification_stream import StreamLimits, write_stream
from quadringent.qualification import verification_process


SCOPE = "a" * 64
STAMP = "2026-10-16T00:00:00Z"


def frames():
    output = io.BytesIO()
    write_stream(output, ({"ID": 1, "TEXT": "é  "},), scope_sha256=SCOPE,
                 dataset="source", started_at=STAMP, completed_at=STAMP)
    return output.getvalue()


def child(script):
    return (sys.executable, "-I", "-c", script)


def collect(tmp_path, script, **kwargs):
    return collect_process_stream(child(script), directory=tmp_path / "collection", input_bytes=b"",
                                  environment={}, scope_sha256=SCOPE, dataset="source", **kwargs)


def test_exit_zero_and_valid_complete_stdout_preserve_exact_private_original(tmp_path):
    data = frames()
    result = collect(tmp_path, f"import sys; sys.stdout.buffer.write({data!r})")
    assert result.path.read_bytes() == data
    assert result.summary.row_count == 1
    assert result.summary.sha256 == hashlib.sha256(data).hexdigest()
    assert result.return_code == 0
    assert stat.S_IMODE(result.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(result.path.parent.stat().st_mode) == 0o700
    assert list(result.path.parent.iterdir()) == [result.path]


def test_valid_footer_does_not_admit_nonzero_process_exit(tmp_path):
    with pytest.raises(ProcessCollectionError, match="process_exit"):
        collect(tmp_path, f"import sys; sys.stdout.buffer.write({frames()!r}); sys.exit(42)")
    assert not list((tmp_path / "collection").iterdir())


def test_success_stops_descendants_after_parent_exit_and_eof(tmp_path):
    marker = tmp_path / "escaped-descendant"
    script = ("import os,sys,time\n"
              "if os.fork()==0:\n"
              " os.close(0); os.close(1); os.close(2)\n"
              " time.sleep(0.4)\n"
              f" open({str(marker)!r},'w').write('still-running')\n"
              " open('original.ndjson','ab').write(b' ')\n"
              " os._exit(0)\n"
              f"sys.stdout.buffer.write({frames()!r})\n")
    result = collect(tmp_path, script)
    time.sleep(0.6)
    assert not marker.exists()
    assert result.path.read_bytes() == frames()


@pytest.mark.parametrize("data", [frames()[:-1], frames() + b"extra\n", b"", b"secret-value\n",
                                   frames().replace(SCOPE.encode(), b"b" * 64),
                                   frames().replace(b'"source"', b'"mirror"')])
def test_malformed_or_foreign_bytes_never_leave_a_success_artifact(tmp_path, data):
    with pytest.raises(ProcessCollectionError) as failure:
        collect(tmp_path, f"import sys; sys.stdout.buffer.write({data!r})")
    assert "secret-value" not in str(failure.value)
    assert not list((tmp_path / "collection").iterdir())


def test_complete_footer_cannot_bypass_hung_process_deadline(tmp_path):
    started = time.monotonic()
    with pytest.raises(ProcessCollectionError, match="deadline"):
        collect(tmp_path, f"import sys,time; sys.stdout.buffer.write({frames()!r}); sys.stdout.flush(); time.sleep(30)",
                limits=StreamLimits(max_seconds=1))
    assert time.monotonic() - started < 5
    assert not list((tmp_path / "collection").iterdir())


def test_large_stderr_is_drained_without_recording_credentials_or_deadlock(tmp_path):
    result = collect(tmp_path, "import sys; "
                     "sys.stderr.buffer.write(b'credential-must-not-survive' * 100000); "
                     f"sys.stdout.buffer.write({frames()!r})")
    assert result.summary.row_count == 1
    assert "credential-must-not-survive" not in repr(result)
    assert list(result.path.parent.iterdir()) == [result.path]


def test_stdin_is_streamed_without_inheriting_parent_environment_or_recording_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("UNRELATED_TEST_TOKEN", "never-forward")
    secret = b"private-credential\n" * 2000
    script = ("import sys,os,hashlib; assert 'UNRELATED_TEST_TOKEN' not in os.environ; "
              f"assert hashlib.sha256(sys.stdin.buffer.read()).hexdigest()=={hashlib.sha256(secret).hexdigest()!r}; "
              f"sys.stdout.buffer.write({frames()!r})")
    result = collect_process_stream(child(script), directory=tmp_path / "collection", input_bytes=secret,
                                    environment={}, scope_sha256=SCOPE, dataset="source")
    assert result.path.read_bytes() == frames()
    assert "private-credential" not in repr(result)


def test_byte_budget_interrupts_output_before_it_is_archived_in_full(tmp_path):
    with pytest.raises(ProcessCollectionError, match="byte_budget"):
        collect(tmp_path, "import sys; sys.stdout.buffer.write(b'x' * 1000000)",
                limits=StreamLimits(max_bytes=100))
    assert not list((tmp_path / "collection").iterdir())


@pytest.mark.parametrize("kind", ["existing", "symlink", "ancestor_symlink"])
def test_directory_is_new_and_has_no_symlink_ancestor(tmp_path, kind):
    directory = tmp_path / "collection"
    if kind == "existing":
        directory.mkdir()
    elif kind == "symlink":
        directory.symlink_to(tmp_path, target_is_directory=True)
    else:
        link = tmp_path / "link"
        link.symlink_to(tmp_path, target_is_directory=True)
        directory = link / "collection"
    with pytest.raises(ProcessCollectionError):
        collect_process_stream(child("raise SystemExit('must not start')"), directory=directory,
                               input_bytes=b"", environment={}, scope_sha256=SCOPE, dataset="source")


def test_frame_row_budget_is_enforced_after_transport(tmp_path):
    output = io.BytesIO()
    write_stream(output, ({"ID": x} for x in range(2)), scope_sha256=SCOPE,
                 dataset="source", started_at=STAMP, completed_at=STAMP)
    with pytest.raises(ProcessCollectionError, match="invalid_stream"):
        collect(tmp_path, f"import sys; sys.stdout.buffer.write({output.getvalue()!r})",
                limits=StreamLimits(max_rows=1))


def test_fsync_failure_prevents_success_and_removes_original(tmp_path, monkeypatch):
    def fail(_fd):
        raise OSError("private storage details")

    monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(ProcessCollectionError) as failure:
        collect(tmp_path, f"import sys; sys.stdout.buffer.write({frames()!r})")
    assert "private storage details" not in str(failure.value)
    assert not list((tmp_path / "collection").iterdir())


def test_new_directory_entry_is_durable_before_starting_the_producer(tmp_path, monkeypatch):
    synced = []
    real_sync, real_popen = os.fsync, verification_process.subprocess.Popen
    parent_inode = tmp_path.stat().st_ino

    def sync(descriptor):
        synced.append(os.fstat(descriptor).st_ino)
        real_sync(descriptor)

    def start(*args, **kwargs):
        assert parent_inode in synced
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(os, "fsync", sync)
    monkeypatch.setattr(verification_process.subprocess, "Popen", start)
    assert collect(tmp_path, f"import sys; sys.stdout.buffer.write({frames()!r})").return_code == 0


def test_expiration_during_final_sync_cannot_return_a_success(tmp_path, monkeypatch):
    offset = [0]
    real_clock, real_sync = time.monotonic, os.fsync

    def sync(descriptor):
        real_sync(descriptor)
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            offset[0] += 2

    monkeypatch.setattr(time, "monotonic", lambda: real_clock() + offset[0])
    monkeypatch.setattr(os, "fsync", sync)
    with pytest.raises(ProcessCollectionError, match="deadline"):
        collect(tmp_path, f"import sys; sys.stdout.buffer.write({frames()!r})",
                limits=StreamLimits(max_seconds=1))
    assert not list((tmp_path / "collection").iterdir())


@pytest.mark.parametrize("overrides", [
    {"command": "echo shell"}, {"command": ()}, {"command": ("x\x00",)},
    {"input_bytes": b"x" * 65537}, {"environment": None}, {"scope_sha256": "invalid"},
    {"dataset": "unrecognized"}, {"limits": None},
])
def test_invalid_configuration_fails_before_process_creation(tmp_path, overrides):
    arguments = dict(command=child("raise SystemExit('must not start')"), directory=tmp_path / "collection",
                     input_bytes=b"", environment={}, scope_sha256=SCOPE, dataset="source")
    arguments.update(overrides)
    with pytest.raises(ProcessCollectionError):
        collect_process_stream(**arguments)
    assert not (tmp_path / "collection").exists()

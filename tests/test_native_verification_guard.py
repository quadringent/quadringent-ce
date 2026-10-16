"""Lifecycle receipts retain original bytes without granting admission."""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from quadringent.qualification import verification_guard as guard
from quadringent.qualification.verification_native import native_journal_processes
from test_native_verification_native import load, native_inputs  # noqa: F401


def encoded(value):
    return (json.dumps(value, sort_keys=True) + "\n").encode()


def save(path, content):
    path.write_bytes(content)
    path.chmod(0o600)


def document(copy, scope, mode):
    return dict(format_version="quadringent-native-journal-guard-v1", mode=mode,
                state="complete", error_code=None, scope_sha256=scope.sha256,
                context_sha256=copy.context_sha256, capture_binding_sha256=copy.binding_sha256,
                copy_boundary_sha256=copy.boundary_sha256, copy_run_id=scope.copy_run_id,
                bundle_sha256=copy.bundle_sha256, native_admission="not_evaluated",
                transaction_consistency_verified=False,
                guards=[dict(phase=phase) for phase in (
                    ("before_business", "after_business") if mode == "journal" else ("after_all_streams",))])


@pytest.fixture
def prepared(native_inputs, tmp_path):
    _, scope, _ = native_inputs
    copy = load(native_inputs)
    bundle = tmp_path / "descriptor.bundle"
    save(bundle, copy.bundle)
    journal = tmp_path / "journal-guard.json"
    save(journal, encoded(document(copy, scope, "journal")))
    java = tmp_path / "java"
    expected = document(copy, scope, "after_all_streams")
    script = (f"#!{sys.executable}\nimport json,os,sys\n"
              "from pathlib import Path\n"
              "assert sys.argv[3]=='io.quadringent.as400.NativeLifecycleGuard'\n"
              "assert sys.stdin.buffer.read()==b'host\\nuser\\nPRIVATE_GUARD_CANARY\\n'\n"
              f"value={expected!r}\n"
              "mode=os.environ.get('GUARD_FAULT','')\n"
              "if mode=='exit':\n print('PRIVATE_GUARD_CANARY',file=sys.stderr);sys.exit(7)\n"
              "if mode=='phase': value['guards'][0]['phase']='before_business'\n"
              "if mode=='scope': value['scope_sha256']='f'*64\n"
              "if mode=='admitted': value['native_admission']='admitted'\n"
              "raw=(json.dumps(value,sort_keys=True)+'\\n').encode()\n"
              "path=Path(sys.argv[27]);path.write_bytes(raw+b' ' if mode=='different' else raw)\n"
              "path.chmod(0o600)\n"
              "if mode=='duplicate': raw=b'{\"state\":\"incomplete\",'+raw[1:]\n"
              "if mode=='oversized': raw=b' '* (4*1024**2+1)\n"
              "sys.stdout.buffer.write(raw)\n")
    java.write_text(script)
    java.chmod(0o700)
    process = native_journal_processes(copy, scope, java=str(java), classpath="/fixture/classes",
        credentials=b"host\nuser\nPRIVATE_GUARD_CANARY\n", environment={}, bundle_path=bundle,
        guard_receipt_path=journal, max_seconds=60)[0].process
    return SimpleNamespace(copy=copy, scope=scope, producer=process, journal=journal, bundle=bundle,
                           directory=tmp_path / "after-all-streams", parent=tmp_path)


def collect(prepared, **changes):
    values = dict(guard_receipt_path=prepared.journal, directory=prepared.directory,
                  check_current=lambda: True, max_seconds=10)
    return guard.collect_native_guards(prepared.copy, prepared.scope, prepared.producer,
                                       **{**values, **changes})


def test_real_process_retains_identical_journal_final_and_stdout_originals(prepared):
    result = collect(prepared)
    assert result.journal_original.path == prepared.journal
    assert result.final_original.path == prepared.parent / "after-all-streams-guard.json"
    assert result.stdout_original.path == prepared.directory / "original.json"
    for original in (result.journal_original, result.final_original, result.stdout_original):
        raw = original.path.read_bytes()
        assert original.byte_count == len(raw)
        assert original.sha256 == hashlib.sha256(raw).hexdigest()
    assert result.final_original.sha256 == result.stdout_original.sha256
    assert result.metadata["native_admission"] == "not_evaluated"
    assert result.metadata["final_guard"]["mode"] == "after_all_streams"
    assert "PRIVATE_GUARD_CANARY" not in repr(result)
    assert not any(key in result.metadata for key in ("command", "input_bytes", "environment"))


@pytest.mark.parametrize("fault", ["exit", "phase", "scope", "admitted", "different", "duplicate", "oversized"])
def test_process_errors_and_altered_documents_return_no_result_or_secret(prepared, fault):
    prepared.producer = replace(prepared.producer, environment={"GUARD_FAULT": fault})
    with pytest.raises(guard.NativeGuardError) as failure:
        collect(prepared)
    assert str(failure.value) == "native_guard_failed"
    assert failure.value.__cause__ is None
    assert "PRIVATE_GUARD_CANARY" not in str(failure.value)


@pytest.mark.parametrize("change", [False, 1, None])
def test_current_fence_requires_exact_true_before_execution(prepared, change):
    with pytest.raises(guard.NativeGuardError):
        collect(prepared, check_current=lambda: change)
    assert not prepared.directory.exists()


def test_fence_loss_after_process_cannot_return_originals(prepared):
    with pytest.raises(guard.NativeGuardError):
        collect(prepared, check_current=lambda: not prepared.directory.exists())


@pytest.mark.parametrize("target", ["bundle", "journal"])
@pytest.mark.parametrize("damage", ["symlink", "fifo", "mode", "hardlink"])
def test_original_inputs_require_regular_private_single_link_files(prepared, target, damage):
    path = getattr(prepared, target)
    if damage == "mode":
        path.chmod(0o644)
    elif damage == "hardlink":
        os.link(path, path.with_suffix(".linked"))
    else:
        path.unlink()
        if damage == "symlink":
            other = prepared.parent / "other"
            save(other, b"other")
            path.symlink_to(other)
        else:
            os.mkfifo(path, 0o600)
    with pytest.raises(guard.NativeGuardError):
        collect(prepared)
    assert not prepared.directory.exists()


def test_bundle_tamper_and_process_scope_cannot_be_rebound(prepared):
    save(prepared.bundle, prepared.copy.bundle + b" ")
    with pytest.raises(guard.NativeGuardError):
        collect(prepared)
    save(prepared.bundle, prepared.copy.bundle)
    command = list(prepared.producer.command)
    command[14] = "f" * 64
    prepared.producer = replace(prepared.producer, command=tuple(command))
    with pytest.raises(guard.NativeGuardError):
        collect(prepared)
    assert not prepared.directory.exists()


def test_original_parent_symlink_is_not_followed(prepared):
    alias = prepared.parent / "alias"
    alias.symlink_to(prepared.parent, target_is_directory=True)
    path = alias / prepared.journal.name
    command = list(prepared.producer.command)
    command[27] = str(path)
    prepared.producer = replace(prepared.producer, command=tuple(command))
    with pytest.raises(guard.NativeGuardError):
        collect(prepared, guard_receipt_path=path)
    assert not prepared.directory.exists()


def test_short_reads_are_consumed_through_eof(prepared, monkeypatch):
    shadow = SimpleNamespace(**vars(os))
    original = os.read
    sizes = []
    def fragmented(descriptor, maximum):
        sizes.append(maximum)
        return original(descriptor, min(3, maximum))
    shadow.read = fragmented
    monkeypatch.setattr(guard, "os", shadow)
    assert collect(prepared).final_original.sha256
    assert len(sizes) > 10 and all(0 < size <= 65536 for size in sizes)


@pytest.mark.parametrize("damage", ["reference", "duplicate", "oversized"])
def test_invalid_journal_receipt_is_refused_before_final_process(prepared, damage):
    raw = prepared.journal.read_bytes()
    if damage == "reference":
        value = json.loads(raw)
        value["copy_run_id"] = "other"
        raw = encoded(value)
    elif damage == "duplicate":
        raw = b'{"state":"incomplete",' + raw[1:]
    else:
        raw = b" " * (4 * 1024**2 + 1)
    save(prepared.journal, raw)
    with pytest.raises(guard.NativeGuardError):
        collect(prepared)
    assert not prepared.directory.exists()


@pytest.mark.parametrize("damage", ["symlink", "hardlink", "mode"])
def test_final_original_paths_are_revalidated_after_process(prepared, monkeypatch, damage):
    original = guard.collect_process_document
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        side = prepared.parent / "after-all-streams-guard.json"
        if damage == "mode":
            side.chmod(0o644)
        else:
            side.unlink()
            if damage == "symlink":
                side.symlink_to(result.path)
            else:
                os.link(result.path, side)
        return result
    monkeypatch.setattr(guard, "collect_process_document", changed)
    with pytest.raises(guard.NativeGuardError):
        collect(prepared)


def test_journal_original_must_still_match_after_final_process(prepared, monkeypatch):
    original = guard.collect_process_document
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        save(prepared.journal, prepared.journal.read_bytes() + b" ")
        return result
    monkeypatch.setattr(guard, "collect_process_document", changed)
    with pytest.raises(guard.NativeGuardError):
        collect(prepared)


def test_existing_final_receipt_is_never_overwritten(prepared):
    path = prepared.parent / "after-all-streams-guard.json"
    save(path, b"untouched")
    with pytest.raises(guard.NativeGuardError):
        collect(prepared)
    assert path.read_bytes() == b"untouched"
    assert not prepared.directory.exists()


@pytest.mark.parametrize("seconds", [0, True, 1.5, 61])
def test_deadline_limit_is_an_explicit_bounded_integer(prepared, seconds):
    with pytest.raises(guard.NativeGuardError):
        collect(prepared, max_seconds=seconds)


def test_deadline_crossed_during_read_never_returns_or_spawns(prepared, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(guard, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    read = guard.os.read
    def delayed(*args):
        value = read(*args)
        clock[0] = 100.0
        return value
    monkeypatch.setattr(guard.os, "read", delayed)
    with pytest.raises(guard.NativeGuardError):
        collect(prepared, max_seconds=1)
    assert not prepared.directory.exists()


@pytest.mark.parametrize("after", [61.0, -1.0, float("inf"), float("nan")])
def test_deadline_and_clock_are_rechecked_after_a_slow_configuration_fence(monkeypatch, after):
    clock = [0.0]
    monkeypatch.setattr(guard.time, "monotonic", lambda: clock[0])

    def current():
        clock[0] = after
        return True

    with pytest.raises(ValueError, match="^native_guard_fence_or_deadline$"):
        guard._Budget(60, current)

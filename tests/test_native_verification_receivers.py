"""Receiver observations retain native originals and grant no cut/admission authority."""

from base64 import b64encode
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import stat
import shutil
import subprocess
import sys

import pytest

from quadringent.capture_binding import CaptureBinding
from quadringent.receiver_identity import BoundCheckpoint, ReceiverIdentity
from quadringent.qualification.verification_contract import Column, ReceiverWindow, VerificationScope
from quadringent.qualification.verification_receivers import (
    ReceiverObservationConfig,
    observe_receiver,
    read_receiver_observation,
    validate_receiver_pair,
)
import quadringent.qualification.verification_receivers as receivers

REVISION = "a" * 40
IDENTITY = ReceiverIdentity("JRNLIB", "JRN", "RCVLIB", "R_Z", "ibmi-ifs-object-v1", "1:123:456:789")
BOOTSTRAP = BoundCheckpoint("scope", "epoch", IDENTITY, 10)
BINDING = CaptureBinding("scope", "epoch", "ibmi", "JRN", "local://fixture/receiver-authority-v1", BOOTSTRAP)
NATIVE_IDENTITY = b"as400-receiver-identity-v1\nidentity\tJRNLIB\tJRN\tRCVLIB\tR_Z\t1\t123\t456\t789\n"
ROW = "receiver\tRCVLIB\tR_Z\tATTACHED\t1\t30\t-\t-\t-\t-\n"


def scope(**changes):
    fields = dict(
        pipeline_id="p",
        table_id="t",
        copy_run_id="copy",
        source_library="DATA",
        source_table="ORDERS",
        destination_database="DEST",
        destination_schema="OUTPUT",
        history_table="HISTORY",
        mirror_table="MIRROR",
        columns=(Column("ID", "int"),),
        primary_key=("ID",),
        capture_binding=BINDING,
        copy_boundary=BINDING.checkpoint(IDENTITY, 15),
        checkpoint=BINDING.checkpoint(IDENTITY, 20),
        receiver_windows=(ReceiverWindow(IDENTITY, 16, 20),),
        expected_snapshot_rows=2,
        producer_revision=REVISION,
        producer_version="0.4.5",
    )
    return VerificationScope(**{**fields, **changes})


def config(**changes):
    return ReceiverObservationConfig(
        **{**dict(java="/fixture/java", classpath="/fixture/producer.jar", producer_revision=REVISION), **changes}
    )


def document(
    *,
    phase="before",
    current_scope=None,
    row=ROW,
    identity_before=NATIVE_IDENTITY,
    identity_after=NATIVE_IDENTITY,
    **changes,
):
    value = {
        "format_version": "quadringent-native-receiver-observation-v1",
        "scope_sha256": (current_scope or scope()).sha256,
        "phase": phase,
        "identity_before_base64": b64encode(identity_before).decode(),
        "catalog_base64": b64encode(("as400-receiver-catalog-v2\n" + row).encode()).decode(),
        "identity_after_base64": b64encode(identity_after).decode(),
    }
    return (json.dumps({**value, **changes}, sort_keys=True, separators=(",", ":")) + "\n").encode()


def parse(content=None, **changes):
    return read_receiver_observation(
        document() if content is None else content, scope=scope(), expected_config=config(), **changes
    )


def test_original_native_texts_and_native_generation_are_preserved_losslessly():
    raw = document()
    observed = parse(raw)
    assert observed.original.path is None
    assert observed.original.sha256 == hashlib.sha256(raw).hexdigest()
    assert observed.original.byte_count == len(raw)
    assert observed.native_originals["identity_before"] == NATIVE_IDENTITY
    assert observed.native_originals["catalog"] == ("as400-receiver-catalog-v2\n" + ROW).encode()
    assert observed.metadata["receiver_identity"] == IDENTITY.to_dict()
    assert observed.metadata["native_admission"] == "not_evaluated"
    assert observed.metadata["stable_cut_established"] is False


@pytest.mark.parametrize(
    "identity",
    [
        NATIVE_IDENTITY.replace(b"789", b"790"),
        NATIVE_IDENTITY.replace(b"JRNLIB", b"OTHER"),
        NATIVE_IDENTITY.replace(b"RCVLIB", b"OTHER"),
        NATIVE_IDENTITY.replace(b"R_Z", b"R_A"),
        NATIVE_IDENTITY.replace(b"\t1\t123", b"\t2\t123"),
    ],
)
@pytest.mark.parametrize("field", ["identity_before", "identity_after"])
def test_changed_native_identity_or_wrong_qualified_object_is_refused(identity, field):
    with pytest.raises(ValueError):
        parse(document(**{field: identity}))


@pytest.mark.parametrize(
    "row",
    [
        "",
        ROW.replace("\t1\t30", "\t17\t30"),
        ROW.replace("\t1\t30", "\t1\t19"),
        ROW.replace("ATTACHED", "UNKNOWN"),
        ROW.replace("\t1\t30", "\t-\t30"),
        ROW.replace("\t1\t30", "\t01\t30"),
        ROW + ROW,
        ROW.replace("\t-\t-\t-\t-", "\tLIB\t-\t-\t-"),
        ROW.replace("RCVLIB", "OTHER"),
        ROW.replace("\tR_Z\t", "\tR_A\t"),
        ROW + "unknown\tline\n",
    ],
)
def test_missing_incomplete_truncated_unknown_or_wrong_ranges_are_refused(row):
    with pytest.raises(ValueError):
        parse(document(row=row))


def test_valid_extra_receivers_are_retained_without_lexical_order_or_chain_claim():
    extra = "receiver\tOTHER\tR_A\tONLINE\t31\t40\tRCVLIB\tR_Z\t-\t-\n"
    observed = parse(document(row=extra + ROW))
    assert observed.metadata["catalog_receiver_count"] == 2
    assert observed.metadata["coverage"] == "same_receiver_observed_ranges_only"
    assert observed.metadata["full_chain_established"] is False


def test_pair_accepts_tail_growth_but_rejects_truncation_or_reordered_phases():
    before = parse()
    after = parse(document(phase="after", row=ROW.replace("\t30\t", "\t35\t")))
    result = validate_receiver_pair(scope(), before, after, expected_config=config())
    assert result.metadata["native_admission"] == "not_evaluated"
    assert result.metadata["stable_cut_established"] is False
    assert result.metadata["checkpoint"] == scope().checkpoint.to_dict()
    for invalid in (parse(document(phase="after", row=ROW.replace("\t30\t", "\t25\t"))), before):
        with pytest.raises(ValueError):
            validate_receiver_pair(scope(), before, invalid, expected_config=config())


@pytest.mark.parametrize(
    "changes",
    [
        {"phase": "later"},
        {"scope_sha256": "b" * 64},
        {"admitted": True},
        {"catalog_base64": "!"},
        {"format_version": "unknown"},
    ],
)
def test_unknown_claims_phase_scope_encoding_and_version_are_refused(changes):
    with pytest.raises(ValueError):
        parse(document(**changes))


def test_duplicate_json_and_nonfinite_input_are_refused():
    raw = document()
    for changed in (
        raw.replace(b'"phase":"before"', b'"phase":"before","phase":"before"'),
        raw.replace(b'"phase":"before"', b'"phase":NaN'),
        raw + b"{}",
        b"\xff",
    ):
        with pytest.raises(ValueError):
            parse(changed)


def executable(tmp_path, content, *, exit_code=0, tail=""):
    path = tmp_path / "synthetic-java"
    path.write_text(
        f"#!{sys.executable}\nimport os,sys,time\n"
        "assert not any(k.startswith('ISERIES_') or k=='UNRELATED_TOKEN' for k in os.environ)\n"
        "assert sys.stdin.buffer.read()==b'offline.invalid\\nREADER\\nfixture-password\\n'\n"
        f"sys.stdout.buffer.write({content!r}); sys.stdout.flush()\n{tail}\nsys.exit({exit_code})\n"
    )
    path.chmod(0o700)
    return path


def collect(tmp_path, content=None, *, phase="before", check_current=lambda: True, **changes):
    java = executable(tmp_path, document(phase=phase) if content is None else content, **changes)
    return observe_receiver(
        scope(),
        phase=phase,
        directory=tmp_path / "observation",
        config=config(java=str(java), max_seconds=1),
        input_bytes=b"offline.invalid\nREADER\nfixture-password\n",
        check_current=check_current,
    )


def test_executable_producer_retains_private_original_without_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("UNRELATED_TOKEN", "fixture-private")
    observed = collect(tmp_path)
    assert observed.original.path.read_bytes() == document()
    assert stat.S_IMODE(observed.original.path.stat().st_mode) == 0o600
    assert "fixture-password" not in repr(observed.metadata)
    assert list(observed.original.path.parent.iterdir()) == [observed.original.path]


@pytest.mark.parametrize("kind", ["nonzero", "malformed", "deadline", "overflow"])
def test_process_errors_eof_deadline_and_output_budget_cannot_yield_an_observation(tmp_path, kind):
    kwargs = {"exit_code": 7} if kind == "nonzero" else {"tail": "time.sleep(4)"} if kind == "deadline" else {}
    payload = b"invalid" if kind == "malformed" else b"x" * (1024 * 1024 + 1) if kind == "overflow" else document()
    with pytest.raises(ValueError):
        collect(tmp_path, payload, **kwargs)
    assert not list((tmp_path / "observation").glob("*"))


@pytest.mark.parametrize("bad", [False, 1, None])
def test_stale_context_before_spawn_has_no_directory_effect(tmp_path, bad):
    with pytest.raises(ValueError):
        collect(tmp_path, check_current=lambda: bad)
    assert not (tmp_path / "observation").exists()


@pytest.mark.parametrize(
    "environment",
    [{"ISERIES_PASSWORD": "never"}, {"JAVA_TOOL_OPTIONS": "-agentlib:bad"}, {"JDK_JAVA_OPTIONS": "-DtrustAll=true"}],
)
def test_process_environment_cannot_carry_credentials_or_jvm_injection(environment):
    with pytest.raises(ValueError):
        config(environment=environment)


def test_range_first_change_and_claimed_metadata_cannot_bypass_original_reparse():
    before = parse()
    after = parse(document(phase="after", row=ROW.replace("\t1\t30", "\t2\t30")))
    with pytest.raises(ValueError):
        validate_receiver_pair(scope(), before, after, expected_config=config())
    forged = replace(parse(document(phase="after")), original=replace(before.original, sha256="f" * 64))
    with pytest.raises(ValueError):
        validate_receiver_pair(scope(), before, forged, expected_config=config())
    metadata = before.metadata
    metadata["native_admission"] = "accepted"
    assert before.metadata["native_admission"] == "not_evaluated"


def test_later_rotation_outside_scope_does_not_require_admitting_a_full_chain():
    extra = "receiver\tRCVLIB\tR_A\tATTACHED\t31\t40\tRCVLIB\tR_Z\t-\t-\n"
    selected = ROW.replace("ATTACHED", "ONLINE").replace("\t-\t-\t-\t-", "\t-\t-\tRCVLIB\tR_A")
    before = parse()
    after = parse(document(phase="after", row=selected + extra))
    measured = validate_receiver_pair(scope(), before, after, expected_config=config())
    assert measured.metadata["full_chain_established"] is False
    assert measured.metadata["lifecycle_between_observations_established"] is False


def test_current_page_at_native_query_limit_is_incomplete_even_if_target_is_present():
    extra = "".join(f"receiver\tLIB\tR{i}\tONLINE\t31\t40\t-\t-\t-\t-\n" for i in range(999))
    with pytest.raises(ValueError):
        parse(document(row=ROW + extra))


def test_signed_native_ids_and_empty_window_at_before_first_position_are_exact():
    identity = replace(IDENTITY, native_generation="1:-9223372036854775808:9223372036854775807:0")
    bootstrap = BoundCheckpoint("scope", "epoch", identity, 0)
    binding = replace(BINDING, bootstrap=bootstrap)
    current = scope(
        capture_binding=binding,
        copy_boundary=bootstrap,
        checkpoint=bootstrap,
        receiver_windows=(ReceiverWindow(identity, 1, 0),),
    )
    raw_identity = NATIVE_IDENTITY.replace(b"\t123\t456\t789", b"\t-9223372036854775808\t9223372036854775807\t0")
    raw = document(current_scope=current, identity_before=raw_identity, identity_after=raw_identity)
    observed = read_receiver_observation(raw, scope=current, expected_config=config())
    assert observed.metadata["receiver_identity"] == identity.to_dict()


def test_unsupported_bootstrap_receiver_and_wrong_producer_revision_fail_before_spawn(tmp_path):
    other = replace(IDENTITY, receiver_name="R_OLD", native_generation="1:123:456:788")
    binding = replace(BINDING, bootstrap=replace(BOOTSTRAP, receiver_identity=other))
    for current, configured in (
        (scope(capture_binding=binding), config()),
        (scope(), config(producer_revision="b" * 40)),
    ):
        with pytest.raises(ValueError):
            observe_receiver(
                current,
                phase="before",
                directory=tmp_path / "not-created",
                config=configured,
                input_bytes=b"",
                check_current=lambda: True,
            )
    assert not (tmp_path / "not-created").exists()


def test_context_change_while_child_runs_cannot_return_an_observation(tmp_path):
    count = 0

    def check_current():
        nonlocal count
        count += 1
        return count < 5

    with pytest.raises(ValueError):
        collect(tmp_path, check_current=check_current)


@pytest.mark.parametrize(
    "changes",
    [
        {"max_bytes": True},
        {"max_bytes": 0},
        {"max_bytes": 1048577},
        {"max_seconds": True},
        {"max_seconds": 0},
        {"max_seconds": 61},
    ],
)
def test_limits_are_exact_bounded_values(changes):
    with pytest.raises(ValueError):
        config(**changes)


@pytest.fixture(scope="module")
def java_receiver_classes(tmp_path_factory):
    homebrew = Path("/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home/bin")
    javac = str(homebrew / "javac") if (homebrew / "javac").is_file() else shutil.which("javac")
    java = str(Path(javac).with_name("java")) if javac else None
    jar = Path.home() / ".m2/repository/net/sf/jt400/jt400/21.0.7/jt400-21.0.7.jar"
    if not java or not jar.is_file():
        pytest.skip("JDK21 and cached JTOpen21.0.7 required; no download is attempted")
    root = Path(__file__).resolve().parents[1]
    classes = tmp_path_factory.mktemp("receiver-java133")
    package = root / "java/src/main/java/io/quadringent/as400"
    sources = [
        package / (name + ".java")
        for name in (
            "TlsTrust",
            "ReadOnlyReceiverCatalog",
            "ReadOnlyReceiverIdentity",
            "NativeVerificationReceiverExport",
        )
    ]
    sources.append(root / "java/src/test/java/io/quadringent/as400/NativeVerificationReceiverExportTest.java")
    environment = {"PATH": str(Path(javac).parent), "LANG": "C", "TZ": "UTC"}
    subprocess.run(
        [javac, "--release", "21", "-cp", str(jar), "-d", str(classes), *map(str, sources)],
        check=True,
        capture_output=True,
        timeout=60,
        env=environment,
    )
    return java, str(classes) + os.pathsep + str(jar), environment


def test_actual_java_unit_contracts_and_java_to_python_original_interoperability(java_receiver_classes):
    java, classpath, environment = java_receiver_classes
    command = [java, "-cp", classpath, "io.quadringent.as400.NativeVerificationReceiverExportTest"]
    tests = subprocess.run(command, check=True, capture_output=True, timeout=30, env=environment)
    assert b"native_verification_receiver_export=PASS" in tests.stdout
    emitted = subprocess.run(
        [*command, "--emit-fixture", scope().sha256], check=True, capture_output=True, timeout=30, env=environment
    )
    observed = parse(emitted.stdout)
    assert observed.native_originals["identity_before"] == NATIVE_IDENTITY
    assert observed.native_originals["catalog"] == ("as400-receiver-catalog-v2\n" + ROW).encode()


def test_collected_document_cannot_hide_nonzero_exit_metadata(tmp_path, monkeypatch):
    original = receivers.collect_process_document

    def altered(*args, **kwargs):
        return replace(original(*args, **kwargs), return_code=7)

    monkeypatch.setattr(receivers, "collect_process_document", altered)
    with pytest.raises(ValueError):
        collect(tmp_path)


def test_original_symlink_substitution_is_safe_and_never_becomes_a_valid_observation(tmp_path, monkeypatch):
    original = receivers.collect_process_document
    outside = tmp_path / "outside.json"
    outside.write_bytes(document())

    def altered(*args, **kwargs):
        result = original(*args, **kwargs)
        result.path.unlink()
        result.path.symlink_to(outside)
        return result

    monkeypatch.setattr(receivers, "collect_process_document", altered)
    with pytest.raises(ValueError) as caught:
        collect(tmp_path)
    assert str(outside) not in str(caught.value)
    assert outside.read_bytes() == document()


def test_extra_catalogue_receiver_requires_a_real_qualified_object_name():
    extra = "receiver\t-\t-\tONLINE\t31\t40\t-\t-\t-\t-\n"
    with pytest.raises(ValueError):
        parse(document(row=ROW + extra))


@pytest.mark.parametrize("first,last", [("31", "1" + "0" * 21), ("1" + "0" * 21, "1" + "0" * 21)])
def test_every_catalogue_row_sequence_is_bounded_by_native_decimal21(first, last):
    extra = f"receiver\tOTHER\tR_A\tONLINE\t{first}\t{last}\t-\t-\t-\t-\n"
    with pytest.raises(ValueError):
        parse(document(row=ROW + extra))

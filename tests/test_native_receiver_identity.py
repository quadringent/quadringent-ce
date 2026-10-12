from unittest.mock import Mock
import pytest

from quadringent.java_catalog import parse_receiver_identity_output, WorkerReceiverCatalog
from quadringent.java_worker import PersistentJavaWorker

OUTPUT = "as400-receiver-identity-v1\nidentity\tJRNLIB\tJRN\tRCVLIB\tRCV\t1\t123\t456\t789\n"
EXPECTED = dict(journal_library="JRNLIB", journal_name="JRN", receiver_library="RCVLIB", receiver_name="RCV", asp_number=1)

def parse(output=OUTPUT, **changes):
    return parse_receiver_identity_output(output, **{**EXPECTED, **changes})

def test_identity_preserves_native_object_components_and_journal_scope():
    value = parse()
    assert value.native_generation_kind == "ibmi-ifs-object-v1"
    assert value.native_generation == "1:123:456:789"
    assert (value.journal_library, value.journal_name, value.receiver_library, value.receiver_name) == ("JRNLIB", "JRN", "RCVLIB", "RCV")

@pytest.mark.parametrize("output", [
    None, b"invalid", "x" * 257, "", OUTPUT.replace("-v1", "-v2"), OUTPUT + OUTPUT.splitlines()[1] + "\n",
    OUTPUT + "\n", OUTPUT.replace("\t789", "\t-"),
    OUTPUT.replace("\t789", "\t789.0"), OUTPUT.replace("\t789", "\t+789"),
    OUTPUT.replace("\t789", "\t0789"), OUTPUT.replace("\t789", "\t٧٨٩"),
    OUTPUT.replace("\t789", "\t9223372036854775808"),
    OUTPUT.replace("\t789", "\t-9223372036854775809"),
    OUTPUT.replace("\t789", "\t789\textra"),
    OUTPUT.replace("\tRCV\t", "\tOTHER\t"),
    OUTPUT.replace("\tRCVLIB\t", "\tOTHER\t"),
    OUTPUT.replace("\tJRN\t", "\tOTHER\t"),
    OUTPUT.replace("\tJRNLIB\t", "\tOTHER\t"),
    OUTPUT.replace("\t1\t123", "\t2\t123"),
])
def test_malformed_missing_ambiguous_or_different_identity_is_refused(output):
    with pytest.raises(ValueError):
        parse(output)

def test_native_signed_bigint_is_canonical_and_not_coerced_to_float():
    value = parse(OUTPUT.replace("\t123\t456\t789", "\t-9223372036854775808\t9223372036854775807\t0"))
    assert value.native_generation == "1:-9223372036854775808:9223372036854775807:0"

@pytest.mark.parametrize("change", [
    {"asp_number": True}, {"asp_number": "1"}, {"asp_number": 2},
    {"receiver_name": "R/OTHER"}, {"receiver_library": " LIB"}, {"receiver_library": "rcvlib"}, {"receiver_name": "rcv"},
    {"journal_name": "JRN\n"}, {"journal_library": "TOOLONGNAME1"},
])
def test_request_identity_validation_precedes_parser_and_worker_io(change):
    with pytest.raises(ValueError):
        parse(**change)

def worker():
    return PersistentJavaWorker(java="java", classpath="classpath", host="test.invalid", user="reader", schema="DATA", table="T", timeout_seconds=1)

def test_worker_command_has_explicit_library_name_and_asp():
    value = worker()
    value._ensure_worker = Mock(return_value=object())
    value._write = Mock()
    value._readline = Mock(side_effect=[*OUTPUT.splitlines(keepends=True), "identity_done\n"])
    assert value.receiver_identity(receiver_library="RCVLIB", receiver_name="RCV", asp_number=1) == OUTPUT
    value._write.assert_called_once_with(value._ensure_worker.return_value, {"cmd": "receiver_identity", "receiver_library": "RCVLIB", "receiver_name": "RCV", "asp_number": 1})
    assert value._readline.call_count == 3

@pytest.mark.parametrize("change", [{"asp_number": True}, {"asp_number": 2}, {"receiver_name": "R/OTHER"}, {"receiver_library": "TOOLONGNAME1"}, {"receiver_name": "rcv"}, {"receiver_library": "rcvlib"}])
def test_invalid_identity_command_never_spawns(change):
    value = worker()
    value._ensure_worker = Mock()
    with pytest.raises(ValueError):
        value.receiver_identity(**{**{k: v for k, v in EXPECTED.items() if k.startswith("receiver_") or k == "asp_number"}, **change})
    value._ensure_worker.assert_not_called()

@pytest.mark.parametrize("failure", [RuntimeError("safe failure"), TimeoutError("deadline")])
def test_failed_identity_request_kills_worker_and_propagates_bounded_failure(failure):
    value = worker()
    value._ensure_worker = Mock(return_value=object())
    value._write = Mock()
    value._readline = Mock(side_effect=failure)
    value._kill_worker = Mock()
    with pytest.raises(type(failure)):
        value.receiver_identity(receiver_library="RCVLIB", receiver_name="RCV", asp_number=1)
    value._kill_worker.assert_called_once()

def test_catalog_identity_has_no_cache_and_validates_exact_expected_scope():
    native = Mock()
    native.receiver_identity.return_value = OUTPUT
    catalog = WorkerReceiverCatalog(native)
    assert catalog.identity(**EXPECTED) == parse()
    assert catalog.identity(**EXPECTED) == parse()
    assert native.receiver_identity.call_count == 2
    with pytest.raises(RuntimeError, match="identity output invalid"):
        catalog.identity(**{**EXPECTED, "journal_name": "OTHER"})


def test_identity_reply_deadline_is_total_not_renewed_by_output():
    value = worker()
    value._ensure_worker = Mock(return_value=object())
    value._write = Mock()
    value._readline = Mock(side_effect=[*OUTPUT.splitlines(keepends=True), "identity_done\n"])
    value.receiver_identity(receiver_library="RCVLIB", receiver_name="RCV", asp_number=1)
    deadlines = [call.args[1] for call in value._readline.call_args_list]
    assert len(deadlines) == 3 and len(set(deadlines)) == 1

def test_identity_reply_rejects_extra_lines_before_completion():
    value = worker()
    value._ensure_worker = Mock(return_value=object())
    value._write = Mock()
    value._readline = Mock(side_effect=["unexpected\n", *OUTPUT.splitlines(keepends=True), "identity_done\n"])
    value._kill_worker = Mock()
    with pytest.raises(RuntimeError, match="identity.*overflow"):
        value.receiver_identity(receiver_library="RCVLIB", receiver_name="RCV", asp_number=1)
    value._kill_worker.assert_called_once()

def test_identity_error_never_relays_remote_diagnostics():
    value = worker()
    value._ensure_worker = Mock(return_value=object())
    value._write = Mock()
    value._readline = Mock(return_value="identity_error=CANARY_PRIVATE_DIAGNOSTIC endpoint\n")
    value._kill_worker = Mock()
    with pytest.raises(RuntimeError) as caught:
        value.receiver_identity(receiver_library="RCVLIB", receiver_name="RCV", asp_number=1)
    assert str(caught.value) == "IBM i receiver identity lookup failed"
    value._kill_worker.assert_called_once()

"""Session identity must bracket the same connections that export table rows."""
from dataclasses import replace
from base64 import b64encode
import json
from pathlib import Path

import pytest

from quadringent.qualification.verification_identity import DestinationIdentityExpectation
from quadringent_control_plane.v2.services.native_collection import NativeCollectionError
from quadringent_control_plane.v2.services.native_replay import StoredCollectionReplay, NativeReplayError
from quadringent.qualification.verification_receivers import ReceiverObservationConfig
from test_native_verification_identity import Cursor
from test_v2_native_collection import (  # noqa: F401
    ROOT, artifact_bytes, case, collected_case, collector, authority, preparer,
)


def identity_setup(value, *, wrong_after=False):
    scope = value.prepared.scope
    expected = DestinationIdentityExpectation("LOCATOR", "ORG", "ACCOUNT", "READER", "READ_WH",
                                              scope.destination_database, scope.destination_schema, "SERVICE")
    original = value.config["connection_factory"]

    def factory():
        connection = original()
        session = 40 + len(value.connections)
        row = ("LOCATOR", "ORG", "ACCOUNT", "READER", "READ_WH", scope.destination_database,
               scope.destination_schema, session, "SERVICE")
        after = (*row[:3], "WRONG", *row[4:]) if wrong_after else row
        connection.cursors = [Cursor(row=row), *connection.cursors, Cursor(row=after)]
        return connection
    return expected, factory


def test_identity_uses_the_actual_history_and_mirror_connections_and_archives_four_originals(collected_case):
    value = collected_case
    expected, factory = identity_setup(value)
    result = collector(value, connection_factory=factory, destination_identity=expected).collect(
        value.prepared, context=value.case.context, directory=value.directory, check_current=lambda: True)
    doc = json.loads(artifact_bytes(value.case, result.manifest))
    assert set(doc["authority_observations"]["destination"]) == {"history", "mirror"}
    for name, connection in zip(("history", "mirror"), value.connections, strict=True):
        assert connection.closed and len(connection.opened) == 4
        observation = doc["authority_observations"]["destination"][name]
        assert observation["stream_sha256"] == result.report["datasets"][name]["sha256"]
        for phase in ("before", "after"):
            record = json.loads(artifact_bytes(value.case, replace(result.manifest, **observation[phase])))
            assert record["origin"] == "dbapi_typed_result_not_wire_bytes"
            assert record["native_admission"] == "not_evaluated"
    assert result.native_cdc_qualified is False


def test_identity_drift_during_export_refuses_collection_and_closes_the_same_session(collected_case):
    value = collected_case
    expected, factory = identity_setup(value, wrong_after=True)
    with pytest.raises(NativeCollectionError, match="^native_collection_failed$"):
        collector(value, connection_factory=factory, destination_identity=expected).collect(
            value.prepared, context=value.case.context, directory=value.directory, check_current=lambda: True)
    assert len(value.connections) == 1 and value.connections[0].closed
    assert not value.directory.exists()


def test_archived_identity_is_replayed_against_server_expectation(collected_case, tmp_path):
    value = collected_case
    expected, factory = identity_setup(value)
    result = collector(value, connection_factory=factory, destination_identity=expected).collect(
        value.prepared, context=value.case.context, directory=value.directory, check_current=lambda: True)
    verifier = StoredCollectionReplay(value.case.storage, ROOT, "org-one", destination_identity=expected)
    replayed = verifier.replay(value.prepared, result.manifest, context=value.case.context, directory=tmp_path / "replay")
    assert replayed.validated_observations == ("destination_identity",)
    assert "destination_identity" not in replayed.missing_authorities
    assert replayed.state == "incomplete" and "stable_cut" in replayed.missing_authorities
    wrong = StoredCollectionReplay(value.case.storage, ROOT, "org-one", destination_identity=replace(expected, role="WRONG"))
    with pytest.raises(NativeReplayError):
        wrong.replay(value.prepared, result.manifest, context=value.case.context, directory=tmp_path / "wrong")


def receiver_setup(value, *, replacement=False):
    scope = value.prepared.scope
    identity = scope.checkpoint.receiver_identity
    native_identity = (f"as400-receiver-identity-v1\nidentity\t{identity.journal_library}\t{identity.journal_name}"
                       f"\t{identity.receiver_library}\t{identity.receiver_name}\t1\t123\t456\t789\n").encode()
    catalog = (f"as400-receiver-catalog-v2\nreceiver\t{identity.receiver_library}\t{identity.receiver_name}"
               "\tATTACHED\t1\t100\t-\t-\t-\t-\n").encode()
    doc = dict(format_version="quadringent-native-receiver-observation-v1", scope_sha256=scope.sha256,
               phase="before", identity_before_base64=b64encode(native_identity).decode(),
               catalog_base64=b64encode(catalog).decode(), identity_after_base64=b64encode(native_identity).decode())
    executable = Path(value.config["java"])
    branch = ("if sys.argv[3].endswith('NativeVerificationReceiverExport'):\n"
              "    assert sys.stdin.buffer.read().endswith(b'SYNTHETIC_STDIN_PASSWORD\\n')\n"
              f"    doc = {doc!r}\n"
              "    doc['phase'] = sys.argv[-1]\n")
    if replacement:
        changed = b64encode(native_identity.replace(b"789", b"790")).decode()
        branch += f"    if doc['phase'] == 'after': doc['identity_after_base64'] = {changed!r}\n"
    branch += "    sys.stdout.write(json.dumps(doc)+'\\n')\n    sys.exit(0)\n"
    executable.write_text(executable.read_text().replace("from pathlib import Path\n", "from pathlib import Path\n" + branch, 1))
    return ReceiverObservationConfig(str(executable), value.config["classpath"], scope.producer_revision)


@pytest.mark.parametrize("case", ["native"], indirect=True)
def test_native_receiver_originals_bracket_the_real_collection_and_replay(collected_case, tmp_path):
    value = collected_case
    config = receiver_setup(value)
    result = collector(value, receiver_config=config).collect(value.prepared, context=value.case.context,
        directory=value.directory, check_current=lambda: True)
    doc = json.loads(artifact_bytes(value.case, result.manifest))
    assert set(doc["authority_observations"]["receiver"]) == {"before", "after"}
    replayed = StoredCollectionReplay(value.case.storage, ROOT, "org-one", receiver_config=config).replay(
        value.prepared, result.manifest, context=value.case.context, directory=tmp_path / "replay")
    assert replayed.validated_observations == ("receiver_sampled_ranges",)
    assert replayed.state == "incomplete" and "stable_cut" in replayed.missing_authorities


@pytest.mark.parametrize("case", ["native"], indirect=True)
def test_native_generation_replacement_during_collection_is_refused(collected_case):
    value = collected_case
    config = receiver_setup(value, replacement=True)
    with pytest.raises(NativeCollectionError):
        collector(value, receiver_config=config).collect(value.prepared, context=value.case.context,
            directory=value.directory, check_current=lambda: True)
    assert not value.directory.exists()

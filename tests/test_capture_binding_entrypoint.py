from copy import deepcopy
from dataclasses import replace
import json
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import as400_continuous_capture as capture
from quadringent.checkpoint import JsonCheckpointStore
from quadringent.contract import JournalPosition
from quadringent.object_store import FileObjectStore
from quadringent.receiver_identity import ReceiverIdentity
from quadringent.receiver_registry import ReceiverNameRegistry


@pytest.fixture
def installed(monkeypatch, tmp_path):
    scope = "reader-source-raw-v1-offline"
    authority_store = FileObjectStore(tmp_path / "authority")
    registry = ReceiverNameRegistry(authority_store, authority="s3://registry-bucket/receiver-authority-v1",
                                    source_system="ibmi", journal="JRN")
    identity = ReceiverIdentity("JLIB", "JRN", "RLIB", "RCV1", "native-test-v1", "generation-a")
    binding = registry.create_or_load_binding(scope_id=scope, identity=identity, sequence=20)
    environment = {
        "QUADRINGENT_STORAGE_BACKEND": "aws", "AS400_RAW_BUCKET": "registry-bucket",
        "AS400_RAW_PREFIX": "raw/run", "AS400_STREAM_KEY": scope,
        "AS400_CHECKPOINT_TABLE": "offline-checkpoints", "AS400_JOURNAL_LIBRARY": "JLIB",
        "AS400_JOURNAL_NAME": "JRN", "AS400_JAVA_CLASSPATH": "offline.jar",
        "ISERIES_HOST": "192.0.2.10", "ISERIES_USER": "OFFLINE",
        "ISERIES_SCHEMA": "TESTLIB", "ISERIES_TABLE": "TESTTABLE",
        "AS400_CAPTURE_BINDING_JSON": json.dumps(binding.to_dict()),
    }
    for key in tuple(os.environ):
        monkeypatch.delenv(key)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("sys.argv", ["capture"])
    checkpoint = JsonCheckpointStore(tmp_path / "checkpoint.json")
    checkpoint_factory = Mock(return_value=checkpoint)
    raw_store = Mock()
    raw_store.list_receipt_keys.return_value = ()
    raw_store.get_bounded.side_effect = FileNotFoundError("window-chain.json")
    object_factory = Mock(side_effect=lambda backend, prefix: authority_store
                          if prefix == "receiver-authority-v1" else raw_store)
    worker = Mock(side_effect=RuntimeError("SOURCE_CONSTRUCTOR_REACHED"))
    monkeypatch.setattr(capture, "_checkpoint_store", checkpoint_factory)
    monkeypatch.setattr(capture, "_object_store", object_factory)
    monkeypatch.setattr(capture, "_source_gate", Mock())
    monkeypatch.setattr(capture, "PersistentJavaWorker", worker)
    return SimpleNamespace(binding=binding, registry=registry, authority_store=authority_store,
                           checkpoint=checkpoint, checkpoint_factory=checkpoint_factory,
                           raw_store=raw_store, object_factory=object_factory, worker=worker)


def test_original_admission_and_empty_bound_checkpoint_reach_the_source_constructor(installed):
    with pytest.raises(RuntimeError, match="SOURCE_CONSTRUCTOR_REACHED"):
        capture.main()
    installed.worker.assert_called_once()
    installed.registry.require(installed.binding, installed.binding.bootstrap)
    assert installed.checkpoint.load_bound() is None
    installed.raw_store.put_once.assert_not_called()


@pytest.mark.parametrize("invalid", ["missing", "unknown-version", "legacy-record", "foreign-scope",
                                    "foreign-authority", "foreign-domain", "foreign-library",
                                    "foreign-journal", "tail", "unknown-field", "oversized"])
def test_invalid_installed_admission_is_rejected_before_storage_or_jvm(installed, monkeypatch, invalid):
    record = deepcopy(installed.binding.to_dict())
    if invalid == "missing":
        monkeypatch.delenv("AS400_CAPTURE_BINDING_JSON")
    elif invalid == "foreign-library":
        monkeypatch.setenv("AS400_JOURNAL_LIBRARY", "OTHER")
    elif invalid == "foreign-journal":
        monkeypatch.setenv("AS400_JOURNAL_NAME", "OTHER")
    elif invalid == "tail":
        monkeypatch.setenv("AS400_BOOTSTRAP_RECEIVER", "__TAIL__")
    elif invalid == "oversized":
        monkeypatch.setenv("AS400_CAPTURE_BINDING_JSON", "x" * 65537)
    else:
        if invalid == "unknown-version":
            record["format_version"] = "future-version"
        elif invalid == "legacy-record":
            record = {"format_version": "as400-checkpoint-v1", "receiver": "RCV1", "sequence": 20}
        elif invalid == "foreign-scope":
            record["scope_id"] = record["bootstrap"]["scope_id"] = "another-reader"
        elif invalid == "foreign-authority":
            record["registry_authority"] = "s3://foreign-bucket/receiver-authority-v1"
        elif invalid == "foreign-domain":
            record["source_system"] = "another-source"
        else:
            record["unexpected"] = "cannot-drop"
        monkeypatch.setenv("AS400_CAPTURE_BINDING_JSON", json.dumps(record))
    with pytest.raises(ValueError):
        capture.main()
    installed.worker.assert_not_called()
    installed.object_factory.assert_not_called()
    installed.checkpoint_factory.assert_not_called()


def test_duplicate_admission_fields_are_rejected_before_storage_or_jvm(installed, monkeypatch):
    raw = json.dumps(installed.binding.to_dict())
    monkeypatch.setenv("AS400_CAPTURE_BINDING_JSON", raw[:-1] + ',"source_system":"ibmi"}')
    with pytest.raises(ValueError, match="duplicate"):
        capture.main()
    installed.worker.assert_not_called()
    installed.object_factory.assert_not_called()


@pytest.mark.parametrize("missing", ["binding", "original-claim"])
def test_missing_durable_admission_is_not_recreated_before_source(installed, missing):
    key = (installed.registry.binding_key(installed.binding.scope_id) if missing == "binding"
           else installed.registry.claim_key(installed.binding.bootstrap.position.receiver))
    path = installed.authority_store.root / key
    path.unlink()
    with pytest.raises(ValueError):
        capture.main()
    installed.worker.assert_not_called()
    assert not path.exists()
    assert installed.checkpoint.load_bound() is None


def test_invalid_durable_admission_does_not_reserve_a_run(installed, monkeypatch):
    path = installed.authority_store.root / installed.registry.binding_key(installed.binding.scope_id)
    path.unlink()
    reservation = Mock()
    monkeypatch.setattr(capture, "_reserve_run", reservation)
    monkeypatch.setattr("sys.argv", ["capture", "--reserve-run-id", "offline-run"])
    with pytest.raises(ValueError, match="durable original"):
        capture.main()
    reservation.assert_not_called()
    installed.worker.assert_not_called()


def test_durable_original_admission_cannot_be_replaced_by_another_epoch(installed):
    binding = installed.binding
    replacement = replace(binding, capture_epoch="another", bootstrap=replace(binding.bootstrap, capture_epoch="another"))
    path = installed.authority_store.root / installed.registry.binding_key(binding.scope_id)
    path.write_text(json.dumps(replacement.to_dict()))
    before = path.read_bytes()
    with pytest.raises(ValueError, match="durable original"):
        capture.main()
    installed.worker.assert_not_called()
    assert path.read_bytes() == before


def test_legacy_checkpoint_is_refused_without_adoption_or_source_io(installed):
    installed.checkpoint.compare_and_set(None, JournalPosition("RCV1", 20))
    before = installed.checkpoint.path.read_bytes()
    with pytest.raises(ValueError, match="legacy|unbound"):
        capture.main()
    installed.worker.assert_not_called()
    assert installed.checkpoint.path.read_bytes() == before


@pytest.mark.parametrize("change", ["scope", "epoch", "journal-library", "generation", "receiver-library", "unclaimed-name"])
def test_bound_checkpoint_must_match_original_scope_and_durable_name_owner(installed, change):
    point = installed.binding.bootstrap
    if change == "scope":
        point = replace(point, scope_id="another")
    elif change == "epoch":
        point = replace(point, capture_epoch="another")
    else:
        field = {"journal-library": "journal_library", "generation": "native_generation",
                 "receiver-library": "receiver_library", "unclaimed-name": "receiver_name"}[change]
        point = replace(point, receiver_identity=replace(point.receiver_identity, **{field: "OTHER"}))
    installed.checkpoint.compare_and_set_bound(None, point)
    before = installed.checkpoint.path.read_bytes()
    with pytest.raises(ValueError):
        capture.main()
    installed.worker.assert_not_called()
    assert installed.checkpoint.path.read_bytes() == before


def test_a_valid_bound_checkpoint_and_claim_are_verified_before_source_io(installed):
    point = replace(installed.binding.bootstrap, sequence=30)
    installed.registry.claim(installed.binding, point)
    installed.checkpoint.compare_and_set_bound(None, point)
    before = installed.checkpoint.path.read_bytes()
    with pytest.raises(RuntimeError, match="SOURCE_CONSTRUCTOR_REACHED"):
        capture.main()
    installed.worker.assert_called_once()
    assert installed.checkpoint.path.read_bytes() == before


def test_a_missing_claim_for_the_durable_current_checkpoint_is_not_repaired(installed):
    identity = replace(installed.binding.bootstrap.receiver_identity, receiver_name="RCV2", native_generation="generation-b")
    point = installed.binding.checkpoint(identity, 1)
    installed.registry.claim(installed.binding, point)
    installed.checkpoint.compare_and_set_bound(None, point)
    path = installed.authority_store.root / installed.registry.claim_key("RCV2")
    path.unlink()
    with pytest.raises(ValueError, match="absent"):
        capture.main()
    installed.worker.assert_not_called()
    assert not path.exists()

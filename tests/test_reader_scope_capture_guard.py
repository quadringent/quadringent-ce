"""Existing RAW without a bound checkpoint stops before the JVM constructor."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest
from quadringent.capture_binding import CaptureBinding
from quadringent.receiver_identity import BoundCheckpoint, ReceiverIdentity


@pytest.mark.parametrize("legacy_scope_flag", ["source-raw-v1", ""])
def test_existing_raw_without_bound_checkpoint_stops_before_source(monkeypatch, legacy_scope_flag):
    path = Path(__file__).resolve().parents[1] / "scripts/as400_continuous_capture.py"
    spec = importlib.util.spec_from_file_location("capture_scope_guard_regression", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    checkpoint = Mock()
    checkpoint.load.return_value = None
    checkpoint.load_bound.return_value = None
    store = Mock()
    store.list_receipt_keys.return_value = ("receipts/authentic.json",)
    environment = {
        "AS400_STREAM_KEY": "reader-source-raw-v1-offline",
        "AS400_CHECKPOINT_SCOPE": legacy_scope_flag,
        "AS400_RAW_BUCKET": "registry-bucket",
        "AS400_JOURNAL_LIBRARY": "JLIB",
        "AS400_JOURNAL_NAME": "JRN",
        "AS400_JAVA_CLASSPATH": "offline.jar",
        "ISERIES_HOST": "192.0.2.10",
        "ISERIES_USER": "OFFLINE",
        "ISERIES_SCHEMA": "TESTLIB",
        "ISERIES_TABLE": "TESTTABLE",
    }
    identity = ReceiverIdentity("JLIB", "JRN", "RLIB", "RCV1", "native-test-v1", "generation-a")
    point = BoundCheckpoint(environment["AS400_STREAM_KEY"], "fixture-epoch", identity, 20)
    binding = CaptureBinding(point.scope_id, point.capture_epoch, "ibmi", "JRN",
                             "s3://registry-bucket/receiver-authority-v1", point)
    environment["AS400_CAPTURE_BINDING_JSON"] = json.dumps(binding.to_dict())
    for key in tuple(module.os.environ):
        monkeypatch.delenv(key)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(sys, "argv", [str(path)])
    monkeypatch.setattr(module, "backend_from_environment", lambda _: "aws")
    monkeypatch.setattr(module, "_checkpoint_store", lambda *_: checkpoint)
    monkeypatch.setattr(module, "_object_store", lambda *_: store)
    monkeypatch.setattr(module, "_source_gate", Mock())
    registry = Mock(spec=module.ReceiverNameRegistry)
    registry.load_binding.return_value = binding
    monkeypatch.setattr(module, "ReceiverNameRegistry", Mock(return_value=registry))
    # The obsolete scoped guard cannot bypass the unconditional bound guard.
    monkeypatch.setattr("quadringent.reader_scope.require_scoped_checkpoint_bootstrap", lambda *_: None)
    monkeypatch.setattr("quadringent.proof_windows.prepare_window_chain", Mock())
    source = Mock(side_effect=RuntimeError("SOURCE_CONSTRUCTOR_REACHED"))
    monkeypatch.setattr(module, "PersistentJavaWorker", source)
    catalog = Mock()
    monkeypatch.setattr(module, "WorkerReceiverCatalog", catalog)
    with pytest.raises(ValueError, match="explicit migration"):
        module.main()
    source.assert_not_called()
    catalog.assert_not_called()
    checkpoint.compare_and_set.assert_not_called()
    checkpoint.compare_and_set_bound.assert_not_called()
    checkpoint.commit.assert_not_called()
    store.put_once.assert_not_called()

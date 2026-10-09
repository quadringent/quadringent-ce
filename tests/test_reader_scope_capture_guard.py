"""La migration manquante bloque le vrai entrypoint avant construction JVM."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize("guard_enabled", [True, False])
def test_existing_raw_without_scoped_checkpoint_stops_before_source(monkeypatch, guard_enabled):
    path = Path(__file__).resolve().parents[1] / "scripts/as400_continuous_capture.py"
    spec = importlib.util.spec_from_file_location("capture_scope_guard_regression", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    checkpoint = Mock()
    checkpoint.load.return_value = None
    store = Mock()
    store.list_receipt_keys.return_value = ("receipts/authentic.json",)
    environment = {
        "AS400_STREAM_KEY": "reader-source-raw-v1-offline",
        "AS400_CHECKPOINT_SCOPE": "source-raw-v1",
        "AS400_JAVA_CLASSPATH": "offline.jar",
        "ISERIES_HOST": "192.0.2.10",
        "ISERIES_USER": "OFFLINE",
        "ISERIES_SCHEMA": "TESTLIB",
        "ISERIES_TABLE": "TESTTABLE",
    }
    for key in tuple(module.os.environ):
        monkeypatch.delenv(key)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(sys, "argv", [str(path)])
    monkeypatch.setattr(module, "backend_from_environment", lambda _: "aws")
    monkeypatch.setattr(module, "_checkpoint_store", lambda *_: checkpoint)
    monkeypatch.setattr(module, "_object_store", lambda *_: store)
    monkeypatch.setattr(module, "_source_gate", Mock())
    # Les étapes ultérieures sont rendues hors réseau pour le témoin causal.
    monkeypatch.setattr("quadringent.proof_windows.prepare_window_chain", Mock())
    source = Mock(side_effect=RuntimeError("SOURCE_CONSTRUCTOR_REACHED"))
    monkeypatch.setattr(module, "PersistentJavaWorker", source)
    catalog = Mock()
    monkeypatch.setattr(module, "WorkerReceiverCatalog", catalog)
    if guard_enabled:
        with pytest.raises(ValueError, match="explicit migration"):
            module.main()
        source.assert_not_called()
    else:
        monkeypatch.setattr("quadringent.reader_scope.require_scoped_checkpoint_bootstrap", lambda *_: None)
        with pytest.raises(RuntimeError, match="SOURCE_CONSTRUCTOR_REACHED"):
            module.main()
        source.assert_called_once()
    catalog.assert_not_called()
    checkpoint.compare_and_set.assert_not_called()
    checkpoint.commit.assert_not_called()
    store.put_once.assert_not_called()

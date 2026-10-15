"""Shared helpers for the local storage backend tests."""
import json
import os
from pathlib import Path

from quadringent.receiver_identity import BoundCheckpoint, ReceiverIdentity

LABEL = "qdt-0a1b2c3d"
IDENTITY = ReceiverIdentity("TESTLIB", "TESTJRN", "TESTLIB", "RCV0001", "native-test-v1", "generation-a")


def init_data_dir(root, label=LABEL, *, marker=True):
    """Create a data directory as the host launcher does and return the storage environment."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    if marker:
        (root / ".quadringent-local").write_text(json.dumps({"format_version": 1, "label": label}))
    return {
        "QUADRINGENT_STORAGE_BACKEND": "local",
        "AS400_RAW_BUCKET": label,
        "AS400_LOCAL_DATA_DIR": str(root),
    }


def bound_point(sequence, *, scope="scope-1", epoch="epoch-1", identity=IDENTITY):
    return BoundCheckpoint(scope, epoch, identity, sequence)


def child_environment(root_of_repo, storage_env):
    environment = dict(os.environ)
    environment.update(storage_env)
    environment["PYTHONPATH"] = str(Path(root_of_repo) / "src")
    return environment

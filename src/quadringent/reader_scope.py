"""Identité du curseur lecteur, isolée de toute autre publication RAW."""
from __future__ import annotations

import hashlib
import json

from .object_store import CheckpointStore, ObjectStore
from .storage_layout import _clean_root


READER_CHECKPOINT_SCOPE = "source-raw-v1"


def reader_checkpoint_stream_key(*, storage_backend: str, source_id: str, journal_library: str,
                                 journal_name: str, raw_bucket: str, raw_prefix: str) -> str:
    backend = {"aws": "s3", "s3": "s3", "gcs": "gcs", "local": "local"}.get(storage_backend.strip().lower())
    if backend is None:
        raise ValueError("unknown reader checkpoint backend")
    root = _clean_root(raw_prefix)
    values = (backend, source_id.strip(), journal_library.strip().upper(),
              journal_name.strip().upper(), raw_bucket.strip(), root)
    if any(not value or any(ord(char) < 32 for char in value) for value in values):
        raise ValueError("incomplete reader source/journal/RAW identity")
    identity = json.dumps(values, ensure_ascii=True, separators=(",", ":")).encode()
    return "reader-source-raw-v1-" + hashlib.sha256(identity).hexdigest()


def require_scoped_checkpoint_bootstrap(checkpoint: CheckpointStore, object_store: ObjectStore) -> None:
    """Un RAW déjà publié exige une migration de curseur explicite.

    Aucun curseur historique partagé n'est copié implicitement. Le format
    des positions/checkpoints reste celui du backend public existant.
    """
    if checkpoint.load() is None and object_store.list_receipt_keys(1000):
        raise ValueError("isolated reader checkpoint absent with existing RAW: explicit migration required")

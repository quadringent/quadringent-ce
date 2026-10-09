from unittest.mock import Mock

import pytest

from quadringent.contract import JournalPosition
from quadringent.reader_scope import require_scoped_checkpoint_bootstrap


def test_existing_raw_requires_explicit_migration_without_checkpoint_writes():
    checkpoint = Mock()
    checkpoint.load.return_value = None
    store = Mock()
    store.list_receipt_keys.return_value = ('receipts/authentic.json',)
    with pytest.raises(ValueError, match="explicit migration"):
        require_scoped_checkpoint_bootstrap(checkpoint, store)
    checkpoint.compare_and_set.assert_not_called()
    checkpoint.commit.assert_not_called()
    store.put_once.assert_not_called()


def test_new_empty_scope_can_start_without_synthesizing_checkpoint():
    checkpoint = Mock()
    checkpoint.load.return_value = None
    store = Mock()
    store.list_receipt_keys.return_value = ()
    require_scoped_checkpoint_bootstrap(checkpoint, store)
    checkpoint.commit.assert_not_called()


def test_explicitly_migrated_checkpoint_does_not_scan_or_modify_raw():
    checkpoint = Mock()
    checkpoint.load.return_value = JournalPosition('RCV0003', 988)
    store = Mock()
    require_scoped_checkpoint_bootstrap(checkpoint, store)
    store.list_receipt_keys.assert_not_called()
    checkpoint.compare_and_set.assert_not_called()


def test_backend_is_explicit_normalized_and_unknown_is_rejected():
    from quadringent.reader_scope import reader_checkpoint_stream_key
    values = dict(source_id="source", journal_library="LIB", journal_name="JRN",
                  raw_bucket="bucket", raw_prefix="root")
    assert reader_checkpoint_stream_key(storage_backend="aws", **values) == reader_checkpoint_stream_key(storage_backend="s3", **values)
    assert reader_checkpoint_stream_key(storage_backend="s3", **values) != reader_checkpoint_stream_key(storage_backend="gcs", **values)
    with pytest.raises(ValueError, match="backend"):
        reader_checkpoint_stream_key(storage_backend="unknown", **values)

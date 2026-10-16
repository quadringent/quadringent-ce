"""Archive replay uses bounded verified bytes, independent of the old workspace."""
import hashlib
from io import BytesIO

import pytest

from test_object_store_file_archive import backend, publish  # noqa: F401


class BoundedOutput(BytesIO):
    def write(self, data):
        assert 0 < len(data) <= 1024**2
        return super().write(data)


def test_archive_can_materialize_after_all_live_source_files_are_deleted(backend, tmp_path):
    _, store, _ = backend
    raw = b"original-row\n" * 210_000
    source = tmp_path / "source"
    source.write_bytes(raw)
    publish(store, source, raw)
    source.unlink()
    output = BoundedOutput()
    store.verify_file("original", expected_sha256=hashlib.sha256(raw).hexdigest(),
                      byte_count=len(raw), output=output)
    assert output.getvalue() == raw


@pytest.mark.parametrize("fault", ["short", "error", "stale"])
def test_replay_refuses_sink_failure_or_supersession(backend, tmp_path, fault):
    _, store, _ = backend
    raw = b"row\n" * 300_000
    source = tmp_path / "source"
    source.write_bytes(raw)
    publish(store, source, raw)
    current = True

    class Output(BytesIO):
        def write(self, data):
            nonlocal current
            if fault == "error":
                raise OSError("synthetic private destination")
            if fault == "short":
                return len(data) - 1
            current = False
            return super().write(data)

    with pytest.raises((ValueError, OSError)):
        store.verify_file("original", expected_sha256=hashlib.sha256(raw).hexdigest(),
                          byte_count=len(raw), output=Output(), check_current=lambda: current)

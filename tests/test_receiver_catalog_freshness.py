from dataclasses import replace

import pytest

from quadringent.continuous import ReceiverSnapshot
from quadringent.java_catalog import CachedReceiverCatalog


ATTACHED = ReceiverSnapshot(
    "QGPL", "R2", 11, 20, status="ATTACHED",
    previous_receiver_library="QGPL", previous_receiver="R1",
    chain_links_observed=True,
)
HISTORICAL = ReceiverSnapshot("QGPL", "R1", 1, 10, status="ONLINE")


class MutableCatalog:
    def __init__(self, rows=(ATTACHED,)):
        self.rows = rows
        self.required = []
        self.error = None

    def snapshot(self, required_receiver=None):
        self.required.append(required_receiver)
        if self.error is not None:
            raise self.error
        return self.rows


def cached(inner, probe=None, **options):
    return CachedReceiverCatalog(
        inner, ttl_polls=30, ttl_seconds=60, clock=lambda: 0.0,
        tail_probe=probe, **options,
    )


@pytest.mark.parametrize("status", ["PARTIAL", "FREED"])
def test_attached_tail_does_not_attest_a_historical_required_receiver(status):
    inner = MutableCatalog((HISTORICAL, ATTACHED))
    catalog = cached(inner, lambda: ATTACHED)
    catalog.snapshot("R1")
    inner.rows = (replace(HISTORICAL, status=status), ATTACHED)

    rows = catalog.snapshot("R1")

    assert rows[0].status == status
    assert inner.required == ["R1", "R1"]


def test_required_receiver_without_tail_probe_always_refetches():
    inner = MutableCatalog()
    catalog = cached(inner)
    catalog.snapshot("R2")
    inner.rows = (replace(ATTACHED, status="FREED"),)

    assert catalog.snapshot("R2")[0].status == "FREED"
    assert inner.required == ["R2", "R2"]


def test_attached_tail_does_not_hide_a_purged_historical_receiver():
    inner = MutableCatalog((HISTORICAL, ATTACHED))
    catalog = cached(inner, lambda: ATTACHED)
    catalog.snapshot("R1")
    inner.rows = (ATTACHED,)

    assert catalog.snapshot("R1") == (ATTACHED,)
    assert inner.required == ["R1", "R1"]


@pytest.mark.parametrize("last", [20, 30])
def test_required_attached_receiver_uses_compatible_fresh_tail_and_preserves_links(last):
    inner = MutableCatalog()
    catalog = cached(inner, lambda: replace(ATTACHED, last_sequence=last,
        previous_receiver_library=None, previous_receiver=None, chain_links_observed=False))
    catalog.snapshot("R2")

    rows = catalog.snapshot("R2")

    assert inner.required == ["R2"]
    assert rows[0].last_sequence == last
    assert rows[0].first_sequence == 11
    assert rows[0].previous_receiver == "R1"
    assert rows[0].previous_receiver_library == "QGPL"
    assert rows[0].chain_links_observed


@pytest.mark.parametrize("probe", [
    None,
    replace(ATTACHED, receiver="R3"),
    replace(ATTACHED, receiver_library="OTHER"),
    replace(ATTACHED, status="ONLINE"),
    replace(ATTACHED, status="PARTIAL"),
    replace(ATTACHED, status="FREED"),
    replace(ATTACHED, status="EMPTY"),
    replace(ATTACHED, status="UNKNOWN"),
    replace(ATTACHED, status=None),
    replace(ATTACHED, first_sequence=1),
    replace(ATTACHED, first_sequence=12),
    replace(ATTACHED, first_sequence=1, last_sequence=30),
    replace(ATTACHED, first_sequence=None),
    replace(ATTACHED, last_sequence=19),
    replace(ATTACHED, last_sequence=None),
])
def test_required_receiver_refetches_before_serving_an_incompatible_tail(probe):
    inner = MutableCatalog()
    catalog = cached(inner, lambda: probe)
    catalog.snapshot("R2")
    inner.rows = (replace(ATTACHED, status="PARTIAL"),)

    assert catalog.snapshot("R2")[0].status == "PARTIAL"
    assert inner.required == ["R2", "R2"]


def test_required_receiver_refetches_after_probe_exception():
    def failing_probe():
        raise RuntimeError("tail unavailable")

    inner = MutableCatalog()
    catalog = cached(inner, failing_probe)
    catalog.snapshot("R2")
    inner.rows = (replace(ATTACHED, status="FREED"),)

    assert catalog.snapshot("R2")[0].status == "FREED"
    assert inner.required == ["R2", "R2"]


def test_tail_cannot_attest_missing_cached_bounds():
    initial = replace(ATTACHED, first_sequence=None, last_sequence=None)
    inner = MutableCatalog((initial,))
    catalog = cached(inner, lambda: initial)
    catalog.snapshot("R2")
    inner.rows = (ATTACHED,)

    assert catalog.snapshot("R2")[0].first_sequence == 11
    assert inner.required == ["R2", "R2"]


@pytest.mark.parametrize("reason", ["historical", "no-probe", "no-attached", "probe-error", "incompatible", "expired"])
def test_required_receiver_never_falls_back_to_stale_metadata_after_failed_refetch(reason):
    initial = (HISTORICAL, ATTACHED) if reason == "historical" else (ATTACHED,)

    def probe():
        if reason == "probe-error":
            raise RuntimeError("tail unavailable")
        if reason == "no-attached":
            return None
        if reason == "incompatible":
            return replace(ATTACHED, last_sequence=19)
        return ATTACHED

    inner = MutableCatalog(initial)
    catalog = cached(inner, None if reason == "no-probe" else probe, max_stale_reuse=5)
    required = "R1" if reason == "historical" else "R2"
    catalog.snapshot(required)
    inner.error = RuntimeError("catalog unavailable")
    if reason == "expired":
        catalog.clock = lambda: 60.0

    with pytest.raises(RuntimeError, match="catalog unavailable"):
        catalog.snapshot(required)

    assert inner.required == [required, required]
    assert catalog.stale_count == 0


def test_required_receiver_rechecks_expiration_after_tail_probe():
    now = [0.0]

    def slow_probe():
        now[0] = 61.0
        return ATTACHED

    inner = MutableCatalog()
    catalog = CachedReceiverCatalog(inner, ttl_polls=30, ttl_seconds=60,
        clock=lambda: now[0], tail_probe=slow_probe)
    catalog.snapshot("R2")
    inner.rows = (replace(ATTACHED, status="PARTIAL"),)

    assert catalog.snapshot("R2")[0].status == "PARTIAL"
    assert inner.required == ["R2", "R2"]


def test_unanchored_diagnostic_catalog_keeps_bounded_stale_fallback():
    inner = MutableCatalog()
    catalog = cached(inner)
    first = catalog.snapshot()
    catalog.clock = lambda: 60.0
    inner.error = RuntimeError("catalog unavailable")

    assert catalog.snapshot() == first
    assert catalog.stale_count == 1

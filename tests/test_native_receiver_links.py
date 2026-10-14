"""Offline qualification checks for source-observed receiver links."""

import hashlib

import pytest

from quadringent.java_catalog import parse_catalog_output
from quadringent.qualification import collect, proof
from test_native_qualification_rotation import fixture as rotation_fixture
from test_native_qualification_rotation_collect import fixture as windows_fixture


LINK_CHANGES = (
    "missing_previous",
    "missing_next",
    "wrong_previous_library",
    "wrong_previous_receiver",
    "wrong_next_library",
    "wrong_next_receiver",
    "omitted_intermediate",
)


def _write_catalog(catalog, rows, *, version="v2"):
    catalog["output"] = (
        f"as400-receiver-catalog-{version}\n"
        + "".join("\t".join(row) + "\n" for row in rows)
    )
    catalog["output_sha256"] = hashlib.sha256(catalog["output"].encode()).hexdigest()


def _catalog_rows(catalog):
    return [line.split("\t") for line in catalog["output"].splitlines()[1:]]


def _upgrade_catalog(catalog):
    rows = [row[:6] for row in _catalog_rows(catalog)]
    linked = []
    for index, row in enumerate(rows):
        previous = rows[index - 1][1:3] if index else ["-", "-"]
        following = rows[index + 1][1:3] if index + 1 < len(rows) else ["-", "-"]
        linked.append(row + previous + following)
    _write_catalog(catalog, linked)


def _alter_links(catalog, change):
    rows = _catalog_rows(catalog)
    if change == "missing_previous":
        rows[1][6:8] = ["-", "-"]
    elif change == "missing_next":
        rows[0][8:10] = ["-", "-"]
    elif change == "wrong_previous_library":
        rows[1][6] = "OTHER"
    elif change == "wrong_previous_receiver":
        rows[1][7] = "OTHER"
    elif change == "wrong_next_library":
        rows[0][8] = "OTHER"
    elif change == "wrong_next_receiver":
        rows[0][9] = "OTHER"
    else:
        # Keep CONT bounds while both retained receivers reference the omitted one.
        rows[0][8:10] = ["S", "MISSING"]
        rows[1][6:8] = ["S", "MISSING"]
    _write_catalog(catalog, rows)


def _linked_windows_fixture():
    args, boundary, checkpoint, catalog = windows_fixture()
    _upgrade_catalog(catalog)
    return args, boundary, checkpoint, catalog


def _linked_rotation_fixture():
    before, after, operations = rotation_fixture()
    for name in ("catalog_before", "catalog_after"):
        _upgrade_catalog(operations[name])
    return before, after, operations


def _assert_windows(args, boundary, checkpoint, catalog):
    assert collect.receiver_windows(boundary, checkpoint, catalog, args) == [
        {"receiver": "R1", "receiver_library": "S", "start": 11, "end": 12},
        {"receiver": "R2", "receiver_library": "S", "start": 13, "end": 15},
    ]


def test_receiver_windows_accepts_reciprocal_v2_cont_links():
    args, boundary, checkpoint, catalog = _linked_windows_fixture()
    _assert_windows(args, boundary, checkpoint, catalog)


@pytest.mark.parametrize("change", LINK_CHANGES)
def test_receiver_windows_rejects_unproven_v2_links_despite_cont_bounds(change):
    args, boundary, checkpoint, catalog = _linked_windows_fixture()
    _assert_windows(args, boundary, checkpoint, catalog)
    _alter_links(catalog, change)
    with pytest.raises(ValueError):
        collect.receiver_windows(boundary, checkpoint, catalog, args)


def test_receiver_windows_excludes_reset_even_with_reciprocal_v2_links():
    args, boundary, checkpoint, catalog = _linked_windows_fixture()
    _assert_windows(args, boundary, checkpoint, catalog)
    rows = _catalog_rows(catalog)
    rows[1][4] = "1"
    _write_catalog(catalog, rows)
    with pytest.raises(ValueError):
        collect.receiver_windows(boundary, checkpoint, catalog, args)


def test_evaluate_rotation_accepts_reciprocal_v2_cont_links():
    before, after, operations = _linked_rotation_fixture()
    result = proof.evaluate_rotation(before, after, operations, historical=True)
    assert result["rotation_qualified"] is True
    assert result["native_cdc_qualified"] is False
    assert result["maximum_10s_qualified"] is False


@pytest.mark.parametrize('bound', (False, True))
def test_current_rotation_refuses_links_without_independent_native_generations(bound):
    from quadringent.receiver_identity import ReceiverIdentity
    from test_native_qualification_bound_boundary import fixture

    before, after, operations = _linked_rotation_fixture()
    if bound:
        _, _, binding = fixture()
        for snapshot, generation in ((before, 'generation-a'), (after, 'generation-b')):
            checkpoint = snapshot['checkpoint']
            identity = ReceiverIdentity('S', 'J', 'S', checkpoint['receiver'], 'synthetic-v1', generation)
            checkpoint.update(bound_checkpoint=binding.checkpoint(identity, checkpoint['sequence']).to_dict(),
                              capture_binding=binding.to_dict())
    reason = 'independently observed native generations' if bound else 'checkpoint identity record format'
    with pytest.raises(ValueError, match=reason):
        proof.evaluate_rotation(before, after, operations)


@pytest.mark.parametrize("change", LINK_CHANGES)
def test_evaluate_rotation_rejects_unproven_v2_links_despite_cont_bounds(change):
    before, after, operations = _linked_rotation_fixture()
    assert proof.evaluate_rotation(before, after, operations, historical=True)["rotation_qualified"] is True
    _alter_links(operations["catalog_after"], change)
    with pytest.raises(ValueError):
        proof.evaluate_rotation(before, after, operations, historical=True)


@pytest.mark.parametrize("change", ("reset_ack", "reset_bounds"))
def test_evaluate_rotation_excludes_reset_even_with_reciprocal_v2_links(change):
    before, after, operations = _linked_rotation_fixture()
    assert proof.evaluate_rotation(before, after, operations, historical=True)["rotation_qualified"] is True
    if change == "reset_ack":
        operations["rotation"]["sequence_option"] = "RESET"
    else:
        rows = _catalog_rows(operations["catalog_after"])
        rows[1][4] = "1"
        _write_catalog(operations["catalog_after"], rows)
    with pytest.raises(ValueError):
        proof.evaluate_rotation(before, after, operations, historical=True)


@pytest.mark.parametrize("fixture_name", ("windows", "rotation"))
def test_historical_v1_catalogs_remain_readable(fixture_name):
    if fixture_name == "windows":
        catalogs = [windows_fixture()[-1]]
    else:
        operations = rotation_fixture()[-1]
        catalogs = [operations[name] for name in ("catalog_before", "catalog_after")]
    for catalog in catalogs:
        original = _catalog_rows(catalog)
        _write_catalog(catalog, [row[:6] for row in original], version="v1")
        parsed = parse_catalog_output(catalog["output"])
        assert [(row.receiver_library, row.receiver) for row in parsed] == [
            (row[1], row[2]) for row in original
        ]
        assert all(not getattr(row, "chain_links_observed", False) for row in parsed)


def test_receiver_windows_refuses_v1_for_multi_receiver_qualification():
    args, boundary, checkpoint, catalog = _linked_windows_fixture()
    _assert_windows(args, boundary, checkpoint, catalog)
    _write_catalog(catalog, [row[:6] for row in _catalog_rows(catalog)], version="v1")
    with pytest.raises(ValueError):
        collect.receiver_windows(boundary, checkpoint, catalog, args)


@pytest.mark.parametrize(
    ("before_version", "after_version"),
    (("v1", "v1"), ("v1", "v2"), ("v2", "v1")),
)
def test_evaluate_rotation_refuses_v1_catalogs(before_version, after_version):
    before, after, operations = _linked_rotation_fixture()
    assert proof.evaluate_rotation(before, after, operations, historical=True)["rotation_qualified"] is True
    for name, version in (
        ("catalog_before", before_version),
        ("catalog_after", after_version),
    ):
        if version == "v1":
            catalog = operations[name]
            _write_catalog(catalog, [row[:6] for row in _catalog_rows(catalog)], version=version)
    with pytest.raises(ValueError):
        proof.evaluate_rotation(before, after, operations, historical=True)

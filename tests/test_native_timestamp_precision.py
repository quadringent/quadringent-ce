"""Équivalence exacte des timestamps JDBC et Snowflake, sans arrondi."""
import pytest

from quadringent.qualification.proof import history_proof, normalize, normalize_history


@pytest.mark.parametrize('left,right', [
    ('2026-01-01 08:00:37.037010', '2026-01-01T08:00:37.03701'),
    ('2026-01-01 08:00:37.000000', '2026-01-01 08:00:37'),
    ('2026-01-01 08:00:37.12345678900', '2026-01-01 08:00:37.123456789'),
    ('2026-01-01 08:00:37.037010+00:00', '2026-01-01 08:00:37.03701+00:00'),
])
def test_history_accepts_only_equivalent_fractional_precision(left, right):
    event = {'EVENT_ID': 'copy-1', 'OPERATION': 'c', 'JOURNAL_RECEIVER': 'SNAPSHOT:run1',
             'JOURNAL_SEQUENCE': 1, 'UPDATED_AT': left}
    assert history_proof(normalize_history([event], {'UPDATED_AT': 'timestamp'}),
                         normalize_history([{**event, 'UPDATED_AT': right}],
                                           {'UPDATED_AT': 'timestamp'}))['equal']


@pytest.mark.parametrize('changed', [
    '2026-01-01 08:00:37.123456788',
    '2026-01-01 08:00:37.1234567891',
    '2026-01-01 08:00:38.123456789',
    '2026-01-01 08:00:37.123456789+01:00',
])
def test_history_rejects_changed_instant_without_truncating_nanoseconds(changed):
    event = {'EVENT_ID': 'copy-1', 'OPERATION': 'c', 'JOURNAL_RECEIVER': 'SNAPSHOT:run1',
             'JOURNAL_SEQUENCE': 1, 'UPDATED_AT': '2026-01-01 08:00:37.123456789'}
    with pytest.raises(ValueError):
        history_proof(normalize_history([event], {'UPDATED_AT': 'timestamp'}),
                      normalize_history([{**event, 'UPDATED_AT': changed}],
                                        {'UPDATED_AT': 'timestamp'}))


def test_text_precision_and_null_remain_distinct():
    assert normalize(None, 'timestamp') is None
    assert normalize('2026-01-01 08:00:37.037010 ', 'text') == '2026-01-01 08:00:37.037010 '

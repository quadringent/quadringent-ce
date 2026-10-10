from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from quadringent_control_plane.v2 import db
from quadringent_control_plane.v2.app import create_v2_app
from quadringent_control_plane.v2.crypto import SecretBox
from quadringent_control_plane.v2.services.evidence import absent_evidence
from quadringent_control_plane.v2.services.loader_telemetry import KubernetesLoaderTelemetry, LoaderIdentity
from test_v2_pipeline_metrics import _seed_pipeline

NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)


class Pods:
    def __init__(self, lines):
        self.lines = lines

    def list_pod_names(self, **kwargs):
        return ('loader',)

    def read_pod_log(self, *args, **kwargs):
        return self.lines


def telemetry(lines):
    return KubernetesLoaderTelemetry(Pods(lines), resolve=lambda _: LoaderIdentity('dst1', 'ORDERS'), now=lambda: NOW)


@pytest.fixture
def client(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'evidence.sqlite3'}"
    db.run_migrations(dsn)
    engine = db.create_engine_for(dsn)
    _seed_pipeline(engine)
    app = create_v2_app(engine=engine, secret_box=SecretBox(SecretBox.generate_key()))
    try:
        yield TestClient(app)
    finally:
        engine.dispose()


def test_live_declared_state_does_not_invent_delivery(client):
    response = client.get('/v2/pipelines/ppl1/evidence')
    assert response.status_code == 200
    body = response.json()
    assert body['pipeline_id'] == 'ppl1'
    assert body['delivery']['state'] == 'absent'
    assert body['delivery']['event_count'] is None
    assert body['reconciliation']['duplicate_count'] is None


def test_unknown_pipeline_is_404(client):
    assert client.get('/v2/pipelines/missing/evidence').status_code == 404


def test_provider_without_evidence_capability_is_explicitly_absent(client):
    client.app.state.pipeline_observation_provider = object()
    assert client.get('/v2/pipelines/ppl1/evidence').json()['provenance'] == 'absent'


def test_loader_delivery_is_bounded_to_this_table_and_never_certifies_reconciliation():
    provider = telemetry('2026-10-10T11:59:00Z delivery table=OTHER new_events=500 mirror_seconds=1\n'
                         '2026-10-10T11:59:30Z delivery table=ORDERS new_events=3 mirror_seconds=6.2')
    result = provider.evidence('ppl1').to_dict()
    assert result['delivery']['event_count'] == 3
    assert result['delivery']['lag_seconds'] == 6.2
    assert result['delivery']['state'] == 'observed'
    assert result['reading']['state'] == 'absent'
    assert result['reconciliation']['state'] == 'absent'
    assert result['reconciliation']['duplicate_count'] is None


def test_stale_delivery_keeps_its_timestamp_without_becoming_current():
    result = telemetry('2026-10-10T10:00:00Z delivery table=ORDERS new_events=3 mirror_seconds=6.2').evidence('ppl1')
    assert result.delivery.state == 'stale'
    assert result.delivery.observed_at == '2026-10-10T10:00:00Z'


def test_future_delivery_and_raw_unstructured_messages_are_not_evidence():
    result = telemetry('2026-10-11T12:00:00Z delivery table=ORDERS new_events=3 mirror_seconds=6.2\n'
                       '2026-10-10T11:59:00Z password=do-not-expose row=private-value').evidence('ppl1').to_dict()
    assert result['delivery']['state'] == 'absent'
    assert 'do-not-expose' not in str(result) and 'private-value' not in str(result)


def test_absence_has_null_counts_rather_than_fabricated_zero():
    result = absent_evidence('ppl1', reason='no provider').to_dict()
    assert all(result[stage]['event_count'] is None for stage in ('reading', 'delivery', 'reconciliation'))


"""HTTP dispatch through real collection; native policy remains an offline fixture."""
from sqlalchemy import update

from quadringent.qualification.verification_collect import collect_verification
from quadringent_control_plane.v2 import schema
from quadringent_control_plane.v2.services.native_context import context_snapshot
from quadringent_control_plane.v2.services.native_verifications import NativeAdmission, NativeArtifact, NativePreparation
from test_native_verification_collect import inputs, published  # noqa: F401
from test_v2_native_verification_routes import native_api, post  # noqa: F401
from test_v2_native_verifications import artifact, authority  # noqa: F401


def test_route_dispatches_all_real_collectors_and_persists_exact_result(native_api, inputs, tmp_path):
    client, engine, _, _, _ = native_api
    values, connections, _ = inputs
    scope = values['scope']
    with engine.begin() as connection:
        connection.execute(update(schema.pipelines).where(schema.pipelines.c.id == scope.pipeline_id)
                           .values(active_run_id=scope.copy_run_id))
    with engine.connect() as connection:
        baseline = context_snapshot(connection, 'org-one', scope.pipeline_id)[3]

    def current():
        with engine.connect() as connection:
            return context_snapshot(connection, 'org-one', scope.pipeline_id)[3] == baseline

    class Preparation:
        def prepare(self, context):
            assert context['copy_run_id'] == scope.copy_run_id
            return NativePreparation(scope, tuple(artifact(name) for name in (
                'copy_intent', 'copy_evidence', 'registry', 'binding', 'checkpoint')))

    class OfflineAdmission:
        calls = 0
        def admit(self, prepared):
            self.calls += 1
            assert prepared.scope == scope
            result = collect_verification(tmp_path / 'http-collection', **{**values, 'check_current': current})
            streams = tuple(NativeArtifact('stream:' + name, 'local://offline-fixture/' + name,
                            ref.summary.sha256, 'offline-version', ref.summary.byte_count)
                            for name, ref in result.streams.items())
            proofs = tuple(artifact(name) for name in ('platform', 'source', 'raw_inventory', 'native_format',
                                                       'receiver_chain', 'destination', 'stable_cut'))
            return NativeAdmission(result.report, streams + proofs, 'offline-integration-fixture-policy')

    admission = OfflineAdmission()
    client.app.state.native_verification_preparer_factory = lambda _: Preparation()
    client.app.state.native_verification_controller_factory = lambda _: admission
    response = post(client)
    assert response.status_code == 201, response.text
    result = response.json()['after']
    assert result['outcome'] == 'verified' and admission.calls == 1
    assert result['result_manifest']['policy_revision'] == 'offline-integration-fixture-policy'
    assert result['result_manifest']['comparison']['transaction_consistency_verified'] is False
    assert len(connections) == 2 and all(connection.closed for connection in connections)
    assert post(client).json() == response.json() and admission.calls == 1
    latest = client.get('/v2/pipelines/pipeline-one/native-verifications/latest').json()
    assert latest['result_sha256'] == result['result_sha256']

"""Real archived-byte replay reaches SQL and HTTP without claiming admission."""

from fastapi.testclient import TestClient

from quadringent_control_plane.v2.app import create_v2_app
from quadringent_control_plane.v2.crypto import SecretBox
from quadringent_control_plane.v2.services.native_observer import collect_and_replay
from quadringent_control_plane.v2.services.native_replay import StoredCollectionReplay
from test_native_source_observation import offline_java  # noqa: F401
from test_v2_native_collection import (  # noqa: F401
    ROOT, authority, case, collected_case, collector, preparer,
)
from test_v2_native_verification_routes import BASE, recorded_result


def test_archived_native_bytes_are_replayed_before_persisted_observation_is_exposed(
    collected_case, offline_java, authority, tmp_path,
):
    value = collected_case
    directory = tmp_path / "actual-observation"
    directory.mkdir()

    class Observer:
        calls = 0

        def observe(self, prepared, *, context, check_current):
            self.calls += 1
            return collect_and_replay(
                collector(value), StoredCollectionReplay(value.case.storage, ROOT, "org-one", **offline_java),
                prepared, context=context, directory=directory, check_current=check_current,
            )

    observer = Observer()
    app = create_v2_app(
        engine=authority[0], secret_box=SecretBox(SecretBox.generate_key()), org_id="org-one",
        require_authentication=True, token_pepper=b"synthetic-replay-api-pepper",
        native_verification_preparer_factory=lambda org: preparer(value.case) if org == "org-one" else None,
        native_verification_observer_factory=lambda org: observer if org == "org-one" else None,
    )
    _, token = app.state.agent_tokens_service.create(
        name="Replay fixture", scope="operate", never_expires=True, created_by="fixture",
    )
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + token
        assert client.get(BASE + "/capabilities").json() == {"available": True, "mode": "observation", "reason_code": None}
        response = client.post(BASE, json={"dry_run": False}, headers={"Idempotency-Key": "real-replay"})
        assert response.status_code == 201, response.text
        result = response.json()["after"]
        assert result["outcome"] == "incomplete" and result["reason_code"] == "native_authority_unavailable"
        manifest = result["result_manifest"]
        assert manifest["comparison"]["state"] == "verified" and manifest["streams_replayed"] == 7
        assert manifest["native_admission"] == "not_evaluated" and manifest["native_cdc_qualified"] is False
        assert manifest["validated_observations"] == ["native_source_observations"]
        assert set(manifest["missing_authorities"]) == {
            "platform", "source_provenance", "receiver_chain", "physical_inventory", "stable_cut", "destination_identity",
        }
        assert observer.calls == 1
        app.state.native_verification_observer_factory = None
        for path in (BASE + "/latest", response.json()["verify"]["path"]):
            assert recorded_result(client.get(path).json()) == recorded_result(result)
        assert observer.calls == 1

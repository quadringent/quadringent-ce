"""Every v2 route reads and changes only its own organization's records."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from quadringent_control_plane.v2 import db as v2_db, schema as v2_schema
from quadringent_control_plane.v2.app import create_v2_app
from quadringent_control_plane.v2.crypto import SecretBox


class RecordingExecutor:
    def __init__(self):
        self.events = []

    def execute(self, *, pipeline_id, event):
        self.events.append((pipeline_id, event))


@pytest.fixture()
def setup(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'scoping.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    with engine.begin() as c:
        for org, p in (("default", "a"), ("other", "b")):
            c.execute(v2_schema.organizations.insert(), {"id": org, "name": org})
            c.execute(v2_schema.sources.insert(), {"id": f"{p}src", "org_id": org, "display_name": "s",
                                                    "ibmi_host": "h.invalid", "ibmi_user": "U", "secret_ciphertext": "x"})
            c.execute(v2_schema.destinations.insert(), {"id": f"{p}dst", "org_id": org, "snowflake_account": "acct",
                                                         "key_pair_ciphertext": "x", "setup_script": "--"})
            c.execute(v2_schema.tables.insert(), {"id": f"{p}tbl", "source_id": f"{p}src", "schema_name": "L",
                                                   "table_name": "T", "readiness": "ready"})
            c.execute(v2_schema.tables.insert(), {"id": f"{p}tbl2", "source_id": f"{p}src", "schema_name": "L",
                                                   "table_name": "T2", "readiness": "ready"})
            c.execute(v2_schema.pipelines.insert(), {"id": f"{p}ppl", "table_id": f"{p}tbl", "destination_id": f"{p}dst",
                                                      "declared_state": "live"})
    executor = RecordingExecutor()
    client = TestClient(create_v2_app(engine=engine, secret_box=SecretBox(SecretBox.generate_key()),
                                      org_id="default", pipeline_executor=executor))
    yield client, executor
    engine.dispose()


def test_lists_only_show_the_organization(setup):
    client, _ = setup
    assert {i["id"] for i in client.get("/v2/pipelines").json()["items"]} == {"appl"}
    assert {i["id"] for i in client.get("/v2/sources").json()["items"]} == {"asrc"}


@pytest.mark.parametrize("path", ["/v2/pipelines/bppl", "/v2/pipelines/bppl/evidence", "/v2/sources/bsrc",
                                  "/v2/sources/bsrc/tables"])
def test_foreign_records_are_not_found(setup, path):
    client, _ = setup
    assert client.get(path).status_code == 404


def test_actions_and_starts_cannot_reach_another_organization(setup):
    client, executor = setup
    key = {"Idempotency-Key": "scoping-test-1"}
    assert client.post("/v2/pipelines/bppl/actions/pause", json={}, headers=key).status_code == 404
    assert client.post("/v2/tables/btbl2/pipeline", json={}, headers={"Idempotency-Key": "scoping-test-2"}).status_code == 404
    assert client.patch("/v2/tables/btbl", json={"key_strategy": "rrn", "acknowledge_rrn": True},
                        headers={"Idempotency-Key": "scoping-test-3"}).status_code == 404
    assert client.put("/v2/tables/btbl/discovered-columns",
                      json={"columns": [{"name": "ID", "type": "INTEGER"}]},
                      headers={"Idempotency-Key": "scoping-test-4"}).status_code == 404
    assert executor.events == []


def test_starting_without_a_destination_ignores_other_organizations(setup):
    client, executor = setup
    response = client.post("/v2/tables/atbl2/pipeline", json={}, headers={"Idempotency-Key": "scoping-test-5"})
    assert response.status_code in (200, 201), response.text
    assert executor.events and executor.events[0][1] == "start"

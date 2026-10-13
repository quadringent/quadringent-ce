"""Entrée du control plane v2 : configuration, migrations, construction d'app.

Ne teste jamais ``uvicorn.run`` lui-même (ouvrirait un vrai socket) — se
limite à ``build_app`` (tout ce qui précède l'appel bloquant) et à la
résolution de configuration, qui doivent rester des refus explicites sans
aucun défaut de production inventé.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from quadringent_control_plane.v2 import db, entrypoint, schema
from quadringent_control_plane.v2.crypto import SecretBox


def _environ(tmp_path: Path, **overrides: str) -> dict[str, str]:
    dsn = f"sqlite:///{tmp_path / 'entrypoint.sqlite3'}"
    base = {
        entrypoint.ENV_DATABASE_URL: dsn,
        "AWS_REGION": "eu-central-1",
        "QUADRINGENT_V2_SECRET_KEY": SecretBox.generate_key().decode("ascii"),
        "QUADRINGENT_V2_TOKEN_PEPPER": "pepper-de-test-entrypoint",
    }
    base.update(overrides)
    return base


def test_build_app_requires_a_database_url(tmp_path: Path) -> None:
    environ = _environ(tmp_path)
    del environ[entrypoint.ENV_DATABASE_URL]
    with pytest.raises(entrypoint.ConfigurationError, match=entrypoint.ENV_DATABASE_URL):
        entrypoint.build_app(environ)


def test_build_app_requires_a_secret_key(tmp_path: Path) -> None:
    from quadringent_control_plane.v2.crypto import SecretKeyUnavailableError

    environ = _environ(tmp_path)
    del environ["QUADRINGENT_V2_SECRET_KEY"]
    with pytest.raises(SecretKeyUnavailableError):
        entrypoint.build_app(environ)


def test_build_app_requires_a_token_pepper(tmp_path: Path) -> None:
    from quadringent_control_plane.v2.crypto import TokenPepperUnavailableError

    environ = _environ(tmp_path)
    del environ["QUADRINGENT_V2_TOKEN_PEPPER"]
    with pytest.raises(TokenPepperUnavailableError):
        entrypoint.build_app(environ)


def test_build_app_applies_migrations_and_serves_openapi(tmp_path: Path) -> None:
    environ = _environ(tmp_path)
    app, engine = entrypoint.build_app(environ)
    try:
        # Un seul cycle de vie, comme sous uvicorn : le gestionnaire de sessions
        # MCP monté dans l'app ne peut démarrer qu'une fois par instance.
        with TestClient(app) as client:
            response = client.get("/v2/openapi.json")
            health = client.get("/v2/healthz")
        assert response.status_code == 200
        assert health.status_code == 200
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({"QUADRINGENT_V2_ORG_ID": "acme"}, "acme"),
        ({"QUADRINGENT_SITE_ID": "acme-site"}, "acme-site"),
        ({"QUADRINGENT_V2_ORG_ID": "acme", "QUADRINGENT_SITE_ID": "acme-site"}, "acme"),
        ({}, "default"),
    ],
)
def test_resolve_org_id_precedence(overrides: dict[str, str], expected: str) -> None:
    assert entrypoint.resolve_org_id(overrides) == expected


def test_build_app_defaults_org_id_from_site_id(tmp_path: Path) -> None:
    environ = _environ(tmp_path, QUADRINGENT_SITE_ID="acme-site")
    app, engine = entrypoint.build_app(environ)
    try:
        assert app.state.org_id == "acme-site"
    finally:
        engine.dispose()


def test_build_app_requires_authentication_by_default(tmp_path: Path) -> None:
    """Tâche « auth-login » : en production (``build_app``), l'anonyme n'est
    plus admin implicite — prouvé de bout en bout, jusqu'à la connexion."""

    environ = _environ(tmp_path, QUADRINGENT_SITE_ID="qauth")
    app, engine = entrypoint.build_app(environ)
    try:
        # ``build_app`` pose ``session_cookie_secure=True`` (voir sa
        # docstring et docs/product/install-default.md) : le cookie de
        # session n'est renvoyé que sur une origine https — ``base_url``
        # doit donc l'être ici, comme un vrai navigateur derrière TLS.
        with TestClient(app, base_url="https://testserver") as client:
            anonymous = client.get("/v2/users")
            assert anonymous.status_code == 401

            created = client.post(
                "/v2/setup/first-admin",
                json={"email": "admin@example.com"},
                headers={"Idempotency-Key": "install-qauth"},
            )
            assert created.status_code == 201
            body = created.json()["after"]

            activated = client.post(
                "/v2/users/activate",
                json={"token": body["activation_token"], "password": "un-mot-de-passe-robuste"},
                headers={"Idempotency-Key": "activate-qauth"},
            )
            assert activated.status_code == 200

            login = client.post(
                "/v2/auth/login",
                json={"email": "admin@example.com", "password": "un-mot-de-passe-robuste"},
            )
            assert login.status_code == 200

            authenticated = client.get("/v2/users")
            assert authenticated.status_code == 200

            me = client.get("/v2/auth/me")
            assert me.status_code == 200
            assert me.json()["email"] == "admin@example.com"
    finally:
        engine.dispose()


def test_fresh_database_accepts_the_first_admin(tmp_path: Path) -> None:
    """Constaté sur GKE : base neuve, aucune ligne ``organizations`` pour
    l'org de l'installation → violation de clé étrangère (HTTP 500) sur
    ``/v2/setup/first-admin``. Le démarrage garantit la ligne, et redémarrer
    ne la duplique pas."""
    environ = _environ(tmp_path, QUADRINGENT_SITE_ID="qqual")
    for attempt in range(2):
        app, engine = entrypoint.build_app(environ)
        try:
            with TestClient(app) as client:
                response = client.post(
                    "/v2/setup/first-admin",
                    json={"email": "admin@example.com"},
                    headers={"Idempotency-Key": "install-qqual"},
                )
            assert response.status_code == 201, (attempt, response.text)
            # SQLite n'applique pas les clés étrangères : on vérifie la ligne.
            with engine.connect() as connection:
                ids = [row[0] for row in connection.exec_driver_sql("SELECT id FROM organizations")]
            assert ids == ["qqual"]
        finally:
            engine.dispose()


def test_build_app_does_not_wire_diagnostic_adapters_outside_a_cluster(tmp_path: Path, monkeypatch) -> None:
    """Hors cluster (pas de ``KUBERNETES_SERVICE_HOST``) : jamais d'adaptateur
    réel branché, même avec ``QUADRINGENT_V2_CAPTURE_IMAGE`` déclarée — voir
    ``entrypoint.py::build_diagnostic_adapters``."""

    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    environ = _environ(
        tmp_path,
        QUADRINGENT_SITE_ID="qprobe",
        QUADRINGENT_V2_CAPTURE_IMAGE="registry.example.test/quadringent/capture@sha256:" + "0" * 64,
    )
    app, engine = entrypoint.build_app(environ)
    try:
        assert app.state.source_probe is None
        assert app.state.table_discovery_client is None
    finally:
        engine.dispose()


def test_build_app_does_not_wire_diagnostic_adapters_without_a_capture_image(tmp_path: Path, monkeypatch) -> None:
    """En cluster mais sans image de capture déclarée : refus explicite,
    jamais un défaut de production inventé."""

    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "192.0.2.10")
    environ = _environ(tmp_path, QUADRINGENT_SITE_ID="qprobe2")
    environ.pop("QUADRINGENT_V2_CAPTURE_IMAGE", None)
    app, engine = entrypoint.build_app(environ)
    try:
        assert app.state.source_probe is None
        assert app.state.table_discovery_client is None
    finally:
        engine.dispose()


def test_build_app_wires_diagnostic_adapters_in_cluster_with_a_capture_image(tmp_path: Path, monkeypatch) -> None:
    """En cluster, avec l'image déclarée et une identité de ServiceAccount
    montée : les deux adaptateurs réels (Jobs Kubernetes éphémères) sont
    branchés."""

    # Certificat auto-signé factice (jamais utilisé pour une vraie connexion
    # ici) — ``ssl.create_default_context`` exige un PEM syntaxiquement
    # valide, contrairement au fragment tronqué qui suffit ailleurs
    # (tests/test_k8s_jobs.py) où l'appel échoue avant la validation TLS.
    fake_ca_pem = (
        "-----BEGIN CERTIFICATE-----\n"
        "MIIDBTCCAe2gAwIBAgIUW+HIrjo+H4hfdCKnHj6DDXhhCN8wDQYJKoZIhvcNAQEL\n"
        "BQAwEjEQMA4GA1UEAwwHZmFrZS1jYTAeFw0yNjA5MjQxMDMwNDVaFw0yNjA5MjUx\n"
        "MDMwNDVaMBIxEDAOBgNVBAMMB2Zha2UtY2EwggEiMA0GCSqGSIb3DQEBAQUAA4IB\n"
        "DwAwggEKAoIBAQDW9Lp61e6FiOUjfLeyeEuW/pFwHTDHhAF48mlerp9O0x0MSEC3\n"
        "OcXLrtpVNyzOf24y3e3oKD9r7lRxvAMrULZAtezQY+ozS8OVsMojzetZiH4kWpDp\n"
        "R/71ZYepwxTJ3knsKz5qB4llCiOFgCZeA89mSMTrZgphNkxlWlNmI8SMiYo3AwBY\n"
        "95hI8HsLmYtRnH70iRYkbOjfYFVpcSJqNRTbYzm2mogBozrSC6o5zb8dSrx3H+V9\n"
        "hAyxAnUWko++7JD7mn9lMfuuXMWEJZwFfmJx5rLUHUPkasEMkbTOTfd6d+FYgVcN\n"
        "DotIH5Kv4wT5skMDmeStjHpcO+Z7I7b0z5KXAgMBAAGjUzBRMB0GA1UdDgQWBBRj\n"
        "ibNJM7wBt9psoMhlTytuejZpezAfBgNVHSMEGDAWgBRjibNJM7wBt9psoMhlTytu\n"
        "ejZpezAPBgNVHRMBAf8EBTADAQH/MA0GCSqGSIb3DQEBCwUAA4IBAQAmliI4Ah54\n"
        "5FLzUY4jWTxZL+6hPo+Ep1lJX90No0UxBp61//qmjRf/dncfzbypc5i0zTEC7VRX\n"
        "ysHHkV9GMWeIaLl1mlmDV8Bx2Ha7tWgsa9wMaarbBnqGoA2Xs/70fNQaACy/VG+b\n"
        "vcoso4kcq3iBBh1myjeD2j+/0qtqPf0tEa5yAFH1UPXx8XS937+wFLv6Mjr7+AAD\n"
        "kAEgYJtjIWQN3ppta+HKgHpnM/6o/BCSj1yS3CSQuwx6hqpC3g1sTgUahlyDDpjE\n"
        "KjpaGNbEY8J7KkMenyJTkFYLGWgN61pSUyhr+Lcy2Q9w1oAlXfn1RbMVWSmqszRc\n"
        "B8t6NharRpYd\n"
        "-----END CERTIFICATE-----\n"
    )
    sa_root = tmp_path / "serviceaccount"
    sa_root.mkdir()
    (sa_root / "token").write_text("header.payload.signature\n", encoding="utf-8")
    (sa_root / "namespace").write_text("quadringent-demo", encoding="utf-8")
    (sa_root / "ca.crt").write_text(fake_ca_pem, encoding="utf-8")

    from quadringent_control_plane import k8s_jobs

    monkeypatch.setattr(k8s_jobs, "SERVICE_ACCOUNT_ROOT", str(sa_root))
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "192.0.2.10")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT_HTTPS", "443")

    environ = _environ(
        tmp_path,
        QUADRINGENT_SITE_ID="qprobe3",
        QUADRINGENT_V2_CAPTURE_IMAGE="registry.example.test/quadringent/capture@sha256:" + "0" * 64,
    )
    app, engine = entrypoint.build_app(environ)
    try:
        assert app.state.source_probe is not None
        assert app.state.table_discovery_client is not None
        # Sans ServiceAccount à identité cloud ni bucket déclarés, l'exécuteur
        # de pipeline (chantier « pipeline-exec ») reste débranché — même
        # garde explicite que pour la sonde/découverte, jamais un défaut de
        # production inventé.
        assert app.state.pipeline_executor is None
    finally:
        engine.dispose()


_FAKE_CA_PEM = (
    "-----BEGIN CERTIFICATE-----\n"
    "MIIDBTCCAe2gAwIBAgIUW+HIrjo+H4hfdCKnHj6DDXhhCN8wDQYJKoZIhvcNAQEL\n"
    "BQAwEjEQMA4GA1UEAwwHZmFrZS1jYTAeFw0yNjA5MjQxMDMwNDVaFw0yNjA5MjUx\n"
    "MDMwNDVaMBIxEDAOBgNVBAMMB2Zha2UtY2EwggEiMA0GCSqGSIb3DQEBAQUAA4IB\n"
    "DwAwggEKAoIBAQDW9Lp61e6FiOUjfLeyeEuW/pFwHTDHhAF48mlerp9O0x0MSEC3\n"
    "OcXLrtpVNyzOf24y3e3oKD9r7lRxvAMrULZAtezQY+ozS8OVsMojzetZiH4kWpDp\n"
    "R/71ZYepwxTJ3knsKz5qB4llCiOFgCZeA89mSMTrZgphNkxlWlNmI8SMiYo3AwBY\n"
    "95hI8HsLmYtRnH70iRYkbOjfYFVpcSJqNRTbYzm2mogBozrSC6o5zb8dSrx3H+V9\n"
    "hAyxAnUWko++7JD7mn9lMfuuXMWEJZwFfmJx5rLUHUPkasEMkbTOTfd6d+FYgVcN\n"
    "DotIH5Kv4wT5skMDmeStjHpcO+Z7I7b0z5KXAgMBAAGjUzBRMB0GA1UdDgQWBBRj\n"
    "ibNJM7wBt9psoMhlTytuejZpezAfBgNVHSMEGDAWgBRjibNJM7wBt9psoMhlTytu\n"
    "ejZpezAPBgNVHRMBAf8EBTADAQH/MA0GCSqGSIb3DQEBCwUAA4IBAQAmliI4Ah54\n"
    "5FLzUY4jWTxZL+6hPo+Ep1lJX90No0UxBp61//qmjRf/dncfzbypc5i0zTEC7VRX\n"
    "ysHHkV9GMWeIaLl1mlmDV8Bx2Ha7tWgsa9wMaarbBnqGoA2Xs/70fNQaACy/VG+b\n"
    "vcoso4kcq3iBBh1myjeD2j+/0qtqPf0tEa5yAFH1UPXx8XS937+wFLv6Mjr7+AAD\n"
    "kAEgYJtjIWQN3ppta+HKgHpnM/6o/BCSj1yS3CSQuwx6hqpC3g1sTgUahlyDDpjE\n"
    "KjpaGNbEY8J7KkMenyJTkFYLGWgN61pSUyhr+Lcy2Q9w1oAlXfn1RbMVWSmqszRc\n"
    "B8t6NharRpYd\n"
    "-----END CERTIFICATE-----\n"
)


def _fake_service_account(tmp_path, monkeypatch, *, namespace: str = "quadringent-demo") -> None:
    sa_root = tmp_path / "serviceaccount"
    sa_root.mkdir()
    (sa_root / "token").write_text("header.payload.signature\n", encoding="utf-8")
    (sa_root / "namespace").write_text(namespace, encoding="utf-8")
    (sa_root / "ca.crt").write_text(_FAKE_CA_PEM, encoding="utf-8")

    from quadringent_control_plane import k8s_jobs

    monkeypatch.setattr(k8s_jobs, "SERVICE_ACCOUNT_ROOT", str(sa_root))
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "192.0.2.10")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT_HTTPS", "443")


def test_build_app_does_not_wire_the_pipeline_executor_without_a_service_account(tmp_path, monkeypatch) -> None:
    """En cluster, avec l'image déclarée mais sans ServiceAccount à identité
    cloud déclaré (``QUADRINGENT_V2_CAPTURE_SERVICE_ACCOUNT``) : la sonde et
    la découverte se branchent (inchangé), l'exécuteur de pipeline non."""

    _fake_service_account(tmp_path, monkeypatch)
    environ = _environ(
        tmp_path,
        QUADRINGENT_SITE_ID="qexec1",
        QUADRINGENT_V2_CAPTURE_IMAGE="registry.example.test/quadringent/capture@sha256:" + "0" * 64,
        QUADRINGENT_RAW_BUCKET="acme-raw",
        QUADRINGENT_RAW_PREFIX_ROOT="quadringent/sales",
        QUADRINGENT_STORAGE_BACKEND="gcs",
        QUADRINGENT_CHECKPOINT_BUCKET="acme-checkpoints",
    )
    app, engine = entrypoint.build_app(environ)
    try:
        assert app.state.source_probe is not None
        assert app.state.pipeline_executor is None
        assert app.state.reconciliation_loop is None
    finally:
        engine.dispose()


def test_build_app_wires_the_pipeline_executor_in_cluster_with_full_configuration(tmp_path, monkeypatch) -> None:
    """En cluster, avec l'image de capture, le ServiceAccount à identité
    cloud, le bucket/préfixe et le classpath Java déclarés : l'exécuteur de
    pipeline réel (``KubernetesPipelineExecutor``) est branché, et la boucle
    de réconciliation démarre dans le lifespan de l'app."""

    from fastapi.testclient import TestClient

    _fake_service_account(tmp_path, monkeypatch)
    monkeypatch.setenv("AS400_JAVA_CLASSPATH", "/app/probe.jar:/app/lib/*")
    environ = _environ(
        tmp_path,
        QUADRINGENT_SITE_ID="qexec2",
        QUADRINGENT_V2_CAPTURE_IMAGE="registry.example.test/quadringent/capture@sha256:" + "0" * 64,
        QUADRINGENT_V2_CAPTURE_SERVICE_ACCOUNT="quadringent-capture",
        QUADRINGENT_RAW_BUCKET="acme-raw",
        QUADRINGENT_RAW_PREFIX_ROOT="quadringent/sales",
        QUADRINGENT_STORAGE_BACKEND="aws",
        QUADRINGENT_CHECKPOINT_TABLE="acme-checkpoints",
    )
    app, engine = entrypoint.build_app(environ)
    try:
        assert app.state.pipeline_executor is not None
        with TestClient(app):
            assert app.state.reconciliation_loop is not None
    finally:
        engine.dispose()


@pytest.mark.parametrize("loader_overrides,expected_mode,expected_polls", [
    ({}, "sql", (1.0, 1.0)),
    ({"QUADRINGENT_V2_LOADER_HISTORY_MODE": "streaming",
      "QUADRINGENT_V2_READER_POLL_SECONDS": "5",
      "QUADRINGENT_V2_LOADER_POLL_SECONDS": "10"}, "streaming", (5.0, 10.0)),
])
def test_build_app_wires_the_destination_loader_image(tmp_path, monkeypatch, loader_overrides, expected_mode, expected_polls) -> None:
    """Le chargeur Snowflake (historique + miroir) vit dans l'image du control
    plane : la chart la passe en ``QUADRINGENT_V2_LOADER_IMAGE`` et
    l'exécuteur réconcilie alors le Deployment chargeur (sinon il reste
    silencieusement inactif et rien n'arrive dans Snowflake)."""

    _fake_service_account(tmp_path, monkeypatch)
    monkeypatch.setenv("AS400_JAVA_CLASSPATH", "/app/probe.jar:/app/lib/*")
    loader = "registry.example.test/quadringent/control-plane@sha256:" + "1" * 64
    environ = _environ(
        tmp_path,
        QUADRINGENT_SITE_ID="qexec3",
        QUADRINGENT_V2_CAPTURE_IMAGE="registry.example.test/quadringent/capture@sha256:" + "0" * 64,
        QUADRINGENT_V2_CAPTURE_SERVICE_ACCOUNT="quadringent-capture",
        QUADRINGENT_V2_LOADER_IMAGE=loader,
        **loader_overrides,
        QUADRINGENT_RAW_BUCKET="acme-raw",
        QUADRINGENT_RAW_PREFIX_ROOT="quadringent/sales",
        QUADRINGENT_STORAGE_BACKEND="aws",
        QUADRINGENT_CHECKPOINT_TABLE="acme-checkpoints",
    )
    app, engine = entrypoint.build_app(environ)
    try:
        config = app.state.pipeline_executor._config
        assert (config.loader_image, config.raw_bucket, config.checkpoint_location) == (
            loader, "acme-raw", "acme-checkpoints",
        )
        assert (config.reader_poll_seconds, config.loader_poll_seconds) == expected_polls
        assert config.loader_flush_each_batch is False
        assert config.loader_history_mode == expected_mode
    finally:
        engine.dispose()


def test_build_app_wires_loader_metrics_and_logs_in_cluster(tmp_path, monkeypatch) -> None:
    from quadringent_control_plane.v2.services.loader_telemetry import KubernetesLoaderTelemetry

    _fake_service_account(tmp_path, monkeypatch)
    app, engine = entrypoint.build_app(_environ(tmp_path))
    try:
        assert isinstance(app.state.pipeline_observation_provider, KubernetesLoaderTelemetry)
        assert app.state.log_source is app.state.pipeline_observation_provider
    finally:
        engine.dispose()


def test_missing_service_account_token_degrades_instead_of_crashing(tmp_path, monkeypatch) -> None:
    """Constaté sur GKE : jeton de ServiceAccount non monté → ``JobsApiError``
    au démarrage, conteneur v2 en CrashLoop. Sans jeton, les adaptateurs
    Kubernetes restent débranchés mais l'API démarre."""

    from quadringent_control_plane import k8s_jobs

    monkeypatch.setattr(k8s_jobs, "SERVICE_ACCOUNT_ROOT", str(tmp_path / "absent"))
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "192.0.2.10")
    monkeypatch.setenv("AS400_JAVA_CLASSPATH", "/app/probe.jar:/app/lib/*")
    environ = _environ(
        tmp_path,
        QUADRINGENT_V2_CAPTURE_IMAGE="registry.example.test/quadringent/capture@sha256:" + "0" * 64,
        QUADRINGENT_V2_CAPTURE_SERVICE_ACCOUNT="quadringent-capture",
        QUADRINGENT_RAW_BUCKET="acme-raw",
        QUADRINGENT_RAW_PREFIX_ROOT="quadringent/sales",
        QUADRINGENT_STORAGE_BACKEND="aws",
        QUADRINGENT_CHECKPOINT_TABLE="acme-checkpoints",
    )
    app, engine = entrypoint.build_app(environ)
    try:
        assert app.state.source_probe is None
        assert app.state.pipeline_executor is None
    finally:
        engine.dispose()


def test_aws_executor_rejects_missing_region_before_clients(monkeypatch):
    monkeypatch.setattr(entrypoint, "in_cluster", lambda: True)
    def forbidden():
        raise AssertionError("clients must not be created without region")
    monkeypatch.setattr(entrypoint, "_executor_clients_from_environment", forbidden)
    values = {entrypoint.ENV_CAPTURE_IMAGE: "registry.example.test/capture:1",
              entrypoint.ENV_CAPTURE_SERVICE_ACCOUNT: "capture",
              "QUADRINGENT_RAW_BUCKET": "raw", "QUADRINGENT_RAW_PREFIX_ROOT": "site",
              "QUADRINGENT_STORAGE_BACKEND": "aws", "AWS_REGION": " "}
    with pytest.raises(entrypoint.ConfigurationError, match="AWS_REGION"):
        entrypoint.build_pipeline_executor(values, None, None)


@pytest.mark.parametrize("backend,primary,fallback,expected", [
    ("aws", "eu-central-1", "us-west-2", "eu-central-1"),
    ("aws", " ", "us-west-2", "us-west-2"),
    ("gcs", "", "", ""),
])
def test_executor_resolves_only_the_configured_site_region(tmp_path, monkeypatch, backend, primary, fallback, expected):
    _fake_service_account(tmp_path, monkeypatch)
    monkeypatch.setattr(entrypoint, "_storage_backend_object_store", lambda *args: object())
    monkeypatch.setenv("AS400_JAVA_CLASSPATH", "/app/probe.jar:/app/lib/*")
    values = _environ(tmp_path, QUADRINGENT_V2_CAPTURE_IMAGE="registry.example.test/capture:1",
                      QUADRINGENT_V2_CAPTURE_SERVICE_ACCOUNT="capture", QUADRINGENT_RAW_BUCKET="raw",
                      QUADRINGENT_RAW_PREFIX_ROOT="site", QUADRINGENT_STORAGE_BACKEND=backend,
                      AWS_REGION=primary, AWS_DEFAULT_REGION=fallback)
    app, engine = entrypoint.build_app(values)
    try:
        assert app.state.pipeline_executor._config.aws_region == expected
    finally:
        engine.dispose()


@pytest.fixture
def startup_database(tmp_path):
    values = _environ(tmp_path, QUADRINGENT_V2_ORG_ID="original")
    db.run_migrations(values[entrypoint.ENV_DATABASE_URL])
    engine = db.create_engine_for(values[entrypoint.ENV_DATABASE_URL])
    try:
        yield engine, values
    finally:
        engine.dispose()


def _database_rows(engine):
    with engine.connect() as connection:
        return {
            table.name: connection.execute(
                select(table).order_by(*table.primary_key.columns)
            ).all()
            for table in schema.metadata.sorted_tables
        }


def _existing_installation(engine, values, org_ids):
    with engine.begin() as connection:
        connection.execute(
            schema.organizations.insert(),
            [{"id": org_id, "name": "Preserved installation"} for org_id in org_ids],
        )
        connection.execute(
            schema.sources.insert(),
            {
                "id": "preserved-source",
                "org_id": org_ids[0],
                "display_name": "Synthetic preserved source",
                "ibmi_host": "source.example.test",
                "ibmi_user": "SYNTHETIC",
                "secret_ciphertext": SecretBox.from_environment(values).encrypt("synthetic-secret"),
            },
        )


def test_organization_bootstrap_creates_one_row_and_restart_preserves_it(startup_database):
    engine, _values = startup_database
    entrypoint.ensure_organization(engine, "original")
    before = _database_rows(engine)
    entrypoint.ensure_organization(engine, "original")
    assert _database_rows(engine) == before
    assert [row.id for row in before["organizations"]] == ["original"]


@pytest.mark.parametrize(
    "existing,requested",
    [
        (["original"], "different"),
        (["original", "other"], "original"),
        (["original", "other"], "different"),
    ],
)
def test_organization_refuses_foreign_or_mixed_database_without_changes(
    startup_database, existing, requested
):
    engine, values = startup_database
    _existing_installation(engine, values, existing)
    before = _database_rows(engine)
    with pytest.raises(entrypoint.ConfigurationError, match="single organisation") as error:
        entrypoint.ensure_organization(engine, requested)
    assert all(org_id not in str(error.value) for org_id in existing + [requested])
    assert "separate database" in str(error.value)
    assert _database_rows(engine) == before


def test_organization_constraint_failure_is_not_silently_accepted(startup_database):
    engine, _values = startup_database
    with pytest.raises(IntegrityError):
        entrypoint.ensure_organization(engine, None)
    assert _database_rows(engine)["organizations"] == []


@pytest.mark.parametrize("existing", [["original"], ["original", "other"]])
def test_build_app_refuses_before_any_adapters_or_app_and_disposes_engine(
    startup_database, monkeypatch, existing
):
    engine, values = startup_database
    _existing_installation(engine, values, existing)
    before = _database_rows(engine)
    values[entrypoint.ENV_ORG_ID] = "different" if len(existing) == 1 else "original"
    monkeypatch.setattr(entrypoint.db, "create_engine_for", lambda _dsn: engine)
    dispose = Mock(wraps=engine.dispose)
    monkeypatch.setattr(engine, "dispose", dispose)
    adapters = {
        "build_diagnostic_adapters": Mock(return_value=(None, None)),
        "build_pipeline_executor": Mock(return_value=(None, None)),
        "build_loader_telemetry": Mock(return_value=None),
        "_build_destination_verifier": Mock(return_value=None),
        "create_v2_app": Mock(),
    }
    for name, adapter in adapters.items():
        monkeypatch.setattr(entrypoint, name, adapter)
    with pytest.raises(entrypoint.ConfigurationError, match="single organisation"):
        entrypoint.build_app(values)
    for adapter in adapters.values():
        adapter.assert_not_called()
    dispose.assert_called_once_with()
    assert _database_rows(engine) == before


@pytest.mark.parametrize("failure_stage", ["secret", "adapter", "app"])
def test_build_app_disposes_engine_on_construction_failure(
    startup_database, monkeypatch, failure_stage
):
    engine, values = startup_database
    monkeypatch.setattr(entrypoint.db, "create_engine_for", lambda _dsn: engine)
    dispose = Mock(wraps=engine.dispose)
    monkeypatch.setattr(engine, "dispose", dispose)
    if failure_stage == "secret":
        values.pop("QUADRINGENT_V2_SECRET_KEY")
    else:
        target = "build_diagnostic_adapters" if failure_stage == "adapter" else "create_v2_app"
        monkeypatch.setattr(entrypoint, target, Mock(side_effect=RuntimeError("Synthetic construction failure")))
        expected_error = RuntimeError
    if failure_stage == "secret":
        from quadringent_control_plane.v2.crypto import SecretKeyUnavailableError

        expected_error = SecretKeyUnavailableError
    with pytest.raises(expected_error):
        entrypoint.build_app(values)
    dispose.assert_called_once_with()

"""Second conteneur control-plane-v2 (FastAPI/uvicorn) — même Pod que v1.

controlPlane.v2.enabled est désactivé par défaut : v1 seule reste le
comportement historique. Ce module vérifie le câblage du conteneur v2 avec
le Postgres embarqué et avec un Postgres externe, les secrets applicatifs
(SECRET_KEY/TOKEN_PEPPER, conservés entre upgrades), les probes /v2/healthz,
et les gardes d'exclusivité/configuration.
"""

from __future__ import annotations

import subprocess
import unittest

import yaml

from test_chart_gcs_backend import GCS_OVERRIDES

VALUES = "infra-values/values-int.yaml"
NAMESPACE = "quadringent-demo"

LAUNCH_FILES = (
    "--set-file", "controlPlane.launch.jobTemplate=infra-values/job-template-int.json",
    "--set-file", "controlPlane.launch.fleetCatalog=infra-values/fleet-catalog-int.json",
    "--set-file", "controlPlane.launch.fleetSidecar=infra-values/fleet-sidecar-int.json",
)


def render(*values: str) -> subprocess.CompletedProcess[str]:
    command = ["helm", "template", "cdc", "chart", "--namespace", NAMESPACE, "-f", VALUES, *LAUNCH_FILES]
    for value in values:
        command += ["--set", value]
    return subprocess.run(command, capture_output=True, text=True)


class ControlPlaneV2Tests(unittest.TestCase):
    def test_v2_has_bounded_writable_tmp_with_read_only_root(self) -> None:
        for backend in ((), GCS_OVERRIDES):
            for launch in ("true", "false"):
                with self.subTest(backend=bool(backend), launch=launch):
                    result = render(*backend, "controlPlane.v2.enabled=true", "postgres.enabled=true",
                                    f"controlPlane.launch.enabled={launch}",
                                    "controlPlane.telemetry.enabled=false")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    deployment = next(d for d in yaml.safe_load_all(result.stdout)
                                      if d and d.get("kind") == "Deployment"
                                      and d["metadata"]["name"].endswith("-control-plane"))
                    pod = deployment["spec"]["template"]["spec"]
                    v2 = next(c for c in pod["containers"] if c["name"] == "control-plane-v2")
                    mounts = {m["mountPath"]: m for m in v2["volumeMounts"]}
                    temporary = mounts["/tmp"]
                    self.assertFalse(temporary.get("readOnly", False))
                    volume = next(v for v in pod["volumes"] if v["name"] == temporary["name"])
                    self.assertEqual(volume["emptyDir"], {"sizeLimit": "128Mi"})
                    self.assertEqual(pod["securityContext"]["fsGroup"], 10001)
                    self.assertTrue(v2["securityContext"]["readOnlyRootFilesystem"])
                    self.assertTrue(v2["securityContext"]["runAsNonRoot"])
                    self.assertEqual(v2["securityContext"]["runAsUser"], 10001)

    def test_aws_v2_region_is_explicit_for_vm_and_eks(self) -> None:
        # Le rendu Helm précède les webhooks EKS : la VM doit disposer du
        # même contrat régional explicite, sans dépendre d'une injection IRSA.
        for platform, region in (("vm", "eu-central-1"), ("eks", "ap-southeast-2")):
            with self.subTest(platform=platform):
                result = render(
                    "controlPlane.v2.enabled=true", "postgres.enabled=true",
                    "storage.backend=aws", f"aws.region={region}",
                    f"controlPlane.serviceAccount.name=quadringent-{platform}",
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                documents = list(yaml.safe_load_all(result.stdout))
                site = next(d for d in documents if d and d.get("kind") == "ConfigMap"
                            and d["metadata"]["name"].endswith("-site"))
                deployment = next(d for d in documents if d and d.get("kind") == "Deployment"
                                  and any(c["name"] == "control-plane-v2" for c in
                                          d["spec"]["template"]["spec"]["containers"]))
                containers = deployment["spec"]["template"]["spec"]["containers"]
                for name in ("control-plane", "control-plane-v2"):
                    container = next(c for c in containers if c["name"] == name)
                    env = container["env"]
                    for variable in ("AWS_REGION", "AWS_DEFAULT_REGION"):
                        matches = [e for e in env if e["name"] == variable]
                        self.assertEqual(matches, [{"name": variable, "value": region}])
                        self.assertEqual(matches[0]["value"], site["data"]["QUADRINGENT_AWS_REGION"])

    def test_gcs_v2_does_not_receive_aws_region(self) -> None:
        result = render(*GCS_OVERRIDES, "controlPlane.v2.enabled=true", "postgres.enabled=true")
        self.assertEqual(result.returncode, 0, result.stderr)
        deployment = next(d for d in yaml.safe_load_all(result.stdout)
                          if d and d.get("kind") == "Deployment"
                          and any(c["name"] == "control-plane-v2" for c in
                                  d["spec"]["template"]["spec"]["containers"]))
        for container in deployment["spec"]["template"]["spec"]["containers"]:
            self.assertFalse({"AWS_REGION", "AWS_DEFAULT_REGION"} &
                             {e["name"] for e in container.get("env", [])})

    def test_disabled_by_default_v1_only_renders(self) -> None:
        result = render()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("control-plane-v2", result.stdout)
        self.assertNotIn("cdc-quadringent-v2-secrets", result.stdout)

    def test_requires_postgres_or_external_database(self) -> None:
        result = render("controlPlane.v2.enabled=true", "postgres.enabled=false")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "controlPlane.v2.enabled requires postgres.enabled=true or externalDatabase.url/existingSecret",
            result.stderr,
        )

    def test_network_bind_requires_authentication_in_english(self) -> None:
        result = render("controlPlane.v2.enabled=true", "postgres.enabled=true", "controlPlane.host=0.0.0.0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("non-loopback controlPlane.host requires controlPlane.auth.enabled=true", result.stderr)

    def test_proxy_authentication_requires_groups_in_english(self) -> None:
        result = render("controlPlane.v2.enabled=true", "postgres.enabled=true", "controlPlane.auth.enabled=true")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("controlPlane.auth.enabled requires non-empty operatorGroups or adminGroups", result.stderr)

    def test_postgres_and_external_database_together_are_refused(self) -> None:
        result = render(
            "controlPlane.v2.enabled=true", "postgres.enabled=true", "externalDatabase.url=postgresql://ext/db"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("mutually exclusive", result.stderr)

    def test_v2_port_must_differ_from_v1_port(self) -> None:
        result = render("controlPlane.v2.enabled=true", "postgres.enabled=true", "controlPlane.v2.port=8844")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("controlPlane.v2.port must differ from controlPlane.port", result.stderr)

    def test_enabled_with_embedded_postgres_composes_the_dsn_at_startup(self) -> None:
        result = render("controlPlane.v2.enabled=true", "postgres.enabled=true")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("name: control-plane-v2", result.stdout)
        self.assertIn('QUADRINGENT_V2_DATABASE_URL="postgresql+psycopg://', result.stdout)
        self.assertIn("name: POSTGRES_HOST", result.stdout)
        self.assertIn("cdc-quadringent-postgres-credentials", result.stdout)
        self.assertIn("key: POSTGRES_PASSWORD", result.stdout)

    def test_enabled_with_external_database_url_passes_it_directly(self) -> None:
        result = render(
            "controlPlane.v2.enabled=true",
            "postgres.enabled=false",
            "externalDatabase.url=postgresql+psycopg://user:pass@ext-host:5432/quadringent",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            'value: "postgresql+psycopg://user:pass@ext-host:5432/quadringent"', result.stdout
        )
        self.assertNotIn("POSTGRES_HOST", result.stdout)

    def test_enabled_with_external_database_existing_secret_references_it(self) -> None:
        result = render(
            "controlPlane.v2.enabled=true",
            "postgres.enabled=false",
            "externalDatabase.existingSecret=site-managed-db",
            "externalDatabase.existingSecretKey=dsn",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('name: "site-managed-db"', result.stdout)
        self.assertIn('key: "dsn"', result.stdout)

    def test_v2_secrets_are_generated_and_kept_across_upgrades(self) -> None:
        result = render("controlPlane.v2.enabled=true", "postgres.enabled=true")
        self.assertEqual(result.returncode, 0, result.stderr)
        documents = result.stdout.split("\n---\n")
        secret = next(
            document
            for document in documents
            if "kind: Secret" in document and "name: cdc-quadringent-v2-secrets" in document
        )
        self.assertIn("helm.sh/resource-policy: keep", secret)
        self.assertIn("SECRET_KEY:", secret)
        self.assertIn("TOKEN_PEPPER:", secret)

    def test_v2_healthz_probes_are_wired(self) -> None:
        result = render("controlPlane.v2.enabled=true", "postgres.enabled=true")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("- /v2/healthz", result.stdout)  # sonde exec en loopback
        self.assertIn("containerPort: 8845", result.stdout)

    def test_new_site_defaults_and_explicit_streaming_values(self) -> None:
        for overrides, expected in (
            ((), ("sql", "1", "1")),
            (("controlPlane.v2.loaderHistoryMode=streaming", "controlPlane.v2.readerPollSeconds=5",
              "controlPlane.v2.loaderPollSeconds=10"), ("streaming", "5", "10")),
        ):
            result = render("controlPlane.v2.enabled=true", "postgres.enabled=true", *overrides)
            self.assertEqual(result.returncode, 0, result.stderr)
            deployment = next(d for d in yaml.safe_load_all(result.stdout) if d and d.get("kind") == "Deployment"
                              and any(c["name"] == "control-plane-v2" for c in d["spec"]["template"]["spec"]["containers"]))
            container = next(c for c in deployment["spec"]["template"]["spec"]["containers"] if c["name"] == "control-plane-v2")
            env = {e["name"]: e.get("value") for e in container["env"]}
            self.assertEqual(tuple(env[k] for k in ("QUADRINGENT_V2_LOADER_HISTORY_MODE",
                             "QUADRINGENT_V2_READER_POLL_SECONDS", "QUADRINGENT_V2_LOADER_POLL_SECONDS")), expected)
            self.assertEqual(env["QUADRINGENT_V2_LOADER_FLUSH_EACH_BATCH"], "false")

    def test_poll_intervals_are_declared_for_dynamic_workers(self) -> None:
        result = render("controlPlane.v2.enabled=true", "postgres.enabled=true",
                        "controlPlane.v2.readerPollSeconds=1", "controlPlane.v2.loaderPollSeconds=1",
                        "controlPlane.v2.loaderFlushEachBatch=true",
                        "controlPlane.v2.loaderHistoryMode=sql")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("name: QUADRINGENT_V2_READER_POLL_SECONDS", result.stdout)
        self.assertIn("name: QUADRINGENT_V2_LOADER_POLL_SECONDS", result.stdout)
        self.assertIn("name: QUADRINGENT_V2_LOADER_FLUSH_EACH_BATCH", result.stdout)
        self.assertIn("name: QUADRINGENT_V2_LOADER_HISTORY_MODE", result.stdout)

    def test_invalid_v2_image_digest_is_refused(self) -> None:
        result = render(
            "controlPlane.v2.enabled=true", "postgres.enabled=true", "controlPlane.v2.image.digest=sha256:bad"
        )
        self.assertNotEqual(result.returncode, 0)

    def test_v1_container_receives_the_v2_upstream_port_when_v2_is_enabled(self) -> None:
        result = render("controlPlane.v2.enabled=true", "postgres.enabled=true")
        self.assertEqual(result.returncode, 0, result.stderr)
        v1_container = result.stdout.split("- name: control-plane-v2")[0]
        self.assertIn("- --v2-upstream-port", v1_container)
        self.assertIn('- "8845"', v1_container)

    def test_v1_container_has_no_v2_upstream_port_when_v2_is_disabled(self) -> None:
        result = render()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("--v2-upstream-port", result.stdout)

    # -- Exécuteur de pipeline v2 (chantier « pipeline-exec ») ----------------

    def test_v2_executor_env_vars_are_wired(self) -> None:
        result = render("controlPlane.v2.enabled=true", "postgres.enabled=true")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("QUADRINGENT_V2_CAPTURE_SERVICE_ACCOUNT", result.stdout)
        # infra-values/values-int.yaml déclare serviceAccount.name=as400-snowflake-capture.
        self.assertIn('value: "as400-snowflake-capture"', result.stdout)
        self.assertIn("QUADRINGENT_V2_READER_TIMEOUT_SECONDS", result.stdout)

    def test_v2_executor_rbac_covers_exactly_the_verbs_used(self) -> None:
        """Deployments : create/get/update/delete (``KubernetesDeploymentsClient``).
        Jobs : create/get/delete (copie initiale, rejeu). Secrets durables :
        create/get/patch (``upsert_secret`` — jamais delete, contrairement aux
        Secrets éphémères des Jobs de diagnostic)."""

        result = render("controlPlane.v2.enabled=true", "postgres.enabled=true")
        self.assertEqual(result.returncode, 0, result.stderr)
        documents = result.stdout.split("\n---\n")
        role = next(
            document
            for document in documents
            if "kind: Role" in document and "control-plane-v2-executor" in document
        )
        self.assertIn('apiGroups: ["apps"]', role)
        self.assertIn('resources: ["deployments"]', role)
        self.assertIn('verbs: ["create", "get", "update", "delete"]', role)
        self.assertIn('resources: ["jobs"]', role)
        self.assertIn('resources: ["secrets"]', role)
        self.assertIn('verbs: ["create", "get", "patch"]', role)
        binding = next(
            document
            for document in documents
            if "kind: RoleBinding" in document and "control-plane-v2-executor" in document
        )
        self.assertIn("name: quadringent-control-plane", binding)

    def test_v2_executor_rbac_absent_when_v2_disabled(self) -> None:
        result = render()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("control-plane-v2-executor", result.stdout)


if __name__ == "__main__":
    unittest.main()

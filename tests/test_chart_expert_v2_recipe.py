"""Render the guide's actual v2 overlays, using synthetic site values only."""

from __future__ import annotations

import base64
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from quadringent_control_plane.v2.crypto import SecretBox
from quadringent_control_plane.v2.entrypoint import resolve_org_id


@pytest.mark.parametrize("external", [False, True], ids=["bundled-postgres", "external-secret"])
def test_documented_v2_recipe_renders_a_private_authenticated_control_plane(tmp_path, external):
    guide = Path("docs/product/install-client.md").read_text()
    overlays = re.findall(r"```yaml\n(.*?)\n```", guide, re.DOTALL)
    assert len(overlays) == 2, "the expert guide must provide both v2 database recipes"
    command = [
        "helm", "template", "quadringent", "chart", "--namespace", "quadringent-demo",
        "-f", "infra-values/values-int.yaml",
    ]
    overlay = yaml.safe_load(overlays[0])
    if external:
        overlay.update(yaml.safe_load(overlays[1]))
    path = tmp_path / "v2.yaml"
    path.write_text(yaml.safe_dump(overlay))
    command += ["-f", str(path)]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    documents = [document for document in yaml.safe_load_all(result.stdout) if document]
    deployment = next(d for d in documents if d["kind"] == "Deployment"
                      and d["metadata"]["name"].endswith("-control-plane"))
    containers = {c["name"]: c for c in deployment["spec"]["template"]["spec"]["containers"]}
    assert "control-plane-v2" in containers
    relay = containers["control-plane"]["args"]
    assert relay[relay.index("--v2-upstream-port") + 1] == "8845"
    assert relay[relay.index("--host") + 1] == "127.0.0.1"
    v2 = containers["control-plane-v2"]
    assert "/app/quadringent_control_plane_v2.py" in " ".join(v2["command"] + v2.get("args", []))
    env = {e["name"]: e for e in v2["env"]}
    site = next(d for d in documents if d["kind"] == "ConfigMap"
                and d["metadata"]["name"].endswith("-site"))
    assert {"configMapRef": {"name": site["metadata"]["name"]}} in v2["envFrom"]
    org_values = {**site["data"], **{e["name"]: e["value"] for e in v2["env"] if "value" in e}}
    assert resolve_org_id(org_values) == site["data"]["QUADRINGENT_SITE_ID"]
    name = "quadringent-quadringent-v2-secrets"
    for variable, key in (("QUADRINGENT_V2_SECRET_KEY", "SECRET_KEY"),
                          ("QUADRINGENT_V2_TOKEN_PEPPER", "TOKEN_PEPPER")):
        assert env[variable]["valueFrom"]["secretKeyRef"] == {"name": name, "key": key}
    secret = next(d for d in documents if d["kind"] == "Secret" and d["metadata"]["name"] == name)
    assert secret["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"
    box = SecretBox(base64.b64decode(secret["data"]["SECRET_KEY"]))
    assert box.decrypt(box.encrypt("synthetic")) == "synthetic"
    assert base64.b64decode(secret["data"]["TOKEN_PEPPER"])
    postgres = [d for d in documents if d["kind"] == "StatefulSet"]
    if external:
        assert not postgres
        assert env["QUADRINGENT_V2_DATABASE_URL"]["valueFrom"]["secretKeyRef"] == {
            "name": "site-managed-db", "key": "url",
        }
    else:
        assert len(postgres) == 1
        assert postgres[0]["spec"]["volumeClaimTemplates"]
        assert env["POSTGRES_PASSWORD"]["valueFrom"]["secretKeyRef"]["key"] == "POSTGRES_PASSWORD"
    assert not any(d["kind"] in {"Ingress", "CronJob", "Job"} for d in documents)
    assert not any(d["kind"] == "Service" and d["metadata"]["name"].endswith("-control-plane")
                   for d in documents)
    capture = next(d for d in documents if d["kind"] == "Deployment" and d != deployment)
    assert capture["spec"]["replicas"] == 0

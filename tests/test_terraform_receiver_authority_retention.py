"""Evaluate the shipped retention guards offline, without an AWS provider."""
from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from quadringent.installer.plan import InstallInputs, base_tfvars_for, build_chart_values
from test_installer_plan import MANIFEST

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "deploy/terraform/aws/base"


def _blocks(text: str, marker: str) -> list[str]:
    result = []
    for match in re.finditer(marker + r"\s*\{", text):
        start = match.end()
        depth, quoted, escaped = 1, False, False
        for index in range(start, len(text)):
            character = text[index]
            if quoted:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    quoted = False
            elif character == '"':
                quoted = True
            elif character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
                if depth == 0:
                    result.append(text[start:index])
                    break
    assert result, f"missing Terraform block: {marker}"
    return result


def _offline_plan(tmp_path: Path, **variables):
    terraform = shutil.which("terraform")
    if terraform is None:
        pytest.skip("Terraform unavailable for provider-free guard evaluation")
    text = (BASE / "main.tf").read_text()
    lifecycle = _blocks(text, r'resource "aws_s3_bucket_lifecycle_configuration" "raw"')[0]
    prefix = re.search(r"\bprefix\s*=\s*([^\n]+)", _blocks(lifecycle, r"\bfilter")[0])
    assert prefix, "lifecycle must target an explicit RAW prefix"
    policy = _blocks(text, r'data "aws_iam_policy_document" "runtime"')[0]
    statements = []
    for statement in _blocks(policy, r"\bstatement"):
        conditions = re.sub(r"\bcondition\s*\{", "condition = [{", statement)
        if "condition = [{" in conditions:
            conditions = conditions.rstrip()[:-1] + "}]"
        statements.append("{" + conditions + "}")
    runtime = _blocks(text, r'resource "aws_iam_policy" "runtime"')[0]
    guard = _blocks(runtime, r"\bprecondition")[0]
    (tmp_path / "variables.tf").write_text((BASE / "variables.tf").read_text())
    (tmp_path / "main.tf").write_text('''
locals {
  raw_bucket_arn = "arn:aws:s3:::offline-raw"
  checkpoint_table_arn = "arn:aws:dynamodb:eu-west-3:000000000000:table/offline-state"
}
output "retention" {
  value = {
    enabled = var.lifecycle_expiration_days > 0
    prefix = ''' + prefix[1] + '''
  }
  precondition {''' + guard + '''}
}
output "policy" {
  value = [''' + ",".join(statements) + ''']
}
''')
    values = {"name": "offline", "region": "eu-west-3", **variables}
    (tmp_path / "test.auto.tfvars.json").write_text(json.dumps(values))
    completed = subprocess.run([terraform, "plan", "-input=false", "-lock=false", "-refresh=false",
                                "-no-color", "-out=offline.tfplan"], cwd=tmp_path,
                               capture_output=True, text=True, timeout=20)
    if completed.returncode:
        return completed, None
    rendered = subprocess.run([terraform, "show", "-json", "offline.tfplan"], cwd=tmp_path,
                              capture_output=True, text=True, timeout=20, check=True)
    return completed, json.loads(rendered.stdout)["planned_values"]["outputs"]


def test_expiration_stays_disabled_by_default(tmp_path):
    completed, outputs = _offline_plan(tmp_path)
    assert completed.returncode == 0, completed.stderr
    assert outputs["retention"]["value"]["enabled"] is False


@pytest.mark.parametrize("prefix", ["", "/", "/raw", "raw/", "raw//orders", " raw", "raw ",
                                   ".", "..", "raw/../orders", "raw/./orders", "raw/*", "raw/%2f",
                                   "receiver-authority-v1", "receiver-authority-v1/names", "raw\\orders", None])
def test_positive_expiration_refuses_unbound_noncanonical_or_authority_prefix(tmp_path, prefix):
    completed, _ = _offline_plan(tmp_path, lifecycle_expiration_days=30, raw_prefix_root=prefix)
    assert completed.returncode != 0
    assert "RAW" in completed.stderr


@pytest.mark.parametrize("days", [-1, 0.5])
def test_expiration_days_must_be_whole_nonnegative_days(tmp_path, days):
    completed, _ = _offline_plan(tmp_path, lifecycle_expiration_days=days, raw_prefix_root="raw/orders")
    assert completed.returncode != 0


@pytest.mark.parametrize("prefix", ["raw", "site/pending", "raw/receiver-authority-v1", "receiver-authority-v1x"])
def test_rendered_raw_retention_never_matches_the_authority(tmp_path, prefix):
    completed, outputs = _offline_plan(tmp_path, lifecycle_expiration_days=30, raw_prefix_root=prefix)
    assert completed.returncode == 0, completed.stderr
    rendered = outputs["retention"]["value"]
    assert rendered == {"enabled": True, "prefix": prefix + "/"}
    assert (prefix + "/orders/batch.json").startswith(rendered["prefix"])
    assert not "receiver-authority-v1/receiver-registry-v1/claim.json".startswith(rendered["prefix"])
    assert not "receiver-authority-v1/copy-intents-v1/run.json".startswith(rendered["prefix"])


def test_runtime_policy_denies_authority_overwrite_and_delete_without_new_grants(tmp_path):
    completed, outputs = _offline_plan(tmp_path)
    assert completed.returncode == 0, completed.stderr
    statements = {item["sid"]: item for item in outputs["policy"]["value"]}
    write = statements["DenyAuthorityNonConditionalPut"]
    deletion = statements["DenyAuthorityDeletion"]
    authority = ["arn:aws:s3:::offline-raw/receiver-authority-v1/*"]
    assert write["effect"] == deletion["effect"] == "Deny"
    assert write["resources"] == deletion["resources"] == authority
    assert write["actions"] == ["s3:PutObject"]
    assert write["condition"] == [{"test": "StringNotEquals", "variable": "s3:if-none-match", "values": ["*"]}]
    assert set(deletion["actions"]) == {"s3:DeleteObject", "s3:DeleteObjectVersion"}
    grants = {sid: set(item["actions"]) for sid, item in statements.items() if item["effect"] == "Allow"}
    assert grants == {"S3RawObjects": {"s3:GetObject", "s3:PutObject", "s3:ListBucket"},
                      "DynamoDbCheckpoints": {"dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Query"}}


@pytest.mark.parametrize("target", ["vm", "cluster"])
def test_installer_passes_the_same_approved_raw_prefix_to_aws_foundation_and_chart(target):
    inputs = InstallInputs(cloud="aws", target=target, region="eu-west-3", name="demo-int")
    values = build_chart_values(inputs, MANIFEST, control_plane_role_arn="arn:aws:iam::000000000000:role/offline")
    assert base_tfvars_for(inputs)["raw_prefix_root"] == values["site"]["rawPrefixRoot"] == "demo-int/pending"
    assert "lifecycle_expiration_days" not in base_tfvars_for(inputs)


def test_gcp_foundation_inputs_remain_unchanged():
    inputs = InstallInputs(cloud="gcp", target="vm", region="europe-west1", project="offline-project", name="demo-int")
    assert "raw_prefix_root" not in base_tfvars_for(inputs)

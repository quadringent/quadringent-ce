"""Plan VM lifecycle changes with mocked providers and a shared legacy state."""
from __future__ import annotations

import json
import io
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from quadringent.installer.apply import TerraformApplyRefused, apply_terraform_module
from quadringent.installer.runner import CommandResult, RecordingRunner

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "deploy/terraform/aws/vm"
LEGACY_MAIN = ROOT / "tests/fixtures/terraform/aws_vm/legacy-main.tf"
OLD_AMI = "ami-00000000000000001"
LATEST_AMI = "ami-00000000000000002"
NEXT_AMI = "ami-00000000000000003"
ARM_AMI = "ami-00000000000000004"


def _run(name: str, *, command: str = "plan", ami: str = LATEST_AMI,
         legacy: bool = False, fresh: bool = False, variables: str = "", replace: bool = False) -> str:
    module = 'module { source = "./legacy" }' if legacy else ""
    replacement = 'plan_options { replace = [aws_instance.vm] }' if replace else ""
    return f'''
run "{name}" {{
  command = {command}
  state_key = "{"fresh" if fresh else "existing"}"
  {module}
  {replacement}
  variables {{
    {variables}
  }}
  override_data {{
    target = data.aws_ssm_parameter.al2023
    values = {{ value = "{ami}" }}
  }}
}}
'''


@pytest.fixture(scope="module")
def vm_plans(tmp_path_factory):
    binary = os.environ.get("QUADRINGENT_TERRAFORM_BINARY")
    plugins = os.environ.get("QUADRINGENT_TERRAFORM_PLUGIN_DIR")
    if not binary or not plugins:
        pytest.skip("Offline mock plans require Terraform >=1.11 and a local provider mirror")
    work = tmp_path_factory.mktemp("aws-vm-mock-plans")
    env = {"PATH": os.environ["PATH"], "TF_IN_AUTOMATION": "1", "CHECKPOINT_DISABLE": "1",
           "AWS_EC2_METADATA_DISABLED": "true", "AWS_CONFIG_FILE": os.devnull,
           "AWS_SHARED_CREDENTIALS_FILE": os.devnull}
    version = subprocess.run([binary, "version", "-json"], env=env, capture_output=True,
                             text=True, check=True, timeout=30)
    assert tuple(map(int, json.loads(version.stdout)["terraform_version"].split(".")[:2])) >= (1, 11)
    shutil.copytree(MODULE, work, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(".terraform", "*.tfstate*", "tests", ".terraform.lock.hcl"))
    legacy = work / "legacy"
    legacy.mkdir()
    for name in ("variables.tf", "versions.tf", "outputs.tf", "cloud-init.tpl.yaml"):
        shutil.copyfile(MODULE / name, legacy / name)
    shutil.copyfile(LEGACY_MAIN, legacy / "main.tf")
    (work / "tests").mkdir()
    suite = '''
mock_provider "aws" {}
mock_provider "cloudinit" {
  mock_data "cloudinit_config" { defaults = { rendered = "synthetic-cloud-init" } }
}
variables {
  name = "synthetic-ami-test"
  vpc_id = "vpc-00000000000000001"
  subnet_id = "subnet-00000000000000001"
  instance_profile_name = "synthetic-profile"
  instance_role_name = "synthetic-role"
}
'''
    suite += _run("legacy_install", command="apply", ami=OLD_AMI, legacy=True)
    suite += _run("first_upgrade")
    suite += _run("record_architecture", command="apply")
    suite += _run("latest_changed", ami=NEXT_AMI)
    suite += _run("disk_resize", variables="root_volume_size_gb = 48")
    suite += _run("architecture_changed", ami=ARM_AMI,
                  variables='architecture = "arm64"\n instance_type = "t4g.medium"')
    suite += _run("explicit_os_replacement", replace=True)
    suite += _run("fresh_install", command="apply", fresh=True, ami=NEXT_AMI)
    (work / "tests/ami.tftest.hcl").write_text(suite)
    init = subprocess.run([binary, f"-chdir={work}", "init", "-backend=false", "-input=false",
                           f"-plugin-dir={plugins}"], env=env, capture_output=True, text=True, timeout=90)
    assert init.returncode == 0, init.stdout + init.stderr
    result = subprocess.run([binary, f"-chdir={work}", "test", "-json", "-verbose"],
                            env=env, capture_output=True, text=True, timeout=120)
    (work / "mock-plans.jsonl").write_text(result.stdout)
    assert result.returncode == 0, result.stdout + result.stderr
    events = [json.loads(line) for line in result.stdout.splitlines()]
    return {event["@testrun"]: event for event in events if event["type"] in {"test_plan", "test_state"}}


def _vm_change(plans, run):
    return next(change["change"] for change in plans[run]["test_plan"]["resource_changes"]
                if change["address"] == "aws_instance.vm")


def test_first_upgrade_preserves_legacy_vm_and_root_disk(vm_plans):
    legacy = vm_plans["legacy_install"]["test_state"]["root_module"]["resources"]
    assert not any(resource["type"] == "terraform_data" for resource in legacy)
    change = _vm_change(vm_plans, "first_upgrade")
    assert change["actions"] == ["no-op"]
    assert change["before"]["ami"] == change["after"]["ami"] == OLD_AMI
    assert change["before"]["id"] == change["after"]["id"]
    assert change["before"]["root_block_device"] == change["after"]["root_block_device"]


def test_later_latest_ami_change_does_not_replace_vm(vm_plans):
    change = _vm_change(vm_plans, "latest_changed")
    assert change["actions"] == ["no-op"]
    assert change["after"]["ami"] == OLD_AMI


def test_new_install_uses_current_latest_ami(vm_plans):
    resources = vm_plans["fresh_install"]["test_state"]["root_module"]["resources"]
    vm = next(resource for resource in resources if resource["address"] == "aws_instance.vm")
    assert vm["values"]["ami"] == NEXT_AMI


def test_architecture_change_is_a_visible_vm_replacement(vm_plans):
    change = _vm_change(vm_plans, "architecture_changed")
    assert set(change["actions"]) == {"delete", "create"}
    assert change["after"]["ami"] == ARM_AMI


def test_explicit_os_replacement_uses_latest_ami(vm_plans):
    change = _vm_change(vm_plans, "explicit_os_replacement")
    assert set(change["actions"]) == {"delete", "create"}
    assert change["after"]["ami"] == LATEST_AMI


def test_other_vm_drift_remains_visible(vm_plans):
    change = _vm_change(vm_plans, "disk_resize")
    assert change["actions"] == ["update"]
    assert change["after"]["root_block_device"][0]["volume_size"] == 48


@pytest.mark.parametrize(("run", "refused"), [
    ("first_upgrade", False), ("architecture_changed", True), ("explicit_os_replacement", True),
])
def test_real_mock_plan_reaches_installer_destruction_guard(vm_plans, tmp_path, run, refused):
    show = ("terraform", "show", "-json", "tfplan")
    runner = RecordingRunner(scripted_results={
        show: CommandResult(show, 0, json.dumps(vm_plans[run]["test_plan"])),
    })
    arguments = dict(module_label="synthetic VM", source_dir=str(MODULE),
                     module_workdir=tmp_path / "installer", tfvars={}, runner=runner,
                     stdout=io.StringIO(), yes=True, allow_destroy=False)
    if refused:
        with pytest.raises(TerraformApplyRefused, match="--allow-destroy"):
            apply_terraform_module(**arguments)
    else:
        apply_terraform_module(**arguments)
    assert any(call[0][:2] == ("terraform", "apply") for call in runner.invocations) is not refused

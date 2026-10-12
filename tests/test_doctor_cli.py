"""``quadringent doctor``: one report, human and JSON outputs, explicit network calls."""

from __future__ import annotations

import io
import json
import os
import stat

import pytest
from fastapi.testclient import TestClient

from quadringent.diagnostics.model import parse_report
from quadringent.installer.cli import _build_parser
from quadringent.installer.doctor import run_doctor
from quadringent.installer.runner import RecordingRunner
from quadringent_control_plane.v2 import db as v2_db, schema as v2_schema
from quadringent_control_plane.v2.app import create_v2_app
from quadringent_control_plane.v2.crypto import SecretBox

ALL_TOOLS = frozenset({"terraform", "helm", "kubectl", "aws", "session-manager-plugin", "gcloud"})


class Refuse:
    calls = 0

    def __call__(self, *args, **kwargs):
        Refuse.calls += 1
        raise ConnectionRefusedError(61, "Connection refused")


def doctor(argv, *, tools=ALL_TOOLS, environ=None, fetch=None, connector=None, tmp_path=None):
    args = _build_parser().parse_args(["doctor", *argv])
    out = io.StringIO()
    runner = RecordingRunner(available_tools=tools)
    code = run_doctor(args, runner, out, environ=environ or {}, fetch=fetch, connector=connector or Refuse(),
                      config_path=(tmp_path / "absent-cli.json") if tmp_path else None)
    return code, out.getvalue(), runner


def test_local_doctor_alone_is_never_ready(tmp_path):
    code, output, _ = doctor(["--json"], tmp_path=tmp_path)
    report = parse_report(json.loads(output))
    assert code == 3 and report.overall()["status"] == "incomplete"
    control = [r for r in report.results if r.code == "control_plane.api"][0]
    assert control.status == "skipped" and "--control-plane" in control.next_action


def test_missing_tool_blocks_with_a_next_action(tmp_path):
    code, output, _ = doctor(["--json"], tools=frozenset({"helm", "kubectl"}), tmp_path=tmp_path)
    report = parse_report(json.loads(output))
    terraform = [r for r in report.results if r.code == "local_tools.terraform"][0]
    assert code == 1 and terraform.status == "failed" and "PATH" in terraform.next_action


def test_cloud_credentials_presence_is_configuration_not_authentication(tmp_path):
    _, output, _ = doctor(["--json", "--cloud", "aws"], environ={"AWS_PROFILE": "profile-canary-123"}, tmp_path=tmp_path)
    report = parse_report(json.loads(output))
    credentials = [r for r in report.results if r.code == "local_config.cloud_credentials"][0]
    assert credentials.layer == "local_config" and "not authenticated" in credentials.limits
    assert "profile-canary-123" not in output  # the profile name itself is never printed


def test_network_checks_only_run_on_request(tmp_path):
    Refuse.calls = 0
    doctor(["--json"], tmp_path=tmp_path)
    assert Refuse.calls == 0
    code, output, _ = doctor(["--json", "--check-ibmi", "ibmi.synthetic.invalid"], tmp_path=tmp_path)
    report = parse_report(json.loads(output))
    assert Refuse.calls == 4 and code == 1
    layers = {r.code: r.status for r in report.results}
    assert layers["network.tcp.database"] == "failed"
    assert layers["tls.database"] == "unknown" and layers["authentication.ibmi_signon"] == "unknown"


def test_human_and_json_outputs_come_from_the_same_report(tmp_path):
    _, as_json, _ = doctor(["--json", "--check-ibmi", "ibmi.synthetic.invalid"], tmp_path=tmp_path)
    _, as_text, _ = doctor(["--check-ibmi", "ibmi.synthetic.invalid"], tmp_path=tmp_path)
    for item in json.loads(as_json)["results"]:
        assert item["code"] in as_text
    assert "BLOCKED" in as_text and "Next:" in as_text


@pytest.fixture()
def control_plane(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'doctor.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    with engine.begin() as c:
        c.execute(v2_schema.organizations.insert(), {"id": "default", "name": "Synthetic"})
        c.execute(v2_schema.destinations.insert(), {"id": "dst1", "org_id": "default", "snowflake_account": "synthetic-acct",
                                                     "key_pair_ciphertext": "gAAAAA==", "setup_script": "--",
                                                     "verification_state": "verified"})
    client = TestClient(create_v2_app(engine=engine, secret_box=SecretBox(SecretBox.generate_key())))

    def fetch(path, params, timeout):
        response = client.get(path, params=params)
        return response.status_code, response.json()

    yield fetch
    engine.dispose()


def test_control_plane_results_are_merged_from_the_real_route(tmp_path, control_plane):
    code, output, _ = doctor(["--json", "--control-plane"], fetch=control_plane, tmp_path=tmp_path)
    codes = {r["code"]: r["status"] for r in json.loads(output)["results"]}
    assert codes["control_plane.api"] == "passed" and codes["destination.verification"] == "passed"


@pytest.mark.parametrize("status,body,expected", [
    (401, {"error": {"code": "invalid_request", "message": "missing identity"}}, "refused the token"),
    (403, {"error": {"code": "insufficient_role", "message": "read"}}, "permission"),
    (503, {"error": {"code": "store_unavailable", "message": "down"}}, "unavailable"),
])
def test_control_plane_errors_are_explained_without_guessing(tmp_path, status, body, expected):
    code, output, _ = doctor(["--json", "--control-plane"], fetch=lambda *a: (status, body), tmp_path=tmp_path)
    control = [r for r in json.loads(output)["results"] if r["code"] == "control_plane.api"][0]
    assert code == 1 and control["status"] == "failed" and expected in control["summary"]


def test_unreachable_control_plane_is_a_failure(tmp_path):
    def fetch(*_args):
        raise ConnectionRefusedError("refused")

    _, output, _ = doctor(["--json", "--control-plane"], fetch=fetch, tmp_path=tmp_path)
    control = [r for r in json.loads(output)["results"] if r["code"] == "control_plane.api"][0]
    assert control["status"] == "failed" and control["evidence"]["error"] == "connection refused"


def test_offline_mode_reuses_a_saved_report_without_calling_anything(tmp_path):
    saved = tmp_path / "report.json"
    doctor(["--json", "--save", str(saved), "--check-ibmi", "ibmi.synthetic.invalid"], tmp_path=tmp_path)
    assert stat.S_IMODE(os.stat(saved).st_mode) == 0o600
    Refuse.calls = 0
    code, output, runner = doctor(["--from-report", str(saved)], tmp_path=tmp_path)
    assert Refuse.calls == 0 and runner.calls == []
    assert code == 1 and "network.tcp.database" in output


def test_save_refuses_to_overwrite(tmp_path):
    saved = tmp_path / "report.json"
    saved.write_text("keep")
    code, output, _ = doctor(["--save", str(saved)], tmp_path=tmp_path)
    assert code == 2 and saved.read_text() == "keep"


def test_export_preview_then_archive(tmp_path):
    archive = tmp_path / "diagnostic.tar.gz"
    code, output, _ = doctor(["--check-ibmi", "ibmi-secret.customer.invalid", "--export", str(archive), "--preview"],
                             tmp_path=tmp_path)
    preview = json.loads(output)
    assert not archive.exists() and {f["name"] for f in preview["files"]} >= {"report.json", "manifest.json"}
    doctor(["--check-ibmi", "ibmi-secret.customer.invalid", "--export", str(archive)], tmp_path=tmp_path)
    assert stat.S_IMODE(os.stat(archive).st_mode) == 0o600
    assert b"ibmi-secret.customer.invalid" not in __import__("gzip").decompress(archive.read_bytes())


def test_help_documents_exit_codes_and_network_flags():
    help_text = _build_parser()._subparsers._group_actions[0].choices["doctor"].format_help()
    for fragment in ("0 ready", "1 blocked", "3 incomplete", "--check-ibmi", "--control-plane", "--tls"):
        assert fragment in help_text


def test_control_plane_mode_without_the_api_extra_says_so(tmp_path, monkeypatch):
    import importlib.util
    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a: None if name == "httpx" else real(name, *a))
    args = _build_parser().parse_args(["doctor", "--control-plane"])
    out = io.StringIO()
    code = run_doctor(args, RecordingRunner(available_tools=ALL_TOOLS), out,
                      environ={"QUADRINGENT_URL": "http://127.0.0.1:9"}, config_path=tmp_path / "absent.json")
    assert code == 2 and "quadringent[api]" in out.getvalue()


def _remote(results, synthetic=False):
    return {"schema": "quadringent.diagnostic.v1", "generated_at": "2026-10-12T19:00:00Z", "scope": "control_plane",
            "synthetic": synthetic, "software": {"product": "quadringent"}, "overall": {}, "results": results}


def _passed(code, layer):
    return {"code": code, "layer": layer, "status": "passed", "required": True, "observed_at": "2026-10-12T19:00:00Z",
            "provenance": "demo", "subject": None, "since": None, "summary": "s", "next_action": "a", "limits": "l",
            "evidence": {}}


def test_a_synthetic_control_plane_report_keeps_the_doctor_report_synthetic(tmp_path):
    body = _remote([_passed("control_plane.api", "control_plane")], synthetic=True)
    _, output, _ = doctor(["--json", "--control-plane"], fetch=lambda *a: (200, body), tmp_path=tmp_path)
    assert json.loads(output)["synthetic"] is True


def test_a_control_plane_report_without_its_own_api_check_is_not_trusted(tmp_path):
    code, output, _ = doctor(["--json", "--control-plane"], fetch=lambda *a: (200, _remote([])), tmp_path=tmp_path)
    control = [r for r in json.loads(output)["results"] if r["code"] == "control_plane.api"]
    assert code != 0 and control and control[0]["status"] in ("unknown", "failed")


def test_a_control_plane_without_diagnostics_packaged_is_explained(tmp_path):
    body = {"error": {"code": "capability_unavailable", "message": "not packaged"}}
    _, output, _ = doctor(["--json", "--control-plane"], fetch=lambda *a: (503, body), tmp_path=tmp_path)
    control = [r for r in json.loads(output)["results"] if r["code"] == "control_plane.api"][0]
    assert "not packaged" in control["summary"] and "database" not in control["next_action"]


@pytest.mark.parametrize("argv", [["--tls"], ["--ibmi-ca-file", "ca.pem"], ["--signon-port", "19476"]])
def test_ibmi_options_without_a_host_are_a_usage_error_not_a_silent_no_op(tmp_path, argv):
    Refuse.calls = 0
    code, output, _ = doctor(argv, tmp_path=tmp_path)
    assert code == 2 and "--check-ibmi" in output
    assert Refuse.calls == 0


@pytest.mark.parametrize("argv", [["--database-port", "0"], ["--command-port", "19475"], ["--pipeline-id", "p-1"]])
def test_more_orphan_options_are_usage_errors(tmp_path, argv):
    code, output, _ = doctor(argv, tmp_path=tmp_path)
    assert code == 2 and ("--check-ibmi" in output or "--control-plane" in output)


def test_ibmi_options_with_a_host_are_accepted(tmp_path):
    code, output, _ = doctor(["--json", "--check-ibmi", "ibmi.synthetic.invalid", "--signon-port", "19476",
                              "--database-port", "19471", "--command-port", "19475"], tmp_path=tmp_path)
    assert code == 1 and parse_report(json.loads(output))


@pytest.mark.parametrize("extra", [["--check-ibmi", "ibmi.synthetic.invalid"], ["--cloud", "aws"], ["--control-plane"]])
def test_a_saved_report_is_never_mixed_with_new_checks(tmp_path, extra):
    saved = tmp_path / "saved.json"
    _, output, _ = doctor(["--json"], tmp_path=tmp_path)
    saved.write_text(output, encoding="utf-8")
    Refuse.calls = 0
    code, output, _ = doctor(["--from-report", str(saved), *extra], tmp_path=tmp_path)
    assert code == 2 and "--from-report" in output and Refuse.calls == 0

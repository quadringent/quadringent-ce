"""Shareable diagnostic export: built from allowed fields, secrets never leave."""

from __future__ import annotations

import gzip
import io
import json
import os
import stat
import tarfile

import pytest

from quadringent.diagnostics.export import (
    ExportPathError,
    build_export,
    write_archive,
)
from quadringent.diagnostics.model import DiagnosticReport, DiagnosticResult, Subject

NOW = "2026-10-12T19:00:00Z"
# Entirely fictional canaries: none of them is a real credential.
CANARIES = {
    "password": "canary-pw-0000-NOT-A-SECRET",
    "token": "qdt_op_CANARYTOKEN0000000000000000",
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJjYW5hcnkifQ.c2lnbmF0dXJlY2FuYXJ5",
    "signed_url": "https://bucket.s3.amazonaws.com/raw/x?X-Amz-Signature=CANARYSIG0000&X-Amz-Credential=AKIACANARY",
    "activation": "https://site.invalid/#/wizard/activate?token=CANARYACTIVATION0000",
    "pem": "-----BEGIN PRIVATE KEY-----\nMIICANARYKEYMATERIAL0000\n-----END PRIVATE KEY-----",
    "connection": "account=canary-acct;user=CANARYUSER;password=canary-pw-1111",
    "host": "ibmi-canary.customer.invalid",
    "account": "canary-snowflake-account",
    "user": "CANARYUSER",
    "cookie": "quadringent_session=CANARYCOOKIE0000",
}


def canary_report() -> DiagnosticReport:
    def item(code, layer, status, subject, **kwargs):
        defaults = dict(observed_at=NOW, provenance="probe:tcp", summary="Synthetic summary.",
                        next_action="Synthetic action.", limits="Synthetic limits.")
        defaults.update(kwargs)
        return DiagnosticResult(code=code, layer=layer, status=status, required=True, subject=subject, **defaults)

    return DiagnosticReport(
        generated_at=NOW, scope="control_plane", synthetic=True,
        software={"product": "quadringent", "version": "0.3.5", "distribution": "checkout"},
        results=(
            item("network.tcp.database", "network", "failed", Subject("host", CANARIES["host"], "database 9471/tcp"),
                 summary=f"TCP to {CANARIES['host']} failed for {CANARIES['user']}.",
                 evidence={"port": 9471, "error": f"refused url={CANARIES['signed_url']}"}),
            item("destination.verification", "destination", "failed", Subject("destination", "dst_canary1", CANARIES["account"]),
                 summary=f"Login failed: {CANARIES['connection']}",
                 next_action=f"Open {CANARIES['activation']} then retry with Bearer {CANARIES['token']}",
                 evidence={"detail": f"Traceback: ValueError({CANARIES['password']}) {CANARIES['jwt']}"}),
            item("pipeline.state", "capture", "failed", Subject("pipeline", "pl_canary1", "SALESLIB.CUSTOMER_TABLE"),
                 limits=f"cookie {CANARIES['cookie']} {CANARIES['pem']}",
                 evidence={"attention_reason": f"Exception: {CANARIES['pem']}"}),
            item("delivery.snowflake", "delivery", "unknown", Subject("pipeline", "pl_canary1", "SALESLIB.CUSTOMER_TABLE"),
                 since="2026-10-12T18:00:00Z", evidence={"evidence_kind": "simulation"}),
        ),
    )


# The control plane knows its own sensitive identities (IBM i user...).
KNOWN = [("user", CANARIES["user"])]


def all_text(bundle) -> str:
    return "\n".join(content.decode("utf-8") for content in bundle.files.values())


def test_no_canary_survives_in_any_exported_file():
    text = all_text(build_export(canary_report(), extra_identifiers=KNOWN))
    for name, value in CANARIES.items():
        for fragment in value.splitlines():
            assert fragment not in text, f"{name} leaked: {fragment}"
    assert "CANARY" not in text.upper().replace("SYNTHETIC", "")


def test_identifiers_are_pseudonymized_consistently_within_one_export():
    bundle = build_export(canary_report(), extra_identifiers=KNOWN)
    report = json.loads(bundle.files["report.json"])
    pipeline_refs = {r["subject"]["ref"] for r in report["results"] if r["subject"]["kind"] == "pipeline"}
    assert len(pipeline_refs) == 1 and next(iter(pipeline_refs)).startswith("pipeline-")
    labels = {r["subject"]["label"] for r in report["results"] if r["subject"]["kind"] == "pipeline"}
    assert labels == {next(iter(pipeline_refs))}
    assert bundle.preview["pseudonymized"]["hosts"] == 1


def test_every_omission_is_explained():
    preview = build_export(canary_report(), extra_identifiers=KNOWN).preview
    reasons = {item["reason"] for item in preview["omitted"]}
    assert any("secret" in reason for reason in reasons)
    assert any("pseudonymized" in reason for reason in reasons)
    assert all(item["field"] for item in preview["omitted"])


def test_manifest_lists_every_file_with_its_checksum():
    bundle = build_export(canary_report(), extra_identifiers=KNOWN)
    manifest = json.loads(bundle.files["manifest.json"])
    import hashlib
    for entry in manifest["files"]:
        assert hashlib.sha256(bundle.files[entry["name"]]).hexdigest() == entry["sha256"]
        assert len(bundle.files[entry["name"]]) == entry["bytes"]
    assert {entry["name"] for entry in manifest["files"]} == set(bundle.files) - {"manifest.json"}
    assert manifest["synthetic"] is True and manifest["limits"]


def test_timeline_is_bounded_and_ordered():
    bundle = build_export(canary_report(), extra_identifiers=KNOWN, max_timeline=2)
    timeline = json.loads(bundle.files["timeline.json"])
    assert len(timeline["events"]) == 2 and timeline["truncated"] is True
    assert [event["at"] for event in timeline["events"]] == sorted(event["at"] for event in timeline["events"])


def test_identifiers_can_be_kept_only_on_explicit_request():
    bundle = build_export(canary_report(), keep_identifiers=True)
    assert CANARIES["host"] in all_text(bundle)
    assert CANARIES["token"] not in all_text(bundle)


def test_archive_bytes_contain_no_canary_and_are_private(tmp_path):
    target = tmp_path / "diagnostic.tar.gz"
    write_archive(build_export(canary_report(), extra_identifiers=KNOWN), target)
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
    raw = target.read_bytes()
    decompressed = gzip.decompress(raw)
    for value in CANARIES.values():
        for fragment in value.splitlines():
            assert fragment.encode() not in raw and fragment.encode() not in decompressed
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        names = archive.getnames()
        assert all("/" in name and ".." not in name for name in names)
        assert all(member.isfile() for member in archive.getmembers())


def test_archive_refuses_overwrite_symlink_and_missing_parent(tmp_path):
    bundle = build_export(canary_report(), extra_identifiers=KNOWN)
    existing = tmp_path / "existing.tar.gz"
    existing.write_text("keep")
    with pytest.raises(ExportPathError):
        write_archive(bundle, existing)
    assert existing.read_text() == "keep"
    link = tmp_path / "link.tar.gz"
    link.symlink_to(tmp_path / "elsewhere.tar.gz")
    with pytest.raises(ExportPathError):
        write_archive(bundle, link)
    assert not (tmp_path / "elsewhere.tar.gz").exists()
    with pytest.raises(ExportPathError):
        write_archive(bundle, tmp_path / "missing" / "x.tar.gz")
    with pytest.raises(ExportPathError):
        write_archive(bundle, tmp_path / "x.zip")


def test_export_document_for_the_cockpit_has_a_preview_and_text_files():
    document = build_export(canary_report(), extra_identifiers=KNOWN).to_document()
    assert document["schema"] == "quadringent.diagnostic-export.v1"
    assert {item["name"] for item in document["preview"]["files"]} == set(document["files"])
    assert all(isinstance(text, str) for text in document["files"].values())


# Formats beyond the canaries above (independent review): none may survive.
import base64  # noqa: E402

TRAPS = {
    "github": "token ghp_abcdefghijklmnopqrstuvwxyz0123456789AB",
    "anthropic": "key sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAA",
    "aws_secret": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "slack": "xox" "b-12345678901-1234567890123-abcdefghijklmnopqrstuvwx",
    "path": "see /home/jdoe/.aws/credentials and C:\\Users\\jdoe\\secrets.txt",
    "ip": "cannot reach 198.51.100.23:9476",
    "host_upper": "AS400.CORP.EXAMPLE.COM refused",
    "b64_key": base64.b64encode(b"-----BEGIN PRIVATE KEY-----\nMIIE...\n-----END PRIVATE KEY-----").decode(),
    "hex": "deadbeef" * 8,
    "email": "contact jdoe@corp.example.com",
    "url_no_scheme": "grafana.corp.example.com/d/abc?auth=SECRET123",
    "password_is": "password is hunter2",
    "account_words": "account xy12345.eu-west-1 failed for user SVC_QDT",
    "dsn": "postgres://u:p@h/db",
    "json_password": '{"password": "hunter2"}',
    "aws_session": "FwoGZXIvYXdzEJr//////////wEaDExampleSessionTokenExampleSessionToken0000000000",
}
NEEDLES = ["ghp_", "sk-ant", "wJalr", "xoxb", "jdoe", "198.51.100.23", "corp.example.com", "SECRET123", "hunter2",
           "SVC_QDT", "xy12345", "MIIE", "LS0tLS1CRUdJTi", "ExampleSessionToken", "deadbeef", "u:p@h"]


def _trap_report(text):
    return DiagnosticReport(
        generated_at=NOW, scope="local", synthetic=False,
        software={"product": "quadringent", "version": "0.3.5", "hostname": "as400.corp.example.com",
                  "distribution": f"password=hunter2 {text}"},
        results=(DiagnosticResult(
            code="network.tcp.database", layer="network", status="failed", required=True, observed_at=NOW,
            provenance="probe:" + text[:20].replace(" ", "_"), summary=text, next_action=text, limits=text,
            subject=Subject("host", "as400.corp.example.com", f"reader at as400.corp.example.com {text[:40]}"),
            evidence={"error": text[:100], "found": True, "tool": text[:100]}),),
    )


@pytest.mark.parametrize("name", sorted(TRAPS))
def test_secret_and_identity_formats_never_survive(name):
    text = TRAPS[name]
    bundle = build_export(_trap_report(text), extra_identifiers=[("host", "as400.corp.example.com"), ("user", "jdoe")])
    exported = "\n".join(content.decode() for content in bundle.files.values()).lower()
    for needle in NEEDLES:
        if needle.lower() in text.lower() or needle in ("hunter2", "corp.example.com"):
            assert needle.lower() not in exported, f"{name}: {needle}"


def test_software_keeps_only_allowed_fields():
    bundle = build_export(_trap_report("x"))
    report = json.loads(bundle.files["report.json"])
    assert set(report["software"]) <= {"product", "version", "distribution"}
    assert "distribution" not in report["software"]  # not one of the known distributions
    assert "as400.corp.example.com" not in bundle.files["manifest.json"].decode()  # the omission names the key, never the value


def test_evidence_values_are_restricted_to_known_vocabularies():
    report = DiagnosticReport(
        generated_at=NOW, scope="local", synthetic=False, software={"product": "quadringent"},
        results=(DiagnosticResult(
            code="network.tcp.database", layer="network", status="failed", required=True, observed_at=NOW,
            provenance="probe:tcp", summary="s", next_action="a", limits="l",
            evidence={"error": "connection refused", "tool": "terraform", "stage_state": "stale",
                      "port": 9471, "readiness": "/etc/passwd"}),),
    )
    evidence = json.loads(build_export(report).files["report.json"])["results"][0]["evidence"]
    assert evidence == {"error": "connection refused", "tool": "terraform", "stage_state": "stale", "port": 9471}


def test_fixed_operator_guidance_is_not_mangled():
    report = DiagnosticReport(
        generated_at=NOW, scope="local", synthetic=False, software={"product": "quadringent"},
        results=(DiagnosticResult(
            code="control_plane.api", layer="control_plane", status="failed", required=True, observed_at=NOW,
            provenance="doctor:control-plane", summary="The control plane answered with a report outside quadringent.diagnostic.v1.",
            next_action="Read one pipeline at a time with ?pipeline_id= (or doctor --pipeline-id).",
            limits="Run quadringent doctor --check-ibmi HOST --tls, optionally with --ibmi-ca-file."),),
    )
    item = json.loads(build_export(report).files["report.json"])["results"][0]
    assert item["summary"].endswith("quadringent.diagnostic.v1.")
    assert "?pipeline_id=" in item["next_action"] and "--ibmi-ca-file" in item["limits"]


def test_subject_labels_and_fixed_refs_leave_only_in_known_shapes():
    def item(code, subject):
        return DiagnosticResult(code=code, layer="local_config", status="passed", required=False, observed_at=NOW,
                                provenance="local:environment", summary="s", next_action="a", limits="l", subject=subject)

    report = DiagnosticReport(
        generated_at=NOW, scope="local", synthetic=False, software={"product": "quadringent"},
        results=(
            item("local_config.cloud_credentials", Subject("tool", "aws", "profile FICTIONAL-PROFILE-7731")),
            item("local_tools.custom", Subject("tool", "fictional-profile-7731")),
            item("control_plane.api", Subject("control_plane", "v2", "operator fictional-operator-7731")),
            item("network.tcp.database", Subject("host", "ibmi.synthetic.invalid", "database 9471/tcp")),
            item("network.tcp.signon", Subject("host", "ibmi.synthetic.invalid", "reader of fictional-operator-7731")),
        ),
    )
    bundle = build_export(report)
    exported = "\n".join(content.decode() for content in bundle.files.values()).lower()
    assert "fictional-profile-7731" not in exported and "fictional-operator-7731" not in exported
    subjects = [r["subject"] for r in json.loads(bundle.files["report.json"])["results"]]
    assert subjects[0] == {"kind": "tool", "ref": "aws", "label": None}
    assert subjects[2]["ref"] == "v2" and subjects[2]["label"] is None
    assert subjects[3]["label"] == "database 9471/tcp"
    assert subjects[4]["label"] is None
    reasons = {o["field"]: o["reason"] for o in json.loads(bundle.files["manifest.json"])["omitted"]}
    assert "local_config.cloud_credentials.subject.label" in reasons and "local_tools.custom.subject.ref" in reasons


def test_keeping_identifiers_never_keeps_free_text_labels():
    def item(code, subject):
        return DiagnosticResult(code=code, layer="network", status="failed", required=True, observed_at=NOW,
                                provenance="probe:tcp", summary="s", next_action="a", limits="l", subject=subject)

    report = DiagnosticReport(
        generated_at=NOW, scope="local", synthetic=False, software={"product": "quadringent"},
        results=(item("network.tcp.signon", Subject("host", "ibmi.synthetic.invalid", "reader fictional-7731")),
                 item("local_config.cloud_credentials", Subject("tool", "aws", "profile fictional-7731"))),
    )
    bundle = build_export(report, keep_identifiers=True)
    exported = "\n".join(content.decode() for content in bundle.files.values())
    assert "fictional-7731" not in exported and "ibmi.synthetic.invalid" in exported
    reasons = {o["field"]: o["reason"] for o in json.loads(bundle.files["manifest.json"])["omitted"]}
    assert reasons["network.tcp.signon.subject.label"] == "free-text label not exported"

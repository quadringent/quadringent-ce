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

"""The release verification page must match the release workflow it describes."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/release.yml"
VERIFY = ROOT / "docs/product/verify-a-release.md"
GOVERNANCE = ROOT / "docs/governance.md"
SUPPORT = ROOT / "SUPPORT.md"
SECURITY = ROOT / "SECURITY.md"
OWNED = (VERIFY, GOVERNANCE, SUPPORT, SECURITY)

CONTACT = "quadringent@proton.me"
REPOSITORY = "quadringent/quadringent-ce"
IMAGE = f"ghcr.io/{REPOSITORY}"
ISSUER = "https://token.actions.githubusercontent.com"
COMPONENTS = ("capture", "control-plane", "verifier")

# Pages written by other authors of the same documentation set.
W1_SIBLING_PAGES = {
    "docs/product/ibm-i-footprint.md",
    "docs/product/your-data.md",
}


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def flat(path: Path) -> str:
    return re.sub(r"\s+", " ", text(path))


@pytest.fixture(scope="module")
def workflow_text() -> str:
    return text(WORKFLOW)


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(text(WORKFLOW))


def image_steps(workflow: dict) -> list[dict]:
    return workflow["jobs"]["images"]["steps"]


# --- image repository, tags ---------------------------------------------------

def test_image_repository_is_the_one_the_page_cites(workflow):
    registry = next(step for step in image_steps(workflow) if step.get("id") == "registry")
    assert 'image="ghcr.io/$owner/quadringent-ce"' in registry["run"]
    page = text(VERIFY)
    assert IMAGE in page
    # The owner/name pair in the identity and in the image path is the same repository.
    assert f"https://github\\.com/{REPOSITORY}/" in page


def test_tag_suffixes_match_the_build_steps(workflow):
    builds = [s for s in image_steps(workflow) if s.get("uses", "").startswith("docker/build-push-action@")]
    suffixes = []
    for build in builds:
        match = re.search(r"outputs\.version }}-([a-z-]+)\s*$", build["with"]["tags"].strip())
        assert match, build["with"]["tags"]
        suffixes.append(match.group(1))
    assert sorted(suffixes) == sorted(COMPONENTS)
    page = text(VERIFY)
    for suffix in COMPONENTS:
        assert f"<tag>-{suffix}" in page


def test_oci_labels_cited_by_the_page_are_set_by_the_build(workflow):
    builds = [s for s in image_steps(workflow) if s.get("uses", "").startswith("docker/build-push-action@")]
    page = text(VERIFY)
    for label in ("source", "version", "revision"):
        name = f"org.opencontainers.image.{label}"
        assert name in page
        assert all(name in build["with"]["labels"] for build in builds)


# --- signing method -----------------------------------------------------------

def test_signing_is_keyless_cosign_of_exactly_three_digests(workflow):
    job = workflow["jobs"]["images"]
    assert job["permissions"]["id-token"] == "write"
    sign = next(step["run"] for step in job["steps"] if "cosign sign" in step.get("run", ""))
    commands = [line.strip() for line in sign.splitlines() if line.strip().startswith("cosign sign")]
    assert len(commands) == 3
    assert all(line.startswith("cosign sign --yes ") and "--key" not in line for line in commands)
    assert all("${IMAGE}@${{ steps." in line for line in commands)
    page = flat(VERIFY)
    assert "cosign sign --yes" in page
    assert "keyless" in page
    assert "three image digests" in page


def test_verify_command_uses_the_workflow_identity_and_issuer(workflow_text):
    page = text(VERIFY)
    workflow_path = ".github/workflows/release.yml"
    assert WORKFLOW == ROOT / workflow_path
    assert "--certificate-identity-regexp" in page
    assert f"--certificate-oidc-issuer {ISSUER}" in page
    regexp = re.search(r"--certificate-identity-regexp '([^']+)'", page).group(1)
    identity = f"https://github.com/{REPOSITORY}/{workflow_path}@refs/tags/v0.3.5"
    assert re.search(regexp, identity)
    # It must reject other repositories, other workflows, branches and bad tags.
    for other in (
        f"https://github.com/other/{REPOSITORY.split('/')[1]}/{workflow_path}@refs/tags/v0.3.5",
        f"https://github.com/{REPOSITORY}/.github/workflows/ci.yml@refs/tags/v0.3.5",
        f"https://github.com/{REPOSITORY}/{workflow_path}@refs/heads/main",
        f"https://github.com/{REPOSITORY}/{workflow_path}@refs/tags/v0.3.5-x",
        f"https://github.com/{REPOSITORY}/{workflow_path}@refs/tags/latest",
    ):
        assert not re.search(regexp, other), other
    # The identity is a tag of the workflow file: the workflow runs only from a tag.
    assert "workflow_dispatch" in workflow_text
    assert 'test "$GITHUB_REF" = "refs/tags/$RELEASE_TAG"' in workflow_text
    assert f"{ISSUER}" in page and "id-token: write" in workflow_text


def test_the_workflow_signs_nothing_else(workflow_text):
    """The page says only images are signed and nothing is attested; keep that true."""
    for forbidden in ("sign-blob", "attest", "slsa", "sbom-attach", "cosign attach", "cosign attest"):
        assert forbidden not in workflow_text.lower(), forbidden
    signing_steps = [s for s in yaml.safe_load(workflow_text)["jobs"]["images"]["steps"] if "cosign" in s.get("run", "")]
    assert len(signing_steps) == 1
    assert workflow_text.count("provenance: false") == 3
    page = flat(VERIFY)
    assert "What is not signed today" in page
    assert "provenance: false" in page


# --- workflow gates cited by the page and by governance.md -------------------

PUBLISHING_JOBS = ("images", "chart", "release")


def test_workflow_is_started_by_hand_only(workflow):
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"workflow_dispatch"}
    assert "No tag push publishes automatically" in flat(ROOT / "docs/releasing.md")
    assert "started by hand" in flat(GOVERNANCE)


@pytest.mark.parametrize("job", PUBLISHING_JOBS)
def test_publishing_jobs_require_the_approval_environment_and_variable(workflow, job):
    definition = workflow["jobs"][job]
    assert definition["environment"] == "publication-approved"
    assert "vars.PUBLICATION_APPROVED == 'true'" in definition["if"]
    # The jobs publish only after validation.
    assert "validate" in definition["needs"]
    page = flat(GOVERNANCE)
    assert "`PUBLICATION_APPROVED=true`" in page and "`publication-approved` environment" in page


def test_validate_job_checks_the_version_and_ci_for_the_exact_commit(workflow):
    validate = workflow["jobs"]["validate"]
    assert "environment" not in validate
    runs = "\n".join(step.get("run", "") for step in validate["steps"])
    assert 'python scripts/sync_version.py --check --release "$RELEASE_TAG"' in runs
    assert 'python scripts/check_release_ci.py --repository "$GITHUB_REPOSITORY" --sha "$GITHUB_SHA"' in runs
    assert "refs/tags/$RELEASE_TAG" in runs
    for job in PUBLISHING_JOBS:
        assert "validate" in workflow["jobs"][job]["needs"]
    page = flat(VERIFY)
    assert "scripts/sync_version.py --check --release" in page and "scripts/check_release_ci.py" in page


def test_arm64_vulnerability_scan_is_a_gate_that_is_not_published(workflow):
    step = next(s for s in image_steps(workflow) if "--platform linux/arm64" in s.get("run", "")
                and "--scanners vuln" in s.get("run", ""))
    run = step["run"]
    assert "--severity HIGH,CRITICAL" in run
    assert "--ignore-unfixed --exit-code 1" in run
    assert "trivy-${name}-arm64-vuln.json" in run and "RUNNER_TEMP" in run
    assert "arm64-vuln" not in text(WORKFLOW).split("SBOM and scan reports artifact")[1].split("- name:")[0]


# --- published assets ---------------------------------------------------------

def test_release_assets_cited_by_the_page_are_uploaded_by_the_workflow(workflow, workflow_text):
    release = next(s for s in workflow["jobs"]["release"]["steps"] if "files" in s.get("with", {}))
    patterns = [line.strip() for line in release["with"]["files"].splitlines() if line.strip()]
    assert patterns == [
        "supply-chain/*",
        "packages/**/*.whl",
        "packages/**/*.tar.gz",
        "packages/**/*.sha256",
        "packages/*.tgz",
    ]
    assert release["with"]["draft"] is True
    sbom_upload = next(s for s in image_steps(workflow) if s.get("name") == "SBOM and scan reports artifact")
    uploaded = [line.strip() for line in sbom_upload["with"]["path"].splitlines() if line.strip()]
    assert uploaded == ["*.sarif", "*.spdx.json", "images.json", "release-manifest.json"]

    page = text(VERIFY)
    for asset in ("images.json", "release-manifest.json", "quadringent-<version>-py3-none-any.whl",
                  "quadringent-<version>.tar.gz", "quadringent-<version>.tgz",
                  "quadringent-demo-<version>.tar.gz"):
        assert asset in page, asset
    for component in COMPONENTS:
        assert f"trivy-{component}.sarif" in workflow_text
    assert "trivy-<component>.sarif" in page
    assert "quadringent-<tag>-<component>.spdx.json" in page
    assert "quadringent-<tag>-<component>-arm64.spdx.json" in page


def test_sbom_and_report_names_match_the_workflow(workflow, workflow_text):
    names = re.findall(r"quadringent-\$\{\{ steps\.version\.outputs\.version \}\}-([a-z-]+)\.spdx\.json", workflow_text)
    assert sorted(names) == sorted(COMPONENTS)
    arm64 = re.findall(r'quadringent-\$\{VERSION\}-([a-z-]+)-arm64\.spdx\.json', workflow_text)
    assert sorted(arm64) == sorted(COMPONENTS)
    # The tag, with its leading v, is the version used in these names.
    version_step = next(s for s in image_steps(workflow) if s.get("id") == "version")
    assert "needs.validate.outputs.tag" in version_step["env"]["VERSION_INPUT"]
    sarif = re.findall(r"output: trivy-([a-z-]+)\.sarif", workflow_text)
    assert sorted(sarif) == sorted(COMPONENTS)


def test_vulnerability_gate_cited_by_the_page(workflow):
    scans = [s for s in image_steps(workflow) if s.get("name", "").startswith("Scan Trivy")]
    assert len(scans) == 3
    for scan in scans:
        assert scan["with"]["severity"] == "HIGH,CRITICAL"
        assert scan["with"]["ignore-unfixed"] is True
        assert scan["with"]["exit-code"] == "1"
        assert scan["with"]["input"].endswith("-amd64")
    release_needs = workflow["jobs"]["release"]["needs"]
    assert "images" in release_needs and "chart" in release_needs
    page = flat(VERIFY)
    for phrase in ("HIGH and CRITICAL", "ignore-unfixed", "amd64 view", "arm64 vulnerability scan"):
        assert phrase in page


def test_wheel_sdist_and_chart_have_no_signature_or_checksum(workflow, workflow_text):
    """The page says these files are unsigned and have no checksum file. Keep that true."""
    chart_steps = workflow["jobs"]["chart"]["steps"]
    checksum_lines = [line for line in workflow_text.splitlines() if ".sha256" in line and "archive_sha256" not in line]
    assert checksum_lines, "the demo checksum copy disappeared; update the page"
    assert all("quadringent-demo-" in line or "dist/*.sha256" in line or "packages/**/*.sha256" in line
               for line in checksum_lines), checksum_lines
    assert not any("sha256sum" in step.get("run", "") and "dist/" in step.get("run", "")
                   and "demo" not in step.get("run", "") for step in chart_steps)
    package = next(s for s in chart_steps if s.get("name") == "Package the chart for release")
    assert "cosign" not in package["run"]
    page = text(VERIFY)
    assert re.search(r"\| Wheel \(`\.whl`\) \| No \| No \|", page)
    assert re.search(r"\| Sdist \(`\.tar\.gz`\) \| No \| No \|", page)
    assert re.search(r"\| Helm chart archive \(`\.tgz`\) \| No \| No \|", page)
    assert re.search(r"\| Synthetic demo archive \| No \| Yes", page)


def test_demo_checksum_format_matches_the_verifier():
    verifier = text(ROOT / "scripts/verify_demo_release.py")
    assert 'checksum != f"{digest}  {archive.name}"' in verifier
    page = text(VERIFY)
    assert "<sha256>  <file name>" in page
    assert "sha256sum -c quadringent-demo-<version>.tar.gz.sha256" in page
    assert 'archive.name != f"{name}.tar.gz"' in verifier and 'name = f"quadringent-demo-{version}"' in verifier


def test_images_json_and_manifest_keys_cited_by_the_page(tmp_path):
    import importlib.util

    # Load by path: putting scripts/ on sys.path would shadow real packages.
    spec = importlib.util.spec_from_file_location("_write_release_manifest", ROOT / "scripts/write_release_manifest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    write_release_manifest = module.write_release_manifest

    digests = ["sha256:" + digit * 64 for digit in "123"]
    manifest, images = tmp_path / "release-manifest.json", tmp_path / "images.json"
    write_release_manifest(repository=IMAGE, capture=digests[0], control_plane=digests[1], verifier=digests[2],
                           version="0.0.0", manifest_path=manifest, images_path=images)
    assert set(json.loads(images.read_text())["images"]) == {"capture", "control_plane", "verifier"}
    keys = json.loads(manifest.read_text())
    page = text(VERIFY)
    for key in ("imageDigest", "controlPlaneImageDigest", "verifierImageDigest", "observabilityImageDigest"):
        assert key in keys and key in page
    assert ".images | to_entries[]" in page and ".images[]" in page


def test_chart_and_installer_pins_cited_by_the_page():
    helpers = text(ROOT / "chart/templates/_helpers.tpl")
    assert "image.digest is required" in helpers
    assert "must be an immutable sha256 digest" in text(ROOT / "chart/templates/control-plane.yaml")
    assert "must be a SHA256 digest" in text(ROOT / "chart/templates/verifier-job.yaml")
    assert 'chart-values.generated.yaml' in text(ROOT / "src/quadringent/installer/plan.py")
    assert r"^sha256:[a-f0-9]{64}$" in text(ROOT / "src/quadringent/installer/manifest.py")
    page = text(VERIFY)
    for name in ("image.digest", "controlPlane.image.digest", "verification.imageDigest",
                 "observability.imageDigest", "chart-values.generated.yaml"):
        assert name in page


def test_chart_default_digests_and_installer_overrides_cited_by_the_page():
    values = yaml.safe_load(text(ROOT / "chart/values.yaml"))
    digest = re.compile(r"^sha256:[a-f0-9]{64}$")
    # Required digests are empty; the verifier and observability ones ship a default.
    assert values["image"]["digest"] == ""
    assert values["controlPlane"]["image"]["digest"] == ""
    assert digest.match(values["verification"]["imageDigest"])
    assert digest.match(values["observability"]["imageDigest"])
    runtime = json.loads(text(ROOT / "site/runtime-0.3.5.json"))
    assert values["verification"]["imageDigest"] != runtime["artifacts"]["verifier_image_digest"]
    plan = text(ROOT / "src/quadringent/installer/plan.py")
    assert '"imageDigest": manifest.verifier_image_digest' in plan
    assert "observability_image_digest" not in plan
    page = flat(VERIFY)
    assert "does not override `observability.imageDigest`" in page
    assert "set every digest yourself from `images.json`" in page


def test_installer_writes_values_then_installs_without_a_prompt():
    apply_source = text(ROOT / "src/quadringent/installer/apply.py")
    write = apply_source.index('values_path.write_text(_yaml_dump(chart_values)')
    template = apply_source.index('"helm", "template"', write)
    install = apply_source.index('"helm", "upgrade", "--install"', template)
    assert write < template < install
    assert "confirm_fn(" not in apply_source[write:install]
    cli = text(ROOT / "src/quadringent/installer/cli.py")
    assert 'return Path.home() / ".quadringent" / name' in cli
    assert "(--dry-run: no command executed, no file written)" in cli
    page = flat(VERIFY)
    assert "~/.quadringent/<name>/" in page
    assert "does not stop for confirmation" in page


def test_current_receipt_is_bound_to_published_artifacts_and_not_unreleased_helpers():
    receipts = sorted(path.name for path in (ROOT / "site").glob("*-[0-9]*.json"))
    assert receipts == ["qualification-0.3.4.json", "qualification-0.4.5.json",
                        "recovery-0.3.4.json", "runtime-0.3.5.json"]
    current = json.loads(text(ROOT / "site/qualification-0.4.5.json"))
    page = flat(VERIFY)
    assert current["version"] == "0.4.5"
    assert current["artifact_source_sha"] == "ad30f16ae9346bd332a482dc1d6de84f2b3e75f1"
    assert current["evidence_snapshot_utc"] == "2026-10-15T18:08:54.157733+00:00"
    assert "qualification-0.4.5.json" in page and current["artifact_source_sha"] in page
    assert current["evidence_snapshot_utc"] in page
    assert "Unreleased" in page and "does not qualify the new doctor or local-mode helpers" in page
    assert current["overall_status"] == "PARTIAL_NOT_FULLY_QUALIFIED"
    assert current["volume"]["initial_four_layer_equality"]["source_rows"] == 1_000_000
    for flag in ("completed_24h_proven", "receiver_rotation_proven",
                 "final_journal_raw_history_mirror_oracle_proven", "general_native_cdc_qualified"):
        assert current["endurance"][flag] is False
    assert current["endurance"]["first_burst"]["ten_second_target_passed"] is False
    assert current["latency"]["p95_or_general_sla_qualified"] is False


def test_before_you_start_names_the_tools_and_the_draft_rule():
    page = flat(VERIFY)
    assert "## Before you start" in text(VERIFY)
    for tool in ("`cosign`", "`jq`", "`git`", "`helm`", "`kubectl`"):
        assert tool in page
    assert "a draft is not an available release" in page
    assert "a draft is not an available release" in flat(ROOT / "docs/product/install-default.md")


def test_qualification_fields_cited_by_the_page():
    import json

    receipt = json.loads(text(ROOT / "site/runtime-0.3.5.json"))
    page = text(VERIFY)
    for field in ("version", "source_commit", "artifacts", "qualification_scope", "limitations",
                  "production_qualified", "external_pilot_validated", "universal_latency_guarantee",
                  "provenance"):
        assert field in receipt and f"`{field}`" in page, field
    assert receipt["production_qualified"] is False
    assert receipt["external_pilot_validated"] is False
    assert receipt["universal_latency_guarantee"] is False
    # release.yml uploads no qualification file; the page says so.
    assert "qualification" not in text(WORKFLOW).split("files: |")[1]
    assert "`release.yml` does not upload a qualification file" in flat(VERIFY).replace("\n", " ")


# --- support, security, governance --------------------------------------------

def test_doctor_export_exists_and_the_support_page_cites_it():
    from quadringent.installer.doctor import add_doctor_subcommand

    parser = argparse.ArgumentParser()
    add_doctor_subcommand(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["doctor", "--export", "diagnostic.tar.gz", "--preview"])
    assert args.export == "diagnostic.tar.gz" and args.preview is True
    page = text(SUPPORT)
    assert "quadringent doctor --export diagnostic.tar.gz --preview" in page
    assert "quadringent doctor --export diagnostic.tar.gz " in page
    assert "--keep-identifiers" in page


def test_support_commitments_are_the_allowed_ones_everywhere():
    support, security, governance = flat(SUPPORT), flat(SECURITY), flat(GOVERNANCE)
    assert "acknowledged within 5 business days" in support
    assert "acknowledged within 5 business days" in security
    assert "latest minor release line only" in support
    assert "latest minor release line only" in security
    assert "latest minor release line only" in governance
    assert "best-effort basis" in support
    assert "no service level agreement" in support.lower()
    assert "no paid support" in support.lower() or "paid support is not announced" in support.lower()
    assert "coordinated disclosure" in security.lower()
    assert "bounty" not in security.lower()
    # The 5-day target is stated for reports sent through the private advisory
    # channel; the email path stays best-effort.
    assert "submitted through the private vulnerability reporting channel" in security
    assert "Email is answered on a best-effort basis" in security
    assert "https://github.com/quadringent/quadringent-ce/security/advisories/new" in security
    # Pre-existing claims that other tests rely on stay in place.
    assert "Private vulnerability reporting is enabled" in security
    assert "without vulnerability details" in security
    assert "public issue" in security
    assert "No response time is promised" not in security


def test_support_channels_exist():
    page = text(SUPPORT)
    assert f"https://github.com/{REPOSITORY}/issues" in page
    assert "mailto:" + CONTACT in page
    assert (ROOT / ".github/ISSUE_TEMPLATE/bug_report.yml").is_file()
    assert (ROOT / ".github/ISSUE_TEMPLATE/pilot_interest.yml").is_file()


def test_governance_states_its_one_identity_sentence_and_its_limits():
    page = text(GOVERNANCE)
    assert "Quadringent is maintained under a pseudonymous project identity." in page
    assert "makes no promise about the future of the maintainers" in flat(GOVERNANCE)
    for decision in (ROOT / "docs/decisions").glob("*.md"):
        assert decision.name in flat(GOVERNANCE) or "docs/decisions/" in page
    # Release rules cited by the page exist in docs/releasing.md.
    releasing = flat(ROOT / "docs/releasing.md")
    for phrase in ("PUBLICATION_APPROVED=true", "publication-approved", "No tag push publishes automatically",
                   "all seven jobs successful"):
        assert phrase in releasing
    workflow = text(WORKFLOW)
    assert "publication-approved" in workflow and "PUBLICATION_APPROVED" in workflow
    for dockerfile in ("docker/Dockerfile", "docker/control-plane.Dockerfile", "docker/verifier.Dockerfile"):
        assert (ROOT / dockerfile).is_file() and dockerfile in page
        assert re.search(r"^FROM \S+@sha256:[0-9a-f]{64}", text(ROOT / dockerfile), re.M)
    assert "ghcr.io/<owner>/quadringent-ce" in page
    assert (ROOT / "LICENSE").is_file()
    assert "Apache License" in text(ROOT / "LICENSE")


# --- hygiene of the files owned here ------------------------------------------

@pytest.mark.parametrize("path", OWNED, ids=lambda p: p.name)
def test_relative_links_resolve(path):
    body = text(path)
    # Ignore fenced code blocks, where brackets are not links.
    prose = re.sub(r"```.*?```", "", body, flags=re.S)
    missing = []
    own_headings = {re.sub(r"[^a-z0-9 -]", "", h.lower()).strip().replace(" ", "-")
                    for h in re.findall(r"^#+\s+(.*)$", body, re.M)}
    for target in re.findall(r"\]\(([^)\s]+)\)", prose):
        if target.startswith("#"):
            assert target[1:] in own_headings, f"{path.name}: missing anchor {target}"
            continue
        if re.match(r"^[a-z][a-z0-9+.-]*:", target):
            continue
        file_part, _, fragment = target.partition("#")
        resolved = (path.parent / file_part).resolve()
        if resolved.is_dir():
            continue
        if resolved.is_file():
            if fragment and resolved.suffix == ".md":
                headings = {re.sub(r"[^a-z0-9 -]", "", h.lower()).strip().replace(" ", "-")
                            for h in re.findall(r"^#+\s+(.*)$", text(resolved), re.M)}
                assert fragment in headings, f"{path.name}: missing anchor {target}"
            continue
        relative = resolved.relative_to(ROOT).as_posix()
        if relative not in W1_SIBLING_PAGES:
            missing.append(target)
    assert not missing, f"{path.name}: unresolved links {missing}"


@pytest.mark.parametrize("path", OWNED, ids=lambda p: p.name)
def test_source_comments_point_to_existing_files(path):
    comments = re.findall(r"<!-- source: ([^ )]+)", text(path))
    if path in (VERIFY, GOVERNANCE, SUPPORT):
        assert comments, f"{path.name} cites no source"
    for comment in comments:
        assert (ROOT / comment).exists(), f"{path.name}: {comment}"


@pytest.mark.parametrize("path", OWNED, ids=lambda p: p.name)
def test_public_text_names_no_private_identity(path):
    body = text(path)
    assert set(re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", body)) <= {CONTACT}
    assert "/Users/" not in body and "/home/" not in body
    assert not re.search(r"(?<![\w.:-])\d{12}(?![\w-])", body), "possible cloud account id"
    for banned in ("world-class", "best-in-class", "industry-leading", "unparalleled", "seamless",
                   "enterprise-grade", "bulletproof", "military-grade", "cutting-edge", "100%"):
        assert banned not in body.lower(), banned

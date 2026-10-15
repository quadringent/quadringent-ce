"""Community files stay valid, linked and free of private identities.

Covers the issue forms, the pull request template, ROADMAP.md, CONTRIBUTING.md and
the Unreleased section of CHANGELOG.md. The checks read the repository only and
open no network connection: external URLs are checked for shape, not fetched.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

import pytest
import yaml
from check_publication import scan_text

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / ".github/ISSUE_TEMPLATE"
FORMS = [TEMPLATES / "bug_report.yml", TEMPLATES / "pilot_interest.yml",
         TEMPLATES / "compatibility_report.yml", TEMPLATES / "question.yml"]
NEW_FORMS = [TEMPLATES / "compatibility_report.yml", TEMPLATES / "question.yml"]
PAGES = [ROOT / "ROADMAP.md", ROOT / "CONTRIBUTING.md"]
PUBLIC_FILES = [*PAGES, ROOT / "CHANGELOG.md", ROOT / ".github/pull_request_template.md",
                TEMPLATES / "config.yml", *NEW_FORMS]
FORM_TYPES = {"markdown", "input", "textarea", "dropdown", "checkboxes"}
REPOSITORY_URL = "https://github.com/quadringent/quadringent-ce"
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")
HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _slug(heading: str) -> str:
    text = re.sub(r"[`*]", "", heading.strip().lower())
    return re.sub(r"[^\w\- ]", "", text).replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    anchors, in_fence = set(), False
    for line in _read(path).splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
        elif not in_fence and (match := HEADING.match(line)):
            anchors.add(_slug(match.group(1)))
    return anchors


def broken_links(page: Path, root: Path = ROOT) -> list[str]:
    broken = []
    for target in LINK.findall(_read(page)):
        if re.match(r"^[a-z][a-z0-9+.-]*:", target):
            continue
        path_part, _, fragment = target.partition("#")
        resolved = page if not path_part else (page.parent / path_part).resolve()
        if not resolved.exists() or (root not in resolved.parents and resolved != root):
            broken.append(target)
        elif fragment and resolved.suffix == ".md" and fragment not in _anchors(resolved):
            broken.append(target)
    return broken


def _load_form(path: Path) -> dict:
    data = yaml.safe_load(_read(path))
    assert isinstance(data, dict), f"{path.name}: the form must be a YAML mapping"
    return data


def _repository_file_from_url(url: str) -> Path | None:
    prefix = f"{REPOSITORY_URL}/blob/main/"
    if url.startswith(prefix):
        return ROOT / url[len(prefix):].split("#")[0]
    return None


# --- issue forms ------------------------------------------------------------------------------


@pytest.mark.parametrize("path", FORMS, ids=lambda p: p.name)
def test_issue_form_has_the_required_github_keys(path: Path) -> None:
    form = _load_form(path)
    assert isinstance(form.get("name"), str) and form["name"].strip()
    assert isinstance(form.get("description"), str) and form["description"].strip()
    body = form.get("body")
    assert isinstance(body, list) and body, f"{path.name}: body must be a non-empty list"
    assert any(item.get("type") != "markdown" for item in body), "a form needs at least one input"
    ids = []
    for item in body:
        kind = item.get("type")
        assert kind in FORM_TYPES, f"{path.name}: unsupported type {kind!r}"
        attributes = item.get("attributes")
        assert isinstance(attributes, dict), f"{path.name}: {kind} needs attributes"
        if kind == "markdown":
            assert isinstance(attributes.get("value"), str) and attributes["value"].strip()
            continue
        assert isinstance(attributes.get("label"), str) and attributes["label"].strip()
        if kind == "dropdown":
            options = attributes.get("options")
            assert isinstance(options, list) and options and all(isinstance(o, str) for o in options)
            assert len(set(options)) == len(options), f"{path.name}: duplicate dropdown option"
        if kind == "checkboxes":
            options = attributes.get("options")
            assert isinstance(options, list) and options
            assert all(isinstance(o, dict) and isinstance(o.get("label"), str) for o in options)
        if "id" in item:
            ids.append(item["id"])
            assert re.fullmatch(r"[A-Za-z0-9_-]+", item["id"])
        validations = item.get("validations", {})
        assert set(validations) <= {"required"}
        assert isinstance(validations.get("required", False), bool)
    assert len(ids) == len(set(ids)), f"{path.name}: duplicate ids"


@pytest.mark.parametrize("path", FORMS, ids=lambda p: p.name)
def test_issue_form_title_prefix_is_a_string_when_present(path: Path) -> None:
    form = _load_form(path)
    assert isinstance(form.get("title", ""), str)
    assert isinstance(form.get("labels", []), list)


def test_existing_issue_forms_are_kept() -> None:
    assert (TEMPLATES / "bug_report.yml").is_file()
    assert (TEMPLATES / "pilot_interest.yml").is_file()


def test_compatibility_report_asks_for_what_the_page_needs() -> None:
    form = _load_form(TEMPLATES / "compatibility_report.yml")
    items = {item["id"]: item for item in form["body"] if "id" in item}
    for key in ("version", "ibmi_release", "tr_level", "journal_setup", "tables", "ccsids",
                "cloud", "snowflake_edition", "worked", "failed", "doctor", "confirmation"):
        assert key in items, key
    assert items["ibmi_release"]["attributes"]["options"][:4] == ["7.3 or earlier", "7.4", "7.5", "7.6"]
    assert "doctor --export" in items["doctor"]["attributes"]["description"]
    # The optional archive is never required.
    assert not items["doctor"].get("validations", {}).get("required", False)
    required_boxes = [o for o in items["confirmation"]["attributes"]["options"] if o.get("required")]
    assert any("no secrets" in o["label"] for o in required_boxes)
    assert "publicly visible" in form["body"][0]["attributes"]["value"]


def test_compatibility_report_requires_only_the_essentials() -> None:
    form = _load_form(TEMPLATES / "compatibility_report.yml")
    required = {item["id"] for item in form["body"]
                if item.get("validations", {}).get("required", False)}
    assert required == {"version", "ibmi_release", "worked"}


def test_question_form_points_to_the_faq_and_forbids_secrets() -> None:
    form = _load_form(TEMPLATES / "question.yml")
    text = yaml.safe_dump(form)
    assert "docs/product/faq.md" in text
    assert "publicly visible" in text
    boxes = next(i for i in form["body"] if i["type"] == "checkboxes")["attributes"]["options"]
    assert any("no secrets" in o["label"] and o.get("required") for o in boxes)


def test_config_points_security_to_the_policy_and_questions_to_the_faq() -> None:
    config = _load_form(TEMPLATES / "config.yml")
    assert config["blank_issues_enabled"] is False
    urls = {link["url"] for link in config["contact_links"]}
    assert f"{REPOSITORY_URL}/security/advisories/new" in urls
    assert f"{REPOSITORY_URL}/blob/main/docs/product/faq.md" in urls
    assert any(url.startswith("mailto:") for url in urls)
    for link in config["contact_links"]:
        assert set(link) == {"name", "url", "about"}
        local = _repository_file_from_url(link["url"])
        assert local is None or local.is_file(), link["url"]


def test_form_links_to_repository_files_resolve() -> None:
    for path in NEW_FORMS:
        for url in re.findall(r"\(([^)\s]+)\)", _read(path)):
            local = _repository_file_from_url(url)
            assert local is None or local.is_file(), f"{path.name}: {url}"


def test_form_used_in_a_link_exists() -> None:
    for page in PAGES:
        for name in re.findall(r"issues/new\?template=([A-Za-z0-9_.-]+)", _read(page)):
            assert (TEMPLATES / name).is_file(), f"{page.name}: no issue form {name}"


# --- pull request template --------------------------------------------------------------------


def test_pull_request_template_covers_the_four_questions() -> None:
    text = _read(ROOT / ".github/pull_request_template.md")
    for heading in ("## What changed", "## Tests and checks run", "## Documentation", "## Safety"):
        assert heading in text, heading
    assert "- [ ] No secrets" in text
    assert "identifiers" in text


# --- ROADMAP and CONTRIBUTING -----------------------------------------------------------------


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_every_relative_link_resolves(page: Path) -> None:
    assert LINK.findall(_read(page)), f"{page.name}: no link at all"
    assert broken_links(page) == []


def test_broken_link_helper_detects_missing_file_and_anchor(tmp_path: Path) -> None:
    other = tmp_path / "other.md"
    other.write_text("# Real heading\n\n```\n# Not a heading\n```\n", encoding="utf-8")
    page = tmp_path / "page.md"
    page.write_text("[ok](other.md#real-heading) [gone](missing.md) [bad](other.md#nope) "
                    "[fence](other.md#not-a-heading) [web](https://example.org)\n", encoding="utf-8")
    assert broken_links(page, root=tmp_path) == ["missing.md", "other.md#nope", "other.md#not-a-heading"]


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_external_links_are_https_and_point_at_the_project_or_a_named_host(page: Path) -> None:
    for target in LINK.findall(_read(page)):
        if target.startswith(("http://", "https://")):
            parsed = urlparse(target)
            assert parsed.scheme == "https" and "." in parsed.netloc, target
            assert parsed.netloc not in {"localhost", "127.0.0.1"}, target
            if parsed.netloc == "github.com":
                assert parsed.path.startswith("/quadringent/quadringent-ce"), target
            local = _repository_file_from_url(target)
            assert local is None or local.exists(), target


def test_roadmap_has_the_three_horizons_and_no_date() -> None:
    text = _read(ROOT / "ROADMAP.md")
    headings = re.findall(r"^## (.*)$", text, re.MULTILINE)
    assert ["Now", "Next", "Later"] == [h for h in headings if h in {"Now", "Next", "Later"}]
    assert "How priorities are chosen" in headings
    assert "compatibility reports" in text.lower() and "pilot feedback" in text.lower()
    assert "no dates" in text.lower()
    prose = re.sub(r"<!--.*?-->|\]\([^)]*\)|`[^`]*`", "", text, flags=re.DOTALL)
    assert not re.search(r"\b(?:19|20)\d{2}\b", prose), "a roadmap item must not carry a year"
    assert not re.search(r"\b(?:Q[1-4]|January|February|March|April|May|June|July|August|September|"
                         r"October|November|December)\b", prose), "a roadmap item must not carry a date"
    for horizon in ("Now", "Next", "Later"):
        body = re.search(rf"^## {horizon}\s*$(.*?)(?=^## |\Z)", text, re.DOTALL | re.MULTILINE)
        assert body, horizon
        items = [line for line in body.group(1).splitlines() if line.startswith("- ")]
        assert items, f"{horizon} has no item"
        for item in items:
            assert LINK.search(item), f"{horizon}: item without a link: {item[:60]}"


def test_roadmap_mentions_local_mode_only_as_in_progress() -> None:
    text = _read(ROOT / "ROADMAP.md")
    for line in text.splitlines():
        if "local mode" in line.lower():
            assert "in progress" in line.lower() and "not released" in line.lower() and "no date" in line.lower()
    assert "local mode" in text.lower()


def test_roadmap_makes_no_production_claim_and_links_the_evidence() -> None:
    text = _read(ROOT / "ROADMAP.md").lower()
    assert "docs/evidence.md" in text
    for phrase in ("production-ready", "production ready", "guaranteed", "will be released"):
        assert phrase not in text, phrase
    for disclaimer in ("dev/int preview", "no production qualification", "not a production qualification"):
        assert disclaimer not in text, disclaimer


def test_contributing_keeps_its_content_and_adds_ways_to_help() -> None:
    text = _read(ROOT / "CONTRIBUTING.md")
    for kept in ("## Checks", "## Principles", "no separate CLA", "pytest is the only Python test runner",
                 "SECURITY.md"):
        assert kept in text, kept
    ways = re.search(r"^## Ways to help\s*$(.*?)(?=^## )", text, re.DOTALL | re.MULTILINE)
    assert ways, "CONTRIBUTING needs a 'Ways to help' section"
    section = ways.group(1).lower()
    for topic in ("compatibility report", "documentation", "good first issue"):
        assert topic in section, topic
    assert text.index("## Ways to help") < text.index("## Checks")


# --- identities and secrets -------------------------------------------------------------------


@pytest.mark.parametrize("path", PUBLIC_FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_file_carries_no_private_identity(path: Path) -> None:
    assert scan_text(_read(path), path.name) == []


def test_scan_text_would_flag_a_private_address() -> None:
    assert scan_text("host 10." + "1.2.3 in the report", "example.md")


@pytest.mark.parametrize("path", PUBLIC_FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_file_has_no_personal_voice_or_non_english_marker(path: Path) -> None:
    text = _read(path)
    prose = re.sub(r"<!--.*?-->|`[^`]*`", "", text, flags=re.DOTALL)
    assert not re.search(r"\b(?:at my company|in my experience|after \d+ years|my employer)\b", prose, re.I)
    assert not re.search(r"[àâçéèêëîïôùûœ]", prose, re.I), "non-English spelling or accent"


def test_maintainer_pages_use_the_project_voice() -> None:
    changelog = _read(ROOT / "CHANGELOG.md")
    unreleased = changelog.split("\n## 0.", 1)[0]
    for name, text in [("ROADMAP.md", _read(ROOT / "ROADMAP.md")),
                       ("CONTRIBUTING.md", _read(ROOT / "CONTRIBUTING.md")),
                       ("CHANGELOG.md (Unreleased)", unreleased)]:
        prose = re.sub(r"<!--.*?-->|```.*?```|~~~.*?~~~|`[^`]*`", "", text, flags=re.DOTALL)
        assert not re.search(r"\b(?:I|I'm|I've|I'll|my|mine|me)\b", prose), f"{name}: first person singular"


def test_only_the_project_contact_address_appears() -> None:
    allowed = {"quadringent@proton.me"}
    for path in PUBLIC_FILES:
        found = set(re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", _read(path)))
        assert found <= allowed, f"{path.name}: unexpected address"


# --- changelog --------------------------------------------------------------------------------


def test_unreleased_section_is_first_and_above_the_latest_version() -> None:
    lines = _read(ROOT / "CHANGELOG.md").splitlines()
    headings = [(i, line) for i, line in enumerate(lines) if line.startswith("## ")]
    assert headings[0][1] == "## Unreleased"
    versions = [i for i, line in headings if re.match(r"## \d+\.\d+\.\d+\b", line)]
    assert versions and headings[0][0] < versions[0]
    assert sum(1 for _, line in headings if line == "## Unreleased") == 1


def test_unreleased_section_has_entries_and_keeps_the_style() -> None:
    text = _read(ROOT / "CHANGELOG.md")
    section = re.search(r"^## Unreleased\s*$(.*?)(?=^## )", text, re.DOTALL | re.MULTILINE)
    assert section
    entries = [line for line in section.group(1).splitlines() if line.startswith("- ")]
    assert len(entries) >= 8
    body = section.group(1).lower()
    for topic in ("quadringent doctor", "export", "organization", "diagnostics"):
        assert topic in body, topic
    assert "not published on pypi" in body
    assert "production" not in body.replace("no production", "")
    # The released sections are untouched below it.
    assert "## 0.3.5 — Reliable cockpit controls and administrator guidance" in text
    assert "## 0.3.1 — Initial public preview" in text


def test_unreleased_links_resolve() -> None:
    assert broken_links(ROOT / "CHANGELOG.md") == []

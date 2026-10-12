"""Public journey checks: pilot contact, security channel and generated pages."""
import re
import shutil
from pathlib import Path

import pytest

from build_site import DOC_PAGES, build

ROOT = Path(__file__).resolve().parents[1]
CONTACT = "quadringent@proton.me"
CTA = "Discuss a DEV/INT pilot"
PILOT_GUIDE = "docs/product/positioning-and-offer.md"
PILOT_TEMPLATE = ".github/ISSUE_TEMPLATE/pilot_interest.yml"
ENTRY_POINTS = ["site/index.html", PILOT_GUIDE, "README.md"]


def read(path: str) -> str:
    return (ROOT / path).read_text()


def flat(text: str) -> str:
    return " ".join(text.split())


def section(markdown: str, heading: str) -> str:
    match = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", markdown, re.S | re.M)
    assert match, f"missing section: {heading}"
    return match[1]


@pytest.fixture(scope="module")
def site(tmp_path_factory) -> Path:
    """Build the real site; fonts are omitted so no npm install is required."""
    source = tmp_path_factory.mktemp("source")
    shutil.copytree(ROOT / "site", source / "site")
    (source / "site" / "style.css").write_text("")
    for name in [*DOC_PAGES, "docs/design-system.md"]:
        if (ROOT / name).is_file():
            (source / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, source / name)
    for asset in (ROOT / "docs").rglob("*.svg"):
        target = source / asset.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(asset, target)
    output = tmp_path_factory.mktemp("built") / "site"
    build("https://github.com/quadringent/quadringent-ce", output, source_root=source,
          site_url="https://quadringent.github.io/quadringent-ce/")
    return output


# C01 — application, trust and security


@pytest.mark.parametrize("path", ENTRY_POINTS)
def test_each_entry_point_offers_the_same_cta_and_a_private_contact(path):
    text = read(path)
    assert CTA in text
    assert f"mailto:{CONTACT}" in text
    assert "without a GitHub account" in flat(text)


@pytest.mark.parametrize("page", ["index.html", "docs/pilot/index.html", "docs/getting-started/index.html"])
def test_generated_pages_keep_a_working_private_contact(site, page):
    html = (site / page).read_text()
    assert f'href="mailto:{CONTACT}' in html
    assert CONTACT in re.sub(r"<[^>]+>", " ", html)


def test_pilot_guide_explains_what_happens_next_without_commitment():
    guide = read(PILOT_GUIDE)
    steps_section = section(guide, "What happens next")
    steps = re.findall(r"^\d+\. \*\*(.+?)\.?\*\*", steps_section, re.M)
    assert steps == ["Technical discovery exchange", "Scope definition", "Separate pilot decision"]
    assert "No response-time commitment" in steps_section
    assert "never accepted automatically" in flat(steps_section)


def test_first_request_asks_for_nothing_sensitive():
    first = flat(section(read(PILOT_GUIDE), CTA))
    for forbidden in ("credentials", "access", "private hostnames", "customer data", "attachments"):
        assert forbidden in first
    template = flat(read(PILOT_TEMPLATE))
    assert "attachments" in template and "credentials" in template


def test_pilot_guide_drops_internal_notes_and_contradictions():
    guide = flat(read(PILOT_GUIDE))
    assert "Presentation direction" not in guide
    assert "without a sales form" not in guide


@pytest.mark.parametrize("path", [*ENTRY_POINTS, PILOT_TEMPLATE])
def test_public_issue_is_described_as_public_never_anonymous(path):
    text = flat(read(path)).lower()
    assert "anonymous description" not in text
    assert "anonymous request" not in text
    if "issues/new?template=pilot_interest.yml" in text or path == PILOT_TEMPLATE:
        assert "public" in text


def test_issue_template_points_to_the_private_channel_and_states_visibility():
    template = read(PILOT_TEMPLATE)
    assert CONTACT in template
    assert "publicly visible" in flat(template)
    assert "never accepted automatically" in flat(template)


def test_security_policy_uses_the_enabled_private_channel():
    policy = flat(read("SECURITY.md"))
    assert "to be enabled" not in policy
    assert "https://github.com/quadringent/quadringent-ce/security/advisories/new" in policy
    assert "Private vulnerability reporting is enabled" in policy
    # The pilot mailbox may only open a private channel, never carry details.
    assert "without vulnerability details" in policy
    assert "public issue" in policy


def test_code_of_conduct_names_a_concrete_private_channel():
    conduct = flat(read("CODE_OF_CONDUCT.md"))
    assert "agreed channel" not in conduct
    assert CONTACT in conduct


# C03 — synthetic demo without IBM i or Snowflake

DEMO_GUIDE = "docs/product/synthetic-demo.md"
DEMO_COMMANDS = ("npm ci --prefix ui", "node ui/scripts/build-demo.mjs --serve")


def test_demo_guide_gives_runnable_commands_and_states_its_limits():
    guide = read(DEMO_GUIDE)
    for command in DEMO_COMMANDS:
        assert command in guide
    text = flat(guide)
    assert "Synthetic demo" in text
    assert "connect-src 'none'" in text
    assert "not a release qualification" in text
    assert "PROVENANCE.json" in text
    assert "http://127.0.0.1:8870/#/cockpit" in text


def test_demo_commands_point_to_scripts_that_exist():
    assert (ROOT / "ui/scripts/build-demo.mjs").is_file()
    assert (ROOT / "ui/scripts/serve-demo.mjs").is_file()
    assert (ROOT / "ui/src/demo/index.html").is_file()


@pytest.mark.parametrize("path", ["site/index.html", "README.md"])
def test_home_and_readme_offer_the_synthetic_demo_first(path):
    text = read(path)
    assert "node ui/scripts/build-demo.mjs --serve" in text
    assert "Synthetic demo" in text


def test_demo_guide_is_published_with_its_commands_intact(site):
    page = (site / "docs/demo/index.html").read_text()
    for command in DEMO_COMMANDS:
        assert command in page
    assert "http://127.0.0.1:8870/#/cockpit" in page

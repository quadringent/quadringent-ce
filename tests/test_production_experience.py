"""Firsthand production experience stays separate from release qualification."""

import re
from pathlib import Path

from build_site import build

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return (ROOT / path).read_text()


def test_production_experience_is_attributed_and_not_a_release_measurement():
    evidence = read("docs/evidence.md")
    section = evidence.split("## Production experience\n", 1)[1].split("\n## ", 1)[0]
    assert "Our maintainer has repeatedly tested Quadringent on production IBM i" in section
    assert "several million rows" in section
    assert "real cluster and VPC endpoint conditions" in section
    assert "separate from the dated synthetic qualification campaigns" in section
    assert "Customer systems and data stay private" in section
    assert "production promotion disabled" in section
    assert "do not negate the maintainer's production testing" in section
    assert "not a release-specific measurement or a service-level commitment" in section
    assert not re.search(r"\b\d+(?:\.\d+)*\b|P95|rows/s", section)
    assert "All tests used synthetic tables" not in evidence
    assert "All tests in the dated qualification campaigns below used synthetic tables" in evidence
    assert "In the historical qualification records:" in evidence


def test_production_experience_links_from_readme_and_landing():
    for path in ("README.md", "site/index.html"):
        text = read(path)
        assert "Our maintainer has tested Quadringent on production IBM i tables containing several million rows." in text
        assert "docs/evidence.md#production-experience" in text


def test_contribution_rules_allow_attribution_without_invented_claims():
    text = read("CONTRIBUTING.md")
    assert "Never claim production use" not in text
    assert "specific, attributed firsthand production experience" in text
    assert "Never invent customers, deployments, references, certifications, service levels, guarantees or metrics" in text


def test_built_production_experience_anchor_and_link(tmp_path):
    tmp_path = tmp_path / "site"
    build("https://github.com/quadringent/quadringent-ce", tmp_path, source_root=ROOT, site_url="https://quadringent.github.io/quadringent-ce/")
    assert 'id="production-experience"' in (tmp_path / "docs/evidence/index.html").read_text()
    assert 'href="docs/evidence/#production-experience"' in (tmp_path / "index.html").read_text()

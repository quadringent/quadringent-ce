"""Search and first-visit copy must identify the product without expanding support."""
import json
import re
from pathlib import Path

from build_site import seo_head

ROOT = Path(__file__).resolve().parents[1]


def test_home_has_a_concise_product_description_and_neutral_install_action():
    home = (ROOT / "site/index.html").read_text()
    description = re.search(r'<meta name="description" content="([^"]+)"', home)[1]
    assert len(description) <= 160
    for term in ("IBM i (AS/400)", "Snowflake", "open-source CDC", "Apache-2.0"):
        assert term in description
    assert ">Install Quadringent</a>" in home
    assert "Your DEV IBM i" not in home
    # This copy change must not remove the actual first-installation prerequisite.
    assert "A non-production IBM&nbsp;i with a journaled table" in home


def test_home_closeout_is_bound_to_the_existing_followup_receipt():
    home = (ROOT / "site/index.html").read_text()
    closeout = json.loads((ROOT / "site/qualification-0.4.5.json").read_text())["followup_2026_10_16"]
    comparison = closeout["final_comparison"]
    assert "2026-10-16 campaign closeout" in home
    assert f'{comparison["source_rows"]:,} SOURCE' in home
    assert f'{comparison["history_rows"]:,} HISTORY' in home
    assert "30 of 97 planned bursts" in home
    assert not closeout["endurance"]["fixed_97_burst_contract_passed"]
    assert "The 24-hour contract failed" in home
    assert "separate qualification harness" in home
    assert "run in progress" not in home


def test_home_structured_data_identifies_the_same_public_source_and_license():
    repository = "https://github.com/example/project"
    head = seo_head("https://example.org/", "Example", "Description", "https://example.org/", repository, home=True)
    schema = json.loads(re.search(r'<script type="application/ld\+json">(.*?)</script>', head, re.S)[1])
    assert schema["@type"] == "SoftwareApplication"
    assert schema["sameAs"] == repository
    assert schema["isAccessibleForFree"] is True
    assert schema["license"] == repository + "/blob/main/LICENSE"
    assert schema["isBasedOn"]["@type"] == "SoftwareSourceCode"
    assert schema["isBasedOn"]["codeRepository"] == repository
    assert "aggregateRating" not in schema and "review" not in schema

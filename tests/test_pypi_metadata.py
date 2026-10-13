"""The package metadata points to the right repository and the long description renders on PyPI."""
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "https://github.com/quadringent/quadringent-ce"
SITE = "https://quadringent.github.io/quadringent-ce/"
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]


def test_project_urls_point_to_the_repository_and_the_site():
    urls = PROJECT["urls"]
    assert urls["Homepage"] == SITE
    assert urls["Source"] == REPOSITORY
    assert urls["Documentation"].startswith(SITE + "docs/")
    assert urls["Changelog"] == f"{REPOSITORY}/blob/main/CHANGELOG.md"
    assert urls["Issues"] == f"{REPOSITORY}/issues"
    assert urls["Security policy"] == f"{REPOSITORY}/blob/main/SECURITY.md"
    for value in urls.values():
        assert value.startswith(("https://github.com/quadringent/quadringent-ce", SITE)), value
    assert (ROOT / "CHANGELOG.md").is_file()
    assert (ROOT / "SECURITY.md").is_file()


def test_classifiers_match_a_dev_int_preview():
    classifiers = PROJECT["classifiers"]
    assert "Development Status :: 3 - Alpha" in classifiers
    assert not [c for c in classifiers if c.startswith("Development Status") and "Alpha" not in c]
    assert not [c for c in classifiers if "Production/Stable" in c or "Mature" in c]
    assert "Programming Language :: Python :: 3.12" in classifiers
    assert "Topic :: Database" in classifiers
    assert any(c.startswith("Operating System :: ") for c in classifiers)
    assert len(classifiers) == len(set(classifiers))
    # With an SPDX license expression, setuptools rejects License classifiers.
    assert PROJECT["license"] == "Apache-2.0"
    assert not [c for c in classifiers if c.startswith("License ::")]
    assert PROJECT["requires-python"] == ">=3.12"


def test_keywords_cover_the_search_terms():
    assert {"ibm-i", "as400", "cdc", "snowflake", "change-data-capture", "replication"} <= set(PROJECT["keywords"])


def test_readme_is_a_file_that_exists_and_has_only_absolute_links():
    readme = PROJECT["readme"]
    assert isinstance(readme, str)
    path = ROOT / readme
    assert path.is_file()
    assert path.suffix == ".md"
    text = path.read_text()
    targets = re.findall(r"\]\(([^)\s]+)", text) + re.findall(r'(?:src|href)="([^"]+)"', text)
    assert targets
    for target in targets:
        assert target.startswith(("https://", "mailto:")), target
    assert not re.findall(r"\]\((?!https://|mailto:)", text)
    for target in targets:
        if target.startswith("https://github.com/"):
            assert target.startswith(REPOSITORY), target


def test_readme_keeps_the_version_line_the_evidence_link_and_the_not_measured_word():
    text = " ".join((ROOT / PROJECT["readme"]).read_text().split())
    assert "Version 0.4 \u00b7 Apache-2.0" in text
    assert "Not measured" in text
    assert f"{REPOSITORY}/blob/main/docs/evidence.md" in text
    assert "Preview, DEV/INT only" not in text
    assert "no production qualification" not in text.lower()


def test_readme_pages_linked_from_the_package_page_exist_in_the_repository():
    text = (ROOT / PROJECT["readme"]).read_text()
    for path in re.findall(re.escape(REPOSITORY) + r"/blob/main/([^)\s#]+)", text):
        assert (ROOT / path).is_file(), path


def test_readme_renders_when_the_renderer_is_installed():
    try:
        import readme_renderer.markdown as renderer
    except ImportError:
        return
    assert renderer.render((ROOT / PROJECT["readme"]).read_text()) is not None


def test_package_page_carries_the_value_statement_and_the_new_pages():
    text = " ".join((ROOT / "docs" / "pypi.md").read_text().split())
    first = " ".join((ROOT / "README.md").read_text().split("\n\n")[1].split())
    assert first in text
    assert "needs no Kafka" in text
    assert "Version 0.4 \u00b7 Apache-2.0" in text
    assert "Not measured" in text
    assert "Start with a proof of concept on a non-production IBM i" in text
    assert f"{REPOSITORY}/blob/main/docs/evidence.md" in text
    for path in (
        "docs/product/faq.md",
        "docs/product/comparison.md",
        "docs/articles/ibm-i-journals-for-cdc.md",
        "docs/articles/capture-delivery-reconciliation.md",
        "docs/articles/as400-to-snowflake-options.md",
    ):
        assert f"{REPOSITORY}/blob/main/{path}" in text, path
        assert (ROOT / path).is_file(), path

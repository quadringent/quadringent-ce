"""Every public trust and W3 page is registered, described and listed in the built sitemap."""
import json
import re
import shutil
from html import unescape
from pathlib import Path

import pytest

from build_site import DOC_DESCRIPTIONS, DOC_PAGES, build

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "https://github.com/quadringent/quadringent-ce"
SITE_URL = "https://quadringent.github.io/quadringent-ce/"

PUBLIC_PAGES = {
    "docs/product/ibm-i-footprint.md": "ibm-i-footprint",
    "docs/product/your-data.md": "your-data",
    "docs/product/verify-a-release.md": "verify-a-release",
    "docs/governance.md": "governance",
    "SUPPORT.md": "support",
    "docs/product/faq.md": "faq",
    "docs/product/comparison.md": "comparison",
    "docs/articles/ibm-i-journals-for-cdc.md": "ibm-i-journals-for-cdc",
    "docs/articles/capture-delivery-reconciliation.md": "capture-delivery-reconciliation",
    "docs/articles/as400-to-snowflake-options.md": "as400-to-snowflake-options",
}


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> Path:
    """Build the home page and guides from the real sources; fonts are omitted."""
    source = tmp_path_factory.mktemp("source")
    shutil.copytree(ROOT / "site", source / "site")
    (source / "site" / "style.css").write_text("")
    for name in DOC_PAGES:
        if (ROOT / name).is_file():
            (source / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, source / name)
    output = tmp_path_factory.mktemp("built") / "site"
    build(REPOSITORY, output, source_root=source, site_url=SITE_URL)
    return output


def test_every_public_page_is_registered_with_its_slug():
    for source, slug in PUBLIC_PAGES.items():
        assert source in DOC_PAGES, source
        assert DOC_PAGES[source][0] == slug, source


def test_registered_slugs_and_titles_are_unique_and_titles_are_specific():
    slugs = [slug for slug, _ in DOC_PAGES.values()]
    titles = [title for _, title in DOC_PAGES.values()]
    assert len(set(slugs)) == len(slugs)
    assert len(set(titles)) == len(titles)
    for source, (slug, title) in DOC_PAGES.items():
        assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug), source
        assert 3 <= len(title.split()) and len(title) <= 80, source


def test_every_registered_page_has_its_own_description():
    assert set(DOC_DESCRIPTIONS) == set(DOC_PAGES)
    for source, description in DOC_DESCRIPTIONS.items():
        assert 60 <= len(description) <= 200, source
        assert description.endswith("."), source
    assert len(set(DOC_DESCRIPTIONS.values())) == len(DOC_DESCRIPTIONS)


@pytest.mark.parametrize("source", sorted(PUBLIC_PAGES))
def test_registered_page_source_exists(source):
    assert (ROOT / source).is_file(), source


@pytest.mark.parametrize("source", sorted(PUBLIC_PAGES))
def test_page_is_built_with_seo_metadata_and_in_the_sitemap(built, source):
    slug = PUBLIC_PAGES[source]
    url = f"{SITE_URL}docs/{slug}/"
    html = (built / "docs" / slug / "index.html").read_text()
    assert f'<link rel="canonical" href="{url}">' in html
    assert f'<meta property="og:url" content="{url}">' in html
    description = unescape(re.search(r'<meta name="description" content="([^"]+)"', html)[1])
    assert description == DOC_DESCRIPTIONS[source]
    assert f'<meta property="og:description" content="{DOC_DESCRIPTIONS[source]}">' in html
    schema = json.loads(re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)[1])
    assert schema["@type"] == "TechArticle"
    assert schema["url"] == url
    assert f"<loc>{url}</loc>" in (built / "sitemap.xml").read_text()


def test_sitemap_lists_exactly_the_home_page_and_the_built_guides(built):
    locations = re.findall(r"<loc>([^<]+)</loc>", (built / "sitemap.xml").read_text())
    guides = {f"{SITE_URL}docs/{DOC_PAGES[source][0]}/" for source in DOC_PAGES if (ROOT / source).is_file()}
    assert len(locations) == len(set(locations))
    assert set(locations) == {SITE_URL, f"{SITE_URL}docs/"} | guides


def test_documentation_index_links_every_built_guide(built):
    index = (built / "docs" / "index.html").read_text()
    for source, (slug, title) in DOC_PAGES.items():
        if (ROOT / source).is_file():
            assert f'<li><a href="{slug}/">' in index, source

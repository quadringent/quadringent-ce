"""The trust and verification pages are reachable from the README, the site and the operator guide."""
import posixpath
import re
import shutil
from pathlib import Path

import pytest

from build_site import DOC_PAGES, build

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "https://github.com/quadringent/quadringent-ce"
SITE_URL = "https://quadringent.github.io/quadringent-ce/"

TRUST_PAGES = [
    "docs/product/ibm-i-footprint.md",
    "docs/product/your-data.md",
    "docs/product/verify-a-release.md",
    "docs/governance.md",
    "SUPPORT.md",
    "SECURITY.md",
]


def read(path: str) -> str:
    return (ROOT / path).read_text()


def relative_links(markdown: str) -> list[str]:
    return re.findall(r"\]\(([^)#\s]+)(?:#[^)\s]*)?\)", markdown)


def resolved_links(path: str) -> set[str]:
    base = posixpath.dirname(path)
    return {posixpath.normpath(posixpath.join(base, link)) for link in relative_links(read(path))
            if "://" not in link and not link.startswith(("mailto:", "/"))}


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> Path:
    """Build the home page and guides; fonts are omitted so no npm install is required."""
    source = tmp_path_factory.mktemp("source")
    shutil.copytree(ROOT / "site", source / "site")
    (source / "site" / "style.css").write_text("")
    for name in [*DOC_PAGES, *TRUST_PAGES]:
        if (ROOT / name).is_file():
            (source / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, source / name)
    output = tmp_path_factory.mktemp("built") / "site"
    build(REPOSITORY, output, source_root=source, site_url=SITE_URL)
    return output


def test_readme_has_a_short_trust_section_with_one_link_per_bullet():
    section = re.search(r"^## Trust and verification\n(.*?)(?=^## )", read("README.md"), re.S | re.M)
    assert section, "README.md has no 'Trust and verification' section"
    bullets = re.findall(r"^- (.+)$", section[1], re.M)
    assert 4 <= len(bullets) <= 6
    for bullet in bullets:
        assert len(relative_links(bullet)) == 1, bullet


def test_readme_comes_after_the_status_line_and_links_the_licensing_page():
    text = read("README.md")
    assert text.index("Status: Preview, DEV/INT only.") < text.index("## Trust and verification")
    assert "[project model](docs/licensing.md)" in text


def test_readme_links_every_trust_page_and_the_targets_exist():
    links = resolved_links("README.md")
    for page in TRUST_PAGES:
        assert page in links, page
        assert (ROOT / page).is_file(), page


def test_home_links_every_trust_page_and_the_targets_exist():
    html = read("site/index.html")
    section = re.search(r'<div class="index" id="trust">.*?</ul>', html, re.S)
    assert section, "site/index.html has no trust list"
    assert 'id="trust"' in html
    linked = re.findall(r'data-repo-path="([^"]+)" href="([^"]+)"', section[0])
    assert sorted(path for path, _ in linked) == sorted(TRUST_PAGES)
    for path, href in linked:
        assert href == f"../{path}"
        assert (ROOT / path).is_file(), path


def test_built_home_links_every_trust_page(built):
    home = (built / "index.html").read_text()
    for page in TRUST_PAGES:
        slug = DOC_PAGES.get(page, (None,))[0]
        expected = f'href="docs/{slug}/"' if slug else f'href="{REPOSITORY}/blob/main/{page}"'
        assert expected in home, page
    assert "data-repo-" not in home


def test_built_readme_guide_links_every_trust_page(built):
    guide = (built / "docs/getting-started/index.html").read_text()
    for page in TRUST_PAGES:
        slug = DOC_PAGES.get(page, (None,))[0]
        expected = f'href="../{slug}/"' if slug else f'href="{REPOSITORY}/blob/main/{page}"'
        assert expected in guide, page


def test_operator_guide_links_footprint_and_release_verification():
    links = resolved_links("docs/product/operator-guide.md")
    for page in ("docs/product/ibm-i-footprint.md", "docs/product/verify-a-release.md"):
        assert page in links, page
        assert (ROOT / page).is_file(), page

"""The evergreen articles and the comparison page keep their promises.

Pages covered:

- docs/articles/ibm-i-journals-for-cdc.md
- docs/articles/capture-delivery-reconciliation.md
- docs/articles/as400-to-snowflake-options.md
- docs/product/comparison.md

The checks read the repository only. They do not open a network connection:
external URLs are checked for shape, not fetched.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

import pytest
from check_publication import scan_text

ROOT = Path(__file__).resolve().parents[1]
ARTICLES = {
    ROOT / "docs/articles/ibm-i-journals-for-cdc.md": "How IBM i journals work for change data capture",
    ROOT / "docs/articles/capture-delivery-reconciliation.md":
        "Capture is not delivery: proving IBM i data reached Snowflake",
    ROOT / "docs/articles/as400-to-snowflake-options.md": "Options to replicate AS/400 (IBM i) data to Snowflake",
}
COMPARISON = ROOT / "docs/product/comparison.md"
PAGES = (*ARTICLES, COMPARISON)

ACCESS_DATE = "2026-10-13"
MIN_EXTERNAL_SOURCES = 4
MIN_WORDS, MAX_WORDS = 900, 1500
DESCRIPTION_MIN, DESCRIPTION_MAX = 60, 160

# "best" alone is banned, but the honest phrase "best effort" (support level) is not marketing.
FORBIDDEN = re.compile(
    r"\b(?:best(?![- ]effort)|leading|revolutionary|seamless(?:ly)?|guaranteed|enterprise-grade|"
    r"world-class|best-in-class|industry-leading|unparalleled|bulletproof|military-grade|cutting-edge)\b"
    r"|100%",
    re.IGNORECASE,
)
_LINK = re.compile(r"(?<!\!)\[[^\]]*\]\(([^)\s]+)\)")
_SOURCE_COMMENT = re.compile(r"<!--\s*source:\s*(.*?)\s*-->", re.DOTALL)
_DESCRIPTION = re.compile(r"^<!--\s*description:\s*(.*?)\s*-->$")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _slug(heading: str) -> str:
    text = re.sub(r"[`*]", "", heading.strip().lower())
    return re.sub(r"[^\w\- ]", "", text).replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    in_fence = False
    anchors: set[str] = set()
    for line in _read(path).splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
        elif not in_fence:
            match = re.match(r"^#{1,6}\s+(.*?)\s*#*\s*$", line)
            if match:
                anchors.add(_slug(match.group(1)))
    return anchors


def broken_links(page: Path, root: Path = ROOT) -> list[str]:
    broken = []
    for target in _LINK.findall(_read(page)):
        if re.match(r"^[a-z][a-z0-9+.-]*:", target):
            continue
        path_part, _, fragment = target.partition("#")
        resolved = page if not path_part else (page.parent / path_part).resolve()
        if not resolved.exists() or (root not in resolved.parents and resolved != root):
            broken.append(target)
        elif fragment and resolved.suffix == ".md" and fragment not in _anchors(resolved):
            broken.append(target)
    return broken


def external_links(page: Path) -> set[str]:
    return {t for t in _LINK.findall(_read(page)) if t.startswith("https://")}


def prose_words(page: Path) -> int:
    """Words a reader reads: no comments, no link targets, no bare URLs, no sources list."""
    text = re.sub(r"<!--.*?-->", "", _read(page), flags=re.DOTALL)
    text = re.sub(r"\]\([^)]*\)", "]", text)
    text = re.sub(r"https?://\S+", "", text)
    text = text.split("\n## Sources")[0]
    return len(re.findall(r"[A-Za-z0-9`*_$'’-]+", text))


def section(page: Path, heading: str) -> str:
    match = re.search(rf"^## {re.escape(heading)}\s*$(.*?)(?=^## |\Z)", _read(page), re.DOTALL | re.MULTILINE)
    assert match, f"{page.name}: no section {heading!r}"
    return match.group(1)


# --- every page -------------------------------------------------------------------------------


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_page_exists_and_starts_with_a_title(page: Path) -> None:
    first = _read(page).splitlines()[0]
    assert first.startswith("# ") and len(first) > 4


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_page_has_a_meta_description_line(page: Path) -> None:
    lines = _read(page).splitlines()[:6]
    found = [m.group(1) for line in lines if (m := _DESCRIPTION.match(line))]
    assert len(found) == 1, f"{page.name}: exactly one `<!-- description: ... -->` line near the top"
    description = found[0]
    assert DESCRIPTION_MIN <= len(description) <= DESCRIPTION_MAX, len(description)
    assert "\n" not in description and "<" not in description and '"' not in description
    assert description.endswith(".")


def test_meta_description_pattern_rejects_a_multiline_comment() -> None:
    assert _DESCRIPTION.match("<!-- description: One line. -->")
    assert not _DESCRIPTION.match("<!-- source: README.md -->")
    assert not _DESCRIPTION.match("description: no comment markers")


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_every_relative_link_resolves(page: Path) -> None:
    assert [t for t in _LINK.findall(_read(page)) if not t.startswith("https://")], "no relative link"
    assert broken_links(page) == []


def test_link_check_catches_a_missing_file_and_a_missing_anchor(tmp_path: Path) -> None:
    (tmp_path / "other.md").write_text("# Other\n\n## Real heading\n", encoding="utf-8")
    page = tmp_path / "page.md"
    page.write_text(
        "[ok](other.md#real-heading) [gone](missing.md) [bad anchor](other.md#nope) "
        "[outside](../../etc/passwd) [web](https://example.org/x)\n",
        encoding="utf-8",
    )
    assert broken_links(page, root=tmp_path) == ["missing.md", "other.md#nope", "../../etc/passwd"]


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_every_source_comment_names_existing_files(page: Path) -> None:
    comments = _SOURCE_COMMENT.findall(_read(page))
    assert comments, f"{page.name}: a claim about Quadringent needs a `<!-- source: path -->` comment"
    for comment in comments:
        for token in (item.strip() for item in comment.split(",")):
            token = re.sub(r"\s*\(.*\)$", "", token)
            assert token and (ROOT / token).exists(), f"{page.name}: source not found: {token!r}"


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_backticked_repository_paths_exist(page: Path) -> None:
    for token in set(re.findall(r"`([A-Za-z0-9_./-]+\.(?:py|java|md|yaml|yml|tf|json|txt|sh))`", _read(page))):
        if "/" in token:
            assert (ROOT / token).exists() or (page.parent / token).exists(), f"no such path {token}"


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_external_links_are_https_and_name_a_host(page: Path) -> None:
    for url in external_links(page):
        parsed = urlparse(url)
        assert parsed.scheme == "https" and "." in parsed.netloc, url
        assert parsed.netloc not in {"localhost", "127.0.0.1"}, url
    for target in _LINK.findall(_read(page)):
        assert not target.startswith("http://"), f"{page.name}: plain http link {target}"


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_no_forbidden_marketing_word(page: Path) -> None:
    body = _read(page)
    assert FORBIDDEN.findall(body) == []


def test_forbidden_word_pattern_catches_the_banned_words_and_allows_best_effort() -> None:
    for text in ("the best option", "A leading tool", "seamless", "guaranteed delivery", "enterprise-grade",
                 "revolutionary", "100% safe"):
        assert FORBIDDEN.search(text), text
    for text in ("answered on a best-effort basis", "Best effort on public issues", "a bestiary"):
        assert FORBIDDEN.search(text) is None, text


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_page_carries_no_private_identity(page: Path) -> None:
    text = _read(page)
    assert scan_text(text, page.name) == []
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text), "no email address belongs on these pages"
    assert "mailto:" not in text
    assert "/Users/" not in text and "/home/" not in text
    assert not re.search(r"(?<![\w.:-])\d{12}(?![\w-])", text), "possible cloud account id"


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_page_keeps_the_preview_status_visible(page: Path) -> None:
    text = " ".join(_read(page).split())
    assert "DEV/INT preview" in text
    assert "no production qualification is claimed" in text.lower()


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_page_gives_the_access_date_of_its_sources(page: Path) -> None:
    assert ACCESS_DATE in _read(page)
    assert "## Sources" in _read(page)


# --- the three articles ------------------------------------------------------------------------


@pytest.mark.parametrize("page,title", ARTICLES.items(), ids=lambda v: v.name if isinstance(v, Path) else "title")
def test_article_title_is_the_agreed_one(page: Path, title: str) -> None:
    assert _read(page).splitlines()[0] == f"# {title}"


@pytest.mark.parametrize("page", ARTICLES, ids=lambda p: p.name)
def test_article_cites_enough_distinct_public_sources(page: Path) -> None:
    sources = external_links(page)
    assert len(sources) >= MIN_EXTERNAL_SOURCES, sorted(sources)
    listed = section(page, "Sources")
    assert listed.count("https://") >= MIN_EXTERNAL_SOURCES


@pytest.mark.parametrize("page", ARTICLES, ids=lambda p: p.name)
def test_article_length_is_in_range(page: Path) -> None:
    words = prose_words(page)
    assert MIN_WORDS <= words <= MAX_WORDS, words


@pytest.mark.parametrize("page", ARTICLES, ids=lambda p: p.name)
def test_article_ends_with_what_quadringent_does_before_its_sources(page: Path) -> None:
    text = _read(page)
    headings = re.findall(r"^## (.+)$", text, re.MULTILINE)
    assert "What Quadringent does" in headings or "Option 4: Quadringent" in headings
    assert headings[-1] == "Sources"
    block = section(page, "What Quadringent does") if "What Quadringent does" in headings else section(
        page, "Option 4: Quadringent")
    docs = [t for t in _LINK.findall(block) if not t.startswith("https://") and t.split("#")[0].endswith(".md")]
    assert docs, "the Quadringent section links the relevant doc pages"
    assert _SOURCE_COMMENT.search(block), "the Quadringent section cites repository sources"


@pytest.mark.parametrize("page", ARTICLES, ids=lambda p: p.name)
def test_article_links_its_sibling_articles(page: Path) -> None:
    others = [p for p in ARTICLES if p != page]
    links = set(_LINK.findall(_read(page)))
    assert any(f"{other.name}" in link for other in others for link in links)


def test_options_article_links_the_comparison_and_the_faq() -> None:
    links = _LINK.findall(_read(ROOT / "docs/articles/as400-to-snowflake-options.md"))
    assert any(link.endswith("product/comparison.md") for link in links)
    assert any(link.endswith("product/faq.md") for link in links)


def test_journal_article_covers_the_agreed_topics() -> None:
    text = _read(ROOT / "docs/articles/ibm-i-journals-for-cdc.md")
    for needle in ("*BOTH", "MNGRCV", "DLTRCV", "DISPLAY_JOURNAL", "RCVJRNE", "QjoRetrieveJournalEntries",
                   "receiver", "authorit"):
        assert needle in text, needle
    assert text.count("ibm.com/docs") >= 4


def test_reconciliation_article_separates_the_three_stages() -> None:
    text = _read(ROOT / "docs/articles/capture-delivery-reconciliation.md")
    for needle in ("Capture", "Delivery", "Reconciliation", "Not measured", "destination_proof"):
        assert needle in text, needle
    assert "reader is running" in text


# --- the comparison page -----------------------------------------------------------------------

PRODUCTS = ("Quadringent", "Qlik Replicate", "Fivetran", "Precisely Connect CDC", "IBM Data Replication",
            "Debezium connector for IBM i")
SECTIONS = ("1. Architecture", "2. IBM i capture method", "3. Snowflake delivery", "4. Licensing model",
            "5. Evidence shown to the operator", "6. Maturity")


def _table_rows(block: str) -> list[list[str]]:
    rows = []
    for line in block.splitlines():
        if line.startswith("|") and not re.match(r"^\|[\s|:-]+\|?$", line):
            rows.append([cell.strip() for cell in line.strip().strip("|").split(" | ")])
    return rows[1:]  # drop the header row


def test_comparison_title_is_the_agreed_one() -> None:
    assert _read(COMPARISON).splitlines()[0] == "# How Quadringent compares"


@pytest.mark.parametrize("heading", SECTIONS)
def test_comparison_section_covers_every_product(heading: str) -> None:
    rows = _table_rows(section(COMPARISON, heading))
    assert [row[0] for row in rows] == list(PRODUCTS)
    assert all(len(row) == 3 for row in rows)


@pytest.mark.parametrize("heading", SECTIONS)
def test_every_row_about_another_product_cites_a_public_page_or_defers_to_the_vendor(heading: str) -> None:
    for product, statement, source in _table_rows(section(COMPARISON, heading)):
        if product == "Quadringent":
            assert "https://" not in source, "Quadringent rows cite this repository"
            assert _LINK.findall(source), "Quadringent rows link repository pages"
            continue
        low = statement.lower()
        assert "according to" in low or "check with the" in low, f"{heading} / {product}: no attribution"
        urls = _LINK.findall(source)
        assert urls and all(u.startswith("https://") for u in urls), f"{heading} / {product}: no public URL"


def test_comparison_states_the_access_date_and_its_method() -> None:
    text = " ".join(_read(COMPARISON).split())
    assert f"read on **{ACCESS_DATE}**" in text
    assert "Check with the vendor" in text or "check with the vendor" in text
    assert "We ran none of these products" in text


def test_comparison_gives_no_price_figure() -> None:
    licensing = section(COMPARISON, "4. Licensing model")
    assert not re.search(r"[$€£]\s?\d|\d\s?(?:USD|EUR|GBP|dollars|euros)", licensing, re.IGNORECASE)
    assert "No price figures are given here" in licensing


def test_comparison_says_where_the_alternatives_are_stronger_and_where_quadringent_differs() -> None:
    stronger = section(COMPARISON, "Where the alternatives are stronger")
    for needle in ("Maturity", "Support", "Breadth", "Ecosystem"):
        assert needle in stronger, needle
    differs = section(COMPARISON, "Where Quadringent differs")
    for needle in ("Apache-2.0", "No Kafka", "Runs in your account", "Evidence per table", "preview"):
        assert needle in differs, needle


def test_comparison_names_the_debezium_dependency_honestly() -> None:
    text = " ".join(_read(COMPARISON).split())
    assert "journal-parsing library from the Debezium project" in text
    assert "ibmi-journal-parsing" in (ROOT / "java/pom.xml").read_text(encoding="utf-8")
    assert "kafka" not in (ROOT / "java/pom.xml").read_text(encoding="utf-8").lower()


def test_comparison_claims_about_quadringent_match_the_repository() -> None:
    assert "Apache-2.0" in (ROOT / "docs/licensing.md").read_text(encoding="utf-8")
    assert "32" in (ROOT / "docs/product/compatibility.md").read_text(encoding="utf-8")
    assert "At most 32 tables per capture session" in _read(COMPARISON)
    assert "Not measured" in _read(ROOT / "docs/product/operator-guide.md")


# --- review follow-ups ------------------------------------------------------------------------


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_pages_cite_no_archived_qlik_release_and_no_archived_ibm_tutorial(page: Path) -> None:
    text = _read(page)
    assert "/replicate/May2022/" not in text and "/replicate/May2025/" not in text
    assert "developer.ibm.com/tutorials" not in text


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_pages_do_not_call_themselves_honest(page: Path) -> None:
    assert not re.search(r"\bhonest(?:ly)?\b", _read(page), re.IGNORECASE)


def test_comparison_does_not_claim_a_precisely_staging_article_or_a_blanket_select_only() -> None:
    text = " ".join(_read(COMPARISON).split())
    assert "S3 staging" not in text
    assert "and `SELECT` only" not in text
    assert "Not verified" in text  # the remote command server caveat from the footprint page

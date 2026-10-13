"""The first screen of the README and the site leads with the value and keeps the honesty box.

Wave W3: value first, status right after it, and a FAQ whose every answer carries
one of four statuses and a source that exists in this repository.
"""
import json
import posixpath
import re
import tomllib
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FAQ = "docs/product/faq.md"

# Pages written in the same wave by other owners. Their existence is enforced by the
# site tests that check every repository link of the home page; here they are only
# required to be linked, not to resolve, so this suite does not depend on merge order.
SIBLING_PAGES = {
    "docs/product/comparison.md",
    "docs/articles/ibm-i-journals-for-cdc.md",
    "docs/articles/capture-delivery-reconciliation.md",
    "docs/articles/as400-to-snowflake-options.md",
}
STATUSES = ("Tested", "Required", "Not tested", "Not supported")
HYPE = ("best-in-class", "world-class", "revolutionary", "seamless", "blazing", "enterprise-grade",
        "next-generation", "cutting-edge", "game-changing", "unmatched", "industry-leading", "effortless")


def read(path: str) -> str:
    return (ROOT / path).read_text()


def flat(text: str) -> str:
    return " ".join(text.split())


def plain(html: str) -> str:
    return flat(re.sub(r"<[^>]+>", " ", html))


def readme_first_screen() -> str:
    """Everything before the cockpit screenshot: the value, the status box, the next steps."""
    text = read("README.md")
    return text[: text.index("![Quadringent cockpit")]


def hero() -> str:
    return re.search(r'<section class="hero".*?</section>', read("site/index.html"), re.S)[0]


# README first screen


def test_readme_leads_with_the_value_statement():
    top = flat(readme_first_screen())
    assert top.startswith("# Quadringent **Open-source change data capture from IBM i journals to Snowflake.**")
    for phrase in ("no Kafka", "no license key", "your own AWS or GCP account",
                   "separate evidence for reading, delivery to Snowflake and reconciliation of events",
                   "Not measured"):
        assert phrase in top, phrase
    assert "IBM i (formerly AS/400 and iSeries)" in top
    # The status sits in the first sentence group, next to the value, not only in the box below.
    lead = top[: top.index("<!-- source")]
    assert "DEV/INT preview" in lead and "no production qualification is claimed" in lead
    # Event counts are what the destination proof reconciles; the page must not imply more.
    assert "table by table" not in top and "your data arrived" not in top
    assert "does not reconcile business values by key" in top


def test_readme_status_box_follows_the_value_and_keeps_every_honesty_statement():
    top = readme_first_screen()
    box = "\n".join(line for line in top.splitlines() if line.startswith(">"))
    text = flat(box.replace(">", " "))
    assert top.index("# Quadringent") < top.index("**Open-source change data capture") < top.index(box.splitlines()[0])
    assert "Community edition, DEV/INT preview" in text
    assert "No production qualification is claimed" in text
    runtime = json.loads(read("site/runtime-0.3.5.json"))
    recovery = json.loads(read("site/recovery-0.3.4.json"))
    assert f'{runtime["version"]} ({runtime["date_utc"]})' in text
    assert "Native CDC writes, reader outage and latency are not established" in text
    assert f'{recovery["version"]} (' in text and "8 cold/warm observations within 10 s" in text
    assert "Not a P95 and not an SLA" in text
    source_version = tomllib.loads(read("pyproject.toml"))["project"]["version"]
    assert f"Current source, {source_version}" in text and "needs fresh qualification" in text


def test_readme_offers_three_next_steps_in_order():
    top = flat(readme_first_screen())
    demo = top.index("Try the synthetic demo")
    doctor = top.index("quadringent doctor")
    compatibility = top.index("(docs/product/compatibility.md)")
    assert demo < doctor < compatibility
    assert "node ui/scripts/build-demo.mjs --serve" in top
    assert "docs/product/synthetic-demo.md" in top
    assert "docs/product/operator-guide.md#2-preflight-and-diagnosis" in top
    assert "(docs/product/faq.md)" in top
    assert "(docs/product/comparison.md)" in top
    for article in ("ibm-i-journals-for-cdc", "capture-delivery-reconciliation", "as400-to-snowflake-options"):
        assert f"(docs/articles/{article}.md)" in top, article
    assert "(docs/product/ibm-i-footprint.md)" in top


def test_readme_keeps_the_trust_section_and_the_licence_statement():
    text = read("README.md")
    assert "## Trust and verification" in text
    assert text.index("**Community edition · DEV/INT preview.**") < text.index("## Trust and verification")
    assert "with no license key or commercial table limit" in flat(text)


# Site hero


def test_hero_leads_with_the_value_statement():
    section = hero()
    title = plain(re.search(r"<h1[^>]*>(.*?)</h1>", section, re.S)[1])
    assert title == ("Open-source CDC from IBM i (AS/400) to Snowflake, "
                     "with separate evidence for reading and delivery.")
    paragraphs = [plain(p) for p in re.findall(r'<p class="hero__lead">(.*?)</p>', section, re.S)]
    assert len(paragraphs) == 3, "value, audience, mechanism"
    assert all(len(p.split()) <= 60 for p in paragraphs), "each lead paragraph stays short"
    lead = " ".join(paragraphs)
    for phrase in ("No Kafka and no license key", "your own AWS or GCP account", "Not measured",
                   "IBM i (formerly AS/400 and iSeries)", "reading back the source, history and mirror"):
        assert phrase in lead, phrase
    # The preview status is in the first paragraph and the eyebrow, not only in the facts block.
    assert "DEV/INT preview" in paragraphs[0] and "no production qualification is claimed" in paragraphs[0]
    assert "DEV/INT preview" in plain(re.search(r'<p class="eyebrow">(.*?)</p>', section, re.S)[1])
    assert "table by table" not in lead and "arrived" not in title.lower()
    head = read("site/index.html")
    for pattern in (r"<title>(.*?)</title>", r'<meta name="description" content="([^"]+)"'):
        assert "AS/400" in re.search(pattern, head, re.S)[1], pattern


def test_hero_keeps_the_honesty_box_next_to_the_value():
    section = hero()
    assert section.index('class="hero__lead"') < section.index('class="release-facts"')
    facts = plain(re.search(r'<dl class="release-facts".*?</dl>', section, re.S)[0])
    assert "DEV/INT preview" in facts
    assert "Not qualified, no SLA" in facts
    assert "native CDC, outage and latency not established" in facts
    assert "no P95 or SLA" in facts


def test_hero_has_one_primary_action_and_offers_the_faq_and_the_comparison():
    section = hero()
    actions = re.search(r'<div class="actions">.*?</div>', section, re.S)[0]
    assert actions.count("action-button--primary") == 1
    assert actions.count("<a ") == 3
    assert 'href="#install"' in actions and "Try the synthetic demo" in actions
    assert f'data-repo-path="{FAQ}"' in actions
    assert 'data-repo-path="docs/product/comparison.md"' in actions
    assert "mailto:" not in actions, "the pilot contact stays lower on the page"
    assert "mailto:" in read("site/index.html")


def test_nav_reaches_the_learn_section():
    html = read("site/index.html")
    nav = re.search(r'<nav class="site-nav".*?</nav>', html, re.S)[0]
    assert 'href="#learn"' in nav


def test_screenshot_caption_does_not_pass_an_old_build_for_the_current_one():
    caption = plain(re.search(r"<figcaption>(.*?)</figcaption>", read("site/index.html"), re.S)[1])
    assert "earlier cockpit build" in caption and "not the current version" in caption


def test_home_links_the_faq_the_comparison_and_the_articles_outside_the_hero():
    html = read("site/index.html")
    learn = re.search(r'<section class="section" id="learn".*?</section>', html, re.S)
    assert learn, "no section with id=learn"
    linked = re.findall(r'data-repo-path="([^"]+)" href="([^"]+)"', learn[0])
    assert sorted(path for path, _ in linked) == sorted([FAQ, *SIBLING_PAGES])
    for path, href in linked:
        assert href == f"../{path}"
    assert (ROOT / FAQ).is_file()
    assert html.index('id="learn"') > html.index('id="install"')


def test_home_keeps_the_synthetic_demo_and_the_trust_section():
    html = read("site/index.html")
    assert "node ui/scripts/build-demo.mjs --serve" in html
    assert 'id="trust"' in html and 'id="qualification"' in html and 'id="limits"' in html


@pytest.mark.parametrize("path", ["README.md", "site/index.html", FAQ])
def test_message_pages_use_no_hype_words(path):
    text = plain(read(path)).lower()
    for word in HYPE:
        assert word not in text, (path, word)


# FAQ


def faq_questions() -> list[tuple[str, str]]:
    blocks = re.split(r"^### ", read(FAQ), flags=re.M)[1:]
    questions = []
    for block in blocks:
        title, _, body = block.partition("\n")
        questions.append((title.strip(), re.split(r"^## ", body, flags=re.M)[0]))
    return questions


def test_faq_covers_what_a_buyer_asks_first():
    titles = " ".join(title.lower() for title, _ in faq_questions())
    for topic in ("ibm i release", "key", "column types", "ccsid", "initial", "add a column", "latency",
                  "per day", "killed", "hours", "receivers", "whole installation", "kubernetes",
                  "other than snowflake", "license fee", "cloud infrastructure", "snowflake bill",
                  "authorities", "my ibm i", "support", "production"):
        assert topic in titles, topic
    assert len(faq_questions()) >= 25


def test_every_faq_answer_carries_exactly_one_of_the_four_statuses():
    seen = set()
    for title, body in faq_questions():
        found = re.findall(r"^\*\*Status: ([A-Za-z ]+)\.\*\*", body, re.M)
        assert len(found) == 1, (title, found)
        assert found[0] in STATUSES, (title, found[0])
        assert len(re.findall(r"\*\*Status:", body)) == 1, title
        seen.add(found[0])
    assert seen == set(STATUSES), "each status should be used at least once"


def test_every_faq_answer_cites_sources_that_exist():
    for title, body in faq_questions():
        comments = re.findall(r"<!--\s*source:\s*(.*?)\s*-->", body, re.S)
        assert len(comments) == 1, (title, comments)
        for token in (item.strip() for item in comments[0].split(",")):
            assert token and (ROOT / token).exists(), (title, token)


def test_faq_gives_published_figures_only_with_their_scope_or_says_not_measured():
    answers = dict(faq_questions())
    latency = flat(answers["What latency has been measured?"])
    recovery = json.loads(read("site/recovery-0.3.4.json"))
    values = [v for site in recovery["sites"].values()
              for v in (site["cold_SQL_to_mirror_upper_bound_s"], site["warm_SQL_to_mirror_upper_bound_s"])]
    assert f"{min(values):.3f} s to {max(values):.3f} s" in latency
    assert "not a P95 and not an SLA" in latency and "0.3.4" in latency
    for question in ("What latency should I expect on the current version?",
                     "How much data per day can it carry?",
                     "How long does the copy of a large table take?",
                     "What will the cloud infrastructure cost?",
                     "What will Snowflake bill?"):
        assert "Not measured" in answers[question], question


def test_faq_states_the_kubernetes_position_without_any_roadmap_hint():
    answer = flat(dict(faq_questions())["Does it run without Kubernetes?"])
    assert "k3s" in answer and "EKS or GKE" in answer and "Not tested" in answer
    assert "quadringent local" in answer and "try-it-local.md" in answer
    assert "has not been validated end to end" in answer
    assert not re.search(r"being prepared|planned|roadmap|upcoming|coming|soon|no date|\b(20\d\d|Q[1-4])\b", answer, re.I)


def test_faq_scopes_the_ibm_i_and_data_claims_to_this_repository():
    answers = {title: flat(body) for title, body in faq_questions()}
    ibm_i = answers["What does it do on my IBM i?"]
    assert "Scope: the Java reader code in this repository" in ibm_i
    assert "not a proof" in ibm_i
    assert "third-party journal-parsing library" in ibm_i and "CHGJRN" in ibm_i
    assert "Scope: the code of this repository" in answers["Does my data go to the project?"]
    ptf = answers["Does it work on IBM i 7.4, 7.6, or 7.3 and earlier?"]
    assert "IBM documents" not in ptf, "attributing to IBM needs a URL and an access date"


def test_faq_opens_with_five_short_answers_that_link_to_real_entries():
    text = read(FAQ)
    short = text[text.index("## Short answers"): text.index("## IBM i source")]
    links = re.findall(r"\]\(#([^)]+)\)", short)
    assert len(links) == 5
    assert set(links) <= anchors(ROOT / FAQ)
    for topic in ("IBM i release", "Primary key", "Production", "Latency", "Cost"):
        assert f"**{topic}" in short, topic
    assert "**Status:" not in short


def relative_links(markdown: str) -> list[str]:
    return re.findall(r"\]\(([^)\s]+)\)", markdown)


def anchors(path: Path) -> set[str]:
    found = set()
    for line in path.read_text().splitlines():
        match = re.match(r"^#{1,6}\s+(.*?)\s*#*\s*$", line)
        if match:
            slug = re.sub(r"[^\w\- ]", "", re.sub(r"[`*]", "", match[1].strip().lower())).replace(" ", "-")
            found.add(slug)
    return found


def test_faq_links_resolve():
    links = relative_links(read(FAQ))
    assert links
    faq_dir = posixpath.dirname(FAQ)
    linked_pages = set()
    for link in links:
        if re.match(r"^[a-z][a-z0-9+.-]*:", link):
            continue
        path, _, fragment = link.partition("#")
        resolved = posixpath.normpath(posixpath.join(faq_dir, path)) if path else FAQ
        linked_pages.add(resolved)
        if resolved in SIBLING_PAGES:
            continue
        target = ROOT / resolved
        assert target.is_file(), link
        if fragment and target.suffix == ".md":
            assert fragment in anchors(target), link
    for page in ("docs/product/ibm-i-footprint.md", "SUPPORT.md", "docs/product/compatibility.md",
                 "docs/product/your-data.md", "docs/governance.md", "docs/product/comparison.md",
                 "docs/articles/as400-to-snowflake-options.md"):
        assert page in linked_pages, page


def test_faq_backticked_paths_exist_and_no_private_identity():
    from check_publication import scan_text

    text = read(FAQ)
    for token in set(re.findall(r"`([A-Za-z0-9_./-]+\.(?:py|java|md|yaml|yml|tf|json|sh))`", text)):
        assert (ROOT / token).exists() or (ROOT / "docs/product" / token).exists(), token
    assert scan_text(text, "faq.md") == []
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text), "no email address belongs on the FAQ"
    assert "/Users/" not in text and "/home/" not in text


def test_faq_does_not_name_other_products_or_vendors():
    """Claims about other products belong to the comparison page, with their public sources."""
    text = read(FAQ)
    questions = " ".join(re.findall(r"^### .*$", text, re.M))
    body = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    for vendor in ("Fivetran", "Qlik", "Precisely", "Striim", "HVR", "Matillion", "AWS DMS", "Airbyte", "Confluent"):
        assert vendor not in body, vendor
    assert "Kafka" in body and "Kafka" not in questions


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.hrefs.append(dict(attrs).get("href", ""))


def test_home_anchor_links_added_by_this_wave_resolve():
    html = read("site/index.html")
    parser = _Links()
    parser.feed(html)
    ids = set(re.findall(r'\bid="([^"]+)"', html))
    for href in parser.hrefs:
        if href.startswith("#"):
            assert href[1:] in ids, href

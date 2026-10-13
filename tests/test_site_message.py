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
    """Everything before the cockpit screenshot: the thesis, the status line, the uncomfortable facts."""
    text = read("README.md")
    return text[: text.index("<picture>")]


def hero() -> str:
    return re.search(r'<section class="hero".*?</section>', read("site/index.html"), re.S)[0]


# README first screen


def test_readme_leads_with_the_thesis_and_the_value_statement():
    top = flat(readme_first_screen())
    assert top.startswith("# Quadringent **A running reader doesn't mean your data arrived.**")
    for phrase in ("no Kafka", "no license key", "your own AWS or GCP account", "Not measured",
                   "journal reading, Snowflake delivery and reconciliation separately"):
        assert phrase in top, phrase
    assert "IBM i (formerly AS/400 and iSeries)" in top
    assert "We build" in top, "the README speaks as the maintainers"
    # Event counts are what the destination proof reconciles; the page must not imply more.
    assert "table by table" not in top
    assert "does not compare business values by key" in top


def test_readme_states_the_status_once_and_points_to_the_evidence_page():
    text = read("README.md")
    top = flat(readme_first_screen())
    assert "Status: Preview, DEV/INT only. We claim no production qualification." in top
    assert len(re.findall(r"no production qualification", text, re.I)) == 1
    assert text.count("DEV/INT") <= 2, "the status line and the pilot call to action"
    assert "[docs/evidence.md](docs/evidence.md)" in top
    source_version = tomllib.loads(read("pyproject.toml"))["project"]["version"]
    assert f"Source version {source_version} needs fresh qualification" in top
    runtime = json.loads(read("site/runtime-0.3.5.json"))
    recovery = json.loads(read("site/recovery-0.3.4.json"))
    assert f'{runtime["version"]} (initial copy)' in top and f'{recovery["version"]} (recovery)' in top
    assert "2026-10-12" in top
    assert set(re.findall(r"\b0\.4\.\d+\b", text)) <= {source_version}, "no stale source version"


def test_readme_names_the_uncomfortable_facts_first():
    top = flat(readme_first_screen())
    assert "3 of 4 targets missed our 10 s latency bar" in top and "a later run passed on all four" in top
    assert "CPU and I/O cost of the reader is Not measured" in top
    assert "Only IBM i 7.5, on one shared server, has been tested, in a historical measurement on one table" in top
    assert "no published summary names the release" in top
    assert top.index("3 of 4 targets") < top.index("Only IBM i 7.5")


def test_readme_shows_the_synthetic_evidence_and_a_real_doctor_transcript():
    text = read("README.md")
    assert "<picture>" in text and 'media="(prefers-color-scheme: dark)"' in text
    for name in ("cockpit-evidence-dark.png", "cockpit-evidence-light.png"):
        assert f"site/assets/{name}" in text and (ROOT / "site/assets" / name).is_file(), name
    alt = re.search(r'<img [^>]*alt="([^"]+)"', text)[1]
    assert "Synthetic demo" in alt and "Not measured" in alt
    assert "120" in text and "118" in text
    assert "real output against a placeholder host" in text and "ibmi.example.test" in text
    assert "Quadringent doctor: BLOCKED" in text


def test_readme_offers_the_next_steps_in_order():
    text = flat(read("README.md"))
    demo = text.index("node ui/scripts/build-demo.mjs --serve")
    doctor = text.index("`quadringent doctor`")
    local = text.index("`quadringent local`")
    compatibility = text.index("(docs/product/compatibility.md)")
    assert demo < doctor < local < compatibility
    assert "(docs/product/synthetic-demo.md)" in text
    assert "docs/product/operator-guide.md#2-preflight-and-diagnosis" in text
    assert "(docs/product/try-it-local.md)" in text
    assert text.count("listed under Unreleased") == 1, "the unreleased note appears once"
    assert "has not been validated end to end" in text
    assert "(docs/product/faq.md)" in text
    assert "(docs/product/comparison.md)" in text
    for article in ("ibm-i-journals-for-cdc", "capture-delivery-reconciliation", "as400-to-snowflake-options"):
        assert f"(docs/articles/{article}.md)" in text, article
    assert "(docs/product/ibm-i-footprint.md)" in text


def test_readme_keeps_the_trust_section_and_the_licence_statement():
    text = read("README.md")
    assert "## Trust and verification" in text
    assert text.index("Status: Preview, DEV/INT only.") < text.index("## Trust and verification")
    assert "with no license key or commercial table limit" in flat(text)
    assert "[project model](docs/licensing.md)" in text


# Site hero


def home() -> str:
    return read("site/index.html")


def text_of_home(html: str) -> str:
    import html as html_module
    inline = re.sub(r"</?(?:span|time|a|strong|code|em|b)(?:\s[^>]*)?>", "", html)
    return flat(html_module.unescape(re.sub(r"<[^>]+>", " ", inline)).replace("\xa0", " "))


def source_version() -> str:
    return tomllib.loads(read("pyproject.toml"))["project"]["version"]


def historical_worst_sql_start() -> float:
    targets = json.loads(read("site/qualification-0.3.4.json"))["cold_warehouse_latency"]["targets"]
    return max(values["maximum_SQL_start_seconds"] for values in targets.values())


def test_hero_leads_with_the_thesis_and_one_sentence_of_value():
    section = hero()
    title = text_of_home(re.search(r"<h1[^>]*>(.*?)</h1>", section, re.S)[1])
    assert title == "A running reader doesn\u2019t mean your data arrived."
    leads = [text_of_home(p) for p in re.findall(r'<p class="hero__lead">(.*?)</p>', section, re.S)]
    assert len(leads) == 1, "one lead paragraph"
    lead = leads[0]
    assert len(lead.split()) <= 60 and lead.count(". ") == 1, "two short sentences: what it is, what it reports"
    what, reports = lead.split(". ")
    for phrase in ("IBM i (AS/400)", "Snowflake", "no Kafka and no license key", "your own AWS or GCP account"):
        assert phrase in what, phrase
    assert len(what.split()) <= 35 and len(reports.split()) <= 35, "each sentence stays scannable"
    for phrase in ("journal reading", "Snowflake delivery", "event-count reconciliation", "each table", "Not measured"):
        assert phrase in reports, phrase
    assert "does not reconcile" not in lead and "business values" not in lead
    head = home()
    for pattern in (r"<title>(.*?)</title>", r'<meta name="description" content="([^"]+)"'):
        assert "AS/400" in re.search(pattern, head, re.S)[1], pattern


def test_hero_has_one_status_line_and_states_the_uncomfortable_fact():
    section = hero()
    status = text_of_home(re.search(r'<p class="status-line">.*?</p>', section, re.S)[0])
    for phrase in ("Preview, DEV/INT only", "No production qualification", "no SLA", f"Source {source_version()}"):
        assert phrase in status, phrase
    runtime = json.loads(read("site/runtime-0.3.5.json"))
    assert f'last observed release {runtime["version"]} ({runtime["date_utc"]})' in status
    assert 'data-repo-path="docs/evidence.md" href="../docs/evidence.md"' in section
    fact = text_of_home(re.search(r'<p class="hero__fact">(.*?)</p>', section, re.S)[1])
    targets = json.loads(read("site/qualification-0.3.4.json"))["cold_warehouse_latency"]["targets"]
    missed = [name for name, values in targets.items() if not values["SQL_and_ACK_threshold_passed"]]
    assert f"{len(missed)} of {len(targets)} targets missed our 10 s bar" in fact
    assert f"{historical_worst_sql_start():.3f} s" in fact
    assert "later 0.3.4 run passed on all four" in fact and "P95" in fact and "SLA" in fact
    # Order: thesis, value, status, fact, action, then the product on the right.
    order = [section.index(token) for token in ("<h1", 'class="hero__lead"', 'class="status-line"',
                                                  'class="hero__fact"', 'class="actions"', "<figure")]
    assert order == sorted(order)
    assert "release-facts" not in section, "the spec card is gone"


def test_hero_has_one_primary_action_and_one_secondary_link():
    section = hero()
    actions = re.search(r'<div class="actions">.*?</div>', section, re.S)[0]
    assert actions.count("action-button--primary") == 1
    assert actions.count("<a ") == 2
    assert 'href="#try"' in actions and "Try the synthetic demo" in actions
    assert f'data-repo-path="{FAQ}"' in actions
    assert "mailto:" not in actions, "the pilot contact stays lower on the page"
    assert "mailto:" in home()


def test_hero_shows_the_current_cockpit_captioned_as_synthetic():
    section = hero()
    figure = re.search(r"<figure.*?</figure>", section, re.S)[0]
    # The displayed image is a 1x crop of the three readings, legible on the fold; the full capture is one click away.
    for name in ("cockpit-evidence-strip-light.png", "cockpit-evidence-strip-dark.png"):
        assert f"assets/{name}" in figure
        assert (ROOT / "site/assets" / name).is_file()
    for name in ("cockpit-evidence-light.png", "cockpit-evidence-dark.png"):
        assert (ROOT / "site/assets" / name).is_file()
    assert 'href="assets/cockpit-evidence-light.png"' in figure, "the full capture, with its Synthetic demo banner, stays linked"
    assert 'width="1220" height="420"' in figure
    assert "prefers-color-scheme: dark" in figure
    text = text_of_home(figure)
    assert "Demonstration" in text and "synthetic data" in text
    caption = text_of_home(re.search(r"<figcaption>(.*?)</figcaption>", figure, re.S)[1])
    assert source_version() in caption, "the screenshot is of the current source version"
    assert "synthetic" in caption and "Not measured" in caption
    alt = re.search(r'alt="([^"]+)"', figure)[1]
    for value in ("120 events", "118 events", "Not measured"):
        assert value in alt, value


def test_nav_has_five_entries_and_each_target_exists():
    html = home()
    nav = re.search(r'<nav class="site-nav".*?</nav>', html, re.S)[0]
    items = re.findall(r"<li>(.*?)</li>", nav, re.S)
    assert len(items) == 5
    hrefs = re.findall(r'href="(#[^"]+)"', nav)
    assert hrefs == ["#how-it-works", "#try", "#evidence", "#docs"]
    assert 'data-repo-root="true"' in nav
    for href in hrefs:
        assert f'id="{href[1:]}"' in html, href


def test_home_is_a_hero_then_five_sections():
    ids = re.findall(r'<section class="section" id="([^"]+)"', home())
    assert ids == ["how-it-works", "try", "evidence", "docs", "services"]
    assert "<table" not in home(), "qualification tables live on the evidence page"
    assert 'id="qualification"' not in home() and 'id="limits"' not in home()
    assert len(home()) < 30000, "the landing page stays short"


def test_caveats_are_stated_once_on_the_home_page():
    html = home()
    text = text_of_home(html)
    assert html.count("DEV/INT") <= 2, "the status line and the pilot call to action"
    assert len(re.findall(r"no production qualification", text, re.I)) <= 2
    for retired in ("Not established", "Not executed", "To establish", "Bounded trials", "Limited path", "Not qualified"):
        assert retired not in text, retired
    assert len(re.findall(r'class="eyebrow', html)) <= 2
    for paragraph in re.findall(r"<(?:p|li|dd)[ >].*?</(?:p|li|dd)>", html, re.S):
        assert paragraph.count("<strong") <= 1, paragraph[:80]
    assert "<strong>" not in re.search(r'<ul class="ledger">.*?</ul>', html, re.S)[0]


def test_how_it_works_draws_the_receiver_chain_with_the_demo_readings():
    html = home()
    chain = re.search(r'<figure class="chain".*?</figure>', html, re.S)[0]
    text = text_of_home(chain)
    receivers = re.findall(r'<li class="chain__item rcv[ "]', chain)
    assert len(receivers) >= 3
    for phrase in ("attached", "detached", "Reader position", "checkpoint outside the reader", "Raw storage",
                   "Snowflake", "120 events", "118 events", "Not measured", "illustrative", "synthetic demo"):
        assert phrase in text, phrase
    assert "text-wrap" not in chain and "gradient" not in chain
    how = text_of_home(re.search(r'<section class="section" id="how-it-works".*?</section>', html, re.S)[0])
    for phrase in ("IBM i (formerly AS/400 and iSeries)", "Db2 for i", "change data capture", "Java/JTOpen",
                   "needs human approval", "MCP", "every call is audited", "never a zero or a silent estimate",
                   "verified on historical versions only"):
        assert phrase in how, phrase


def test_only_commands_use_the_screen_block_and_the_doctor_excerpt_is_labelled_real():
    html = home()
    screens = re.findall(r'<pre class="screen[^"]*"[^>]*>(.*?)</pre>', html, re.S)
    assert len(screens) == 3
    joined = text_of_home(" ".join(screens))
    for command in ("npm ci --prefix ui", "node ui/scripts/build-demo.mjs --serve", "quadringent install --cloud gcp",
                    "quadringent doctor --check-ibmi ibmi.example.test --tls"):
        assert command in joined, command
    assert "BLOCKED" not in joined, "output is not styled as a command"
    output = text_of_home(re.search(r'<pre class="code terminal__out".*?</pre>', html, re.S)[0])
    for phrase in ("BLOCKED", "[FAIL]", "[UNKN]", "[SKIP]", "ibmi.example.test", "The doctor never sends IBM i credentials",
                   "lines omitted", "exit 1"):
        assert phrase in output, phrase
    # The excerpt starts at the first failing check and marks every cut with the number of lines dropped.
    assert output.index("local_tools.helm") < output.index("network.tcp.command")
    assert output.index("[FAIL] local_tools.helm") < output.index("2 more [FAIL] lines omitted") < output.index("[FAIL] network.tcp.command")
    assert output.index("6 more [FAIL] lines omitted") < output.index("[UNKN]")
    shown = output.count("[FAIL] ") - len(re.findall(r"\[\.\.\. \d+ more \[FAIL\]", output))
    omitted = sum(int(n) for n in re.findall(r"\[\.\.\. (\d+) more \[FAIL\]", output))
    assert shown + omitted == 10 and "10 failed" in output, "shown plus omitted lines add up to the header count"
    note = text_of_home(re.search(r'<p class="terminal__note">(.*?)</p>', html, re.S)[1])
    assert "Real output" in note and "placeholder host" in note


def test_evidence_section_links_the_evidence_page_and_every_receipt():
    html = home()
    section = re.search(r'<section class="section" id="evidence".*?</section>', html, re.S)[0]
    assert 'data-repo-path="docs/evidence.md" href="../docs/evidence.md"' in section
    for receipt in ("runtime-0.3.5.json", "recovery-0.3.4.json", "qualification-0.3.4.json"):
        assert f'href="{receipt}"' in section and (ROOT / "site" / receipt).is_file()
    text = text_of_home(section)
    for word in ("Measured", "Not tested", "Not measured"):
        assert word in text
    assert f"{source_version()}: native CDC, reader outage, latency and complete cloud-loss recovery need fresh qualification" in text
    assert "not a P95 and not an SLA" in text
    from build_site import DOC_PAGES
    assert DOC_PAGES["docs/evidence.md"][0] == "evidence"
    assert (ROOT / "docs/evidence.md").is_file()


def test_home_links_the_faq_the_comparison_and_the_articles_outside_the_hero():
    html = home()
    learn = re.search(r'<div class="index" id="learn">.*?</ul>', html, re.S)
    assert learn, "no list with id=learn"
    linked = re.findall(r'data-repo-path="([^"]+)" href="([^"]+)"', learn[0])
    assert sorted(path for path, _ in linked) == sorted([FAQ, *SIBLING_PAGES])
    for path, href in linked:
        assert href == f"../{path}"
    assert (ROOT / FAQ).is_file()
    assert html.index('id="learn"') > html.index('id="try"')


def test_home_keeps_the_synthetic_demo_and_the_trust_list():
    html = home()
    assert "node ui/scripts/build-demo.mjs --serve" in html
    assert 'id="trust"' in html and 'id="evidence"' in html and 'id="docs"' in html


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
        found = re.findall(r"^(Tested|Required|Not tested|Not supported)\. ", body, re.M)
        assert len(found) == 1, (title, found)
        assert found[0] in STATUSES, (title, found[0])
        assert "Status:" not in body, title
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
        assert topic in short, topic
    assert "Status:" not in short


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


def test_documented_demo_counts_equal_the_cockpit_fixture():
    """The 120 / 118 figures live in several docs; the fixture is their single source."""
    fixture = read("ui/src/data/fixtures/cockpitDemo.ts")
    reading = int(re.search(r"const reading = \{[^}]*?event_count:\s*(\d+)", fixture, re.S)[1])
    delivery = int(re.search(r"const delivery = \{[^}]*?event_count:\s*(\d+)", fixture, re.S)[1])
    assert (reading, delivery) == (120, 118)
    for path in ("README.md", "docs/product/synthetic-demo.md",
                 "docs/articles/capture-delivery-reconciliation.md"):
        text = flat(read(path))
        assert f"{reading}" in text and f"{delivery}" in text, path

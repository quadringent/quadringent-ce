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


def test_readme_states_version_and_licence_once_and_points_to_the_evidence_page():
    text = read("README.md")
    top = flat(readme_first_screen())
    assert top.count("Version 0.4 \u00b7 Apache-2.0") == 1
    for disclaimer in ("Preview, DEV/INT only", "no production qualification", "claim no production", "needs fresh qualification"):
        assert disclaimer.lower() not in text.lower(), disclaimer
    assert "(docs/evidence.md)" in top
    project = tomllib.loads(read("pyproject.toml"))["project"]
    assert project["version"].startswith("0.4."), "the status line says Version 0.4"
    source_version = project["version"]
    published = re.search(r"latest published prerelease (\d+\.\d+\.\d+)", top)[1]
    assert f"Source {source_version} with Unreleased changes; latest published prerelease {published}" in top
    assert set(re.findall(r"\b0\.4\.\d+\b", text)) <= {source_version, published}, "no stale source version"
    runtime = json.loads(read("site/runtime-0.3.5.json"))
    recovery = json.loads(read("site/recovery-0.3.4.json"))
    assert f'(release {runtime["version"]}, {runtime["date_utc"]})' in top
    assert f'reader-kill recovery with source writes on {recovery["version"]}' in top and "four cloud targets" in top
    assert "initial copy of synthetic tables was tested against a real IBM i" in top and "end to end" not in top
    assert len(runtime["initial_copy"]["sites"]) == 4 == len(recovery["sites"])


def test_readme_first_screen_is_thesis_status_proof_risk_reversal_and_a_poc_call_to_action():
    top = flat(readme_first_screen())
    order = ["A running reader doesn't mean your data arrived.", "Version 0.4 \u00b7 Apache-2.0",
             "tested against a real IBM i and a real Snowflake account",
             "What a proof of concept touches", "[Run a POC on your DEV IBM i]",
             "[talk to us about a pilot]"]
    positions = [top.index(item) for item in order]
    assert positions == sorted(positions)
    # The risk-reversal items are facts that the cited pages and tests back.
    for item in ("installs nothing on your IBM i", "docs/product/ibm-i-footprint.md", "docs/product/your-data.md",
                 "`quadringent doctor`", "`quadringent uninstall` removes the Helm release",
                 "bucket, checkpoints and identities stay", "docs/product/install-default.md#uninstall",
                 "Apache-2.0, with no license key"):
        assert item in top, item
    assert "installation guide is the path that exists today" in top
    assert "`quadringent local`" in top and "is not in a published release yet" in top
    # The uncomfortable facts moved to the evidence page, where they stay complete.
    evidence = flat(read("docs/evidence.md"))
    for fact in ("3 of 4 targets missed our 10 s bar", "IBM i CPU and I/O cost is Not measured",
                 "Only IBM i 7.5 has been tested, on one shared qualification server",
                 "The published summaries do not name the release"):
        assert fact in evidence, fact
    assert "uninstall" in flat(read("docs/product/install-default.md")).lower()
    assert "## Uninstall" in read("docs/product/install-default.md")


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
    doctor = text.index("`quadringent doctor` ([preflight and diagnosis]")
    local = text.index("`quadringent local` copies one table")
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
    assert text.index("Version 0.4 \u00b7 Apache-2.0") < text.index("## Trust and verification")
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
    for phrase in ("journal reading", "Snowflake delivery", "event-count reconciliation"):
        assert phrase in reports, phrase
    assert "each table" in reports.lower()
    assert "does not reconcile" not in lead and "business values" not in lead
    head = home()
    for pattern in (r"<title>(.*?)</title>", r'<meta name="description" content="([^"]+)"'):
        assert "AS/400" in re.search(pattern, head, re.S)[1], pattern


def minor_version() -> str:
    return ".".join(source_version().split(".")[:2])


# Disclaimers retired by W8. The evidence page keeps every limit factually; front pages do not apologise.
RETIRED_DISCLAIMERS = ("Preview, DEV/INT only", "DEV/INT preview", "no production qualification",
                       "We claim no production", "not a release qualification", "nothing here is a production claim",
                       "not yet run in production", "No paid offer")


def test_hero_has_a_version_line_and_no_disclaimer():
    section = hero()
    status = text_of_home(re.search(r'<p class="status-line">.*?</p>', section, re.S)[0])
    assert status.startswith(f"Version {minor_version()} \u00b7 Apache-2.0")
    published = re.search(r"latest published prerelease (\d+\.\d+\.\d+)", status)[1]
    assert f"Source {source_version()} with Unreleased changes, latest published prerelease {published}" in status
    for retired in (*RETIRED_DISCLAIMERS, "no SLA", "No production"):
        assert retired not in text_of_home(section), retired
    assert "hero__fact" not in section and "release-facts" not in section
    # Order: thesis, value, version, proof, actions, then the product on the right.
    order = [section.index(token) for token in ("<h1", 'class="hero__lead"', 'class="status-line"', 'class="proof-line"',
                                                  'class="actions"', "<figure")]
    assert order == sorted(order)


def test_hero_proof_line_matches_the_receipts_and_claims_nothing_more():
    """The initial comparison stays bound to the published release and its dated receipt."""
    raw = re.search(r'<p class="proof-line">.*?</p>', hero(), re.S)[0]
    proof = text_of_home(raw)
    current = json.loads(read("site/qualification-0.4.5.json"))
    initial = current["volume"]["initial_four_layer_equality"]
    assert current["publication_status"] == "PUBLIC_OBSERVED_SNAPSHOT"
    assert current["overall_status"] == "PARTIAL_NOT_FULLY_QUALIFIED"
    assert initial["status"] == "PASS_SCOPED_INITIAL_FULL_TYPED_EQUALITY"
    assert initial["source_rows"] == initial["raw_snapshot_events"] == initial["history_rows"] == initial["mirror_rows"] == 1_000_000
    assert initial["business_fields"] == 7 and initial["ignored_business_fields"] == initial["logical_duplicates"] == 0
    assert initial["foreign_history_epochs_filtered_out"] is False
    assert proof.startswith("One million synthetic rows compared across the source, original RAW snapshot, Snowflake history and mirror on AWS VM ")
    assert f'(release {current["version"]}, {initial["finished_utc"][:10]})' in proof
    assert "end to end" not in proof
    assert current["endurance"]["completed_24h_proven"] is False
    assert current["latency"]["p95_or_general_sla_qualified"] is False
    for word in ("production", "customer", "pilot", "SLA", "latency", "P95", "guarantee", "certif"):
        assert word.lower() not in proof.lower(), word
    assert raw.count("<a ") == 1 and 'data-repo-path="docs/evidence.md" href="../docs/evidence.md">Receipt summaries, volumes and limits</a>' in raw
    evidence = re.search(r'<section class="section" id="evidence".*?</section>', home(), re.S)[0]
    assert current["artifact_source_sha"][:12] in evidence
    assert "UTC snapshot" in text_of_home(evidence)
    assert "This snapshot does not report the run’s current state" in text_of_home(evidence)
    for receipt in ("qualification-0.4.5.json", "runtime-0.3.5.json", "recovery-0.3.4.json"):
        assert f'href="{receipt}"' in evidence and (ROOT / "site" / receipt).is_file()
    assert 'data-repo-path="docs/evidence.md" href="../docs/evidence.md"' in evidence


def test_historical_proof_scopes_remain_separate_from_the_current_initial_comparison():
    runtime = json.loads(read("site/runtime-0.3.5.json"))
    recovery = json.loads(read("site/recovery-0.3.4.json"))
    assert runtime["public_ready"] and recovery["public_ready"]
    assert runtime["production_qualified"] is False and runtime["external_pilot_validated"] is False
    assert runtime["initial_copy"]["new_source_writes"] == 0
    sites = runtime["initial_copy"]["sites"]
    assert len(sites) == 4 and runtime["initial_copy"]["tables_per_target"] == 2
    assert all(site["passed"] for site in sites.values())
    assert {site["mirror_rows"] for site in sites.values()} == {110}
    assert len(recovery["sites"]) == 4
    assert all(site["actual_reader_SIGKILL_and_ACK_inside_stop_passed"] for site in recovery["sites"].values())
    assert recovery["observed_at_utc"].startswith(runtime["date_utc"])
    evidence = flat(read("docs/evidence.md"))
    assert "110" in evidence and "Only IBM i 7.5 has been tested" in evidence
    release = re.compile(r"(?<![\d.])7\.5(?!\d)")
    assert not release.search(json.dumps(runtime)) and not release.search(json.dumps(recovery))
    current = json.loads(read("site/qualification-0.4.5.json"))
    assert current["version"] != runtime["version"] != recovery["version"]


def test_the_uncomfortable_10_s_fact_stays_on_the_page_in_the_evidence_ledger():
    targets = json.loads(read("site/qualification-0.3.4.json"))["cold_warehouse_latency"]["targets"]
    passed = [name for name, values in targets.items() if values["SQL_and_ACK_threshold_passed"]]
    ledger = text_of_home(re.search(r'<ul class="ledger">.*?</ul>', home(), re.S)[0])
    assert f"passed our 10 s bar on {len(passed)} of {len(targets)} targets" in ledger
    assert f"{historical_worst_sql_start():.3f} s" in ledger and "P95" in ledger
    assert "The later run above passed on all four" in ledger


def test_hero_has_one_poc_call_to_action_one_text_link_and_reaches_the_zero_setup_demo():
    section = hero()
    actions = re.search(r'<div class="actions">.*?</div>', section, re.S)[0]
    assert actions.count("action-button--primary") == 1
    assert actions.count("<a ") == 2, "W7: one call to action and one text link"
    assert re.search(r'action-button--primary" data-repo-path="docs/product/install-default.md" '
                     r'href="\.\./docs/product/install-default\.md">Run a POC on your DEV IBM i</a>', actions)
    assert 'href="#pilot-contact">Talk to us about a pilot</a>' in actions
    assert "mailto:" not in actions, "the pilot contact stays lower on the page"
    assert "mailto:" in home()
    assert (ROOT / "docs/product/install-default.md").is_file()
    # The zero-setup demo stays one click from the hero, in the screenshot caption, and is the second column of #try.
    figure = re.search(r"<figcaption>(.*?)</figcaption>", section, re.S)[1]
    assert '<a href="#try">Run it yourself, no setup</a>' in figure
    assert "node ui/scripts/build-demo.mjs --serve" in home()


def test_hero_lists_five_verified_short_assurances_and_the_preconditions():
    """The risk-reversal list: plain, short, scoped, and each item is backed by a page of this repository."""
    block = re.search(r'<ul class="poc__list">.*?</ul>', hero(), re.S)[0]
    items = re.findall(r"<li>(.*?)</li>", block, re.S)
    assert len(items) == 5
    for item in items:
        assert len(text_of_home(item).split()) <= 30, item
        assert "<strong" not in item
    text = text_of_home(block)
    footprint, your_data = flat(read("docs/product/ibm-i-footprint.md")), flat(read("docs/product/your-data.md"))
    compatibility = flat(read("docs/product/compatibility.md"))
    # "Only reads" is scoped to the reader, as the footprint page scopes it.
    assert "Read-only on your IBM i: SELECT and IBM journal APIs, nothing installed" in text
    assert "Nothing is installed on the IBM i" in footprint and "The reader code issues `SELECT` statements" in footprint
    assert 'data-repo-path="docs/product/ibm-i-footprint.md"' in block
    # The precondition is on the list: journaling with *BOTH images is switched on by an administrator, receivers pile up.
    assert "Needs the POC tables journaled with before and after images" in text and "Prerequisites and retention" in text
    assert "STRJRNPF" in footprint and "IMAGES(*BOTH)" in footprint and "they accumulate until you delete them" in footprint
    assert "Journaled tables, `*BOTH` images | Required" in compatibility
    assert "stays in your own AWS or GCP and Snowflake accounts" in text
    assert "The reader, the loaders and the control plane run in the Kubernetes cluster" in your_data
    assert 'data-repo-path="docs/product/your-data.md"' in block
    # The doctor and local mode are in the source, not in a published release: the page says so.
    assert "`quadringent doctor`" in changelog_unreleased() and "`quadringent local`" in changelog_unreleased()
    assert "(next release)" in text
    assert "never signs on" in footprint and "without signing on" in text
    # Uninstall: the install guide's own words, including the Terraform resources that keep running and billing.
    install = flat(read("docs/product/install-default.md"))
    assert "removes only the Helm release" in install
    assert "Terraform resources, raw data, checkpoints and identities remain" in install
    assert "quadringent uninstall removes the release; cloud resources, raw data and checkpoints stay, and bill, until you destroy them" in text
    assert "terraform destroy" in install
    assert "Snowflake tables" not in text, "no page states what happens to Snowflake tables on uninstall"
    # No unscoped limit claim: the capture session holds at most 32 tables.
    assert "no table limit" not in home()
    assert "ISERIES_TABLES must have at most 32 tables" in read("java/src/main/java/io/quadringent/as400/JournalSession.java")
    assert "at most 32 tables per capture session" in compatibility
    assert "commercial table limit" in flat(read("README.md")) and "commercial table limit" in text_of_home(home())


def changelog_unreleased() -> str:
    match = re.search(r"^## Unreleased\n(.*?)(?=^## )", read("CHANGELOG.md"), re.S | re.M)
    assert match
    return match[1]


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
    assert "synthetic" in caption
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


def words(fragment: str) -> int:
    return len(text_of_home(re.sub(r"<(script|style|svg)\b.*?</\1>", "", fragment, flags=re.S)).split())


def test_landing_page_stays_concise():
    """W7 asked for 1085 words (30% under the 1551-word page it replaced). W8 adds three things that reviewers asked for
    and that a buyer decides on: the scoped proof line, the IBM i preconditions (journaling, receiver growth, billable
    resources after uninstall) and the short list of what a POC needs. That is about 100 words more, so the page cap is
    1200 and the hero is capped on its own; doc rows stay a title and a few words."""
    html = home()
    body = re.search(r"<body>.*</body>", html, re.S)[0]
    assert words(body) <= 1200
    assert words(hero()) <= 290, "the hero: lead, status, proof, two links, list of five, screenshot caption"
    assert words(re.search(r'<div class="hero__copy">.*?</div>\s*<div class="hero__aside">', hero(), re.S)[0]) <= 160
    assert words(re.search(r'<div class="needs">.*?</ul>', html, re.S)[0]) <= 75
    hero_text = text_of_home(hero())
    figure = text_of_home(re.search(r"<figcaption>(.*?)</figcaption>", hero(), re.S)[1])
    assert len(figure.split()) <= 25, "one-line caption"
    for description in re.findall(r"</a><span>(.*?)</span>", html, re.S):
        assert len(description.split()) <= 8, description
    assert hero_text


OVERCLAIM_TERMS = ("production-ready", "production-grade", "enterprise-grade", "battle-tested", "used in production",
                   "in production at", "customers", "trusted by", "guaranteed", "sla", "zero data loss", "100%")


def test_try_section_lists_what_a_poc_needs_with_sources():
    """A buyer can see what to bring: needs only, each backed by the compatibility and install pages."""
    html = home()
    block = re.search(r'<div class="needs">.*?</div>', html, re.S)[0]
    items = [text_of_home(item) for item in re.findall(r"<li>(.*?)</li>", block, re.S)]
    assert len(items) == 4 and all(len(item.split()) <= 20 for item in items), items
    text = text_of_home(block)
    compatibility, install = flat(read("docs/product/compatibility.md")), flat(read("docs/product/install-default.md"))
    assert "A non-production IBM i with a journaled table that has a primary key" in text
    assert "Primary key or unique constraint | Required" in compatibility and "Journaled tables" in compatibility
    assert "read authority on the journal, its receivers and the tables" in text and "`*USE` on the journal" in compatibility
    assert "An AWS or GCP account, with Terraform, Helm and kubectl on the machine that installs" in text
    assert "terraform, helm and kubectl must be on PATH" in install
    assert "A Snowflake account, and an administrator to run the setup script once" in text
    assert "Runs the script generated by the wizard once" in compatibility
    assert 'data-repo-path="docs/product/compatibility.md"' in block
    assert not re.search(r"\b\d+ (?:minutes?|hours?|days?)\b", text), "no time estimate was measured"
    assert html.index('class="needs"') < html.index('<div class="try">')


def test_pilot_offer_is_forward_looking_and_keeps_the_no_commitment_statement():
    section = text_of_home(re.search(r'<section class="section" id="services".*?</section>', home(), re.S)[0])
    assert "We offer to help you run a pilot" in section and "We help with pilots" not in section
    assert "We never accept a request automatically and make no response-time commitment" in section
    support = flat(read("SUPPORT.md"))
    assert "We never accept a request automatically, and there is no response-time commitment" in support
    positioning = flat(read("docs/product/positioning-and-offer.md"))
    assert "This offer is not available for purchase" in positioning
    assert json.loads(read("site/runtime-0.3.5.json"))["external_pilot_validated"] is False


def test_home_has_no_disclaimer_no_overclaim_term_and_no_retired_word():
    html = home()
    text = text_of_home(html)
    for phrase in (*RETIRED_DISCLAIMERS, "DEV/INT", "Preview", "no SLA", "No production"):
        assert phrase.lower() not in html.lower(), phrase
    evidence = re.search(r'<section class="section" id="evidence".*?</section>', html, re.S)[0]
    denial = "not a P95 or an SLA"
    assert text_of_home(evidence).count(denial) == 1
    claims = text.replace(denial, "", 1)
    for term in OVERCLAIM_TERMS:
        assert not re.search(rf"(?<![\w-]){re.escape(term)}(?![\w-])", claims.lower()), term
    for retired in ("Not established", "Not executed", "To establish", "Bounded trials", "Limited path", "Not qualified"):
        assert retired not in text, retired
    assert len(re.findall(r'class="eyebrow', html)) <= 2
    for paragraph in re.findall(r"<(?:p|li|dd)[ >].*?</(?:p|li|dd)>", html, re.S):
        assert paragraph.count("<strong") <= 1, paragraph[:80]
    assert "<strong>" not in re.search(r'<ul class="ledger">.*?</ul>', html, re.S)[0]
    assert "preview" not in re.search(r'<meta name="description" content="([^"]+)"', html)[1].lower()


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
                   "needs human approval", "MCP", "every call is audited", "never a zero or a silent estimate"):
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
    # The excerpt shows the first failing check and marks every cut with the number of lines dropped.
    assert output.index("[FAIL] local_tools.helm") < output.index("9 more [FAIL] lines omitted") < output.index("[UNKN]")
    shown = output.count("[FAIL] ") - len(re.findall(r"\[\.\.\. \d+ more \[FAIL\]", output))
    omitted = sum(int(n) for n in re.findall(r"\[\.\.\. (\d+) more \[FAIL\]", output))
    assert shown + omitted == 10 and "10 failed" in output, "shown plus omitted lines add up to the header count"
    assert '"Next:" line omitted' in output, "the dropped Next: lines are announced"
    note = text_of_home(re.search(r'<p class="terminal__note">(.*?)</p>', html, re.S)[1])
    assert "Real output" in note and "placeholder host" in note


def test_evidence_section_links_the_evidence_page_and_every_receipt():
    html = home()
    section = re.search(r'<section class="section" id="evidence".*?</section>', html, re.S)[0]
    assert 'data-repo-path="docs/evidence.md" href="../docs/evidence.md"' in section
    for receipt in ("qualification-0.4.5.json", "runtime-0.3.5.json", "recovery-0.3.4.json", "qualification-0.3.4.json"):
        assert f'href="{receipt}"' in section and (ROOT / "site" / receipt).is_file()
    text = text_of_home(section)
    assert text_of_home(re.search(r"<h2[^>]*>(.*?)</h2>", section, re.S)[1]) == "What we have measured"
    for word in ("Measured", "Not tested", "Not measured"):
        assert word in text
    assert "Complete cloud-loss recovery on 0.4.5, with all original provider storage removed" in text
    assert "retained-storage rebuilds above do not cover it" in text
    assert "Earlier published 0.4.2 has no receipt here" in text
    assert "Upper bounds, not a P95" in text
    assert "polling upper bounds, not a P95 or an SLA" in text
    assert "1 of 97 planned bursts" in text and "remained unproven" in text
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


POC_ADVICE = "Start with a proof of concept on a non-production IBM i"


def test_production_guidance_is_poc_first_advice_where_a_deployer_decides():
    install = flat(read("docs/product/install-default.md"))
    assert POC_ADVICE in install and "We help with pilots" in install and "(../evidence.md)" in install
    answers = {title: flat(body) for title, body in faq_questions()}
    production = answers["Can I run it in production?"]
    assert POC_ADVICE in production and "We help with pilots" in production and "(../evidence.md)" in production
    assert not re.search(r"\bNo\.|not supported|no production|blocks production", production, re.I), "no denial phrasing"
    short = flat(read(FAQ))
    assert f"| Production | {POC_ADVICE}" in short
    # The factual limits the old answer carried stay findable on the evidence page.
    evidence = flat(read("docs/evidence.md"))
    assert "blocks production promotion" in evidence and "Production use | Not tested" in evidence


def test_removed_disclaimers_do_not_come_back_on_the_front_pages():
    banned = ("Preview, DEV/INT only", "We claim no", "no production qualification", "No production qualification",
              "is a DEV/INT preview", "nothing on this page is a production", "claimed production")
    for path in ("README.md", "docs/pypi.md", "docs/product/faq.md", "docs/product/try-it-local.md",
                 "docs/governance.md", "SUPPORT.md", "ROADMAP.md", "docs/product/comparison.md",
                 "docs/product/compatibility.md", "docs/product/install-default.md", "docs/evidence.md",
                 *sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "docs/articles").glob("*.md"))):
        text = flat(read(path))
        for phrase in banned:
            assert phrase not in text, (path, phrase)
    unreleased = read("CHANGELOG.md").split("\n## 0.", 1)[0]
    assert "qualify nothing" not in unreleased and "no new qualification" not in unreleased


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
    assert "not a P95 and not a guarantee" in latency and "0.3.4" in latency
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


def test_local_guide_requires_unreleased_source_and_compatible_images_before_first_command():
    text = read("docs/product/try-it-local.md")
    preparation = flat(text[:text.index("quadringent local init")])
    assert "Unreleased" in preparation
    assert "0.4.5 wheel does not include" in preparation
    assert "images built from the same checkout" in preparation
    assert "python -m pip install '.[api]'" in preparation
    for dockerfile, tag, option in (
        ("docker/Dockerfile", "quadringent-local-capture:unreleased", "--capture-image"),
        ("docker/control-plane.Dockerfile", "quadringent-local-loader:unreleased", "--loader-image"),
    ):
        assert (ROOT / dockerfile).is_file()
        assert f"docker build -f {dockerfile} -t {tag} ." in preparation
        assert f"{option} {tag}" in text


def test_local_guide_distinguishes_preconfirmation_checks_from_copy_and_services():
    start = read("docs/product/try-it-local.md").split("### 4. Start", 1)[1].split("### 5. Watch", 1)[0]
    before, after = flat(start).split("Only after confirmation", 1)
    for step in ("pulls the images", "file system", "signs on", "catalog"):
        assert step in before
    for step in ("admission", "copies the table", "loader"):
        assert step in after


def test_local_guide_costs_include_idle_time_and_each_resume_minimum():
    costs = flat(read("docs/product/try-it-local.md").split("Costs:", 1)[1].split("## Stop", 1)[0])
    assert "while the warehouse is running" in costs
    assert "idle time" in costs
    assert "60 second minimum each time it resumes" in costs
    assert "repeated resumes" in costs.lower()
    assert "bills only while loading" not in costs

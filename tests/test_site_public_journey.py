"""Public journey checks: pilot contact, security channel and generated pages."""
import re
import shutil
import tomllib
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


# C05 — version, evidence, rendering and discovery

import json  # noqa: E402
from html.parser import HTMLParser  # noqa: E402


def historical_summary() -> dict:
    """The original 0.3.4 trial, retained separately from later campaigns."""
    return json.loads(read("site/qualification-0.3.4.json"))


def release_version() -> str:
    """The published release named in the hero; its qualification may not be published yet."""
    facts = re.search(r'<dl class="release-facts".*?</dl>', read("site/index.html"), re.S)[0]
    return re.search(r'<dt>Version</dt><dd><span class="mono">([0-9.]+)</span>', facts)[1]


def homepage_section(html: str, section_id: str) -> str:
    match = re.search(rf'<section[^>]*id="{section_id}".*?</section>\s*(?=<section|</main>)', html, re.S)
    assert match, section_id
    return match[0]


def text_of(html: str) -> str:
    return flat(re.sub(r"<[^>]+>", " ", html))


def scoped_summary(name: str) -> dict:
    return json.loads(read(f"site/{name}.json"))


def evidence_block(name: str) -> str:
    return re.search(rf'<div[^>]*id="{name}".*?<!-- /{name} -->', read("site/index.html"), re.S)[0]


def target_rows(block: str) -> dict[str, str]:
    return dict(re.findall(r'<tr data-target="([^"]+)">(.*?)</tr>', block, re.S))


def test_hero_uses_latest_scoped_observations_without_transferring_versions():
    runtime = scoped_summary("runtime-0.3.5")
    recovery = scoped_summary("recovery-0.3.4")
    facts = text_of(re.search(r'<dl class="release-facts".*?</dl>', read("site/index.html"), re.S)[0])
    source_version = tomllib.loads(read("pyproject.toml"))["project"]["version"]
    assert release_version() == source_version
    assert f'{source_version}: native CDC, outage, latency and complete cloud-loss recovery need fresh qualification' in facts
    assert f'Historical {runtime["version"]}: fresh initial copy on 4 targets' in facts
    assert f'{recovery["version"]} recovery' in facts
    assert "8 cold/warm observations within 10 s" in facts
    assert "gate failed on 3" not in facts


def test_current_runtime_copy_numbers_and_upgrade_paths_match_receipt():
    runtime = scoped_summary("runtime-0.3.5")
    block = evidence_block("runtime-035")
    text = text_of(block)
    assert runtime["version"] in text and runtime["date_utc"] in block
    sites = runtime["initial_copy"]["sites"]
    rows = target_rows(block)
    assert set(rows) == set(sites)
    assert f'{sum(site["new_executed_copy_jobs"] for site in sites.values())} new copy Jobs' in text
    assert f'{runtime["initial_copy"]["new_source_writes"]} new source writes' in text
    for target, values in sites.items():
        assert values["passed"] and values["typed_values_exact"]
        cells = re.findall(r'<td class="mono">([0-9]+)</td>', rows[target])
        assert cells == [str(values["mirror_rows"]), str(values["history_C_images"]), str(values["new_executed_copy_jobs"])]
        assert runtime["upgrade"]["sites"][target]["path"] in text_of(rows[target])
    assert "EKS: 0.3.5 → 0.3.4 → 0.3.5" in text
    assert runtime["eks_version_cycle"]["postgres_pvc_retained"]
    assert "same PostgreSQL PVC" in text and "pipelines paused" in text
    assert "public installer refused" in text and "direct Helm" in text
    assert runtime["upgrade"]["sites"]["aws-vm"]["public_installer_full_pass"] is False
    assert "Not established" in text and "Not executed" in text


def test_runtime_publication_gate_is_preserved_and_draft_is_not_claimed_published():
    runtime = scoped_summary("runtime-0.3.5")
    assert isinstance(runtime["public_ready"], bool)
    if runtime["public_ready"] is False:
        text = text_of(evidence_block("runtime-035"))
        assert "Publication requires cleanup review" in text
        assert "published runtime qualification" not in text.lower()
    assert "runtime-0.3.5.json" in read("site/index.html")


def test_recovery_targets_counts_faults_and_latency_match_receipt():
    summary = scoped_summary("recovery-0.3.4")
    assert summary["public_ready"] is True
    block = evidence_block("recovery-034")
    text = text_of(block)
    assert summary["observed_at_utc"] in block
    assert "new PostgreSQL PVCs" in text and "4 reader SIGKILL" in text
    assert "acknowledged inside the stops" in text and "not an initial copy from empty" in text
    assert "SQL begin → mirror" in text and "upper bounds" in text
    assert "not P95 or an SLA" in text
    rows = target_rows(block)
    assert set(rows) == set(summary["sites"])
    for target, values in summary["sites"].items():
        assert values["actual_reader_SIGKILL_and_ACK_inside_stop_passed"]
        assert values["complete_reconstruction_passed"] and not values["same_historical_PVC_reused"]
        assert values["full_typed_rows_and_history_exact"] and values["EVENT_ID_unique"]
        cells = re.findall(r'<td class="mono">([0-9.]+)</td>', rows[target])
        assert cells == [str(values["mirror_rows"]), str(values["history_images"]),
                         f'{values["cold_SQL_to_mirror_upper_bound_s"]:.3f}', f'{values["warm_SQL_to_mirror_upper_bound_s"]:.3f}']
        assert "Passed" in text_of(rows[target])
        assert max(values["cold_SQL_to_mirror_upper_bound_s"], values["warm_SQL_to_mirror_upper_bound_s"]) <= summary["latency"]["threshold_seconds"]


def test_original_threshold_failure_remains_historical_with_its_original_clock():
    summary = historical_summary()
    block = evidence_block("historical-034")
    text = text_of(block)
    assert "historical" in text.lower()
    assert "Above threshold" in text
    cdc = summary["typed_cdc_readback"]
    assert f'{cdc["history_images_per_site"]} history images' in text
    assert f'{cdc["mirror_rows_per_site"]} mirror rows' in text
    assert f'{cdc["duplicate_event_ids"]} duplicate event IDs' in text
    assert "136" in text and "148" not in text
    assert "not merged" in text and "SQL start → visible" in text
    for target, values in summary["cold_warehouse_latency"]["targets"].items():
        row = target_rows(block)[target]
        cells = re.findall(r'<td class="mono">([0-9.]+)</td>', row)
        assert cells == [f'{values["maximum_SQL_start_seconds"]:.3f}', f'{values["maximum_ACK_seconds"]:.3f}',
                         f'{values["maximum_source_client_launch_seconds"]:.3f}']
        assert ("Passed" if values["SQL_and_ACK_threshold_passed"] else "Above threshold") in text_of(row)
    home = read("site/index.html")
    assert home.index('id="runtime-035"') < home.index('id="recovery-034"') < home.index('id="historical-034"')


def test_historical_results_are_labelled_and_not_compared_as_equivalent():
    qualification = homepage_section(read("site/index.html"), "qualification")
    history = re.search(r'<details class="history" id="historical-02".*?</details>', qualification, re.S)
    assert history
    assert "historical, artifacts not published" in text_of(history[0]).lower()
    assert "not comparable" in text_of(history[0])
    assert not re.search(r">0\.2\.\d+", qualification[:qualification.index(history[0])])


def test_limits_name_every_gap_of_the_current_version():
    runtime = scoped_summary("runtime-0.3.5")
    limits = text_of(homepage_section(read("site/index.html"), "limits"))
    assert runtime["current_version_native_CDC_writes_outage_and_latency"]["status"] == "NOT_ESTABLISHED"
    for word in ["Native CDC writes, reader outage and latency on 0.3.5", "Complete infrastructure loss on 0.3.5",
                 "AWS public-installer upgrade", "same PostgreSQL PVC", "P95", "SLA", "Initial copy from empty on 0.3.4"]:
        assert word in limits


def test_readme_matches_scoped_receipts_and_preserves_historical_results():
    runtime = scoped_summary("runtime-0.3.5")
    recovery = scoped_summary("recovery-0.3.4")
    readme = flat(read("README.md"))
    source_version = tomllib.loads(read("pyproject.toml"))["project"]["version"]
    assert f'Current source is **{source_version}**' in readme
    assert f'The published **{runtime["version"]}**' in readme
    assert "need fresh qualification" in readme
    assert "Historical measurements apply only to their original artifacts and scopes" in readme
    for name in ["runtime-0.3.5.json", "recovery-0.3.4.json", "qualification-0.3.4.json"]:
        assert name in readme
    assert "8 new copy Jobs" in readme and "110 history C images" in readme
    assert "same PostgreSQL PVC" in readme and "direct Helm" in readme
    assert "148 history images" in readme and "136 history images" in readme
    assert "8 cold/warm observations" in readme and "not P95 or an SLA" in readme
    assert "Targets qualified by batch" not in readme and "independently observed" not in readme
    for values in recovery["sites"].values():
        assert f'{values["cold_SQL_to_mirror_upper_bound_s"]:.3f}' in readme
        assert f'{values["warm_SQL_to_mirror_upper_bound_s"]:.3f}' in readme
    for values in historical_summary()["cold_warehouse_latency"]["targets"].values():
        assert f'{values["maximum_SQL_start_seconds"]:.3f}' in readme


def test_home_names_the_platform_once_with_its_former_names():
    home = text_of(read("site/index.html"))
    assert "IBM i (formerly AS/400 and iSeries)" in home
    assert "Db2 for i" in home
    assert "change data capture" in home.lower()


KNOWN_TAGS = {
    "a", "abbr", "article", "b", "blockquote", "body", "br", "caption", "code", "dd", "del", "details", "div",
    "dl", "dt", "em", "figcaption", "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "head", "header",
    "hr", "html", "i", "img", "kbd", "li", "link", "main", "meta", "nav", "ol", "p", "picture", "pre", "s",
    "script", "section", "small", "source", "span", "strong", "sub", "summary", "sup", "svg", "path", "circle",
    "g", "table", "tbody", "td", "th", "thead", "time", "title", "tr", "ul",
}


class PageScan(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags, self.links, self.canonical, self.h1, self.ld = set(), [], [], 0, []
        self._ld = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.add(tag)
        if tag == "h1":
            self.h1 += 1
        if tag == "link" and attrs.get("rel") == "canonical":
            self.canonical.append(attrs["href"])
        if tag in ("a", "link", "img", "source", "script") and (attrs.get("href") or attrs.get("src")):
            self.links.append(attrs.get("href") or attrs.get("src"))
        self._ld = tag == "script" and attrs.get("type") == "application/ld+json"

    def handle_data(self, data):
        if self._ld:
            self.ld.append(data)
            self._ld = False


def built_pages(site: Path) -> list[Path]:
    return sorted(site.rglob("*.html"))


def test_generated_pages_have_no_unknown_tags_and_keep_placeholders(site):
    for page in built_pages(site):
        scan = PageScan()
        scan.feed(page.read_text())
        assert scan.tags <= KNOWN_TAGS, (page, scan.tags - KNOWN_TAGS)
    installation = text_of((site / "docs/installation/index.html").read_text())
    assert "ghcr.io/<owner>/quadringent-ce" in installation.replace("&lt;", "<").replace("&gt;", ">")
    api = text_of((site / "docs/api/index.html").read_text()).replace("&lt;", "<").replace("&gt;", ">")
    assert "t=<unix>,v1=<hex>" in api


def test_architecture_diagram_is_rendered_not_left_as_source(site):
    page = (site / "docs/architecture/index.html").read_text()
    assert "language-mermaid" not in page
    tag = re.search(r'<img[^>]+architecture\.svg[^>]*>', page)
    assert tag
    src, alt = re.search(r'src="([^"]+)"', tag[0])[1], re.search(r'alt="([^"]+)"', tag[0])[1]
    assert len(alt) > 40
    assert (site / "docs/architecture" / src).resolve().is_file()
    assert "GCS" in text_of(page) and "API v2" in text_of(page)


def test_every_page_has_one_canonical_one_h1_and_valid_structured_data(site):
    descriptions = []
    for page in built_pages(site):
        html = page.read_text()
        scan = PageScan()
        scan.feed(html)
        assert len(scan.canonical) == 1, page
        assert scan.h1 == 1, (page, scan.h1)
        for block in scan.ld:
            json.loads(block)
        descriptions.append(re.search(r'<meta name="description" content="([^"]+)"', html)[1])
    assert len(descriptions) == len(set(descriptions)), "meta descriptions must be distinct"


def test_internal_links_resolve_and_sitemap_lists_every_page(site):
    base = "https://quadringent.github.io/quadringent-ce/"
    for page in built_pages(site):
        scan = PageScan()
        scan.feed(page.read_text())
        for link in scan.links:
            if link.startswith(("http:", "https:", "mailto:", "#", "data:")):
                continue
            path, _, fragment = link.partition("#")
            target = (page.parent / path).resolve()
            if path.endswith("/") or target.is_dir():
                target = target / "index.html"
            assert target.is_file(), (page, link)
            if fragment and target.suffix == ".html":
                assert f'id="{fragment}"' in target.read_text(), (page, link)
    listed = set(re.findall(r"<loc>([^<]+)</loc>", (site / "sitemap.xml").read_text()))
    pages = {base + str(p.relative_to(site).parent).replace(".", "").strip("/") for p in built_pages(site)}
    assert {u.rstrip("/") for u in listed} == {u.rstrip("/") for u in pages}


def test_subpath_site_does_not_publish_an_ineffective_robots_file(site):
    assert not (site / "robots.txt").exists()


def test_cloud_command_names_the_assets_of_the_published_release():
    install = homepage_section(read("site/index.html"), "install")
    assert f"--assets-dir ./quadringent-{release_version()}" in install


def test_links_to_repository_files_point_to_existing_files(site):
    prefix = "https://github.com/quadringent/quadringent-ce/blob/main/"
    for page in built_pages(site):
        for link in re.findall(r'href="([^"]+)"', page.read_text()):
            if link.startswith(prefix):
                path = link.removeprefix(prefix).split("#")[0]
                assert (ROOT / path).exists(), (page, link)


# C02 — compatibility and prerequisites before installation

COMPAT = "docs/product/compatibility.md"


def test_compatibility_page_marks_every_row_tested_required_or_not_tested():
    page = read(COMPAT)
    rows = re.findall(r"^\| ([^|]+) \| (Tested|Required|Not tested)[^|]* \|", page, re.M)
    assert len(rows) >= 12
    statuses = {status for _, status in rows}
    assert statuses == {"Tested", "Required", "Not tested"}
    text = flat(page)
    for topic in ("IBM i 7.5", "*BOTH", "primary key", "RGZPFM", "CLRPFM", "commitment control", "CCSID 65535"):
        assert topic in text, topic


def test_authorities_cite_ibm_and_never_ask_for_all_object_authority():
    page = read(COMPAT)
    section_text = flat(section(page, "IBM i authorities"))
    assert "https://www.ibm.com/docs/en/i/7.5?topic=services-display-journal-table-function" in section_text
    assert "*USE" in section_text and "*EXECUTE" in section_text and "*OBJEXIST" in section_text
    assert "not been tested at runtime" in section_text
    assert "*ALLOBJ is not required" in section_text
    assert "Do not grant *ALLOBJ" in section_text


def test_retention_is_a_formula_with_a_labelled_hypothetical_example():
    text = flat(section(read(COMPAT), "Journal receiver retention"))
    assert "maximum outage + maximum lag + recovery time" in text
    assert "Hypothetical example" in text
    assert "not a recommendation" in text
    assert "DLTRCV(*YES)" in text


def test_snowflake_prerequisites_split_provisioning_from_runtime_and_by_mode():
    script = read("src/quadringent_control_plane/v2/services/destinations.py")
    for setting in ("XSMALL", "AUTO_SUSPEND = 60", "AUTO_RESUME = TRUE", "INITIALLY_SUSPENDED = TRUE"):
        assert setting in script, setting
    text = flat(section(read(COMPAT), "Snowflake prerequisites"))
    for phrase in ("Provisioning administrator", "Runtime service role", "AUTO_SUSPEND = 60",
                   "AUTO_RESUME = TRUE", "no stage, pipe or storage integration", "Snowpipe Streaming",
                   "ACCOUNTADMIN is not required"):
        assert phrase in text, phrase


def test_cost_section_uses_code_defaults_and_marks_credits_not_measured():
    text = flat(section(read(COMPAT), "Default resources and cost"))
    aws = read("deploy/terraform/aws/vm/variables.tf")
    gcp = read("deploy/terraform/gcp/vm/variables.tf")
    assert re.search(r'default\s*=\s*"t3\.medium"', aws) and "t3.medium" in text
    assert re.search(r'default\s*=\s*"e2-medium"', gcp) and "e2-medium" in text
    assert re.search(r'default\s*=\s*40\b', aws) and re.search(r'default\s*=\s*40\b', gcp) and "40 GB" in text
    assert "Not measured" in text
    assert "deploy/terraform/aws/vm/variables.tf" in text


def test_compatibility_is_one_click_from_home_install_and_getting_started(site):
    home = (site / "index.html").read_text()
    assert 'href="docs/compatibility/"' in home
    for guide in ("installation", "getting-started", "pilot", "operator-guide"):
        assert 'href="../compatibility/"' in (site / f"docs/{guide}/index.html").read_text(), guide


def test_getting_started_points_to_the_default_installer_first(site):
    page = (site / "docs/getting-started/index.html").read_text()
    assert 'href="../installation/"' in page
    helm = page.find('href="../helm-installation/"')
    assert helm == -1 or page.index('href="../installation/"') < helm


def test_journal_example_respects_the_ibm_threshold_rule():
    guide = read("docs/product/install-client.md")
    assert not ("THRESHOLD(*NONE)" in guide and "MNGRCV(*SYSTEM)" in guide)



# C04 — operator guide

def test_operator_guide_covers_the_operating_path_and_is_published(site):
    guide = read("docs/product/operator-guide.md")
    headings = re.findall(r"^## \d\. (.+)$", guide, re.M)
    assert headings == ["First installation", "Preflight", "First copy", "Read the evidence",
                        "Outage", "Recovery", "Backup and restore", "Stop and uninstall"]
    page = (site / "docs/operator-guide/index.html").read_text()
    assert "--check-ibmi &lt;host&gt;" in page
    assert "Not measured" in page and "Synthetic or Historical" in page

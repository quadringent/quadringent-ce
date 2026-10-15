"""No public page overclaims, and every number we publish stays sourced.

Four guards, each a function of a repository root so that the mutation tests
can run it on a temporary copy with a planted overclaim:

1. overclaim terms (production-ready, battle-tested, SLA, 100%, ...) appear on
   no public page; negated factual mentions are allowed only where the explicit
   allowlist below says so;
2. every latency or volume figure on README.md, docs/pypi.md and the site
   carries a version, a date or a link to docs/evidence.md;
3. the proof line on README.md, docs/pypi.md and the site matches site/runtime-0.3.5.json and
   what docs/evidence.md says about the IBM i release that was tested;
4. docs/evidence.md keeps every number of the receipts and every limit.

The checks read the repository only. They never connect to anything.
"""

from __future__ import annotations

import json
import re
import shutil
from collections import Counter
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# Public pages
# ---------------------------------------------------------------------------

ROOT_PAGES = ("README.md", "SUPPORT.md", "ROADMAP.md", "CONTRIBUTING.md")
SITE_PAGE = "site/index.html"
FIGURE_PAGES = ("README.md", "docs/pypi.md", SITE_PAGE)
EVIDENCE = "docs/evidence.md"
RUNTIME_RECEIPT = "site/runtime-0.3.5.json"
RECOVERY_RECEIPT = "site/recovery-0.3.4.json"
QUALIFICATION_RECEIPT = "site/qualification-0.3.4.json"
RECEIPTS = (RUNTIME_RECEIPT, RECOVERY_RECEIPT, QUALIFICATION_RECEIPT)

# Copied into a temporary tree by the mutation tests.
TREE_FILES = (*ROOT_PAGES, "CHANGELOG.md", SITE_PAGE, *RECEIPTS)


def read(root: Path, path: str) -> str:
    return (root / path).read_text(encoding="utf-8")


def flat(text: str) -> str:
    return " ".join(text.split())


def normalize(text: str) -> str:
    """Make hyphens, spaces and quotes comparable (non-breaking, typographic)."""
    for old, new in ((" ", " "), (" ", " "), (" ", " "), ("‐", "-"), ("‑", "-"),
                     ("’", "'"), ("‘", "'")):
        text = text.replace(old, new)
    return text


def changelog_unreleased(text: str) -> str:
    match = re.search(r"^## Unreleased\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    return match[1] if match else ""


# ---------------------------------------------------------------------------
# Blocks: the unit of a claim. A paragraph, list item or table row in
# Markdown; the innermost block element in HTML. Each block keeps the links
# it contains and the nearest heading above it.
# ---------------------------------------------------------------------------


@dataclass
class Block:
    page: str
    text: str
    heading: str = ""
    hrefs: list[str] = field(default_factory=list)
    code: bool = False

    @property
    def context(self) -> str:
        return f"{self.heading} {self.text} {' '.join(self.hrefs)}"


def markdown_blocks(page: str, text: str) -> list[Block]:
    blocks: list[Block] = []
    heading = ""
    current: list[str] = []
    fence: str | None = None

    def flush() -> None:
        if current:
            blocks.append(Block(page, normalize(" ".join(current)), heading, code=False))
            current.clear()

    for line in text.splitlines():
        stripped = line.strip()
        marker = re.match(r"(~~~|```)", stripped)
        if fence:
            blocks.append(Block(page, normalize(line), heading, code=True))
            if marker and marker[1] == fence:
                fence = None
            continue
        if marker:
            flush()
            fence = marker[1]
            continue
        title = re.match(r"#{1,6}\s+(.*)", stripped)
        if title:
            flush()
            heading = title[1]
            blocks.append(Block(page, normalize(title[1]), heading))
        elif not stripped or stripped.startswith("<!--") and stripped.endswith("-->"):
            flush()
        elif stripped.startswith("|"):
            flush()
            blocks.append(Block(page, normalize(stripped), heading))
        elif re.match(r"([-*+]|\d+\.)\s", stripped):
            flush()
            current.append(stripped)
        else:
            current.append(stripped)
    flush()
    for block in blocks:
        block.hrefs = re.findall(r"\]\(([^)\s]+)", block.text)
    return blocks


BLOCK_TAGS = frozenset({
    "p", "li", "td", "th", "dt", "dd", "figcaption", "h1", "h2", "h3", "h4", "h5", "h6", "summary",
    "blockquote", "caption", "pre", "div", "section", "article", "header", "footer", "nav", "main", "body",
})
SKIPPED_TAGS = frozenset({"style", "head_script"})
ATTRIBUTE_TEXT = ("alt", "title", "aria-label", "content")


class HtmlBlocks(HTMLParser):
    """Visible text, in blocks. Alternative text, titles and the meta description count as visible."""

    def __init__(self, page: str) -> None:
        super().__init__(convert_charrefs=True)
        self.page = page
        self.blocks: list[Block] = []
        self._stack: list[tuple[str, Block]] = []
        self._root = Block(page, "")
        self._heading = ""
        self._skip = 0
        self._ld = False

    def _open(self, tag: str) -> Block:
        block = Block(self.page, "", self._heading)
        self._stack.append((tag, block))
        self.blocks.append(block)
        return block

    def _current(self) -> Block:
        return self._stack[-1][1] if self._stack else self._root

    def handle_starttag(self, tag, attrs):
        attrs = {key: (value or "") for key, value in attrs}
        if tag == "style":
            self._skip += 1
            return
        if tag == "script":
            if attrs.get("type") == "application/ld+json":
                self._ld = True
                self._open("script")
            else:
                self._skip += 1
            return
        if tag == "meta" and attrs.get("name") in ("description",) or attrs.get("property") in (
                "og:description", "og:title", "twitter:description"):
            self._current().text += " " + attrs.get("content", "")
        if tag in BLOCK_TAGS:
            self._open(tag)
        block = self._current()
        if tag == "a" and attrs.get("href"):
            block.hrefs.append(attrs["href"])
        for name in ATTRIBUTE_TEXT:
            if name != "content" and attrs.get(name):
                block.text += " " + attrs[name]
        if tag in {"br", "td", "th", "li"}:
            block.text += " "

    def handle_endtag(self, tag):
        if tag == "style" or (tag == "script" and not self._ld):
            self._skip = max(0, self._skip - 1)
            return
        if tag == "script" and self._ld:
            self._ld = False
        if tag in BLOCK_TAGS or tag == "script":
            for index in range(len(self._stack) - 1, -1, -1):
                if self._stack[index][0] == tag:
                    if re.fullmatch(r"h[1-6]", tag):
                        self._heading = flat(self._stack[index][1].text)
                    del self._stack[index:]
                    break

    def handle_data(self, data):
        if self._skip:
            return
        self._current().text += data


def html_blocks(page: str, html: str) -> list[Block]:
    parser = HtmlBlocks(page)
    parser.feed(html)
    parser.close()
    blocks = [parser._root, *parser.blocks]
    for block in blocks:
        block.text = flat(normalize(unescape(block.text)))
        block.hrefs = [flat(unescape(href)) for href in block.hrefs]
    return [block for block in blocks if block.text]


def page_blocks(root: Path, page: str) -> list[Block]:
    text = read(root, page)
    if page == SITE_PAGE:
        return html_blocks(page, text)
    if page == "CHANGELOG.md":
        text = changelog_unreleased(text)
    return markdown_blocks(page, text)


def public_pages(root: Path) -> list[str]:
    """README, PyPI page, site, every docs page, SUPPORT, ROADMAP, CHANGELOG (Unreleased) and CONTRIBUTING."""
    docs = sorted(path.relative_to(root).as_posix() for path in (root / "docs").rglob("*.md"))
    return [*ROOT_PAGES, "CHANGELOG.md", SITE_PAGE, *docs]


# ---------------------------------------------------------------------------
# Guard 1: overclaim terms
# ---------------------------------------------------------------------------

OVERCLAIM_TERMS: dict[str, re.Pattern[str]] = {
    "production-ready": re.compile(r"production[- ]ready", re.I),
    "production-grade": re.compile(r"production[- ]grade", re.I),
    "enterprise-grade": re.compile(r"enterprise[- ]grade", re.I),
    "battle-tested": re.compile(r"battle[- ]tested", re.I),
    "used in production": re.compile(r"\bused in production\b", re.I),
    "in production at": re.compile(r"\bin production at\b", re.I),
    "our customers": re.compile(r"\bour customers\b", re.I),
    "trusted by": re.compile(r"\btrusted by\b", re.I),
    "guaranteed": re.compile(r"\bguaranteed\b", re.I),
    "zero data loss": re.compile(r"\bzero[- ]data[- ]loss\b|\bzero data loss\b", re.I),
    "100%": re.compile(r"(?<![\w.,])100(?:\s?%|\s+percent\b)", re.I),
    "SLA": re.compile(r"\bSLAs?\b|\bservice[- ]level agreements?\b", re.I),
}

# A negated, factual mention ("we have no SLA", "not guaranteed") is a fact, not a
# claim: it is allowed on any page for these terms. A non-negated mention fails everywhere.
NEGATABLE_TERMS = frozenset({"SLA", "guaranteed"})

# (page, term, text found around the match): a mention that is neither an
# offer nor a claim about Quadringent. Keep this list empty unless a sentence
# truly needs it.
ALLOWED_CONTEXT: tuple[tuple[str, str, str], ...] = (
    # CSS width in the visual-system reference: min(100% - 64px, 1320px).
    ("docs/design-system.md", "100%", "min(100%"),
)

NEGATION = re.compile(
    r"\b(?:no|not|nor|neither|never|nothing|without|cannot|can't|isn't|aren't|doesn't|don't|does not|do not|"
    r"is not|are not)\b", re.I)


def sentences(text: str) -> list[str]:
    return [part for part in re.split(r"(?<=[.!?])\s+(?=[A-Z`(\[|])", text) if part]


def is_negated(text: str, start: int) -> bool:
    """True when a negation word comes before the match inside its own sentence."""
    before = text[:start]
    sentence_start = max(before.rfind(". "), before.rfind("| "), before.rfind("? "), before.rfind("! "), -1) + 1
    return bool(NEGATION.search(before[sentence_start:]))


def overclaim_violations(root: Path) -> list[str]:
    found: list[str] = []
    for page in public_pages(root):
        for block in page_blocks(root, page):
            for term, pattern in OVERCLAIM_TERMS.items():
                for match in pattern.finditer(block.text):
                    around = block.text[max(0, match.start() - 12):match.end() + 12]
                    if any(page == p and term == t and needle in around for p, t, needle in ALLOWED_CONTEXT):
                        continue
                    if term in NEGATABLE_TERMS and is_negated(block.text, match.start()):
                        continue
                    found.append(f"{page}: overclaim term {term!r}: ...{around.strip()}...")
    return found


# ---------------------------------------------------------------------------
# Guard 2: a figure is dated, versioned or linked to the evidence page
# ---------------------------------------------------------------------------

LATENCY = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)?\s?(?:s|sec|secs|seconds|ms|milliseconds|min|minutes)\b(?![-/\w])", re.I)
VOLUME = re.compile(
    r"(?<![\w.])\d[\d,]*(?:\.\d+)?\s?(?:k|m)?\s?(?:rows|events|images|writes|records|updates|inserts|tables|"
    r"MB|GB|TB|per second|per minute)\b", re.I)
VERSION = re.compile(r"(?<![\d.])(?:0|[1-9]\d*)\.\d+\.\d+(?!\d|\.\d)")
DATE = re.compile(r"\b20\d{2}-\d{2}-\d{2}\b")
EVIDENCE_LINK = re.compile(r"evidence\.md")

# Numbers shown in the generated demo are not measurements; they may appear
# next to the word "Synthetic" without a version.
DEMO_FIGURES = frozenset({"120 events", "118 events"})


def figure_violations(root: Path) -> list[str]:
    found: list[str] = []
    for page in FIGURE_PAGES:
        for block in page_blocks(root, page):
            if block.code:
                continue
            figures = [m[0] for m in (*LATENCY.finditer(block.text), *VOLUME.finditer(block.text))]
            figures = [f for f in figures if not (f in DEMO_FIGURES and "synthetic" in block.context.lower())]
            if not figures:
                continue
            context = block.context
            if VERSION.search(context) or DATE.search(context) or EVIDENCE_LINK.search(context):
                continue
            found.append(f"{page}: figure {figures[0]!r} has no version, date or evidence link: {block.text[:100]}")
    return found


# ---------------------------------------------------------------------------
# Guard 3: the proof line matches the receipts
# ---------------------------------------------------------------------------

# A proof line is a positive sentence that says something was tested on a real
# IBM i. Sentences that say the opposite ("has not been validated against a real
# IBM i") are limits, not proof lines.
PROOF_ANCHOR = re.compile(
    r"\b(?:tested|validated|verified|proven|proved|exercised|ran|run|observed|qualified)\b[^.;]{0,80}\breal IBM i\b",
    re.I)
PROOF_REQUIRED_ON = ("README.md", "docs/pypi.md", SITE_PAGE)
# A proof line says what was tested, not what it proves about production, pilots or latency.
PROOF_FORBIDDEN = re.compile(
    r"production|customer|pilot|\bSLA\b|latency|P95|outage|native|guarantee|certif", re.I)
IBMI_RELEASE = re.compile(r"IBM i (\d+\.\d+)")
# The one way a proof line may name a release without the receipt naming it: say that it is the
# only release tested, which docs/evidence.md says in the same words ("Only IBM i 7.5 has been tested").
ONLY_TESTED = re.compile(
    r"IBM i (\d+\.\d+) is the only IBM i release we have tested|only IBM i (\d+\.\d+) has been tested", re.I)


def only_release_tested(root: Path) -> str | None:
    match = ONLY_TESTED.search(flat(read(root, EVIDENCE)).replace("Only IBM i", "only IBM i"))
    return (match[1] or match[2]) if match else None


def receipt(root: Path, path: str) -> dict:
    return json.loads(read(root, path))


def release_named_by_receipt(runtime: dict) -> str | None:
    """The IBM i release a receipt names, if any. The 0.3.5 receipt names none."""
    dump = json.dumps(runtime)
    match = IBMI_RELEASE.search(dump) or re.search(r'"ibm_?i_(?:release|version)":\s*"(\d+\.\d+)"', dump, re.I)
    return match[1] if match else None


def proof_lines(root: Path, page: str) -> list[tuple[str, Block]]:
    lines = []
    for block in page_blocks(root, page):
        if block.code:
            continue
        for sentence in sentences(block.text):
            if PROOF_ANCHOR.search(sentence) and not NEGATION.search(sentence):
                lines.append((sentence, block))
    return lines


def proof_violations(root: Path) -> list[str]:
    found: list[str] = []
    runtime = receipt(root, RUNTIME_RECEIPT)
    recovery = receipt(root, RECOVERY_RECEIPT)
    known_versions = {receipt(root, path)["version"] for path in RECEIPTS}
    known_dates = {runtime["date_utc"]}
    known_dates.update(DATE.findall(json.dumps(recovery)))
    known_dates.update(receipt(root, QUALIFICATION_RECEIPT)["date_utc"].split("T")[:1])
    named = release_named_by_receipt(runtime)
    evidence = flat(read(root, EVIDENCE))

    if f"## {runtime['version']}, {runtime['date_utc']}" not in evidence:
        found.append(f"{EVIDENCE}: no section for {runtime['version']}, {runtime['date_utc']}")
    if named and f"IBM i {named}" not in evidence:
        found.append(f"{EVIDENCE}: the receipt names IBM i {named} but the evidence page does not")
    for flag in ("production_qualified", "external_pilot_validated", "universal_latency_guarantee"):
        if runtime.get(flag) is not False:
            found.append(f"{RUNTIME_RECEIPT}: {flag} is not false; the proof line cannot say more than the receipt")
    if not runtime.get("public_ready") or not all(s["passed"] for s in runtime["initial_copy"]["sites"].values()):
        found.append(f"{RUNTIME_RECEIPT}: not public or not passed on every target")

    for page in public_pages(root):
        for block in page_blocks(root, page):
            if re.search(r"\bReceipts are public\b", block.text):
                found.append(f"{page}: 'Receipts are public'; the underlying receipts are retained privately, "
                             "say 'Receipt summaries are public'")
        lines = proof_lines(root, page)
        if page in PROOF_REQUIRED_ON and not lines:
            found.append(f"{page}: no proof line (a sentence saying it was tested on a real IBM i)")
        for sentence, block in lines:
            tag = f"{page}: proof line {sentence[:80]!r}"
            if runtime["version"] not in sentence:
                found.append(f"{tag} does not name release {runtime['version']}")
            if runtime["date_utc"] not in sentence:
                found.append(f"{tag} does not name {runtime['date_utc']}")
            if "Snowflake" not in sentence:
                found.append(f"{tag} does not name Snowflake")
            if "synthetic" not in block.text.lower():
                found.append(f"{tag} does not say the tables were synthetic")
            only = only_release_tested(root)
            for statement in ONLY_TESTED.finditer(sentence):
                if (statement[1] or statement[2]) != only:
                    found.append(f"{tag} says IBM i {statement[1] or statement[2]} is the only release tested; "
                                 f"{EVIDENCE} says {only}")
            claimed = ONLY_TESTED.sub("", sentence)
            for release in IBMI_RELEASE.findall(claimed):
                if named is None:
                    found.append(f"{tag} names IBM i {release}, but the receipt does not name the IBM i release")
                elif release != named:
                    found.append(f"{tag} names IBM i {release}; the receipt names {named}")
            for version in VERSION.findall(sentence):
                if version not in known_versions:
                    found.append(f"{tag} names version {version}, which has no receipt")
            for date in DATE.findall(sentence):
                if date not in known_dates:
                    found.append(f"{tag} names {date}, which no receipt carries")
            forbidden = PROOF_FORBIDDEN.search(sentence)
            if forbidden:
                found.append(f"{tag} claims more than the receipt shows: {forbidden[0]!r}")
            if re.search(r"recover", sentence, re.I) and recovery["version"] not in sentence:
                found.append(f"{tag} mentions recovery without tying it to {recovery['version']}")
            if re.search(r"end[- ]to[- ]end", sentence, re.I):
                found.append(f"{tag} says 'end to end'; the receipts cover an initial copy and, on 0.3.4, recovery")
            if page in PROOF_REQUIRED_ON:
                scope = block.text.lower()
                for word in ("initial copy", "four", recovery["version"]):
                    if word not in scope:
                        found.append(f"{tag}: the proof line block does not carry its scope ({word!r})")
            if page in FIGURE_PAGES:
                links = " ".join(block.hrefs) + " " + block.text
                if not re.search(r"evidence\.md|runtime-0\.3\.5\.json", links):
                    found.append(f"{tag} has no link to the evidence page or the receipt")
    return found


# ---------------------------------------------------------------------------
# Guard 4: the evidence page keeps every number and every limit
# ---------------------------------------------------------------------------

# Historical figures of versions whose artifacts are not in this repository
# (0.2.x, 0.3.2, 0.3.3): no receipt to derive them from, so they are pinned.
# They are the figures the existing tests already require; this list keeps
# them if those tests are rewritten.
PINNED_HISTORICAL = (
    "2.317 to 7.992 s", "4.904 to 7.041 s", "5.013 to 7.776 s", "7.827 s at most", "6.35 to 31.31 s",
    "12.28 s, then 10.37 s", "154 events and 110 rows", "5.06 s", "upgrade 0.2.3 to 0.2.5",
)

# Limits that must stay findable on the evidence page. The wording may change,
# the topic may not.
EVIDENCE_LIMITS = (
    ("production not qualified", r"production.{0,40}not qualified|not qualified.{0,40}production"),
    ("external pilot not validated", r"external[- ](?:pilot|user)"),
    ("no P95", r"\bP95\b"),
    ("no SLA", r"\bSLA\b"),
    ("no universal latency guarantee", r"universal latency"),
    ("IBM i CPU and I/O cost Not measured", r"IBM i CPU and I/O cost.{0,20}Not measured"),
    ("IBM i 7.4 and 7.6 Not tested", r"IBM i 7\.4 and 7\.6.{0,20}Not tested"),
    ("only one shared server", r"one shared qualification server"),
    ("reconciliation counts events only", r"Reconciliation counts events"),
    ("Snowflake cost Not measured, no invoice", r"Snowflake cost per 24 h.{0,30}Not measured"),
    ("native CDC on 0.3.5 Not tested", r"0\.3\.5.{0,60}native CDC"),
    ("0.3.5 infrastructure loss Not tested", r"0\.3\.5 complete infrastructure loss"),
    ("AWS public installer refused", r"refused an AMI-change replacement"),
    ("EKS PVC reused, not a cloud-loss rebuild", r"same PostgreSQL PVC"),
    ("initial copy from empty on 0.3.4 Not tested", r"Initial copy from empty on 0\.3\.4"),
    ("baseline restore not re-read", r"separate typed read after that restoration"),
    ("nondefault load settings", r"[Nn]ondefault (?:EKS )?load settings"),
    ("3 of 4 targets missed 10 s", r"3 of 4 targets missed our 10 s bar"),
    ("cockpit on loopback without authentication", r"loopback without authentication"),
    ("not an external certification", r"not an external certification"),
    ("four evidence words", r"Measured.*Not measured.*Not tested.*Not supported"),
)


def numbers_in(value) -> list[str]:
    """Every number of a receipt that the evidence page prints, as the page prints it."""
    out: list[str] = []
    if isinstance(value, bool):
        return out
    if isinstance(value, float):
        out.append(f"{value:.3f}")
    elif isinstance(value, int):
        out.append(str(value))
    elif isinstance(value, dict):
        for key, item in value.items():
            if key in {"active_plus60s_autosuspend_credit_upper", "invoice_verified"}:
                continue
            out.extend(numbers_in(item))
    elif isinstance(value, list):
        for item in value:
            out.extend(numbers_in(item))
    return out


def evidence_violations(root: Path) -> list[str]:
    found: list[str] = []
    evidence = flat(normalize(read(root, EVIDENCE)))
    runtime = receipt(root, RUNTIME_RECEIPT)
    recovery = receipt(root, RECOVERY_RECEIPT)
    historical = receipt(root, QUALIFICATION_RECEIPT)

    def need_number(number: str, where: str) -> None:
        if not re.search(rf"(?<![\d.]){re.escape(number)}(?![\d]|\.\d)", evidence):
            found.append(f"{EVIDENCE}: {where} number {number} is missing")

    initial = runtime["initial_copy"]
    for number in (initial["source_rows_per_table"], initial["new_source_writes"]):
        need_number(str(number), RUNTIME_RECEIPT)
    for site in initial["sites"].values():
        for number in (site["mirror_rows"], site["history_C_images"], site["new_executed_copy_jobs"]):
            need_number(str(number), RUNTIME_RECEIPT)
    for site in runtime["upgrade"]["sites"].values():
        if site["path"] not in evidence:
            found.append(f"{EVIDENCE}: upgrade path {site['path']!r} is missing")
    if " to ".join(runtime["eks_version_cycle"]["sequence"]) not in evidence:
        found.append(f"{EVIDENCE}: the EKS version cycle is missing")

    for site in recovery["sites"].values():
        for key in ("cold_SQL_to_mirror_upper_bound_s", "warm_SQL_to_mirror_upper_bound_s"):
            need_number(f"{site[key]:.3f}", RECOVERY_RECEIPT)
        need_number(str(site["mirror_rows"]), RECOVERY_RECEIPT)
        need_number(str(site["history_images"]), RECOVERY_RECEIPT)
    need_number(str(recovery["latency"]["threshold_seconds"]), RECOVERY_RECEIPT)

    typed = historical["typed_cdc_readback"]
    need_number(str(typed["history_images_per_site"]), QUALIFICATION_RECEIPT)
    need_number(str(typed["mirror_rows_per_site"]), QUALIFICATION_RECEIPT)
    cold = historical["cold_warehouse_latency"]
    need_number(f"{cold['source_client_startup_seconds']:.2f}", QUALIFICATION_RECEIPT)
    need_number(str(cold["source_writes"]), QUALIFICATION_RECEIPT)
    for site in cold["targets"].values():
        for key in ("maximum_SQL_start_seconds", "maximum_ACK_seconds", "maximum_source_client_launch_seconds"):
            need_number(f"{site[key]:.3f}", QUALIFICATION_RECEIPT)

    for fact in PINNED_HISTORICAL:
        if fact not in evidence:
            found.append(f"{EVIDENCE}: historical figure {fact!r} is missing")

    for label, pattern in EVIDENCE_LIMITS:
        if not re.search(pattern, evidence, re.S | re.I):
            found.append(f"{EVIDENCE}: limit missing: {label}")

    for name in (RUNTIME_RECEIPT, RECOVERY_RECEIPT, QUALIFICATION_RECEIPT):
        if Path(name).name not in evidence:
            found.append(f"{EVIDENCE}: does not link {Path(name).name}")
    return found


# ---------------------------------------------------------------------------
# The real repository
# ---------------------------------------------------------------------------


def test_no_public_page_contains_an_overclaim_term():
    assert overclaim_violations(ROOT) == []


def test_every_latency_or_volume_figure_is_versioned_dated_or_linked_to_the_evidence_page():
    assert figure_violations(ROOT) == []


def test_proof_line_on_readme_and_site_matches_the_receipts():
    assert proof_violations(ROOT) == []


def test_evidence_page_keeps_every_receipt_number_and_every_limit():
    assert evidence_violations(ROOT) == []


def test_pages_scanned_include_everything_public():
    pages = public_pages(ROOT)
    for expected in (*ROOT_PAGES, "CHANGELOG.md", SITE_PAGE, "docs/pypi.md", EVIDENCE, "docs/product/faq.md"):
        assert expected in pages
    assert len(pages) >= 25
    assert page_blocks(ROOT, SITE_PAGE), "the site produced no visible text"


def test_allowlist_stays_minimal():
    assert NEGATABLE_TERMS == {"SLA", "guaranteed"}
    assert len(ALLOWED_CONTEXT) <= 1


# ---------------------------------------------------------------------------
# Mutation checks: each guard fails on a planted overclaim, in a temporary copy.
# ---------------------------------------------------------------------------


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    for name in TREE_FILES:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    shutil.copytree(ROOT / "docs", tmp_path / "docs", ignore=shutil.ignore_patterns("*.svg", "*.png"))
    return tmp_path


def plant(root: Path, path: str, old: str | None, new: str) -> None:
    """Replace ``old`` by ``new`` in a copied page; append ``new`` when ``old`` is None."""
    target = root / path
    text = target.read_text(encoding="utf-8")
    if old is None:
        if path == SITE_PAGE:
            text = text.replace("</main>", f"<p>{new}</p></main>", 1)
        else:
            text = text.rstrip("\n") + f"\n\n{new}\n"
    else:
        assert old in text, (path, old)
        text = text.replace(old, new, 1)
    target.write_text(text, encoding="utf-8")


def new_violations(guard, root: Path, before: list[str]) -> list[str]:
    """Violations that the planted change adds, counting repeats (a page may already carry an identical one)."""
    remaining = Counter(before)
    added = []
    for item in guard(root):
        if remaining[item]:
            remaining[item] -= 1
        else:
            added.append(item)
    return added


PLANTED = {
    "production-ready": "Quadringent is production-ready.",
    "production-grade": "A production-grade pipeline for IBM i.",
    "enterprise-grade": "Enterprise-grade replication.",
    "battle-tested": "A battle-tested reader.",
    "used in production": "It is used in production by many teams.",
    "in production at": "Running in production at a large retailer.",
    "our customers": "Our customers replicate nightly.",
    "trusted by": "Trusted by IBM i shops.",
    "guaranteed": "Delivery is guaranteed.",
    "zero data loss": "Zero data loss, always.",
    "100%": "100% of rows arrive.",
    "SLA": "An SLA is included with every pilot.",
}


@pytest.mark.parametrize("term", sorted(PLANTED))
def test_mutation_each_overclaim_term_fails_on_the_readme(tree, term):
    before = overclaim_violations(tree)
    plant(tree, "README.md", None, PLANTED[term])
    assert any(f"{term!r}" in item for item in new_violations(overclaim_violations, tree, before)), term


@pytest.mark.parametrize("page", [
    "docs/pypi.md", "SUPPORT.md", "ROADMAP.md", "CONTRIBUTING.md", "docs/product/faq.md",
    "docs/product/install-default.md", "docs/articles/capture-delivery-reconciliation.md", "docs/governance.md",
    EVIDENCE, SITE_PAGE,
])
def test_mutation_an_overclaim_fails_on_every_public_page(tree, page):
    before = overclaim_violations(tree)
    plant(tree, page, None, "We are battle-tested and production-ready.")
    added = new_violations(overclaim_violations, tree, before)
    assert any("battle-tested" in item and item.startswith(page) for item in added)


def test_mutation_changelog_unreleased_is_scanned_but_released_notes_are_not(tree):
    before = overclaim_violations(tree)
    text = read(tree, "CHANGELOG.md")
    (tree / "CHANGELOG.md").write_text(text.replace("## 0.4.2", "- Zero data loss.\n\n## 0.4.2", 1), encoding="utf-8")
    assert any("zero data loss" in item for item in new_violations(overclaim_violations, tree, before))
    (tree / "CHANGELOG.md").write_text(text.rstrip("\n") + "\n\n- Zero data loss in a released note.\n", encoding="utf-8")
    assert new_violations(overclaim_violations, tree, before) == []


def test_mutation_visible_site_text_attributes_and_structured_data_are_scanned(tree):
    before = overclaim_violations(tree)
    plant(tree, SITE_PAGE, None, '<img src="x.png" alt="A production-ready cockpit">')
    assert any("production-ready" in item for item in new_violations(overclaim_violations, tree, before))
    plant(tree, SITE_PAGE, None, '<script type="application/ld+json">{"description": "Trusted by banks"}</script>')
    assert any("trusted by" in item for item in new_violations(overclaim_violations, tree, before))
    plant(tree, SITE_PAGE, None, "<span>Battle&nbsp;tested</span>")
    assert any("battle-tested" in item for item in new_violations(overclaim_violations, tree, before))


def test_mutation_negated_mentions_pass_and_positive_mentions_fail_on_any_page(tree):
    before = overclaim_violations(tree)
    plant(tree, EVIDENCE, None, "We have no SLA and no P95.")
    plant(tree, "SUPPORT.md", None, "There is no price or SLA.")
    plant(tree, "README.md", None, "We have no SLA.")
    plant(tree, "docs/product/faq.md", None, "This is not an SLA.")
    assert new_violations(overclaim_violations, tree, before) == []
    plant(tree, "docs/product/faq.md", None, "Every pilot includes an SLA.")
    added = new_violations(overclaim_violations, tree, before)
    assert any(item.startswith("docs/product/faq.md") and "SLA" in item for item in added)
    plant(tree, EVIDENCE, None, "We offer an SLA for pilots.")
    plant(tree, "SUPPORT.md", None, "Delivery is guaranteed.")
    added = new_violations(overclaim_violations, tree, before)
    assert any(item.startswith(EVIDENCE) and "SLA" in item for item in added)
    assert any(item.startswith("SUPPORT.md") and "guaranteed" in item for item in added)


def test_mutation_the_css_width_is_the_only_percent_exception(tree):
    before = overclaim_violations(tree)
    plant(tree, "docs/design-system.md", None, "Reliability is 100% in our tests.")
    assert any(item.startswith("docs/design-system.md") and "100%" in item
               for item in new_violations(overclaim_violations, tree, before))


@pytest.mark.parametrize("page,sentence", [
    ("README.md", "Replication latency is 2 s."),
    ("README.md", "We load 5,000 rows per second."),
    ("README.md", "A table copy takes 3 minutes."),
    ("docs/pypi.md", "Changes arrive in 4 seconds."),
    ("docs/pypi.md", "It copies 1 GB of rows."),
    (SITE_PAGE, "Changes reach Snowflake in 7 s."),
    (SITE_PAGE, "We replicated 200 events."),
])
def test_mutation_an_unscoped_figure_fails(tree, page, sentence):
    before = figure_violations(tree)
    plant(tree, page, None, sentence)
    assert len(new_violations(figure_violations, tree, before)) == 1


@pytest.mark.parametrize("page,sentence", [
    ("README.md", "Replication latency is 2 s (0.3.5)."),
    ("README.md", "Replication latency was 2 s on 2026-10-12."),
    ("README.md", "Replication latency is 2 s, see [evidence](docs/evidence.md)."),
    ("docs/pypi.md", "Changes arrive in 4 seconds on 0.3.4."),
    (SITE_PAGE, 'Changes reach Snowflake in 7 s (<a href="../docs/evidence.md">evidence</a>).'),
    (SITE_PAGE, "Changes reached Snowflake in 7 s on 2026-10-12."),
])
def test_mutation_a_scoped_figure_passes(tree, page, sentence):
    before = figure_violations(tree)
    plant(tree, page, None, sentence)
    assert new_violations(figure_violations, tree, before) == []


def test_mutation_demo_figures_pass_only_next_to_the_word_synthetic(tree):
    before = figure_violations(tree)
    plant(tree, "README.md", None, "Synthetic demo: 120 events read, 118 events delivered.")
    assert new_violations(figure_violations, tree, before) == []
    plant(tree, "README.md", None, "We read 120 events and delivered 118 events.")
    assert len(new_violations(figure_violations, tree, before)) == 1


def test_mutation_a_figure_in_a_code_fence_is_not_a_claim(tree):
    before = figure_violations(tree)
    plant(tree, "README.md", None, "~~~sh\nsleep 10 s\n~~~")
    assert new_violations(figure_violations, tree, before) == []


PROOF = ("Initial copy of synthetic tables tested against a real IBM i and a real Snowflake account on four cloud "
         "targets (release 0.3.5, 2026-10-12), plus reader-kill recovery with source writes on 0.3.4. "
         "Receipt summaries are public: [evidence](docs/evidence.md).")
LINK = "[evidence](docs/evidence.md)"
HTML_LINK = '<a href="../docs/evidence.md">evidence</a>'


def with_proof(tree: Path, page: str, sentence: str) -> None:
    """Plant a proof line, in the markup of the page."""
    plant(tree, page, None, sentence.replace(LINK, HTML_LINK) if page == SITE_PAGE else sentence)


def without_proof_lines(tree: Path, page: str) -> None:
    """Make the anchor of every existing proof line disappear."""
    text = read(tree, page)
    text = re.sub(r"real(?:\s|&nbsp;)+IBM(?:\s|&nbsp;)+i", "real machine", text)
    (tree / page).write_text(text, encoding="utf-8")


@pytest.mark.parametrize("page", ["README.md", "docs/pypi.md", SITE_PAGE])
def test_mutation_a_correct_proof_line_passes(tree, page):
    before = proof_violations(tree)
    with_proof(tree, page, PROOF)
    assert new_violations(proof_violations, tree, before) == []


@pytest.mark.parametrize("page", ["README.md", SITE_PAGE])
@pytest.mark.parametrize("sentence,expected", [
    (PROOF.replace("0.3.5, 2026-10-12", "0.3.4, 2026-10-12"), "does not name release 0.3.5"),
    (PROOF.replace("0.3.5, 2026-10-12", "0.3.5, 2026-10-11"), "does not name 2026-10-12"),
    (PROOF.replace("0.3.5, 2026-10-12", "0.3.9, 2026-10-12"), "version 0.3.9, which has no receipt"),
    (PROOF.replace("2026-10-12", "2026-10-12 and 2027-01-05"), "2027-01-05, which no receipt carries"),
    (PROOF.replace("real IBM i", "real IBM i 7.5"), "the receipt does not name the IBM i release"),
    (PROOF.replace("a real Snowflake account", "a real warehouse"), "does not name Snowflake"),
    (PROOF.replace("synthetic tables", "customer tables"), "does not say the tables were synthetic"),
    (PROOF.replace("(release", "in production at a bank (release"), "claims more than the receipt shows"),
    (PROOF.replace("tested against", "tested with a native CDC latency of 2 s against"), "claims more than"),
    (PROOF.replace("with source writes on 0.3.4", "with source writes"), "mentions recovery without tying it"),
])
def test_mutation_a_proof_line_that_drifts_from_the_receipts_fails(tree, page, sentence, expected):
    before = proof_violations(tree)
    with_proof(tree, page, sentence)
    added = new_violations(proof_violations, tree, before)
    assert any(expected in item for item in added), added


def test_mutation_only_release_tested_statement_must_match_the_evidence_page(tree):
    before = proof_violations(tree)
    ok = PROOF.replace("on 0.3.4.", "on 0.3.4: IBM i 7.5 is the only IBM i release we have tested.")
    with_proof(tree, "docs/product/faq.md", ok)
    assert new_violations(proof_violations, tree, before) == []
    with_proof(tree, "docs/product/install-default.md", ok.replace("IBM i 7.5", "IBM i 7.6"))
    added = new_violations(proof_violations, tree, before)
    assert any("install-default" in item and "is the only release tested" in item for item in added), added
    with_proof(tree, "docs/pypi.md", ok.replace("is the only IBM i release we have tested", "ran fine"))
    added = new_violations(proof_violations, tree, before)
    assert any("docs/pypi.md" in item and "does not name the IBM i release" in item for item in added), added


def test_mutation_a_proof_line_without_a_link_fails_on_the_front_pages(tree):
    before = proof_violations(tree)
    with_proof(tree, "README.md", PROOF.replace(" Receipt summaries are public: [evidence](docs/evidence.md).", ""))
    # The same block already links the evidence page in a real README; isolate with a blank line.
    plant(tree, "docs/pypi.md", None, PROOF.replace(" Receipt summaries are public: [evidence](docs/evidence.md).", ""))
    added = new_violations(proof_violations, tree, before)
    assert any(item.startswith("docs/pypi.md") and "no link to the evidence page" in item for item in added)


@pytest.mark.parametrize("page", ["README.md", SITE_PAGE])
def test_mutation_a_missing_proof_line_fails(tree, page):
    without_proof_lines(tree, page)
    assert any(item.startswith(f"{page}: no proof line") for item in proof_violations(tree))


def test_mutation_a_negative_sentence_is_not_a_proof_line(tree):
    before = proof_violations(tree)
    plant(tree, "docs/product/faq.md", None, "We have not tested it against a real IBM i 7.6.")
    assert new_violations(proof_violations, tree, before) == []


def test_mutation_a_proof_line_elsewhere_is_checked_too(tree):
    before = proof_violations(tree)
    plant(tree, "docs/product/faq.md", None, PROOF.replace("real IBM i", "real IBM i 7.6"))
    added = new_violations(proof_violations, tree, before)
    assert any(item.startswith("docs/product/faq.md") and "7.6" in item for item in added)


def test_mutation_an_ibm_i_release_is_allowed_only_if_the_receipt_names_it(tree):
    path = tree / RUNTIME_RECEIPT
    data = json.loads(path.read_text())
    data["ibmi_release"] = "7.5"
    path.write_text(json.dumps(data))
    before = proof_violations(tree)
    plant(tree, "docs/product/faq.md", None, PROOF.replace("real IBM i", "real IBM i 7.5"))
    assert [item for item in new_violations(proof_violations, tree, before) if "faq" in item] == []
    plant(tree, "docs/product/install-default.md", None, PROOF.replace("real IBM i", "real IBM i 7.6"))
    added = new_violations(proof_violations, tree, before)
    assert any("install-default" in item and "the receipt names 7.5" in item for item in added)


@pytest.mark.parametrize("flag", ["production_qualified", "external_pilot_validated", "universal_latency_guarantee"])
def test_mutation_the_receipt_must_stay_unpromoted(tree, flag):
    before = proof_violations(tree)
    path = tree / RUNTIME_RECEIPT
    data = json.loads(path.read_text())
    data[flag] = True
    path.write_text(json.dumps(data))
    assert any(flag in item for item in new_violations(proof_violations, tree, before))


def test_mutation_the_proof_line_follows_the_receipt_version(tree):
    with_proof(tree, "README.md", PROOF)
    path = tree / RUNTIME_RECEIPT
    data = json.loads(path.read_text())
    data["version"] = "0.3.6"
    data["date_utc"] = "2026-10-20"
    path.write_text(json.dumps(data))
    assert any("does not name release 0.3.6" in item for item in proof_violations(tree))


def test_mutation_the_evidence_page_fails_when_it_loses_a_receipt_number(tree):
    before = evidence_violations(tree)
    recovery = receipt(tree, RECOVERY_RECEIPT)
    cold = f"{recovery['sites']['gcp-vm']['cold_SQL_to_mirror_upper_bound_s']:.3f}"
    first = receipt(tree, QUALIFICATION_RECEIPT)["cold_warehouse_latency"]["targets"]["gcp-vm"]
    historical = f"{first['maximum_SQL_start_seconds']:.3f}"
    path = tree / EVIDENCE
    original = path.read_text()
    for number in (cold, historical, "2.317 to 7.992 s", "148", "55", "direct Helm", "0.3.5 to 0.3.4 to 0.3.5"):
        assert number in original, number
        path.write_text(original.replace(number, "REDACTED"))
        assert new_violations(evidence_violations, tree, before), number
    path.write_text(original)


@pytest.mark.parametrize("label,pattern", EVIDENCE_LIMITS, ids=[label for label, _ in EVIDENCE_LIMITS])
def test_mutation_the_evidence_page_fails_when_it_loses_a_limit(tree, label, pattern):
    path = tree / EVIDENCE
    original = path.read_text()
    if not re.search(pattern, flat(original), re.S | re.I):
        pytest.fail(f"the clean evidence page does not carry the limit {label!r}")
    path.write_text(re.sub(pattern, "", original, flags=re.S | re.I))
    assert any(f"limit missing: {label}" in item for item in evidence_violations(tree)), label


def test_mutation_the_proof_line_cannot_say_end_to_end_or_drop_its_scope(tree):
    before = proof_violations(tree)
    for page in ("README.md", "docs/pypi.md"):
        plant(tree, page, "tested against a real IBM i", "tested end to end against a real IBM i")
    added = new_violations(proof_violations, tree, before)
    assert {item.split(":")[0] for item in added if "end to end" in item} == {"README.md", "docs/pypi.md"}
    before = proof_violations(tree)
    plant(tree, "README.md", "Initial copy", "Copy")
    assert any("initial copy" in item for item in new_violations(proof_violations, tree, before))


def test_mutation_receipts_are_public_is_refused_because_the_source_receipts_are_private(tree):
    before = proof_violations(tree)
    plant(tree, "docs/pypi.md", "Receipt summaries are public.", "Receipts are public.")
    assert any("Receipts are public" in item for item in new_violations(proof_violations, tree, before))


def test_evidence_page_says_the_source_receipts_are_retained_privately():
    assert "source receipts behind the published summaries are retained privately" in flat(read(ROOT, EVIDENCE))

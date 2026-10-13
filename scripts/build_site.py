"Build the product site with documentation links to the selected repository."
import argparse
from html import escape, unescape
import json
import posixpath
from pathlib import Path
import re
import shutil
from urllib.parse import urlparse, urlsplit

DOC_PAGES = {
    "README.md": ("getting-started", "Getting started with IBM i CDC to Snowflake"),
    "docs/product/synthetic-demo.md": ("demo", "Try the cockpit with synthetic data"),
    "docs/product/compatibility.md": ("compatibility", "Compatibility and prerequisites for IBM i and Snowflake"),
    "docs/product/install-default.md": ("installation", "Install Quadringent on AWS or GCP"),
    "docs/product/install-client.md": ("helm-installation", "Helm installation and prerequisites"),
    "docs/product/operator-guide.md": ("operator-guide", "Operator guide: install, evidence and recovery"),
    "docs/architecture.md": ("architecture", "IBM i to Snowflake replication architecture"),
    "docs/product/quadringent-destination-proof-contract.md": ("delivery-evidence", "Reading, delivery and reconciliation evidence"),
    "docs/operations.md": ("operations", "Operate and recover Quadringent"),
    "docs/product/native-qualification.md": ("qualification", "Qualify replication, outages and recovery"),
    "docs/api-v2.md": ("api", "Control plane API and evidence"),
    "SECURITY.md": ("security", "Security and access boundaries"),
    "docs/licensing.md": ("community", "Community Edition and Apache-2.0"),
    "docs/product/positioning-and-offer.md": ("pilot", "Prepare an IBM i to Snowflake pilot"),
    "docs/product/ibm-i-footprint.md": ("ibm-i-footprint", "What Quadringent installs and does on your IBM i"),
    "docs/product/your-data.md": ("your-data", "Where your IBM i data goes and how to stop"),
    "docs/product/verify-a-release.md": ("verify-a-release", "Verify a Quadringent release against the public code"),
    "docs/governance.md": ("governance", "Governance and continuity of an open-source IBM i CDC project"),
    "SUPPORT.md": ("support", "Support for Quadringent Community"),
    "docs/product/faq.md": ("faq", "Questions for a first call about IBM i to Snowflake CDC"),
    "docs/product/comparison.md": ("comparison", "How Quadringent compares with other IBM i to Snowflake options"),
    "docs/articles/ibm-i-journals-for-cdc.md": ("ibm-i-journals-for-cdc", "How IBM i journals work for change data capture"),
    "docs/articles/capture-delivery-reconciliation.md": ("capture-delivery-reconciliation", "Capture is not delivery: proving IBM i data reached Snowflake"),
    "docs/articles/as400-to-snowflake-options.md": ("as400-to-snowflake-options", "Options to replicate AS/400 (IBM i) data to Snowflake"),
}

# Search snippets: what each page lets a reader do, not a shared template.
DOC_DESCRIPTIONS = {
    "README.md": "Start with Quadringent Community: what it replicates from IBM i to Snowflake, its preview status and how to try it.",
    "docs/product/synthetic-demo.md": "Run the Quadringent cockpit locally with synthetic data, without IBM i, Snowflake, cloud access or credentials.",
    "docs/product/compatibility.md": "Check IBM i releases, journaling, keys, authorities, receiver retention, Snowflake roles and default resources before installing Quadringent.",
    "docs/product/install-default.md": "Install Quadringent on AWS or GCP, on a dedicated VM or an existing cluster, with a reviewed Terraform plan.",
    "docs/product/install-client.md": "Install Quadringent with Helm: secrets, values, table readiness, journaling and commissioning steps.",
    "docs/product/operator-guide.md": "Operate a DEV/INT installation: preflight, first copy, reading evidence, outages, recovery, backup and stop.",
    "docs/architecture.md": "How Quadringent reads IBM i journals over TLS, keeps raw data and checkpoints, and loads Snowflake history and mirror tables.",
    "docs/product/quadringent-destination-proof-contract.md": "How Quadringent separates journal reading, Snowflake delivery and reconciliation evidence, and what each proves.",
    "docs/operations.md": "Operate and recover Quadringent readers: parked readers, journal freshness, checkpoints and audit.",
    "docs/product/native-qualification.md": "How Quadringent qualifies replication, reader outages and recovery on admitted release artifacts.",
    "docs/api-v2.md": "Quadringent control plane API v2: sources, destinations, pipelines, evidence, confirmations and audit.",
    "SECURITY.md": "Quadringent security boundaries: authentication, network flows, TLS, identities, data retention and vulnerability reporting.",
    "docs/licensing.md": "Quadringent Community Edition under Apache-2.0: what is free, what may be offered later and what stays granted.",
    "docs/product/positioning-and-offer.md": "Prepare and request a DEV/INT pilot of IBM i to Snowflake replication, privately or on GitHub.",
    "docs/product/ibm-i-footprint.md": "Check what Quadringent installs and runs on your IBM i, which authorities each part needs, what it changes and what it never does, before you approve access.",
    "docs/product/your-data.md": "Follow your IBM i data from the journal to Snowflake, see what leaves your accounts, and follow the steps to stop, uninstall and keep your data.",
    "docs/product/verify-a-release.md": "Verify image signatures, SBOMs and pinned digests of a published Quadringent release on your own machine, and learn what a passing check does not prove.",
    "docs/governance.md": "How the pseudonymous Quadringent project is run, how decisions are made and what remains available if the maintainers stop.",
    "SUPPORT.md": "Find out what support exists for Quadringent Community, where to ask for help and what is not offered.",
    "docs/product/faq.md": "Answers to the questions a data or IBM i team asks on a first call: scope, IBM i impact, data location, status, cost and support.",
    "docs/product/comparison.md": "Compare Quadringent with other ways to move IBM i data to Snowflake, with sourced vendor facts and the trade-offs on both sides.",
    "docs/articles/ibm-i-journals-for-cdc.md": "How IBM i journals and journal receivers record changes, and what a change data capture reader must handle: retention, keys and recovery.",
    "docs/articles/capture-delivery-reconciliation.md": "Why reading a journal does not prove Snowflake is current, and how to separate capture, delivery and reconciliation evidence.",
    "docs/articles/as400-to-snowflake-options.md": "Compare the main ways to replicate AS/400 (IBM i) data to Snowflake: batch extracts, commercial CDC, Kafka-based CDC and Quadringent.",
}

# Python-Markdown passes unknown inline tags through; placeholders such as
# <owner> or <host> would then vanish in the browser. Escape only those.
KNOWN_HTML_TAGS = frozenset("""a abbr b blockquote br caption code dd del details div dl dt em figcaption figure
h1 h2 h3 h4 h5 h6 hr i img kbd li ol p pre s small span strong sub summary sup table tbody td th thead
time tr ul""".split())


def escape_unknown_tags(html: str) -> str:
    def replace(match: re.Match) -> str:
        return match[0] if match[2].lower() in KNOWN_HTML_TAGS else escape(match[0], quote=False)
    return re.sub(r"<(/?)([A-Za-z][A-Za-z0-9_-]*)([^<>]*)>", replace, html)


def public_site_base(value: str) -> str:
    parsed = urlparse(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.port is not None or parsed.query or parsed.fragment
            or re.fullmatch(r"https://[A-Za-z0-9.-]+(?:/[A-Za-z0-9_.-]+)*/?", value) is None
            or any(part in (".", "..") for part in parsed.path.split("/"))):
        raise ValueError("Public site HTTPS URL required, without credentials or parameters")
    return value.rstrip("/") + "/"


def seo_head(url: str, title: str, description: str, base: str, repository: str, *, home: bool) -> str:
    schema = {
        "@context": "https://schema.org",
        "@type": "SoftwareApplication" if home else "TechArticle",
        "name": "Quadringent" if home else title,
        "url": url, "description": description,
    }
    if home:
        schema.update(applicationCategory="DeveloperApplication", operatingSystem="Linux",
                      license=repository + "/blob/main/LICENSE", isAccessibleForFree=True)
    else:
        schema["publisher"] = {"@type": "Organization", "name": "Quadringent", "url": base}
    structured = json.dumps(schema, ensure_ascii=False).replace("<", "\\u003c")
    return (
        f'<link rel="canonical" href="{escape(url, quote=True)}">\n'
        '<meta name="robots" content="index,follow">\n'
        f'<meta property="og:title" content="{escape(title, quote=True)}">\n'
        f'<meta property="og:description" content="{escape(description, quote=True)}">\n'
        f'<meta property="og:url" content="{escape(url, quote=True)}">\n'
        '<meta property="og:type" content="website">\n'
        f'<meta property="og:image" content="{escape(base + "assets/cockpit-connection.png", quote=True)}">\n'
        '<meta name="twitter:card" content="summary_large_image">\n'
        f'<script type="application/ld+json">{structured}</script>\n'
    )


def publish_guides(root: Path, output: Path, base: str, repository: str) -> list[str]:
    import markdown
    available = {name: details for name, details in DOC_PAGES.items() if (root / name).is_file()}
    urls = [base]
    navigation = '<a href="../../">Quadringent</a> · <a href="../">Documentation</a> · ' + f'<a href="{repository}">GitHub</a>'
    index_links = []
    for source, (slug, title) in available.items():
        description = DOC_DESCRIPTIONS.get(source) or f"{title}. English Community Edition documentation."
        body = escape_unknown_tags(markdown.markdown((root / source).read_text(),
                                                     extensions=["fenced_code", "tables", "toc", "sane_lists"]))

        def link(match):
            attr, raw = match[1], unescape(match[2])
            parsed = urlsplit(raw)
            if parsed.scheme or parsed.netloc or raw.startswith(("#", "/")):
                return match[0]
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source), parsed.path))
            if attr == "href" and resolved in available:
                target = "../" + available[resolved][0] + "/"
            elif attr == "src" and resolved.startswith("site/assets/"):
                target = "../../" + resolved.removeprefix("site/")
            elif attr == "src":
                target = repository.replace("https://github.com/", "https://raw.githubusercontent.com/") + "/main/" + resolved
            else:
                target = repository + "/blob/main/" + resolved
            if parsed.fragment:
                target += "#" + parsed.fragment
            return f'{attr}="{escape(target, quote=True)}"'

        body = re.sub(r'(href|src)="([^"]+)"', link, body)
        url = base + f"docs/{slug}/"
        head = seo_head(url, title, description, base, repository, home=False)
        page = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)} · Quadringent</title><meta name="description" content="{escape(description, quote=True)}">
{head}<link rel="stylesheet" href="../../style.css"></head>
<body><main class="guide"><nav aria-label="Documentation">{navigation}</nav>
<p class="guide__scope">Community Edition · DEV/INT preview</p>
<article>{body}</article><footer><a href="{repository}/blob/main/{source}">View source on GitHub</a></footer>
</main></body></html>'''
        folder = output / "docs" / slug
        folder.mkdir(parents=True)
        (folder / "index.html").write_text(page)
        urls.append(url)
        index_links.append(f'<li><a href="{slug}/">{escape(title)}</a></li>')
    if available:
        docs = output / "docs"
        title = "Quadringent documentation"
        (docs / "index.html").write_text(
            '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{title}</title><meta name="description" content="English installation, evidence, operations and security guides.">'
            + seo_head(base + "docs/", title, "English installation, evidence, operations and security guides.", base, repository, home=False)
            + '<link rel="stylesheet" href="../style.css"></head><body><main class="guide"><nav><a href="../">Quadringent</a></nav>'
            f'<h1>{title}</h1><ul>{"".join(index_links)}</ul></main></body></html>'
        )
        urls.append(base + "docs/")
    return urls

# Polices du cockpit : copiées depuis les paquets npm verrouillés de l'UI,
# jamais versionnées dans site/ (flux binaires non relisibles par les contrôles).
FONT_PACKAGES = {
    "ibm-plex-sans-latin-wght-normal.woff2": "@fontsource-variable/ibm-plex-sans",
    "ibm-plex-mono-latin-400-normal.woff2": "@fontsource/ibm-plex-mono",
    "ibm-plex-mono-latin-500-normal.woff2": "@fontsource/ibm-plex-mono",
}


def repository_base(repository_url: str) -> str:
    """Accepte une URL web GitHub de dépôt, sans identifiant ni suffixe ambigu."""
    parsed = urlparse(repository_url)
    if (parsed.scheme != "https" or parsed.netloc != "github.com"
            or parsed.query or parsed.fragment
            or re.fullmatch(r"https://github\.com/[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9][A-Za-z0-9_.-]*/?",
                            repository_url) is None):
        raise ValueError("GitHub repository HTTPS URL required, without parameters or credentials")
    return repository_url.rstrip("/")


def font_sources(root: Path) -> dict[str, Path]:
    """Polices référencées par la feuille de style, résolues dans ui/node_modules."""
    stylesheet = root / "site" / "style.css"
    names = set(re.findall(r'url\("assets/fonts/([^"/]+\.woff2)"\)',
                           stylesheet.read_text() if stylesheet.is_file() else ""))
    if not names:
        return {}
    lock = json.loads((root / "ui" / "package-lock.json").read_text())["packages"]
    sources = {}
    for name in sorted(names):
        package = FONT_PACKAGES.get(name)
        if package is None:
            raise ValueError(f"Font has no declared provenance: {name}")
        installed = root / "ui" / "node_modules" / package
        manifest = installed / "package.json"
        locked = lock.get(f"node_modules/{package}", {}).get("version")
        if not manifest.is_file() or json.loads(manifest.read_text()).get("version") != locked:
            raise ValueError(f"{package} absent or different from ui/package-lock.json: run npm ci --prefix ui")
        source = installed / "files" / name
        if not source.is_file():
            raise ValueError(f"Font absent from package {package}: {name}")
        sources[name] = source
    return sources


def build(repository_url: str, output: Path, *, source_root: Path | None = None, site_url: str | None = None) -> None:
    base = repository_base(repository_url)
    site_base = public_site_base(site_url) if site_url else None
    if output.exists():
        raise ValueError("The output directory must be new")
    root = source_root or Path(__file__).resolve().parents[1]
    fonts = font_sources(root)
    shutil.copytree(root / "site", output)
    for name, source in fonts.items():
        target = output / "assets" / "fonts" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    stylesheet = output / "style.css"
    if fonts:
        stylesheet.write_text(re.sub(r'url\("\.\./ui/node_modules/[^"]+"\) format\("[^"]+"\),\s*', "",
                                     stylesheet.read_text()))
    page = output / "index.html"
    text = page.read_text()
    text = re.sub(r'data-repo-path="([A-Za-z0-9_./-]+)" href="[^"]+"',
                  lambda m: f'href="{base}/blob/main/{m[1]}"', text)
    text = re.sub(r'data-repo-root="true" href="[^"]+"', f'href="{base}"', text)
    page.write_text(text)
    if site_base:
        title_match = re.search(r"<title>(.*?)</title>", text, re.S)
        description_match = re.search(r'<meta name="description" content="([^"]+)"', text)
        title = unescape(title_match[1]) if title_match else "Quadringent — IBM i CDC to Snowflake"
        description = unescape(description_match[1]) if description_match else "IBM i journals to Snowflake. Community Edition DEV/INT preview."
        text = text.replace("</head>", seo_head(site_base, title, description, site_base, base, home=True) + "</head>")
        for source, (slug, _) in DOC_PAGES.items():
            if (root / source).is_file():
                text = text.replace(f'href="{base}/blob/main/{source}"', f'href="docs/{slug}/"')
        page.write_text(text)
        urls = publish_guides(root, output, site_base, base)
        (output / "sitemap.xml").write_text('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' +
            "".join(f"<url><loc>{escape(url)}</loc></url>" for url in urls) + "</urlset>\n")
        # Crawlers read robots.txt only at the host root; a copy under a
        # project path is ignored, so it is written only for a root site.
        if urlparse(site_base).path in ("", "/"):
            (output / "robots.txt").write_text(f"User-agent: *\nAllow: /\n\nSitemap: {site_base}sitemap.xml\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--site-url", help="Canonical HTTPS URL of the published site")
    args = parser.parse_args()
    build(args.repository_url, args.output, site_url=args.site_url)

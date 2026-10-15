"""La présentation statique reste dans la famille visuelle du cockpit.

Les jetons du site sont une copie partielle du rendu effectif du cockpit :
``product-tokens.css`` puis les surcharges ``:root`` de ``commercial.css``, dans
l'ordre d'import de ``ui/src/main.tsx``. Toute valeur partagée doit rester
identique dans les thèmes clair et sombre.
Les liens, ancres et fichiers référencés par la page doivent exister.
"""
from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
import re
import subprocess

import pytest

import build_site

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"
STYLES = ROOT / "ui" / "src" / "styles"
PRODUCT_TOKENS = STYLES / "product-tokens.css"
# Couche chargée après les jetons : elle redéfinit le thème clair seulement
# (les blocs sombres, plus spécifiques, l'emportent toujours).
COMMERCIAL = STYLES / "commercial.css"

# Blocs comparés : thème clair, sombre par préférence, sombre explicite.
SCOPES = {
    "light": r"(?<![\w-]):root\s*\{",
    "dark-preference": r":root:not\(\[data-theme=\"light\"\]\)\s*\{",
    "dark-explicit": r":root\[data-theme=\"dark\"\]\s*\{",
}


def declarations(css: str, opening: str) -> dict[str, str]:
    match = re.search(opening, css)
    assert match, f"bloc {opening} absent"
    start, depth = match.end(), 1
    for index in range(start, len(css)):
        depth += {"{": 1, "}": -1}.get(css[index], 0)
        if depth == 0:
            body = re.sub(r"/\*.*?\*/", "", css[start:index], flags=re.S)
            return {name: " ".join(value.split())
                    for name, value in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", body)}
    raise AssertionError("bloc non fermé")


def test_commercial_layer_is_imported_after_product_tokens():
    imports = re.findall(r"import '\./styles/([\w-]+)\.css';", (ROOT / "ui" / "src" / "main.tsx").read_text())
    assert imports[0] == "product-tokens" and imports[-1] == "commercial"


def effective_tokens(scope: str) -> dict[str, str]:
    tokens = declarations(PRODUCT_TOKENS.read_text(), SCOPES[scope])
    if scope == "light":
        tokens.update(declarations(COMMERCIAL.read_text(), SCOPES[scope]))
    return tokens


@pytest.mark.parametrize("scope", sorted(SCOPES))
def test_site_tokens_match_rendered_product_tokens(scope: str):
    site = declarations((SITE / "style.css").read_text(), SCOPES[scope])
    product = effective_tokens(scope)
    shared = sorted(set(site) & set(product))
    assert len(shared) >= (30 if scope == "light" else 15)
    drift = {name: (site[name], product[name]) for name in shared if site[name] != product[name]}
    assert drift == {}
    # Les jetons ajoutés par le site restent préfixés et éditoriaux.
    extra = set(site) - set(product)
    assert all(name.startswith(("--site-", "--t-display", "--t-section", "--section-")) for name in extra), extra


def test_site_uses_product_typography_only():
    css = (SITE / "style.css").read_text()
    assert "IBM Plex Sans Variable" in css and "IBM Plex Mono" in css
    assert not re.search(r"serif\s*[;,\"]", css.replace("sans-serif", "")), "police serif interdite"
    assert not re.search(r"(linear|radial|conic)-gradient|backdrop-filter", css)
    families = set(re.findall(r"font-family:\s*([^;]+);", css))
    assert families <= {'"IBM Plex Sans Variable"', '"IBM Plex Mono"', "var(--font-sans)", "var(--font-mono)"}


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.links: list[dict[str, str]] = []
        self.images: list[dict[str, str]] = []
        self.figures: list[list[str]] = []
        self._figure: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        values = {key: value or "" for key, value in attrs}
        if "id" in values:
            self.ids.add(values["id"])
        if tag in {"a", "link"}:
            self.links.append(values)
        if tag == "img":
            self.images.append(values)
        if tag == "source":
            self.images.append({"src": values["srcset"], "alt": "x" * 41, "width": "1", "height": "1"})
        if tag == "figure":
            self._figure = []
        if self._figure is not None and tag == "img":
            self._figure.append(f"img:{values.get('src', '')}")

    def handle_endtag(self, tag):
        if tag == "figure" and self._figure is not None:
            self.figures.append(self._figure)
            self._figure = None

    def handle_data(self, data):
        if self._figure is not None and data.strip():
            self._figure.append(data.strip())


@pytest.fixture(scope="module")
def page() -> _Page:
    parser = _Page()
    parser.feed((SITE / "index.html").read_text())
    return parser


def test_repository_links_target_existing_files(page: _Page):
    repo_links = [link for link in page.links if "data-repo-path" in link]
    assert len(repo_links) >= 8
    for link in repo_links:
        target = ROOT / link["data-repo-path"]
        assert target.is_file(), link
        # Le rendu local (servi depuis la racine) et le build Pages ciblent le même fichier.
        assert link["href"] == f"../{link['data-repo-path']}"


def test_local_references_and_anchors_resolve(page: _Page):
    for link in page.links:
        href = link.get("href", "")
        if href.startswith("#"):
            assert href[1:] in page.ids, href
        elif href.startswith("assets/fonts/") and href.endswith(".woff2"):
            assert href.removeprefix("assets/fonts/") in build_site.FONT_PACKAGES, href
        elif href.startswith("mailto:"):
            # The private pilot contact: an address, never a local path or a query with data.
            assert re.fullmatch(r"mailto:quadringent@proton\.me(\?subject=[A-Za-z0-9%]+)?", href), href
        elif not href.startswith(("../", "https://", "data:")):
            assert (SITE / href).is_file(), href
    for image in page.images:
        assert (SITE / image["src"]).is_file(), image
        assert len(image.get("alt", "")) > 40 and image.get("width") and image.get("height")


def test_product_captures_are_labelled_synthetic(page: _Page):
    captures = [figure for figure in page.figures if any(item.startswith("img:") for item in figure)]
    assert captures
    for figure in captures:
        text = " ".join(figure)
        assert "Demonstration" in text and "synthetic" in text, text


def test_fonts_come_from_locked_packages_with_their_license():
    css = (SITE / "style.css").read_text()
    referenced = set(re.findall(r'url\("assets/fonts/([^"/]+\.woff2)"\)', css))
    assert referenced == set(build_site.FONT_PACKAGES)
    # Aucun flux binaire de police versionné : la copie se fait au build.
    ignored = subprocess.run(["git", "check-ignore", "-q", "site/assets/fonts/exemple.woff2"], cwd=ROOT)
    assert ignored.returncode == 0
    for family in ("Sans", "Mono"):
        license_text = (SITE / "assets" / "fonts" / f"OFL-IBM-Plex-{family}.txt").read_text()
        assert "SIL Open Font License" in license_text and "IBM Corp." in license_text


def test_green_on_black_is_limited_to_command_blocks():
    """The 5250-like look styles commands only: no global green theme, no gradients, no CRT effects."""
    css = (SITE / "style.css").read_text()
    uses = [m.start() for m in re.finditer(r"var\(--site-screen-(?:fg|dim|bg)\)", css)]
    assert uses
    for position in uses:
        selector = css[css.rfind("}", 0, position) + 1:css.index("{", css.rfind("}", 0, position))]
        assert ".screen" in selector or ".code .screen__dim" in selector, selector.strip()
    assert "--site-screen-fg" in declarations(css, SCOPES["light"])
    for dark in ("dark-preference", "dark-explicit"):
        assert "--site-screen-fg" not in declarations(css, SCOPES[dark]), "the screen block is the same in both themes"
    assert not re.search(r"text-shadow|@keyframes|animation:", css)
    page_html = (SITE / "index.html").read_text()
    assert page_html.count('<pre class="screen') == 3, "only the three command examples"


def rules(css: str) -> list[tuple[str, str]]:
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return [(selector.strip(), body) for selector, body in re.findall(r"([^{}@]+)\{([^{}]*)\}", css)]


def test_status_words_are_words_never_squares_on_the_site():
    html = (SITE / "index.html").read_text()
    assert "status-word__mark" not in html and "status-word__label" not in html
    css = (SITE / "style.css").read_text()
    assert "status-word__mark" not in css
    for use in re.findall(r'<span class="status-word[^"]*">(.*?)</span>', html):
        assert re.fullmatch(r"[A-Za-z][^<]*", use), use


def test_site_has_no_coloured_rail_eyebrow_card_or_callout():
    """The anti-slop rules of docs/design-system.md, enforced on the stylesheet and the page."""
    css = (SITE / "style.css").read_text()
    for selector, body in rules(css):
        assert not re.search(r"border-(left|right|inline-start|inline-end)\s*:\s*[2-9]", body), selector
        for side in ("left", "inline-start"):
            colour = re.search(rf"border-{side}\s*:\s*1px solid ([^;]+)", body)
            if colour:
                assert re.search(r"--text-(strong|subtle)|--line-", colour[1]), (selector, colour[1])
        assert not re.search(r"box-shadow\s*:[^;]*inset\s+-?\d+px\s+0\s+0", body), selector
        assert not re.search(r"box-shadow\s*:\s*(?!none)[^;]*rgba", body), selector
        for radius in re.findall(r"border-radius\s*:\s*([^;]+)", body):
            assert re.fullmatch(r"0|var\(--(radius-panel|r-sm|r-md)\)", radius.strip()), (selector, radius)
        if "text-transform: uppercase" in body:
            assert selector.endswith("th"), f"uppercase only on table column headers: {selector}"
        assert not re.search(r"var\(--signal-[a-z]+-soft\)", body), f"no tinted callout background: {selector}"
    assert ".eyebrow" not in css and "provenance-note" not in css and "metric-tile" not in css
    html = (SITE / "index.html").read_text()
    for class_name in ("eyebrow", "card", "callout", "badge", "pill", "tile"):
        assert not re.search(rf'class="[^"]*\b{class_name}\b', html), class_name
    labels = re.sub(r"<[^>]+>", " ", html).replace("Cockpit · ", "")
    assert labels.count(" · ") == 1 and re.search(r"Version \d+\.\d+ · Apache-2\.0", labels), (
        "no dot separators in labels, except the single status line Version X.Y · Apache-2.0")

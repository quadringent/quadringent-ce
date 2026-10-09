"Build the product site with documentation links to the selected repository."
import argparse
import json
from pathlib import Path
import re
import shutil
from urllib.parse import urlparse

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


def build(repository_url: str, output: Path, *, source_root: Path | None = None) -> None:
    base = repository_base(repository_url)
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build(args.repository_url, args.output)

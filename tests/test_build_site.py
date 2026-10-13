import json
from pathlib import Path

import pytest

from build_site import build, repository_base


def test_published_site_has_canonical_metadata_and_discovery_files(tmp_path):
    root = tmp_path / "source"
    (root / "site").mkdir(parents=True)
    (root / "site/index.html").write_text('<html><head><title>Quadringent</title></head><body><h1>IBM i to Snowflake</h1></body></html>')
    (root / "site/style.css").write_text("")
    (root / "README.md").write_text("# Getting started\n\nInstall Community.\n\n[Security](SECURITY.md)\n\n![Cockpit](site/assets/cockpit.png)")
    (root / "SECURITY.md").write_text("# Security\n\nUse scoped identities.")
    out = tmp_path / "built"
    build("https://github.com/example/project", out, source_root=root, site_url="https://example.github.io/project/")
    home = (out / "index.html").read_text()
    assert 'rel="canonical" href="https://example.github.io/project/"' in home
    assert 'property="og:title"' in home
    assert 'application/ld+json' in home
    assert 'https://example.github.io/project/docs/security/' in (out / "sitemap.xml").read_text()
    assert not (out / "robots.txt").exists()  # ignored by crawlers below the host root
    guide = (out / "docs/getting-started/index.html").read_text()
    assert 'href="../security/"' in guide
    assert 'href="../../style.css"' in guide
    assert 'src="../../assets/cockpit.png"' in guide


@pytest.mark.parametrize("url", ["http://example.invalid", "https://user:password@example.invalid", "https://example.invalid/?token=x", "https://example.invalid/#x"])
def test_invalid_site_identity_does_not_create_output(tmp_path, url):
    out = tmp_path / "built"
    with pytest.raises(ValueError):
        build("https://github.com/example/project", out, site_url=url)
    assert not out.exists()


@pytest.mark.parametrize("url", [
    "http://github.com/example/project",
    "https://example.invalid/example/project",
    "https://user:password@github.com/example/project",
    "https://github.com:443/example/project",
    "https://github.com/example/project?token=example",
    "https://github.com/example/project#fragment",
    'https://github.com/example/project" onfocus="example',
    "https://github.com/example/project\n",
    "https://github.com/example/project/../other",
])
def test_unsafe_repository_url_does_not_create_output(tmp_path: Path, url: str):
    output = tmp_path / "built"
    with pytest.raises(ValueError):
        build(url, output)
    assert not output.exists()


def test_generated_links_target_selected_repository(tmp_path: Path):
    source = tmp_path / "source"
    site = source / "site"
    site.mkdir(parents=True)
    (site / "index.html").write_text(
        '<a data-repo-path="docs/product/install-client.md" href="../old.md">Installer</a>'
        '<a data-repo-root="true" href="../README.md">Code</a>'
        '<a href="#offre">Offre</a><link rel="stylesheet" href="style.css">'
    )
    (site / "style.css").write_text("body { color: black; }")
    output = tmp_path / "built"
    build("https://github.com/example/project/", output, source_root=source)
    page = (output / "index.html").read_text()
    assert 'href="https://github.com/example/project/blob/main/docs/product/install-client.md"' in page
    assert 'href="https://github.com/example/project"' in page
    assert 'href="#offre"' in page and 'href="style.css"' in page
    assert "data-repo-" not in page and "../old.md" not in page
    assert (output / "style.css").read_bytes() == (site / "style.css").read_bytes()


def test_existing_output_is_preserved(tmp_path: Path):
    (tmp_path / "kept").write_text("retained")
    with pytest.raises(ValueError):
        build("https://github.com/example/project", tmp_path)
    assert (tmp_path / "kept").read_text() == "retained"
    assert repository_base("https://github.com/example/project/") == "https://github.com/example/project"


def _font_source(source: Path, package: str, version: str, files: dict[str, bytes]) -> None:
    root = source / "ui" / "node_modules" / package
    (root / "files").mkdir(parents=True)
    (root / "package.json").write_text(json.dumps({"name": package, "version": version}))
    for name, content in files.items():
        (root / "files" / name).write_bytes(content)


def _font_tree(tmp_path: Path, *, installed: str = "5.3.0") -> Path:
    source = tmp_path / "source"
    (source / "site").mkdir(parents=True)
    (source / "site" / "index.html").write_text('<link rel="stylesheet" href="style.css">')
    (source / "site" / "style.css").write_text(
        '@font-face { src: url("assets/fonts/ibm-plex-sans-latin-wght-normal.woff2"); }\n'
        '@font-face { src: url("assets/fonts/ibm-plex-mono-latin-400-normal.woff2"); }\n')
    lock = {"packages": {f"node_modules/{name}": {"version": "5.3.0"}
                         for name in ("@fontsource-variable/ibm-plex-sans", "@fontsource/ibm-plex-mono")}}
    (source / "ui").mkdir()
    (source / "ui" / "package-lock.json").write_text(json.dumps(lock))
    _font_source(source, "@fontsource-variable/ibm-plex-sans", installed,
                 {"ibm-plex-sans-latin-wght-normal.woff2": b"sans"})
    _font_source(source, "@fontsource/ibm-plex-mono", "5.3.0",
                 {"ibm-plex-mono-latin-400-normal.woff2": b"mono"})
    return source


def test_referenced_fonts_are_copied_from_locked_packages(tmp_path: Path):
    source = _font_tree(tmp_path)
    output = tmp_path / "built"
    build("https://github.com/example/project", output, source_root=source)
    fonts = output / "assets" / "fonts"
    assert (fonts / "ibm-plex-sans-latin-wght-normal.woff2").read_bytes() == b"sans"
    assert (fonts / "ibm-plex-mono-latin-400-normal.woff2").read_bytes() == b"mono"


@pytest.mark.parametrize("defect", ["missing", "unlocked_version", "unknown_font"])
def test_font_provenance_defect_does_not_create_output(tmp_path: Path, defect: str):
    source = _font_tree(tmp_path, installed="5.4.0" if defect == "unlocked_version" else "5.3.0")
    if defect == "missing":
        next((source / "ui" / "node_modules" / "@fontsource" / "ibm-plex-mono" / "files").iterdir()).unlink()
    if defect == "unknown_font":
        (source / "site" / "style.css").write_text('@font-face { src: url("assets/fonts/autre.woff2"); }')
    output = tmp_path / "built"
    with pytest.raises(ValueError):
        build("https://github.com/example/project", output, source_root=source)
    assert not output.exists()


def test_local_preview_font_fallback_is_removed_from_output(tmp_path: Path):
    source = _font_tree(tmp_path)
    css = source / "site" / "style.css"
    css.write_text('@font-face { src: url("../ui/node_modules/@fontsource/ibm-plex-mono/files/'
                   'ibm-plex-mono-latin-400-normal.woff2") format("woff2"),\n'
                   '    url("assets/fonts/ibm-plex-mono-latin-400-normal.woff2") format("woff2"); }')
    output = tmp_path / "built"
    build("https://github.com/example/project", output, source_root=source)
    built = (output / "style.css").read_text()
    assert "node_modules" not in built and 'url("assets/fonts/ibm-plex-mono-latin-400-normal.woff2") format("woff2");' in built


def test_root_site_publishes_robots_with_its_sitemap(tmp_path):
    root = tmp_path / "source"
    (root / "site").mkdir(parents=True)
    (root / "site/index.html").write_text("<html><head><title>Q</title></head><body><h1>Q</h1></body></html>")
    (root / "site/style.css").write_text("")
    out = tmp_path / "built"
    build("https://github.com/example/project", out, source_root=root, site_url="https://docs.example.org/")
    assert "Sitemap: https://docs.example.org/sitemap.xml" in (out / "robots.txt").read_text()


def test_unknown_inline_tags_are_escaped_and_known_markup_is_kept(tmp_path):
    root = tmp_path / "source"
    (root / "site").mkdir(parents=True)
    (root / "site/index.html").write_text("<html><head><title>Q</title></head><body></body></html>")
    (root / "site/style.css").write_text("")
    (root / "README.md").write_text("# Start\n\nPull ghcr.io/<owner>/app, run --check-ibmi <host>.\n\n"
                                    "<details><summary>More</summary>Kept</details>\n\n`code <x>`\n")
    out = tmp_path / "built"
    build("https://github.com/example/project", out, source_root=root, site_url="https://example.github.io/project/")
    page = (out / "docs/getting-started/index.html").read_text()
    assert "ghcr.io/&lt;owner&gt;/app" in page and "--check-ibmi &lt;host&gt;" in page
    assert "<details><summary>More</summary>" in page
    assert "<code>code &lt;x&gt;</code>" in page


def test_readme_picture_renders_as_html_with_both_theme_images(tmp_path):
    import subprocess, sys
    out = tmp_path / "site"
    subprocess.run([sys.executable, "scripts/build_site.py", "--repository-url", "https://github.com/quadringent/quadringent-ce",
                    "--site-url", "https://quadringent.github.io/quadringent-ce/", "--output", str(out)], check=True,
                   cwd=Path(__file__).resolve().parents[1], capture_output=True)
    page = (out / "docs" / "getting-started" / "index.html").read_text()
    assert "&lt;picture" not in page and "<picture>" in page
    assert 'srcset="../../assets/cockpit-evidence-dark.png"' in page
    assert 'src="../../assets/cockpit-evidence-light.png"' in page

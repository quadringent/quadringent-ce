# Quadringent visual system

The cockpit defines the visual language shared with the static product site.
The site reuses its tokens and components with a wider editorial hierarchy.

## Sources

| Element | Reference |
|---|---|
| Light and dark tokens | `ui/src/styles/product-tokens.css` |
| Effective light overrides | `ui/src/styles/commercial.css`, imported last |
| Focus and motion | `ui/src/styles/base.css` |
| Self-hosted fonts | `ui/src/styles/brand.css` |
| Product shell | `ui/src/styles/product-shell.css` |
| Foundation components | `ui/src/styles/foundation.css`, `ui/src/components/foundation/` |
| Brand mark | `ui/src/components/ForgeMark.tsx` |
| Product site | `site/style.css`, `site/index.html` |

`commercial.css` overrides the light canvas, text, rules, signals, radius and
shadows. More specific dark blocks retain the product tokens.
`tests/test_site_design_system.py` checks token parity for light, system dark
and forced dark, plus the import order. Only editorial `--site-*`,
`--t-display`, `--t-section` and `--section-gap` tokens are site-specific.

## Principles and color

Use a quiet instrument: pale canvas, white ruled panels, square controls and no
shadows, gradients, backdrop blur or entrance animation. Motion communicates a
value change and respects `prefers-reduced-motion`. Every status has a word;
color alone cannot convey state.

Missing measurements remain `—` or `absent`. Show provenance beside values.
The top bar is 72 px high with a lower rule and a 1 px current-item underline.
The existing `--surface-rail*` tokens are unused by current screens.

| Role | Token | Effective light value |
|---|---|---|
| Canvas | `--surface-canvas` | `#f2f2ef` |
| Panel | `--surface-panel` | `#fff` |
| Quiet surface | `--surface-quiet` | `#e9e9e5` |
| Strong/main/muted text | `--text-strong`, `--text-main`, `--text-muted` | `#111210`, `#343631`, `#656861` |
| Subtle/strong rules | `--line-subtle`, `--line-strong` | `rgba(17,18,16,.09)`, `.16` |
| Activity and interaction | `--signal-live` | `#2563eb` |
| Observed measurement | `--signal-ok` | `#1a7f37` |
| Attention/background | `--signal-attention`, `--signal-attention-soft` | `#8a5a13`, `#f4ead7` |
| Incident/background | `--signal-incident`, `--signal-incident-soft` | `#a43e2d`, `#f6e3df` |
| Even/hover table bands | `--band-even`, `--band-hover` | `#eef6ee`, `#e3f0e4` |

Blue means activity or interaction. The primary action uses ink
(`--text-strong`), as does `.page-action--primary`. Green asserts an
observed measurement, not merely enabled capture. Amber marks a proof limit,
demonstration or requested verification. Red marks an incident, refusal or stop.

## Type, spacing and shape

IBM Plex Sans Variable 100–700 supplies text. IBM Plex Mono 400/500 supplies
values, sequences, identifiers, versions and commands with
`tabular-nums slashed-zero`. Body: 15 px/1.5; introduction: 17 px;
captions: 13 px; micro labels: 11 px/500, uppercase, 0.04–0.07 em tracking.

Product page titles use 52 px/520, −0.055 em tracking and 0.98 line height.
Cockpit v2 titles are about 30 px/500. The site uses 52 px for its main title
and 32 px/500/−0.03 em for sections. There is no serif face.

Fonts are served locally. `scripts/build_site.py` copies WOFF2 files from
the locked `@fontsource` packages after `npm ci --prefix ui`, verifies their
versions, and removes the development `ui/node_modules` fallback from the
output. Keep the IBM OFL-1.1 license texts in `site/assets/fonts/`.

The spacing scale is 4, 8, 12, 16, 20, 24, 32, 40 px
(`--space-1` through `--space-8`). Effective light
`--radius-panel` is 0; `--r-md` is 12 px for large panels and
`--r-sm` is 4 px for focus. `--shadow-panel` is `none`.
Interactive targets are at least 44 px high. Content width is
`min(100% − 64px, 1320px)`, falling back to 16 px side margins.

## Keyboard and components

Use a `2px solid var(--focus-ring)` focus ring offset by 3 px.
The skip-to-content link precedes navigation. Sticky-bar anchors use
`scroll-padding-top`. Cockpit tables support arrow-key navigation;
F3/F5/F9/F12 apply according to the current screen.

| Component | Contract |
|---|---|
| `ActionButton` | 44 px, 13 px/500, ruled panel, square corners; one primary action per area; sensitive operations require confirmation and service availability |
| `StatusWord` | 8 px square, 2 px radius and a word; `live`, `copying`, `paused`, `attention`, `stopped`; site adds measured `--proven` and documentary `--neutral` |
| `MetricTile` | Micro label, 28 px mono value and provenance; measured green, estimated amber, absent gray; site adds version, target and window |
| `BandedTable` | Uppercase micro headings, pale green bands, mono values; numeric headings and cells right-aligned; keyboard-focusable cockpit rows |
| Provenance banner | Amber word and attention background beside any demonstration or synthetic value |
| `StatusWord` hollow variant | `--absent` on the site: an outlined square with the word Absent or Not measured; the square is never filled, so a missing value never looks measured |
| `ForgeMark` | Two crossed 1.7 px strokes and a dot, monochrome `currentColor`, 26–30 px |

On small site screens, put the result column next to the identifier and expose
a focusable scrolling region with `tabindex="0"` and `role="region"`.

## Site composition and verification

The page reads as a thesis, a product, then evidence. The hero holds the title
("A running reader doesn't mean your data arrived."), one lead sentence, one
status line, one uncomfortable fact and one primary action, with a 1x crop of the
cockpit Evidence tab (the three readings, 1220 x 420) beside it, so its text
stays legible and the product is visible at 1440 x 900 and early on a phone.
The full 1440 x 900 capture, with its Synthetic demo banner, is one link away. The navigation has five entries. Below the hero come five sections:
how it works, try it, evidence, documentation and working with us. Section
headings vary and some have no lede; there is no eyebrow before every heading.

Site-only elements, built from the tokens above:

| Element | Contract |
|---|---|
| Status line | One paragraph under the lead: an attention `StatusWord`, the production statement, the source version and the latest observed release with its date, and a link to `docs/evidence.md`. It is the only place that states the preview status at length |
| Uncomfortable fact | One sentence with a 2 px attention rule on the left; figures come from the published receipts |
| Receiver chain | A horizontal ordered list of IBM i journal receivers (names are illustrative, labelled detached or attached, with a read bar in `--signal-live`), the reader position drawn as a tick that is also the checkpoint, then the raw storage and Snowflake panels. Below it, three readings from the synthetic demo and, directly under them, the legend of the state and provenance words in two columns. Stacked vertically below 900 px. It uses tokens only: no gradient, no icon, no animation |
| Screen block (`.screen`) | Commands only: green text (`--site-screen-fg`) on a near-black ground (`--site-screen-bg`), monospace, the same in both themes. Command output stays on `.code`. The green is limited to this block; there is no green theme, CRT typeface or scanline |
| Ledger | Evidence rows with a `StatusWord` and one sentence each, no bold labels |
| Index list | Documentation links as a title and a one-line description, in two columns; replaces card grids. Two tiers: entries marked `index__lead` come first, larger, with their description in body text; the rest follow under a "Reference" label in a compact row, and below 760 px their descriptions are hidden (the links stay) |
| Terminal excerpt | Real command output on `.code`, never restyled as a command. It starts at the first failing check and every cut is a dim marker that states how many lines were dropped, so shown plus omitted lines equal the count in the header |

Qualification tables, historical results and open limits live in
`docs/evidence.md`, published as a site page by `scripts/build_site.py`. The
landing page keeps a four-row ledger and links there. The `BandedTable`
component and its small-screen rule apply to those tables.

Screenshots come from the local v2 fixture
`ui/src/data/fixtures/cockpitDemo.ts`, in light and dark themes
(`site/assets/cockpit-evidence-*.png` for the full capture, and the cropped
`cockpit-evidence-strip-*.png`, cut from it at 1x, for the hero). The banner of the
screenshot shows its source version, which the caption states; retake it when
the version changes. Each figure states **Demonstration · synthetic data**. The
design-system test enforces that provenance. Review and anonymize any real
installation screenshot and its metadata before publication.

Update cockpit components and tests first, then shared site tokens. Run:

```sh
python -m pytest -q tests/test_site_design_system.py tests/test_build_site.py
```

Verify the rendered cockpit at 1440 px with keyboard and mouse, in both themes.
Verify the site at 1440, 1024 and 390 px, build it with
`scripts/build_site.py`, and check output links before publishing.

## Presentation references

The static page helps a reader understand IBM i → RAW and checkpoint →
Snowflake, then try the demo or discuss a pilot. Three hierarchy
references informed the reading order:
[Debezium](https://debezium.io/documentation/reference/stable/) introduces
capture before connectors; [Temporal](https://docs.temporal.io/) proceeds from
concept to getting started and deployment; [Airbyte](https://docs.airbyte.com/)
separates replication from other paths. These references claim no equivalent
capabilities or scope. The page includes no fictional prices, customer logos,
simulated live states or availability claims.

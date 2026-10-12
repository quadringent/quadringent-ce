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
Cockpit v2 titles are about 30 px/500. The site uses 56 px for its main title
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
| `ForgeMark` | Two crossed 1.7 px strokes and a dot, monochrome `currentColor`, 26–30 px |

On small site screens, put the result column next to the identifier and expose
a focusable scrolling region with `tabindex="0"` and `role="region"`.

## Site composition and verification

The site presents the product bar, release facts, labeled cockpit demonstration,
four-stage journey, cockpit, installation, qualification measurements and table,
open limits, then the offer. Measurements always retain version, targets and
observation window.

Screenshots come from the local v2 fixture
`ui/src/data/fixtures/cockpitDemo.ts`, in light and dark themes. Each figure
states **Demonstration · synthetic data**. The design-system test enforces
that provenance. Review and anonymize any real installation screenshot and
its metadata before publication.

Update cockpit components and tests first, then shared site tokens. Run:

```sh
python -m pytest -q tests/test_site_design_system.py tests/test_build_site.py
```

Verify the rendered cockpit at 1440 px with keyboard and mouse, in both themes.
Verify the site at 1440, 1024 and 390 px, build it with
`scripts/build_site.py`, and check output links before publishing.

## Presentation references

The static page helps a reader understand IBM i → RAW and checkpoint →
Snowflake, then install Community or discuss a DEV/INT pilot. Three hierarchy
references informed the reading order:
[Debezium](https://debezium.io/documentation/reference/stable/) introduces
capture before connectors; [Temporal](https://docs.temporal.io/) proceeds from
concept to getting started and deployment; [Airbyte](https://docs.airbyte.com/)
separates replication from other paths. These references claim no equivalent
capabilities or scope. The page includes no fictional prices, customer logos,
simulated live states or availability claims.

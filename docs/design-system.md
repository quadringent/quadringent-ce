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

## Anti-slop rules

These rules apply to the site and the cockpit. They exist so the product does
not look generated. A change that breaks one of them is rejected, in code and
in review. `tests/test_site_design_system.py` enforces the site side.

1. No coloured vertical rails. No `border-left`, `border-inline-start` or
   `inset Npx 0 0` accent bar on a card, callout, list row, alert or result, in
   any colour. State is a word, plus a small inline glyph only where a dense
   table needs scanning. Never a coloured edge.
2. No decorative status squares. A glyph stays only where it carries state in a
   scannable place (cockpit tables, evidence stage headers). The marketing site
   uses words only.
3. No eyebrows. No spaced uppercase label above a heading, inside a card or in a
   footer. Uppercase micro labels are allowed only as table column headers.
4. No card-in-card, no rounded panels, no shadows. A panel is flat, square, with
   at most a 1 px rule, and only for a real object: a screenshot, a form, a
   table, a terminal. Lists are plain lists separated by whitespace or one
   hairline.
5. No callout boxes. A sentence that matters is the first sentence of its
   section, in the text flow, with no tinted background or border.
6. No grid of identical tiles (same icon, title, line and button). Use an
   editorial list or a table.
7. No ornamental dividers, dot separators in labels ("A · B · C"), or badges
   and pills used as decoration.
8. No generic iconography. The mark and the favicon are the only icons.

Concision applies to front-facing copy. The hero is a headline, at most two
short sentences, one status line, one primary action and one text link. A
caption is one line. A section intro is one sentence or none; prefer showing a
screenshot, the receiver chain, terminal output or a table. A documentation
link is a title and at most eight words. Facts that leave the landing page move
to `docs/evidence.md` or the linked guide; they are never deleted.

## Principles and color

Use a quiet instrument: pale canvas, flat square controls, hairline rules and no
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
(`--text-strong`), as does `.page-action--primary`. In the cockpit, green
asserts an observed measurement, not merely enabled capture; amber marks a proof
limit, demonstration or requested verification; red marks an incident, refusal
or stop. On the site these colours are not used for state: the word is enough.
Colour never forms an edge, a rail or a tinted block.

## Type, spacing and shape

IBM Plex Sans Variable 100–700 supplies text. IBM Plex Mono 400/500 supplies
values, sequences, identifiers, versions and commands with
`tabular-nums slashed-zero`. Body: 15 px/1.5; introduction: 17 px;
captions: 12–13 px; table column headers: 11 px/500, uppercase, 0.04 em tracking.

Product page titles use 52 px/520, −0.055 em tracking and 0.98 line height.
Cockpit v2 titles are about 30 px/500. The site uses 52 px for its main title
and 32 px/500/−0.03 em for sections. There is no serif face.

Fonts are served locally. `scripts/build_site.py` copies WOFF2 files from
the locked `@fontsource` packages after `npm ci --prefix ui`, verifies their
versions, and removes the development `ui/node_modules` fallback from the
output. Keep the IBM OFL-1.1 license texts in `site/assets/fonts/`.

The spacing scale is 4, 8, 12, 16, 20, 24, 32, 40 px
(`--space-1` through `--space-8`). `--radius-panel`,
`--r-md` and `--r-sm` are 0, and `--shadow-panel` is `none`, in both themes.
Interactive targets are at least 44 px high. Content width is
`min(100% − 64px, 1320px)`, falling back to 16 px side margins.

## Keyboard and components

Use a `2px solid var(--focus-ring)` focus ring offset by 3 px.
The skip-to-content link precedes navigation. Sticky-bar anchors use
`scroll-padding-top`. Cockpit tables support arrow-key navigation;
F3/F5/F9/F12 apply according to the current screen.

| Component | Contract |
|---|---|
| `ActionButton` | 44 px, 13 px/500, 1 px rule, square corners; one primary action per area; sensitive operations require confirmation and service availability |
| `StatusWord` | The word carries the state. In dense cockpit tables a small inline glyph may precede it; on the site it is the word alone (`--absent` is muted ink, so a missing value never looks measured) |
| `BandedTable` | Uppercase column headings, pale bands, mono values; numeric headings and cells right-aligned; keyboard-focusable cockpit rows |
| Provenance | The words Demonstration and synthetic data, in text beside any demonstration value, with no tinted banner on the site |
| `ForgeMark` | Two crossed 1.7 px strokes and a dot, monochrome `currentColor`, 26–30 px |

On small site screens, put the result column next to the identifier and expose
a focusable scrolling region with `tabindex="0"` and `role="region"`.

## Site composition and verification

The page reads as a thesis, a product, then evidence. The hero holds the title
("A running reader doesn't mean your data arrived."), one lead of two sentences,
one status line, the uncomfortable fact as plain text, one primary action and
one text link, with a 1x crop of the cockpit Evidence tab (the three readings,
1220 x 420) beside it, so its text stays legible and the product is visible at
1440 x 900 and early on a phone. The caption is one line, and the full 1440 x 900
capture is one link away. The navigation has five entries. Below the hero come
five sections: how it works, try it, evidence, documentation and working with
us. Headings vary and some have no intro.

Site-only elements, built from the tokens above:

| Element | Contract |
|---|---|
| Status line | One paragraph under the lead: the words Preview, DEV/INT only, the production statement, the source version and the latest observed release with its date, and a link to `docs/evidence.md`. It is the only place that states the preview status at length |
| Uncomfortable fact | One plain paragraph, no rule, box or colour; figures come from the published receipts |
| Receiver chain | A horizontal ordered list of IBM i journal receivers (names are illustrative, labelled detached or attached, with a neutral read bar), the reader position drawn as a 1 px tick that is also the checkpoint, then raw storage and Snowflake, each in a 1 px outline with no fill. Below it, the three readings from the synthetic demo as words and values. Stacked vertically below 900 px. Tokens only: no gradient, no icon, no animation |
| Screen block (`.screen`) | Commands only: green text (`--site-screen-fg`) on a near-black ground (`--site-screen-bg`), monospace, the same in both themes. Command output stays on `.code`. The green is limited to this block; there is no green theme, CRT typeface or scanline |
| Ledger | Evidence rows with the evidence word and one sentence each, no bold labels |
| Index list | Documentation links as a title and a description of at most eight words, in two columns, one hairline per row, no tiers and no sub-labels. Below 760 px each row stacks |
| Terminal excerpt | Real command output on `.code`, never restyled as a command. Every cut is a dim marker that states what was dropped, so shown plus omitted lines equal the count in the header |

Qualification tables, historical results and open limits live in
`docs/evidence.md`, published as a site page by `scripts/build_site.py`. The
landing page keeps a four-row ledger and links there. The `BandedTable`
component and its small-screen rule apply to those tables.

Screenshots come from the local v2 fixture
`ui/src/data/fixtures/cockpitDemo.ts`, in light and dark themes
(`site/assets/cockpit-evidence-*.png` for the full capture, and the cropped
`cockpit-evidence-strip-*.png`, cut from it at 1x, for the hero). The banner of the
screenshot shows its source version, which the caption states; retake it when
the version changes. Each figure states Demonstration and synthetic data in its
caption. The design-system test enforces that provenance. Review and anonymize any real
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

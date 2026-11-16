# Styles: fully flexible, still safe

Status: design, agreed direction, being built. When it is built this file becomes `docs/STYLES.md`.

Dashboards are already fully flexible in what they show. This makes them fully flexible in how
they look: a person or their AI agent can restyle anything with real CSS, with a small theme file
as the quick way to change colours and fonts.

## Two layers

1. **Theme file** (`ui/theme.yaml`): named settings, no CSS. Colours (accent, background, surface,
   text, muted, border, good, bad, a chart palette), separate values for dark mode, font families
   from the fonts the project ships or the built-in stacks, a type scale, corner radius, density,
   card style. Every value becomes a CSS custom property (`--eui-…`) on the page. Per-dashboard
   overrides: `theme:` at the top of a dashboard file. Ready-made themes ship with easy-ui and can
   be copied as a starting point (`easy-ui themes`, `easy-ui theme use NAME`).
2. **Stylesheets** (`ui/styles/*.css`): ordinary CSS, any amount, applied after the built-in
   styles in file-name order. Layout, typography, animation, print rules, phone and desktop
   differences: anything CSS can do.

## Stable names to style against

Documented and kept stable across versions (changing one is a breaking change):

- page: `.eui-app`, `[data-project="…"]`, `[data-theme="light|dark"]`
- dashboard: `.eui-dashboard`, `[data-dashboard="<slug>"]`
- layout: `.eui-row`, `[data-row="N"]`
- component: `.eui-component`, `[data-type="stat|line|area|bar|table|text|controls"]`,
  `[data-id="<component id>"]`, plus any classes from the component's `class:` setting
- parts: `.eui-title`, `.eui-value`, `.eui-unit`, `.eui-delta`, `.eui-hint`, `.eui-chart`,
  `.eui-readout`, `.eui-table`, `.eui-th`, `.eui-td`, `.eui-control`, `.eui-empty`, `.eui-error`
- every component may set `id:` (stable name) and `class:` (space-separated custom hooks) in the
  dashboard file

Charts are drawn on a canvas, which CSS cannot reach, so the chart reads its colours, fonts, line
widths and grid style from the CSS custom properties of its own element. Setting
`--eui-chart-1: …` on one component restyles just that chart.

`easy-ui style-guide --json` prints every name and custom property with a description, for agents.

## Fonts and images

Files under `ui/assets/` (woff2 fonts; png, jpg, webp, svg images) are published with the
dashboards and served from the same origin, so `@font-face` and `background-image` work with
relative URLs. SVG is served as an image only, never inline.

## Safety rules (what keeps "fully flexible" from becoming dangerous)

- CSS cannot run code: the script policy does not change (`script-src 'self'`, no inline script).
- CSS cannot send data out: the content security policy already allows images, fonts and styles
  only from the page's own origin, and `easy-ui validate` also rejects any `url()` or `@import`
  that is not a relative path inside `ui/assets/` or `ui/styles/`, plus `expression()`,
  `behavior:`, `-moz-binding` and `@namespace`.
- Custom styles never apply to the sign-in screen, the security settings screen, the preview
  badge or error pages about the dashboard itself: those render in a separate document tree that
  loads only the built-in stylesheet.
- Stylesheets, the theme and assets are published, versioned and rolled back together with the
  dashboards. Limits on file count and size apply. Assets are checked by type and size.
- What remains possible on purpose: custom CSS can hide or cover what a page shows. That is the
  same bound dashboards already have (a mistaken or tricked author can change what a page shows,
  never what the server does or who can sign in), and every change is a version that can be
  inspected and rolled back.

## Checks that help an agent

- `easy-ui validate` parses the CSS, reports unknown `eui-` names and custom properties with the
  closest match, and warns when theme text and background colours have low contrast.
- `easy-ui preview` reloads on any change to theme, styles or assets.
- `easy-ui screenshot` shows the result at desktop and phone width, light and dark.
- Exports (`easy-ui export`) carry the theme, styles and assets, so a shared copy looks the same.

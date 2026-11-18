# Styles: fully flexible, still safe

Dashboards are fully flexible in what they show; this page is about how they look. A person or
their agent can restyle anything with real CSS, and change colours and fonts with a small theme
file. easy-ui checks every one of these files before it is used, publishes it together with the
dashboards, and keeps it away from easy-ui's own screens.

Three kinds of files in the project's `ui/` folder make up the *look*:

| File | What it is |
| --- | --- |
| `ui/theme.yaml` | Named settings, no CSS: colours (light and dark), fonts, text size, corners, spacing, panel style, chart lines. |
| `ui/styles/*.css` | Ordinary CSS, applied after easy-ui's own styles in file-name order (`10-layout.css` before `20-type.css`). |
| `ui/assets/` | `.woff2` fonts and `.png`, `.jpg`, `.webp` and `.svg` pictures, for the theme and the stylesheets. |

Every one of them is optional. Without any, dashboards look as they always did.

<p><img src="screenshots/theme-trail-light.png" alt="The trail theme, light" width="1280"></p>

<p>
<img src="screenshots/theme-instrument-dark.png" alt="The instrument theme in dark mode: teal on near-black, monospaced figures, outlined panels" width="560">
<img src="screenshots/theme-dusk-phone.png" alt="The dusk theme on a phone: lavender, rounded type, soft raised panels" width="200">
</p>

## Restyle a dashboard in five minutes

A walkthrough for an agent (or a person) working in a project folder. Every step can be checked
from the terminal, and nothing is visible to anyone until `easy-ui publish`.

1. **See what can be styled.** `easy-ui style-guide --json` lists every class, data attribute,
   custom property and theme setting, with a description of each. `easy-ui themes` lists the
   ready-made themes.

2. **Start from a theme.** Copy one into the project, then change what you like in it:

   ```sh
   easy-ui theme use trail          # writes ui/theme.yaml; never overwrites one without --force
   easy-ui preview                  # prints a link; the page reloads by itself on every save
   ```

   ```yaml
   # ui/theme.yaml (excerpt)
   colors:
     accent: "#2c6a4f"              # colours in quotes: YAML reads an unquoted # as a comment
     chart: ["#2c6a4f", "#b07d22", "#4f78a0"]
   dark:
     accent: "#78c29c"
   fonts:
     headings: slab                 # a built-in stack, or a family from font_files
     numbers: mono
   radius: 6
   density: compact
   cards: outline
   chart:
     grid: dotted
   ```

3. **Change the layout and type with CSS.** Write `ui/styles/10-look.css`:

   ```css
   @font-face { font-family: "Field Notes"; src: url("../assets/fonts/field-notes.woff2") format("woff2"); }
   .eui-app { --eui-font-heading: "Field Notes", serif; }
   .eui-dashboard-title { font-size: 4.5rem; font-style: italic; }
   .eui-row[data-row="3"] { display: grid; grid-template-columns: 1fr; }   /* the third row: one chart per line */
   @media (max-width: 560px) { .eui-row { gap: 12px; } }
   ```

4. **Restyle one chart.** Give the component an id in its dashboard file, then set the chart's
   custom properties on it. Only that chart changes:

   ```yaml
   - type: line
     id: heart
     class: vital
     title: Resting heart rate
     query: heart
     x: day
     y: [Resting heart rate]
   ```

   ```css
   [data-id="heart"] {
     --eui-chart-1: #b0383a;
     --eui-chart-line-width: 4;
     --eui-chart-grid-style: dashed;
     --eui-chart-height: 340px;
   }
   .eui-component.vital .eui-title { color: #b0383a; }
   ```

5. **One dashboard, its own colours.** A dashboard file can override the theme for itself only:

   ```yaml
   theme:
     colors:
       accent: "#a2451d"            # light mode; dark mode under dark:
     cards: outline
   ```

6. **Check, look, publish.**

   ```sh
   easy-ui validate                              # dashboards and the look; unknown eui- names come with "did you mean"
   easy-ui screenshot overview --out a.png       # also --phone and --dark
   easy-ui publish                               # the dashboards and the look become one new version
   easy-ui rollback                              # brings back the previous version, look included
   ```

## The theme file

`ui/theme.yaml` holds named settings only. Each one becomes a CSS custom property (`--eui-...`)
in a stylesheet easy-ui generates (`ui/theme.css` as the browser sees it), for light and dark
mode. Unknown settings are errors, with the closest correct name.

| Setting | What it is |
| --- | --- |
| `name`, `description` | A short name and one sentence. |
| `colors` | Light mode: `accent`, `background`, `surface`, `text`, `muted`, `border`, `grid`, `good`, `bad`, and `chart` (1 to 8 series colours). `#rgb` or `#rrggbb`, in quotes. |
| `dark` | The same colours for dark mode. Colours left out use easy-ui's dark values (the accent uses the light one). |
| `fonts` | `body`, `headings`, `numbers`: a built-in stack (`system-sans`, `book`, `transitional`, `didone`, `slab`, `humanist`, `geometric`, `grotesque`, `rounded`, `mono`) or a family from `font_files`. |
| `font_files` | Families the project ships: `Family: [{file: fonts/x.woff2, weight: 400, style: normal}]`, with files in `ui/assets/`. |
| `type` | `size` (base text size, 12 to 22 px) and `scale` (how much larger each heading step is, 1.1 to 1.6). |
| `radius` | Panel corners, 0 to 32 px. |
| `density` | `compact`, `normal` or `roomy`. |
| `cards` | `flat`, `outline`, `raised` or `plain`. |
| `chart` | `line_width` (0.5 to 6), `grid` (`solid`, `dashed`, `dotted`, `none`), `area_opacity` (0 to 1), `bar_radius` (0 to 12). |

`easy-ui validate` warns when colours are hard to read together: text against panels below 4.5:1,
secondary text, the accent, good and bad below 3:1, chart colours below 2:1. A warning does not
stop publishing.

A dashboard's own `theme:` takes `colors`, `dark`, `fonts`, `type`, `radius`, `density`, `cards`
and `chart`, and applies to that dashboard only.

Ready-made themes ship with easy-ui; each has a light and a dark palette and works at phone width:

| Theme | Character |
| --- | --- |
| `almanac` | easy-ui's own look as a theme: calm blue, book-serif headings. |
| `trail` | A field map: sage paper, pine and ochre, slab-serif figures, dotted gridlines. |
| `instrument` | A lab instrument: cold grey and teal, compact, monospaced figures, outlined panels, square corners. |
| `dusk` | Lavender and plum, rounded type, roomy spacing, soft raised panels. |
| `harbour` | Chalk blue and navy, high-contrast display-serif headings, no panels at all. |

## Stable names to style against

Documented and kept stable across versions: renaming one is a breaking change.
`easy-ui style-guide` prints all of them with a description.

- **Dashboard area:** `.eui-app` (with `data-project` and `data-theme="light|dark"`),
  `.eui-dashboard` (with `data-dashboard="<slug>"`), `.eui-dashboard-title`,
  `.eui-dashboard-description`, `.eui-status`.
- **Layout:** `.eui-row` with `data-row="N"` (1 for the first row). Every component is a size
  container (`container-type: inline-size`), so a project's `@container (max-width: ...)` rules
  follow the component's own width.
- **Components:** `.eui-component` with `data-type="stat|line|area|bar|timeline|table|text|controls"` and
  `data-id="<id>"` (the `id:` setting, or `r<row>c<column>` counted from 0), plus the classes of
  its `class:` setting.
- **Parts:** `.eui-title`, `.eui-value`, `.eui-unit`, `.eui-delta`, `.eui-hint`, `.eui-note`, `.eui-chart`,
  `.eui-readout`, `.eui-legend`, `.eui-chart-card`, `.eui-table`, `.eui-th`, `.eui-td`, `.eui-text`, `.eui-control`,
  `.eui-empty`, `.eui-error`. A figure takes its size and font from `.eui-value`
  (`.eui-value { font-size: 4rem; font-family: ... }` changes it; the unit is 0.4 of it);
  `--eui-size-figure` and `--eui-font-numbers` do the same for every stat at once.

Every component takes two more settings in its dashboard file:

- `id:` a stable name, lowercase letters, digits and `-`, starting with a letter, unique in the
  dashboard (not of the form `r1c2`, which easy-ui uses for components without one).
- `class:` one class or a list of them (at most 8), for stylesheets to hook on. They may not
  start with `eui-`, which is easy-ui's.

Other class names in the page (`c-head`, `stat-figure`, ...) are not part of this list and can
change between versions.

### Custom properties

Charts are drawn as SVG by the chart library, which takes its colours, fonts, line widths and
grid style as values rather than from CSS rules, so each chart reads them from the custom
properties of its own element (and reads them again when the theme or a stylesheet changes). Set
them on a component (`[data-id="..."]`), a row, a dashboard or `.eui-app`. The groups:

- colours: `--eui-accent`, `--eui-accent-text`, `--eui-on-accent`, `--eui-accent-soft`,
  `--eui-background`, `--eui-surface`, `--eui-text`, `--eui-muted`, `--eui-border`, `--eui-grid`,
  `--eui-good`, `--eui-bad`, `--eui-chart-1` ... `--eui-chart-8`
- type: `--eui-font-body`, `--eui-font-heading`, `--eui-font-numbers`, `--eui-size-xs`,
  `--eui-size-s`, `--eui-size-m`, `--eui-size-l`, `--eui-size-xl`, `--eui-size-display`,
  `--eui-size-figure`
- shape and space: `--eui-radius`, `--eui-radius-small`, `--eui-gap`, `--eui-row-gap`,
  `--eui-pad`, `--eui-card-background`, `--eui-card-shadow`, `--eui-card-padding`
- charts: `--eui-chart-height`, `--eui-chart-line-width`, `--eui-chart-grid-style`,
  `--eui-chart-area-opacity`, `--eui-chart-bar-radius`, `--eui-chart-font`,
  `--eui-chart-font-size`, `--eui-chart-label`, `--eui-chart-axis`, `--eui-chart-grid`,
  `--eui-chart-band-opacity`, `--eui-chart-reference`, `--eui-chart-tooltip-shadow`

What the chart tokens do:

| Token | Default | What it sets |
| --- | --- | --- |
| `--eui-chart-1` ... `-8` | from the accent (a theme's `chart:` list) | Series colours, in order; also the colours of `colors_by` categories and timeline lanes. In dark mode the first is lightened to 4.5:1 against the panel. |
| `--eui-chart-height` | `clamp(200px, 20cqi + 112px, min(420px, 66svh))` | Height of a chart. `cqi` is a hundredth of the component's own width, so a wider chart is taller, up to 420 px and never more than about two thirds of a short screen. A fixed value (`340px`) works too. A timeline is as tall as its lanes. |
| `--eui-chart-line-width` | `2` | Width of lines, in px; a dashed series uses the same width. |
| `--eui-chart-grid-style` | `solid` | `solid`, `dashed`, `dotted` or `none`. |
| `--eui-chart-area-opacity` | `0.16` | Top of an area's fade (it fades to almost nothing at the bottom). |
| `--eui-chart-band-opacity` | `0.14` (dark `0.22`) | Fill of a `band:`, in the first series' colour. |
| `--eui-chart-bar-radius` | `3` | Rounded top corners of bars (only the top of a stack), in px; timeline spans use up to 4. |
| `--eui-chart-font`, `--eui-chart-font-size` | the body font, `11` | Axis labels. Figures in charts are always of equal width. |
| `--eui-chart-label` | `var(--eui-muted)` | Axis labels and the unit above the axis. |
| `--eui-chart-axis` | `var(--eui-border)` | The baseline under bars. |
| `--eui-chart-grid` | `var(--eui-grid)` | Gridlines. |
| `--eui-chart-reference` | `var(--eui-muted)` | A `reference:` line and its label. |
| `--eui-chart-tooltip-shadow` | a hairline and a soft shadow | Edge and shadow of the hover card (`.eui-chart-card`), which also uses `--eui-surface` and `--eui-radius-small`. |

A theme's `chart:` settings (`line_width`, `grid`, `area_opacity`, `bar_radius`) set the matching
tokens, so the five ready-made themes and a project's own theme restyle every chart.

## Fonts and pictures

Files under `ui/assets/` (at most two folder levels deep) are published with the dashboards and
served from the same address as the page, after sign-in, so `@font-face` and `background-image`
work with relative addresses: from a stylesheet `url("../assets/logo.svg")`, from the theme
`file: fonts/notes.woff2`. Each file is checked by its name *and* its first bytes (a `.png` must
start like a PNG). An SVG may not contain scripts, event attributes, `foreignObject`, entity
declarations or links to other files, and is only ever served as a picture: opened on its own it
is a download in a sandbox that can run nothing (`Content-Security-Policy: default-src 'none';
sandbox`, `Content-Disposition: attachment`).

`@font-face` rules in the project's stylesheets must name a `.woff2` file in `ui/assets/`. They
are moved into the generated theme stylesheet (browsers do not load fonts declared inside a
dashboard's own document tree), so a project font is declared for the whole page; its name may
not be one easy-ui's own screens use (`system-ui`, `Georgia`, `Arial`, ...), so it cannot change
how they look.

## Where the look applies, and where never

Each dashboard is drawn in its own document tree (a shadow root) inside a frame that keeps it in
its box. That tree loads easy-ui's stylesheet and then the project's stylesheets; nothing else on
the page can be reached by them. So the project's CSS never applies to:

- the top bar (project name, menu, sign-out), the sign-in screen, the sign-in settings,
- the preview badge ("Preview - not published"), which keeps easy-ui's fixed colours and font,
- easy-ui's pages that list problems, and its "not found" and "could not load" messages.

The generated theme stylesheet (only values easy-ui checked itself) also colours the top bar and
the list of dashboards. It is taken away on the sign-in settings and easy-ui's own pages, and the
sign-in screen never has it: the look is only asked for after signing in. While the theme, a
stylesheet or an asset has a mistake, the preview shows a page listing it and draws the
dashboards with easy-ui's own look.

This was checked in a browser with a hostile stylesheet that tries to hide the page, cover it
with a fixed full-screen box, push the dashboard over the top bar and change every font: the
badge and the sign-out button stay visible, on top and clickable, and the sign-in settings look
exactly as without it.

## An app's own look

Each of a project's apps may bring a look of its own, in its folder: `styles/*.css`,
`assets/...` and ready-made `themes/*.yaml`. The same checks and limits apply as to the
project's files, counted per app, and every problem names the app's file (for example
`apps/fitness_sleep/styles/sleep.css`). An app's stylesheet may also not use `@scope`, `@page` or
`@view-transition`.

An app's stylesheet applies **only to what that app shows**: its own dashboards and the sections
it adds to other dashboards. easy-ui marks those with `data-app="<app>"` (on the dashboard, on a
row made only of the app's components, and on each component) and rewrites every selector of the
app's stylesheet, also inside `@media`, `@supports`, `@layer` and `@container`:

```css
/* apps/fitness_sleep/styles/sleep.css */
.eui-title { letter-spacing: 0.02em; }

/* what the page receives */
:where([data-app="fitness-sleep"]) .eui-title, .eui-title:where([data-app="fitness-sleep"]) { letter-spacing: 0.02em; }
```

The mark goes on the app's dashboard as a whole, so an app's stylesheet also applies to the
sections *other* apps add to that dashboard (they sit inside it); those sections get their own
app's stylesheet as well. On a section an app adds to one of the project's own dashboards, only
that app's stylesheet (and the project's) applies.

**Who wins** when an app's rule and a project rule set the same thing on the app's content is
decided by the ordinary CSS cascade, in this order:

1. `!important` beats a normal declaration, so an app's `!important` beats the project's normal
   rules.
2. Cascade layers: a normal rule outside any `@layer` beats a normal rule inside one (for
   `!important` it is the other way round). Layers are ordered by where their name first
   appears, and the apps' stylesheets come first, so a layer an app names comes before one the
   project adds later; a layer name used by both is one layer.
3. Weight (specificity): the `:where()` easy-ui adds weighs nothing, but the app's own selector
   keeps its weight, so an app's `.eui-row .eui-title` beats a project's `.eui-title`.
4. Order: with everything above equal, the project's rule wins, because the apps' stylesheets
   load before the project's.

So the project has the last word whenever its rule is at least as heavy as the app's, of the
same importance and not in a layer when the app's is not. To be sure of it, write the project's
rule unlayered with a selector at least as specific as the app's (`[data-app="fitness-sleep"]
.eui-title` is usually enough), and with `!important` where the app used it. A project that
replaces an app's dashboard with its own file (`ui/dashboards/<name>.yaml`) drops that app's look
from it altogether.
An app's `@font-face` rules are kept in `theme.css` before the project's; its fonts and pictures
are served under `/ui/apps/<app>/assets/`, its stylesheets under `/ui/apps/<app>/styles/`, after
sign-in only, like the project's. An app's themes are listed by `easy-ui themes` as
`<app>/<name>` and copied into the project with `easy-ui theme use <app>/<name>`; they use the
built-in fonts.

## Safety rules

- **CSS cannot run code.** The page's policy is unchanged: `script-src 'self'`, no inline
  script, `style-src 'self'` with no inline styles. The theme's values reach the page as a
  same-origin stylesheet, and a dashboard's own `theme:` as checked values set with
  `element.style`.
- **CSS cannot send data out.** The page's policy allows styles, pictures and fonts from its own
  address only, and easy-ui refuses before publishing any `url()`, `image-set()` or `@import` that
  is not a relative path to a file in `ui/assets/` or `ui/styles/` (for an app: its own
  `assets/` and `styles/`; also when written with CSS
  escapes such as `u\72l(`). Inside `url()`, `image-set()`, `image()` and `cross-fade()`,
  `var()`, `env()`, `attr()`, `if()` and `inherit()` are refused (they would put an address
  together where it cannot be checked), so is a plain quoted text as an `@font-face` `src` (write
  `url(...)`), and so
  is a custom property holding a text that is a web address (`"https://..."`, `"//..."`,
  `"data:..."`; a text that merely mentions one, like `"Data from https://..."`, is fine). Also
  refused: `expression()`, `element()`, `src()`, `behavior:`,
  `-moz-binding`, `@namespace` and `@document`. Unknown at-rules, unclosed blocks, strings or
  comments, and control characters are errors too.
- **Files are read the safe way.** Like dashboards, the look is read without following links,
  with limits: at most 20 stylesheets (200 KB each, 1 MB together), 60 assets (2 MB each, 8 MB
  together) and a 64 KB theme; sizes are checked before a file is read, and a folder past a limit
  is not read any further. A published version records the size and SHA-256 of every file;
  each is checked again (hash and content) before it is served, and a version whose files no
  longer match is not used.
- **What remains possible, on purpose:** a project's CSS can hide, move or cover what its own
  dashboards show. That is the same bound dashboards already have: a mistaken or tricked author
  can change what a page shows, never what the server does or who can sign in. Every change is a
  version that can be inspected and rolled back.

## Publishing, versions, previews, copies and pictures

- `easy-ui publish` puts the dashboards and the look into one new version (files read-only, the
  manifest lists each with its size and hash). Publishing an unchanged look with unchanged
  dashboards makes no new version. A look with mistakes is never published.
- `easy-ui rollback [VERSION]` shows an earlier version's dashboards *and* look.
  `easy-ui rollback --restore-files` also copies them back into `ui/`; style files it would
  replace are first moved to `ui/.before-restore-<time>/`, and it refuses to write through a link.
- An archive published with `publish.publish_from_archive` may carry `theme.yaml`,
  `styles/*.css` and `assets/...` (optionally under `ui/`) beside the dashboard files, and an
  app's look as `apps/<app>/styles/*.css` and `apps/<app>/assets/...`.
- A version keeps each app's look with the project's; `rollback --restore-files` puts back only
  the project's own files.
- `easy-ui preview` reloads when anything in `ui/theme.yaml`, `ui/styles/` or `ui/assets/`,
  or in an app's `styles/` or `assets/`, changes; the page swaps in the new stylesheets without a blank moment.
- `easy-ui export` writes the look beside the page (`ui/theme.css`, `ui/styles/`, `ui/assets/`),
  so a shared copy looks the same. With `--single-file` the look goes inside the one file: the
  theme stylesheet and every stylesheet (each allowed by its hash in the file's own policy), with
  the fonts and pictures they name as `data:` addresses (pictures no stylesheet names are left
  out, and not listed). A stylesheet that uses `@import` cannot be carried that way, so a
  single file is refused (`export_look_not_embeddable`) rather than leaving part of the look out;
  export a folder instead. With `--encrypt` the numbers are locked but the look is not, in a
  folder or a single file (the page's policy cannot load a stylesheet from locked data); the
  command lists every file of the look the copy holds and says so.
- `easy-ui screenshot` waits for the stylesheets and fonts before it takes the picture.

## Commands

| Command | What it does |
| --- | --- |
| `easy-ui style-guide [--json]` | Every stable name, custom property and theme setting, with descriptions and an example. |
| `easy-ui themes [--json]` | The ready-made themes, and those the project's apps ship as `<app>/<name>` (with `--json`, also the text of each). |
| `easy-ui theme use NAME [--force]` | Copy a ready-made theme (or an app's, `<app>/<name>`) to `ui/theme.yaml`. Refuses (exit 5, `theme_exists`) to replace one without `--force`. |
| `easy-ui validate` | Checks the dashboards and the look; `--json` adds a `styles` part with `ok`, `errors`, `warnings` and `files`. |

## For developers

The Python side, by job:

- gather: `styles.read_style_dir(ui_dir)` (the project's folder), `publish.read_archive_parts(tar)`
  (an archive), `publish._version_style_files(vdir, manifest)` (a published version)
- check: `styles.check(files)` (everything together) with `css.check_stylesheet`,
  `theme.check_theme` and `styles.asset_type`; `theme.check_overrides` for a dashboard's `theme:`;
  `styles.validate_project(config)` for `easy-ui validate`
- publish and roll back: `publish._publish_files(..., style_files=)` with `publish._write_styles`;
  `publish.load_published_styles(where)` (hash-checked); `styles.restore` through
  `publish.restore_files`
- serve: `PublishedSource.styles()` and `preview.WorkingSource.styles()` return a
  `styles.StyleSet`; `server.api_styles` (`GET /api/styles`) and `server.page_ui_file`
  (`GET /ui/theme.css`, `/ui/styles/<name>.css`, `/ui/assets/<path>`)
- export: `export._look` and `export._write_tree(..., look)`, using `StyleSet.files()`

In the browser: `look.js` (the theme stylesheet and when the page may use it), `dashboard.js`
(the dashboard's shadow root and its stylesheets), `components.js` (the stable names),
`charts.js` (`chartStyle(el)` reads a chart's custom properties) and `theme.js`
(`applyOverrides` for a dashboard's `theme:`).

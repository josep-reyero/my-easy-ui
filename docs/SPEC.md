# Dashboard format: `easy-ui/v1`

A dashboard is one YAML file in the project's `ui/dashboards/` folder, or in the `dashboards/`
folder of one of the project's apps (see [Dashboards from apps](#dashboards-from-apps)). The file
name is the page address: `ui/dashboards/training.yaml` is the dashboard `training`. Names use
lowercase letters, digits and `-` (up to 41 characters, starting with a letter or digit).

A dashboard is a **view**, not a program. It says which data to read (named SQL queries) and how to
show it (a layout of fixed components). It never contains JavaScript, HTML or CSS, so anyone, or
their AI agent, can rewrite it without risk. Throw it away and write a new one whenever the
questions change.

The quickest way to see the whole vocabulary from the command line is `easy-ui components --json`.
To see what data there is, run `easy-ui tables`.

## The project's settings (`project.toml`)

easy-ui reads `[project]` (`name`, `title`, `version`, `apps`), `[ui]`, and the *names* of
`[tables]` and `[views]` from the project's `project.toml` (**my-easy-software**'s apps contract,
section 1). Everything else in those tables, and `[settings]` and `[storage]`, belongs to
**my-easy-software**. Unknown top-level tables and unknown keys in `[project]` or `[ui]` are errors.

```toml
[project]
name = "fitness"
title = "My fitness"
apps = ["fitness_garmin", "fitness_sleep"]    # the apps, by package name, in order

[tables.days]                                 # a shared table that several apps may write
key = ["date"]
columns = { date = "text", steps = "integer", hours = "real" }

[views.weekly]                                # a view over the shared tables
sql = "SELECT strftime('%Y-%W', date) AS week, sum(steps) AS steps FROM days GROUP BY 1"

[ui]
accent = "#2f6fdf"                            # optional: the project's colour
dashboards = ["overview", "sleep"]            # optional: menu order; the first is the landing page
refresh_after = ["fitness-garmin.sync"]       # optional, for mes: rebuild the data after these jobs
# tables = ["days", "weekly"]                 # optional: fewer tables than the default
```

**The data.** Dashboards read a copy of the project's shared database,
`<data folder>/<project>.sqlite`, made by `easy-ui data build`. Only these tables reach it
(contract 4.6): each shared table (`[tables]`, in file order; its rows are kept per app in
`<name>__rows`, and dashboards see only the merged view `<name>`), then each `[views]` entry, then
each app's `[ui] tables` (in the order of `apps`). `[ui] tables` in project.toml replaces that
list with some of its names, in its own order.

**The menu.** The names in `dashboards` must exist (a project or app dashboard); the others follow
in the order they were found: each app's dashboards, in the order of `apps`, then the project's
own, by file name.

Any other setting is refused with the closest correct name, or, for one that belongs elsewhere
(`[jobs.<name>]` in an app's `app.toml`, the app list under `[project] apps`), with where it
goes. A project.toml without `[project] apps` is refused too.

## A complete example

```yaml
spec: easy-ui/v1
title: Training
description: Distance and steps over time.

params:
  since:
    type: date
    label: From
    default: today-90d
  kind:
    type: choice
    label: Kind
    default: all
    options:
      - all
      - {value: run, label: Running}
      - {value: ride, label: Cycling}

queries:
  totals:
    sql: |
      SELECT sum(distance_m) AS distance, count(*) AS days
      FROM days WHERE day >= :since
  daily:
    sql: |
      SELECT day, distance_m AS "Distance", steps AS "Steps"
      FROM days WHERE day >= :since ORDER BY day
  recent:
    sql: |
      SELECT day, kind, distance_m, duration_s,
             duration_s * 1000.0 / nullif(distance_m, 0) AS pace
      FROM activities
      WHERE day >= :since AND (:kind = 'all' OR kind = :kind)
      ORDER BY day DESC

layout:
  - - type: controls
  - - type: stat
      title: Distance
      query: totals
      value: distance
      format: distance
      hint: Everything you walked, ran and rode.
    - type: stat
      title: Active days
      query: totals
      value: days
      format: integer
  - - type: line
      title: Distance per day
      query: daily
      x: day
      y: Distance
      format: distance
    - type: bar
      title: Steps per day
      query: daily
      x: day
      y: [Steps]
      format: integer
  - - type: table
      title: Recent activities
      query: recent
      limit: 20
      columns:
        - {column: day, label: Day, format: date}
        - {column: kind, label: Kind}
        - {column: distance_m, label: Distance, format: distance}
        - {column: duration_s, label: Time, format: duration}
        - {column: pace, label: Pace, format: pace}
  - - type: text
      body: |
        ## How to read this page

        Distance comes from your watch. **Pace** is minutes per kilometre.
        More about it on [the project page](https://example.org/easy-ui).
```

## Top level

| Key | Required | What it is |
|---|---|---|
| `spec` | yes | Exactly `easy-ui/v1`. |
| `title` | yes | The page title (up to 120 characters). |
| `description` | no | One or two sentences (up to 500 characters). |
| `params` | no | The values a viewer can change, by name. |
| `queries` | no | Named read-only SQL queries. Needed by every component except `text` and `controls`. |
| `layout` | yes | A list of rows; each row is a list of 1 to 6 components. |
| `theme` | no | This dashboard's own look: `colors`, `dark`, `fonts`, `type`, `radius`, `density`, `cards`, `chart`, as in `ui/theme.yaml` (see [STYLES.md](STYLES.md)). |
| `app` | no | The app the dashboard belongs to. easy-ui sets it on an app's dashboards when it composes them; written by hand in an app's dashboard it must name that same app, and the project's own dashboards may not have it. |

Any other key is an error. YAML anchors and aliases (`&` / `*`), merge keys (`<<`) and repeated keys
are errors too, so what you read is exactly what runs.

## Params

A param is a value the viewer can change with a control, used in queries as `:name`. Names use
lowercase letters, digits and `_`, start with a letter and are at most 31 characters. Every param
needs a `type` and a `default`. `label` is optional (default: the name, capitalised).

| Type | Keys | Value sent to SQL |
|---|---|---|
| `choice` | `type`, `label`, `default`, `options` | One of the option values (text or number). |
| `int` | `type`, `label`, `default`, `min`, `max` | A whole number between `min` and `max`. |
| `date` | `type`, `label`, `default` | Text `YYYY-MM-DD`. |
| `text` | `type`, `label`, `default` | Text up to 200 characters (`""` allowed). |

- **choice** `options` is a list of up to 100 entries, each a value (`30`, `run`) or
  `{value, label}`. The default must be one of the values. YAML reads `yes`, `no`, `on`, `off` as
  true/false, so put such options in quotes: `'no'`.
- **date** `default` is a date (`2026-01-31`), `today`, or `today-<N><unit>` / `today+<N><unit>`
  with unit `d` (days), `w` (weeks), `m` (months, clamped to the end of the month) or `y` (years),
  for example `today-90d`. Relative defaults are worked out each time the page is opened.
- **int** without `min`/`max` accepts any whole number up to ±2^53.

Values from the browser are checked against the type before any query runs; anything else is
refused with a plain message. Params are always **bound**, never pasted into the SQL, so a value
can never change what a query does.

## Queries

```yaml
queries:
  name:
    sql: SELECT ... WHERE day >= :since
```

Rules (checked by `easy-ui validate` and again by the server for every request):

- exactly one statement, starting with `SELECT` or `WITH` (or `VALUES`); a trailing `;` is fine;
- parameters only as `:name`, and every `:name` must be declared under `params`;
- only the tables listed in `project.toml` `[ui] tables` exist (views are copied as tables);
- no `ATTACH`, `PRAGMA` (also not `pragma_*` functions), no writes or schema changes, no
  `sqlite_master`, `dbstat` or extension functions such as `load_extension` or `readfile`;
- text values go in single quotes (`'run'`); double quotes are for names (`"Distance"`);
- at most 10 named parts (`WITH name AS (...)`, `WINDOW name AS (...)`), and a part may not
  take the name of a table or of something SQLite has built in (`sqlite_...`, `pragma_...`,
  `json_each`, `dbstat`, ...);
- one value (a piece of text) may be at most 200 KB, a query may use at most 64 MB of memory and
  return at most 100 columns, and a `LIKE` pattern may be at most 300 characters;
- each query has a time limit (2 seconds by default) and returns at most 5000 rows (and about
  5 MB); beyond that the result is cut and marked `truncated`. A query that is still running
  when its time is up is stopped (its worker process is killed), whatever it is doing.

**Where SQL runs.** Every dashboard query runs in a worker process that is killed when its time
is up: the queries the server runs for a page (`POST /api/query`), the checks of
`easy-ui validate`, `easy-ui publish` and the preview, and the queries of `easy-ui export`. Two
things read SQLite files in the easy-ui process itself, with no worker to kill:
`easy-ui data build` (it reads the listed tables and views of the project's file and writes the
new copy; the preview builds its data the same way) and `easy-ui tables` (with `--source` it reads the project's file). Their reading of the
project's file runs read-only, in SQLite's defensive mode with untrusted schema, with the same
64 MB SQLite memory cap the queries have (sorting and temporary results stay in that memory,
never in files) and a time limit (10 minutes for a build, 60 seconds for `easy-ui tables`);
the build's reading also has an authorizer that lets it read only the listed tables and views.
A view that goes past one of these limits stops the command with a plain message, and a build
that stops changes nothing. These commands are run by the project's own account, on its own file.

`WITH RECURSIVE`, window functions, `json_each` / `json_tree` and SQLite's built-in functions all
work. Name result columns the way they should appear on the page: `steps AS "Steps per day"`.
That name is what charts show in their legend.

## Layout and components

`layout` is a list of rows. Each row is a list (note the `- -` for the first component of a row):

```yaml
layout:
  - - type: stat      # row 1, first component
      ...
    - type: stat      # row 1, second component
      ...
  - - type: line      # row 2
      ...
```

Components in a row share its width equally. On a narrow screen rows stack.

All components take `type`. Unknown settings are errors; the message lists the allowed ones.

Every component also takes two settings for stylesheets (see [STYLES.md](STYLES.md)):

| Setting | What it is |
|---|---|
| `id` | A stable name: lowercase letters, digits and `-`, starting with a letter (up to 41 characters), unique in the dashboard, not of the form `r1c2`. On the page as `data-id`. Without it the id is `r<row>c<column>`, counted from 0. |
| `class` | One class name or a list of up to 8, for the project's stylesheets to hook on. Same characters as `id`; may not start with `eui-`. |
| `app` | Set by easy-ui on the components an app contributes (its name); on the page as `data-app`, which scopes the app's stylesheets. Written by hand in an app's file it must name that app; the project's own files (`ui/dashboards/`, `ui/sections/`) may not have it. |

A layout item may also be an **anchor**, a place where apps can add sections:

```yaml
layout:
  - - type: controls
  - anchor: activity          # sections "after:activity" go here
  - - type: table
      ...
```

An anchor holds only `anchor:` and a name (lowercase letters, digits and `-`), each name once per
file. It draws nothing; a dashboard read on its own ignores its anchors.

### `stat`

One big number from the **first row** of a query.

| Setting | Required | Meaning |
|---|---|---|
| `title` | yes | Heading. |
| `query` | yes | A query name. |
| `value` | yes | Column holding the number. |
| `format` | no | A format (see below). |
| `delta` | no | Column holding a change to show next to the value. |
| `delta_format` | no | Format for the delta (default: same as `format`). |
| `better` | no | `higher` or `lower`: which way of the delta is good news. A good change is drawn in the accent colour, a bad one in a muted red. Not set: neutral grey (a change is not always good or bad). |
| `hint` | no | Short explanation under the number (up to 500 characters). |

A value that is not a number (with format `date` or `datetime`, or text) is shown smaller than
numbers, so a date never outweighs the figures next to it.

### `line`, `area`

A line chart (or with the area under each line filled).

| Setting | Required | Meaning |
|---|---|---|
| `title` | yes | Heading. |
| `query` | yes | A query name. |
| `x` | yes | Column for the horizontal axis (a date or a category). |
| `y` | yes | A column, or a list of up to 8 columns, one series each. |
| `format` | no | Format for the y values. |
| `unit` | no | Short unit, e.g. `bpm` (up to 12 characters): written once above the axis and after values in the readout and table. |
| `step` | no | The gap between the lines across the chart, in the query's own numbers (for `distance` in metres, `5000` is a line every 5 km). Default: round steps from the values (0, 20 km, 40 km; 0, 30 min, 1 h). A step that would draw more than 40 lines is ignored. |
| `readout` | no | The value in the chart's heading: `latest` (default: the newest point on a date axis, or the one under the pointer), `pointer` (only while pointing at the chart) or `none`. |
| `partial` | no | A period still in progress (this week so far): `last` (the last row) or a column that is true (`1`) for such rows. Drawn with a dashed line into an open point; the readout and the table say "so far". |

```yaml
- type: bar
  title: Distance per week
  query: weekly
  x: week
  y: Distance
  format: distance
  step: 10000        # a line every 10 km
  partial: last      # this week is not over yet
```

### `bar`

Same settings as `line`, plus `stacked` (`true` / `false`, default `false`). A `partial` bar is
drawn lighter.

### `table`

| Setting | Required | Meaning |
|---|---|---|
| `title` | yes | Heading. |
| `query` | yes | A query name. |
| `columns` | no | Which columns to show, in order: a column name, or `{column, label, format}`. Default: all. |
| `limit` | no | Show at most this many rows (1 to 5000). |
| `phone` | no | On narrow screens (560 px and less): `cards` (default: each row as a small card, the first column as its heading, then one "label  value" line per column) or `scroll` (the table as it is, scrolled sideways). |

### `text`

| Setting | Required | Meaning |
|---|---|---|
| `title` | no | Heading. |
| `body` | yes | Text in the markdown subset below (up to 10,000 characters). |

The markdown subset: paragraphs (separated by a blank line), headings `#` to `####`, `**bold**`,
`*italic*` or `_italic_`, lists with `-` or `1.`, and links `[text](https://...)`. Links must use
`https://`; anything else is a validation error. A backslash shows a character literally (`\*`).
Everything else, including HTML, is shown as plain text.

### `controls`

The inputs for the params.

| Setting | Required | Meaning |
|---|---|---|
| `title` | no | Heading. |
| `params` | no | Which params to show, in order. Default: all. |

Params without a control on the page keep their default; validation warns about them.

## Formats

| Name | Input | Example |
|---|---|---|
| `number` | number | `1234.5` → `1,234.5` (at most 2 decimals) |
| `integer` | number | `1234.5` → `1,235` |
| `decimal1` | number | `12` → `12.0` |
| `percent` | a fraction | `0.256` → `25.6%` |
| `duration` | seconds | `25620` → `7 h 07 min`, `720` → `12 min`, `65` → `1 min 05 s`, `5` → `5 s` (to the minute from one hour on; never looks like a time of day) |
| `distance` | metres | `850` → `850 m`, `12345` → `12.35 km` |
| `date` | `YYYY-MM-DD…` text or Unix seconds | `2026-03-14` → `14 Mar 2026` |
| `datetime` | `YYYY-MM-DD HH:MM…` text or Unix seconds | `2026-03-14T07:05:09Z` → `14 Mar 2026 07:05` |
| `pace` | seconds per km | `312` → `5:12 /km` |

A missing value (NULL) always shows as `—`. Without a format, numbers use `number` and text is
shown unchanged. Text given to a numeric format is shown unchanged unless it is plain numeric text.
Rounding is half away from zero; a value that rounds to zero has no minus sign. `,` groups
thousands and `.` is the decimal point. The exact contract, with examples the
browser renderer is tested against, is `easyui.formats.FORMATS`.

## Checking a dashboard

`easy-ui validate` checks every file in `ui/dashboards/` (or the files you name). When dashboard
data has been built (`easy-ui data build`) it also runs each query with the default values to
check the tables and every column a component names, also in a file that has other mistakes, so
one round shows them all. `easy-ui publish` (and the preview) also runs each query with every
choice option, each int param's `min` and `max`, and an empty and a 200-character text for each
text param, so a query that is too slow or fails for another control value is usually caught
before anyone sees it (one that is slow only for one particular text is still stopped at its
time limit). Each problem says where it is and what
to do:

```json
{
  "file": "ui/dashboards/training.yaml",
  "line": 31,
  "where": "layout[1][0].querry",
  "place": "row 2, first component (stat), setting querry",
  "problem": "\"querry\" is not a setting of a stat.",
  "allowed": ["type", "title", "query", "value", "format", "delta", "delta_format", "hint"],
  "did_you_mean": "query",
  "hint": "Remove it or use one of the allowed settings.",
  "level": "error"
}
```

`where` is a path into the file: `layout[1][0]` is the first component of the second row;
`place` says the same in words, and is what the text output shows. A mistake in a query's SQL
points at the line of the file where the unknown name appears, with the closest column name.
Warnings (`"level": "warning"`) point at things that work but are probably not meant: a query no
component uses, a param no query uses, params without a control.

In a composed dashboard each problem names the file it is in: a mistake in an app's section says
`apps/<package>/dashboards/<file>.yaml` (or `<package> (installed)/...` for an installed app),
its line, and a `place` such as `section "walks", row 1, first component (bar), setting y`.

## Dashboards from apps

A project's apps (see `[project] apps`) can bring dashboards, sections for other dashboards, and
a look. easy-ui finds each app's folder in `apps.lock.json` (written by `mes apps sync`), or,
when that file does not exist, at `apps/<package>/` in the project. The list is checked against
project.toml:

- When it is out of date only because project.toml changed (the file's hash, or the packages
  `[project] apps` lists or their order) and every package `[project] apps` lists has an
  `apps/<package>/app.toml` inside the project folder, easy-ui reads exactly those apps from
  there, in that order (as if there were no list), and prints a one-line note on stderr ending
  in "run: mes apps sync". Nothing outside the project folder is read in that case.
- Otherwise a stale list is refused with "Run: mes apps sync": a damaged list, a list for another
  project, a listed path that does not match, or a project.toml change when some app is
  installed outside the project.

It reads every file of an app from the open app folder, never through a link, never outside
it, and never more than a set size.

An app's `app.toml` says what it brings (easy-ui reads `[app] name` and `title`,
`[data] tables`, and `[ui]`):

```toml
[ui]
dashboards = ["walks"]                       # dashboards/walks.yaml
tables = ["walks_log"]                       # some of [data] tables, for dashboards to read

[ui.contributes.overview]
sections = "dashboards/overview-walks.yaml"  # sections for the dashboard "overview"
```

**Sections** are rows an app adds to another dashboard (the project's or another app's):

```yaml
spec: easy-ui/sections-v1
sections:
  - id: walks                    # unique in the dashboard
    position: after:activity     # start, end, or after:<anchor>
    queries:                     # names start with the app's table prefix (walks_)
      walks_per_week:
        sql: SELECT strftime('%Y-%W', date) AS week, sum(km) AS "Walked km" FROM walks_log GROUP BY 1
    layout:
      - - type: bar
          title: Walking per week
          query: walks_per_week
          x: week
          y: Walked km
```

A file holds up to 10 sections; each has up to 10 queries and 10 rows. Sections read the
dashboard's params but cannot declare their own, and their query names may not clash with the
dashboard's. Sections at the same place keep the order of `apps`. A section after an anchor the
dashboard does not have goes to the end, with a warning; sections for a dashboard that does not
exist are left out, with a warning.

**The project has the last word.** `ui/dashboards/<name>.yaml` replaces an app's dashboard of
that name (it keeps the app's place in the menu). `ui/sections/<app>.<dashboard>.yaml`, with the
app's name, replaces that app's sections in that dashboard; with `sections: []` it removes them.
Two apps with a dashboard of the same name are an error until the project adds its own.

**The look.** An app's `styles/*.css`, `assets/` and `themes/*.yaml` are checked like the
project's (see [STYLES.md](STYLES.md)). Its stylesheets apply only to what it contributes:
easy-ui rewrites each selector `S` to `:where([data-app="<app>"]) S, S:where([data-app="<app>"])`
and loads them before the project's, which therefore win.

`easy-ui dashboards` lists the apps, every dashboard and section with the file it came from, and
what overrides what (`--json` for agents). `easy-ui compose` prints the composition as JSON
(`{"dashboards": [{"slug", "from", "replaces", "sections": [{"app", "id", "file", "placed"}]}],
"order", "warnings"}`), and `easy-ui compose --out DIR` writes the composed files. Validate,
preview (which reloads when an app's file changes), publish, export and screenshot all work on
the composed dashboards; a published version keeps the composed files, the file and app each
piece came from, and the project's own files.

## Compiled form (what the browser receives)

The server sends `GET /api/dashboards/{slug}` as the compiled form. It contains no SQL: only
params (with defaults worked out), which params each query uses, the components with every
setting present (`null` when not set; `class` is a list or `null`), and `theme`: the dashboard's
own look as checked custom properties, `{"both": {...}, "light": {...}, "dark": {...}}`, or
`null`. Text is sent as a tree of blocks for the renderer to turn
into DOM nodes; it never contains HTML.

```json
{
  "spec": "easy-ui/v1",
  "slug": "training",
  "title": "Training",
  "description": "Distance and steps over time.",
  "params": [
    {"name": "since", "type": "date", "label": "From", "default": "2026-08-18", "default_rule": "today-90d"},
    {"name": "kind", "type": "choice", "label": "Kind", "default": "all",
     "options": [{"value": "all", "label": "all"}, {"value": "run", "label": "Running"}]},
    {"name": "min_km", "type": "int", "label": "At least (km)", "default": 0, "min": 0, "max": 200},
    {"name": "search", "type": "text", "label": "Search", "default": "", "max_length": 200}
  ],
  "queries": {
    "totals": {"params": ["since"]},
    "daily": {"params": ["since"]}
  },
  "layout": [
    [{"id": "r0c0", "type": "controls", "title": null, "params": ["since", "kind", "min_km", "search"]}],
    [
      {"id": "r1c0", "type": "stat", "title": "Distance", "query": "totals", "value": "distance",
       "format": "distance", "delta": null, "delta_format": null, "better": null,
       "hint": "Everything you walked, ran and rode."},
      {"id": "r1c1", "type": "line", "title": "Distance per day", "query": "daily", "x": "day",
       "y": ["Distance"], "format": "distance", "unit": null, "step": null, "readout": "latest",
       "partial": null},
      {"id": "r1c2", "type": "bar", "title": "Steps", "query": "daily", "x": "day",
       "y": ["Steps"], "format": "integer", "unit": null, "stacked": false, "step": null,
       "readout": "latest", "partial": null}
    ],
    [
      {"id": "r2c0", "type": "table", "title": "Recent", "query": "daily", "limit": 20,
       "columns": [{"column": "day", "label": "Day", "format": "date"},
                   {"column": "Distance", "label": "Distance", "format": null}], "phone": "cards"},
      {"id": "r2c1", "type": "text", "title": null, "blocks": [
        {"t": "h", "level": 2, "c": [{"t": "text", "v": "How to read this page"}]},
        {"t": "p", "c": [
          {"t": "text", "v": "Pace is "},
          {"t": "b", "c": [{"t": "text", "v": "minutes"}]},
          {"t": "text", "v": " per km. See "},
          {"t": "a", "href": "https://example.org/easy-ui", "c": [{"t": "text", "v": "the project page"}]}
        ]},
        {"t": "ul", "items": [[{"t": "text", "v": "one"}], [{"t": "i", "c": [{"t": "text", "v": "two"}]}]]}
      ]}
    ]
  ]
}
```

Block nodes: `{"t": "h", "level": 1-4, "c": [inline]}`, `{"t": "p", "c": [inline]}`,
`{"t": "ul" | "ol", "items": [[inline], ...]}`. Inline nodes: `{"t": "text", "v": "..."}`,
`{"t": "b", "c": [inline]}`, `{"t": "i", "c": [inline]}`,
`{"t": "a", "href": "https://...", "c": [inline]}`.

Every component in `layout` also has `"class"` (a list or `null`), left out of the example
above for brevity. A dashboard with `theme:` sends, for example,
`"theme": {"both": {"--eui-chart-line-width": "3"}, "light": {"--eui-accent": "#a2451d"}, "dark": {}}`.

`table.columns` is `null` when every column should be shown in query order. `controls.params` is
always a list. Every component, and the dashboard, also has `"app"`: the name of the app it came
from, or `null` for the project's own.

### Running queries

`POST /api/query` with `{"dashboard": "training", "queries": ["totals", "daily"], "params":
{"since": "2026-01-01"}}` returns:

```json
{"results": {
  "totals": {"columns": ["distance", "days"], "rows": [[194174.9, 30]], "truncated": false},
  "daily": {"columns": ["day", "Distance", "Steps"], "rows": [["2026-04-01", 6120.5, 8161]], "truncated": false}
}}
```

Params left out use their default. Only queries named in a **published** dashboard can run.
Binary values and non-finite numbers come back as `null`. A query that fails gives
`{"error": {"code", "message", "hint"}}` in its own slot. Each query may run for 2 seconds, and
all the queries of one request share 4 seconds, so one slow page cannot hold up the others.

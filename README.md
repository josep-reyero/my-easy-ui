# my-easy-ui

A small dashboard server and browser renderer for apps built with my-easy-software. One server
process serves one app.

## The idea

Dashboards are **disposable views**. Your data stays where it is, in your app. A dashboard is a
short YAML file that says which data to read (a few named, read-only SQL queries) and how to show
it (rows of fixed components: numbers, line, area and bar charts, tables, text and controls).
It contains no JavaScript, HTML or CSS.

Because a dashboard is only a description, you or your AI agent can reshape it whenever your
questions change: add a chart, drop a table, start a new page for this month's goal. There is
nothing to break. `easy-ui validate` explains every mistake in plain words (where it is, what is
allowed, the closest match), and the server re-checks everything before it shows anything.

## How it keeps your data safe

- Dashboards never touch the app's own database. `easy-ui data build` copies only the tables
  listed in `app.toml` into a read-only file, and dashboards read that copy.
- The server only runs queries that are written in a published dashboard. The browser sends a
  query's name and the control values, never SQL.
- Queries run in a sandbox: read-only and immutable file, one `SELECT` only, no `ATTACH`, no
  `PRAGMA`, no writes, no extension or file functions, values always bound as parameters, a time
  limit, a row limit and a size limit.
- Sign-in is always required, and the server listens only on this machine.

## Try the demo

```sh
easy-ui demo ./easy-ui-demo        # a small app with made-up data
cd ./easy-ui-demo/app
easy-ui preview                    # prints a link; open it in a browser on this computer
```

The preview shows the dashboards straight from `ui/dashboards/`, with no password and nothing
published. Edit a dashboard file and save it: the open page shows the change within a couple of
seconds, or lists what to fix. `easy-ui demo` also prints the commands for the whole journey
below with the demo's own folders.

## Quick start

In an app folder (the one holding `app.toml`):

```toml
# app.toml
[app]
name = "fitness"
title = "My fitness"

[ui]
data = "fitness.sqlite"          # the app's SQLite file, inside its data folder
tables = ["days", "activities"]  # only these reach dashboards
accent = "#2f6fdf"               # optional
dashboards = ["overview"]        # optional: menu order; the first opens at the start
```

```sh
easy-ui data build        # copy the allowed tables for the dashboards
easy-ui tables            # see the tables and columns, with sample values
easy-ui components        # the whole vocabulary (add --json for agents)
# write ui/dashboards/overview.yaml (see docs/SPEC.md)
easy-ui validate          # check every dashboard against the data
easy-ui preview           # look at it (one-time link, no password, nothing published)
easy-ui publish           # make it the version the server shows
easy-ui set-password      # the person types the sign-in password (12+ characters)
easy-ui check             # a checklist: not root, private folder, password, data, published
easy-ui serve             # http://127.0.0.1:8765, until you stop it with Ctrl-C
```

`easy-ui rollback` shows the previous published version again; your files in `ui/dashboards/`
stay as they are (the command says so), and `easy-ui rollback N --restore-files` puts version N's
files back so you can edit from there.

**Passwords and agents.** Only a person can set the sign-in password: `easy-ui set-password`
reads it from the terminal, never from an argument, a variable or a pipe, and answers an agent
with exit code 6 ("needs the human"). An agent looks at pages with `easy-ui preview`, whose link
works once (it then prints a new one for the next browser). Automated tests set a made-up
password with the library function `easyui.auth.set_password(state.auth, "...")`.

**Running it for real.** Started by hand, `easy-ui serve` runs until it is stopped. As a service,
`easy-ui units --render-only` writes systemd files that start it on the first visit (socket
activation) under its own locked-down account; then it stops by itself after 10 minutes without
use. Pass `--allow-host NAME` (or `units --allow-host NAME`) with the name the private network's
proxy uses, so the server accepts requests under that name.

Every command accepts `--json` (only JSON on stdout, errors as JSON) and `--app-dir PATH`, before
or after the command. Commands that run until stopped (`serve`, `preview`) still print their
result when stopped with Ctrl-C or SIGTERM. Exit codes: 0 ok, 1 error, 2 usage, 3 not found, 4
not allowed, 5 refused for safety, 6 needs you, 75 try again later.

The full dashboard format, with examples, is in [docs/SPEC.md](docs/SPEC.md); the look of the
page is described in [docs/DESIGN.md](docs/DESIGN.md).

![The demo's overview dashboard](docs/screenshots/overview-desktop-light.png)

## Where things live

- The app's data: `~/.local/share/mes/<app>/` (`MES_DATA_HOME` changes the base). easy-ui only
  reads it.
- easy-ui's own state: `~/.local/state/easy-ui/<app>/` (`EASYUI_STATE_HOME` changes the base),
  with `data/` (the dashboard data copies and the `current` link), `specs/` (published dashboard
  versions) and `auth/` (password hash and sessions). To start over, delete that folder.
- Sample data for the preview: `ui/fixtures/*.sqlite`, `*.sqlite3` or `*.db` in the app (made-up
  data only), used when the app has no data yet or with `easy-ui preview --fixture`.

## Status

v0.1. Everything above works: the dashboard format and its checks, the data copy, the query
sandbox, publishing with versions and rollback, the server with sign-in, the browser page, the
preview and the systemd files. Known limits: formatting is English-style only (`1,234.5`,
`14 Mar 2026`); charts space points evenly (one per row), which suits daily and weekly series; a
browser that has never signed in can be kept out by someone on the private network who keeps
sending wrong passwords (a browser that has signed in before is not affected, and
`easy-ui set-password` clears the pause); sharing dashboards outside your own devices (a static
export) is planned and not part of v0.1. Tested with Chromium; not yet with Safari or Firefox.

## Development

```sh
uv sync
uv run pytest
```

Python 3.12 or newer. Runtime dependencies: starlette, uvicorn, argon2-cffi, PyYAML.

## License

MIT

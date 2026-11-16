# my-easy-ui

A small dashboard server and browser renderer for projects built with my-easy-software. One
server process serves one project.

## The idea

Dashboards are **disposable views**. Your data stays where it is, in your project. A dashboard is a
short YAML file that says which data to read (a few named, read-only SQL queries) and how to show
it (rows of fixed components: numbers, line, area and bar charts, tables, text and controls).
It contains no JavaScript, HTML or CSS.

Because a dashboard is only a description, you or your AI agent can reshape it whenever your
questions change: add a chart, drop a table, start a new page for this month's goal. There is
nothing to break. `easy-ui validate` explains every mistake in plain words (where it is, what is
allowed, the closest match), and the server re-checks everything before it shows anything.

## How it keeps your data safe

- Dashboards never touch the project's own database. `easy-ui data build` copies only the tables
  listed in `project.toml` into a read-only file, and dashboards read that copy.
- The server only runs queries that are written in a published dashboard. The browser sends a
  query's name and the control values, never SQL.
- Queries run in a sandbox: read-only and immutable file, one `SELECT` only, no `ATTACH`, no
  `PRAGMA`, no writes, no extension or file functions, values always bound as parameters, a time
  limit, a row limit and a size limit.
- Sign-in is always required, and the server listens only on this machine. Sign in with a
  passkey (Face ID, Touch ID, Windows Hello, a phone or a security key); the password stays as a
  fallback and can be switched off, and one-time recovery codes cover a lost device.
- A shared copy (`easy-ui export`) holds only the chosen dashboards' numbers for one set of
  control values: no SQL, no tables, nothing of the sign-in.

## Try the demo

```sh
easy-ui demo ./easy-ui-demo        # a small project with made-up data
cd ./easy-ui-demo/app
easy-ui preview                    # prints a link; open it in a browser on this computer
```

The preview shows the dashboards straight from `ui/dashboards/`, with no password and nothing
published. Edit a dashboard file and save it: the open page shows the change within a couple of
seconds, or lists what to fix. `easy-ui demo` also prints the commands for the whole journey
below with the demo's own folders.

## Quick start

In a project folder (the one holding `project.toml`, shared with my-easy-software):

```toml
# project.toml
[project]
name = "fitness"                 # lowercase letters, digits and -; at most 24 characters for a server
title = "My fitness"

[ui]
data = "fitness.sqlite"          # the project's SQLite file, inside its data folder
tables = ["days", "activities"]  # only these reach dashboards
accent = "#2f6fdf"               # optional
dashboards = ["overview"]        # optional: menu order; the first is the landing page
refresh_after = ["sync"]         # optional, read by mes: jobs after which it runs easy-ui data build
```

easy-ui reads only `[project]` and `[ui]`; the other tables (`[apps]`, `[jobs]`, `[storage]`)
belong to my-easy-software. An old `app.toml` gets a message saying how to rename it.

```sh
easy-ui data build        # copy the allowed tables for the dashboards
easy-ui tables            # see the tables and columns, with sample values
easy-ui components        # the whole vocabulary (add --json for agents)
# write ui/dashboards/overview.yaml (see docs/SPEC.md)
easy-ui validate          # check every dashboard against the data
easy-ui preview           # look at it (one-time link, no password, nothing published)
easy-ui publish           # make it the version the server shows
easy-ui set-password      # the person types the first sign-in password (12+ characters)
easy-ui check             # a checklist: not root, private folder, password, data, published
easy-ui serve             # http://127.0.0.1:8765, until you stop it with Ctrl-C
```

`easy-ui rollback` shows the previous published version again; your files in `ui/dashboards/`
stay as they are (the command says so), and `easy-ui rollback N --restore-files` puts version N's
files back so you can edit from there.

**Passkeys.** After the first sign-in with the password, the page offers to add a passkey
(**Sign-in** in the header: add, name and remove passkeys, switch password sign-in off, make
recovery codes). Passkeys work on `http://localhost` and over HTTPS (for example behind the
private network's proxy); on other plain-http addresses the page uses the password. Signing in
with a passkey is never slowed down by someone guessing passwords. From the server's terminal:
`easy-ui passkeys` (list), `easy-ui passkeys remove NAME`, `easy-ui sign-in` (status),
`easy-ui sign-in password on|off`, `easy-ui recovery-codes` (ten one-time codes, shown once on
the terminal only).

**Passwords and agents.** Only a person can set the sign-in password: `easy-ui set-password`
reads it from the terminal, never from an argument, a variable or a pipe, and answers an agent
with exit code 6 ("needs the human"); so does `easy-ui recovery-codes`. An agent looks at pages
with `easy-ui preview`, whose link works once (it then prints a new one for the next browser),
or takes a picture with `easy-ui screenshot`. Automated tests set a made-up password with the
library function `easyui.auth.set_password(state.auth, "...")`.

**Sharing a copy.** `easy-ui export --out DIR` writes a self-contained folder (`index.html`
and its files) with the published dashboards and their numbers for one set of control values
(`--set NAME=VALUE`, `--dashboard NAME`). It needs no server and makes no network request, so
it can be opened from the disk or put on any static host. The command lists exactly what
becomes visible. With `--encrypt` the data is locked with AES-256-GCM and the key exists only
in the link's ending (`#k=...`), which browsers never send to the host:

```sh
easy-ui export --out ./share --dashboard overview --set since=2026-01-01 --encrypt
```

**Pictures for agents.** `easy-ui screenshot [DASHBOARD] --out FILE.png [--phone] [--dark]`
draws the working dashboards (or `--published`) in a headless Chromium-based browser already
installed on this computer (`EASYUI_CHROMIUM` picks one) and saves the whole page; no server,
no password, no network.

**Running it for real.** Started by hand, `easy-ui serve` runs until it is stopped. On a
server, `easy-ui units --render-only` writes the systemd socket and service for the project's
web account `mes-<project>-web`: started on the first visit (socket activation), stopped after
10 minutes without use, and able to read only the project's `ui/` folder and write only
`ui/auth/`. It prints the folders, owners and modes to create (see [Where things
live](#where-things-live)). Pass `--allow-host NAME` (to `serve` or `units`) with the name the
private network's proxy uses, so the server accepts requests under that name. Commands for the
sign-in on a server run as the web account, for example
`sudo -u mes-fitness-web easy-ui set-password --ui-dir /var/lib/mes/fitness/ui`.

Every command accepts `--json` (only JSON on stdout, errors as JSON), `--project-dir PATH` (the
folder holding `project.toml`; `--app-dir` still works) and `--ui-dir PATH` (the ui folder alone,
as the web account uses it), before or after the command. Commands that run until stopped (`serve`, `preview`) still print their
result when stopped with Ctrl-C or SIGTERM. Exit codes: 0 ok, 1 error, 2 usage, 3 not found, 4
not allowed, 5 refused for safety, 6 needs you, 75 try again later.

The full dashboard format, with examples, is in [docs/SPEC.md](docs/SPEC.md); the look of the
page is described in [docs/DESIGN.md](docs/DESIGN.md).

![The demo's overview dashboard](docs/screenshots/overview-desktop-light.png)

<p>
<img src="docs/screenshots/activities-phone.png" alt="A table on a phone, each row as a card" width="260">
<img src="docs/screenshots/signin.png" alt="The sign-in page: passkey first, the password as a fallback" width="520">
</p>

## Where things live

On your own computer:

- The project's data: `~/.local/share/mes/<project>/` (`MES_DATA_HOME` changes the base), or the
  folder named for this computer in `~/.config/mes/config.toml` (`MES_CONFIG` names another file):

  ```toml
  [projects.fitness]
  data_dir = "~/Documents/fitness-archive"
  ```

  easy-ui only reads it.
- easy-ui's own folder (the *ui folder*): `~/.local/state/mes/<project>/ui/` (`MES_STATE_HOME`
  changes the base; `EASYUI_STATE_HOME`, mostly for tests, makes it `$EASYUI_STATE_HOME/<project>/`), with `data/` (the dashboard data copies and the
  `current` link), `specs/` (published dashboard versions), `auth/` (password hash, passkeys,
  recovery codes, sessions) and `project.json` (the name, title, accent and menu order, so the
  server never needs `project.toml`). To start over, delete that folder.
- Sample data for the preview: `ui/fixtures/*.sqlite`, `*.sqlite3` or `*.db` in the project
  (made-up data only), used when the project has no data yet or with `easy-ui preview --fixture`.

On a server (`MES_ROOT=/var/lib/mes`, set by my-easy-software for its jobs):

| Folder | Owner : group | Mode | Why |
| --- | --- | --- | --- |
| `/var/lib/mes/<p>` | `mes-<p>` : `mes-<p>-web` | 0710 | the web account may pass through, not list |
| `/var/lib/mes/<p>/data` | (my-easy-software) | | the project's own data; the web account cannot enter |
| `/var/lib/mes/<p>/ui`, `ui/data`, `ui/specs` | `mes-<p>` : `mes-<p>-web` | 2750 | written by the jobs (`data build`, `publish`), read by the web account |
| `/var/lib/mes/<p>/ui/auth` | `mes-<p>-web` : `mes-<p>-web` | 0700 | the sign-in, written only by the web account |

`data build` and `publish` keep what they write readable by that group whatever the umask. The
service sees nothing else under `/var/lib/mes` (it is hidden from it), and `easy-ui` itself must
be installed outside `/var/lib/mes` and outside home folders (for example `/usr/local/bin`).

## Status

v1. Everything above works: the dashboard format and its checks, the data copy, the query
sandbox, publishing with versions and rollback, the server with passkeys, password and recovery
codes, the browser page, the preview, static exports, pictures and the systemd files. Known
limits: formatting is English-style only (`1,234.5`, `14 Mar 2026`); charts space points evenly
(one per row), which suits daily and weekly series; a browser that has never signed in can be
kept from signing in *with the password* by someone on the private network who keeps sending
wrong passwords (passkeys and recovery codes are not affected); passkeys need `localhost` or
HTTPS; a shared copy shows one set of control values. Tested with Chromium; not yet with Safari
or Firefox; the systemd files are checked by tests, not yet on a real server.

## Development

```sh
uv sync
uv run pytest
```

Python 3.12 or newer. Runtime dependencies: starlette, uvicorn, argon2-cffi, PyYAML and
cryptography (passkey signatures and locked exports). The passkey checks are written here (no
WebAuthn library) and tested against a recorded browser ceremony.

## License

MIT

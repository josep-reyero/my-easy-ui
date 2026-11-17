"""Reading the parts of a project's ``project.toml`` that easy-ui uses.

easy-ui reads ``project.toml`` itself (it does not depend on my-easy-software),
following the apps contract (my-easy-software ``docs/apps-contract.md``,
sections 1 and 6.5). It reads ``[project]`` (``name``, ``title``, ``version``,
``apps``), ``[ui]``, and only the *names* of ``[tables]`` and ``[views]``
(my-easy-software checks the rest)::

    [project]
    name = "fitlab"
    title = "Fit lab"
    apps = ["fitlab_walks", "fitlab_rides"]   # the apps' packages; order matters

    [tables.activities]                      # a shared table: dashboards read its view "activities"
    key = ["id"]
    columns = { id = "text", date = "text" }

    [views.weekly]                           # a cross-app view
    sql = "SELECT ..."

    [ui]
    accent = "#2a7f62"                       # optional
    dashboards = ["overview", "walks"]       # optional: menu order; the first is the landing page
    tables = ["activities", "weekly"]        # optional: narrows what dashboards may read
    refresh_after = ["fitlab-rides.rebuild"] # optional, read by mes: job ids

Unknown keys are errors (with the closest correct name, and for settings that
belong elsewhere, such as ``[jobs]`` or ``[ui] data``, where they go). The data
the dashboards read is always the shared database ``<data folder>/<project>.sqlite``.

The apps themselves (their ``app.toml`` files, found through ``apps.lock.json``
or in ``<project>/apps/<package>/``) are read by ``easyui.project``; the
properties ``apps`` and ``tables`` below read them when asked.

On a server the dashboard server's account cannot read the project's folder at
all, so ``data build`` and ``publish`` also write ``project.json`` into the ui
folder, and ``load(ui_dir=...)`` builds the configuration from that file alone
when there is no ``project.toml`` to read.

Public API
----------
``TOP_KEYS``, ``PROJECT_KEYS``, ``UI_KEYS``  the keys allowed at the top, in ``[project]`` and in ``[ui]``.
``SHARED_NAME_RE``, ``PACKAGE_RE``, ``JOB_ID_RE``  name patterns (contract section 0).
``AppConfig`` (frozen dataclass; ``ProjectConfig`` is the same class)
    ``name``, ``title``, ``app_dir`` (Path: the project folder, or the ui folder when
    loaded from ``project.json``), ``manifest`` (Path of project.toml or project.json),
    ``packages`` (the ``[project] apps`` packages, in order), ``shared_tables`` and ``views``
    (the names of ``[tables]`` and ``[views]``, in file order), ``ui_tables`` (the ``[ui] tables``
    list, or None), ``accent`` (str | None), ``dashboards`` (tuple of slugs: the menu order; the
    first one is the landing page; the others follow in discovery order), ``version`` (str | None),
    ``refresh_after`` (tuple of job ids, for mes), ``manifest_sha256`` (of project.toml's exact
    bytes, for checking apps.lock.json), ``ui_dir`` (Path | None: a ui folder given with
    ``--ui-dir``), ``from_ui_dir`` (bool), ``auth_dir`` (Path | None: a sign-in folder given with
    ``--auth-dir``). Properties: ``dashboards_dir`` (``<app_dir>/ui/dashboards``),
    ``sections_dir`` (``<app_dir>/ui/sections``), ``data_dir`` (the project's data folder),
    ``data`` (the shared database's file name, ``<project>.sqlite``), ``apps``
    (``easyui.project.load_apps``: raises ``ConfigError``), ``tables`` (the allowlist of contract
    section 4.6), ``state`` (``paths.StatePaths`` of the ui folder; its ``auth`` is the sign-in
    folder, see ``easyui.paths``). Methods: ``source_db()`` (Path of ``<project>.sqlite``),
    ``require_tables()`` (raises ``ConfigError`` when the allowlist is empty), ``to_dict()``,
    ``public()`` (``{name, title, accent}`` for the browser), ``snapshot()`` (what
    ``project.json`` holds).
``load(app_dir=None, *, ui_dir=None, auth_dir=None) -> AppConfig``
    ``app_dir`` is the project's folder or its ``project.toml``; ``None`` means the
    nearest folder at or above the current directory holding ``project.toml``, and
    when there is none and ``ui_dir`` is given, the ``project.json`` in that folder.
    Raises ``NotFound`` when there is none and ``ConfigError`` when it is wrong.
``parse(data, *, app_dir, manifest, sha256=None) -> AppConfig``
    Checks an already-parsed TOML table.
``write_snapshot(config)``  writes ``project.json`` into the ui folder (atomically).
"""

from __future__ import annotations

import contextlib
import dataclasses
import difflib
import hashlib
import json
import os
import re
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from easyui import paths
from easyui.errors import ConfigError, NotFound

TOP_KEYS = ("project", "tables", "views", "settings", "ui", "storage", "backup")
PROJECT_KEYS = ("name", "title", "version", "apps")
UI_KEYS = ("accent", "dashboards", "tables", "refresh_after")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}\Z")
MAX_DASHBOARDS = 100
TABLE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}\Z")
SHARED_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}\Z")
PACKAGE_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}\Z")
ACCENT_RE = re.compile(r"^#[0-9a-fA-F]{6}\Z")
JOB_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}\.[a-z][a-z0-9-]{0,30}\Z")
JOB_RE = JOB_ID_RE
VERSION_RE = re.compile(r"^[0-9A-Za-z.+_-]{1,40}\Z")
MAX_TABLES = 64
MAX_APPS = 16
MAX_SHARED = 32
MAX_PROJECT_TOML_BYTES = 256_000
MAX_SNAPSHOT_BYTES = 64_000

PROJECT_EXAMPLE = '[project]\nname = "myproject"\ntitle = "My project"\napps = ["myproject_main"]'


@dataclass(frozen=True)
class AppConfig:
    name: str
    title: str
    app_dir: Path
    manifest: Path
    packages: tuple[str, ...] = field(default_factory=tuple)
    shared_tables: tuple[str, ...] = field(default_factory=tuple)
    views: tuple[str, ...] = field(default_factory=tuple)
    ui_tables: tuple[str, ...] | None = None
    accent: str | None = None
    dashboards: tuple[str, ...] = field(default_factory=tuple)
    version: str | None = None
    refresh_after: tuple[str, ...] = field(default_factory=tuple)
    manifest_sha256: str | None = None
    ui_dir: Path | None = None
    from_ui_dir: bool = False
    auth_dir: Path | None = None

    @property
    def dashboards_dir(self) -> Path:
        return self.app_dir / "ui" / "dashboards"

    @property
    def sections_dir(self) -> Path:
        return self.app_dir / "ui" / "sections"

    @property
    def data_dir(self) -> Path:
        return paths.app_data_dir(self.name)

    @property
    def data(self) -> str:
        """The shared database's file name in the data folder: always ``<project>.sqlite``."""
        return f"{self.name}.sqlite"

    @property
    def apps(self) -> tuple[Any, ...]:
        """The project's apps (``easyui.project.App``), in ``[project] apps`` order. Raises ``ConfigError``."""
        if self.from_ui_dir:
            return ()
        from easyui import project  # noqa: PLC0415 - project reads this module's config

        return project.load_apps(self)

    @property
    def tables(self) -> tuple[str, ...]:
        """What dashboards may read (contract 4.6): shared views, [views], then each app's [ui] tables."""
        if self.from_ui_dir:
            return ()
        from easyui import project  # noqa: PLC0415

        return project.allowlist(self, self.apps)

    @property
    def state(self) -> paths.StatePaths:
        if self.ui_dir is not None:
            return paths.StatePaths(self.ui_dir, self.auth_dir or paths.auth_dir(self.name, self.ui_dir))
        sp = paths.state_paths(self.name)
        return sp if self.auth_dir is None else paths.StatePaths(sp.root, self.auth_dir)

    def source_db(self) -> Path:
        if self.from_ui_dir:
            raise ConfigError(
                "This command needs the project's own folder (the one with project.toml), but only its published "
                "dashboard folder was given.",
                hint="Run it inside the project's folder, or pass --project-dir with the folder that holds project.toml.",
            )
        return self.data_dir / self.data

    def require_tables(self) -> tuple[str, ...]:
        tables = self.tables
        if not tables:
            raise ConfigError(
                "The project has no shared tables, views or app tables for the dashboards, so no data would be "
                "copied for them.",
                hint="Declare the shared tables in project.toml ([tables.<name>]), or list an app's own tables under "
                "[ui] tables in its app.toml. Run: easy-ui tables --source to see what the project's data file has.",
            )
        return tables

    def public(self) -> dict[str, Any]:
        return {"name": self.name, "title": self.title, "accent": self.accent}

    def snapshot(self) -> dict[str, Any]:
        return {
            "v": 1,
            "name": self.name,
            "title": self.title,
            "accent": self.accent,
            "dashboards": list(self.dashboards),
            "version": self.version,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "app_dir": str(self.app_dir),
            "apps": list(self.packages),
            "shared_tables": list(self.shared_tables),
            "views": list(self.views),
            "ui_tables": list(self.ui_tables) if self.ui_tables is not None else None,
            "accent": self.accent,
            "dashboards": list(self.dashboards),
            "version": self.version,
            "refresh_after": list(self.refresh_after),
        }


ProjectConfig = AppConfig


def _where(e: tomllib.TOMLDecodeError) -> str:
    m = re.search(r"line (\d+)", str(e))
    return f" on line {m.group(1)}" if m else ""


# Where a setting that does not belong in project.toml (at that place) goes instead.
_ELSEWHERE = {
    ("", "apps"): 'The project lists its apps under [project]: apps = ["<package>"]; each app is a package folder '
                  "with its own app.toml (see: mes guide apps).",
    ("", "jobs"): "Jobs belong in the app.toml of the app that runs them; to change a schedule from the project, use "
                  "[settings.<app>].",
    ("", "app"): "project.toml describes the project in a [project] table; an [app] table belongs in an app's own "
                 "app.toml.",
    ("ui", "data"): "Dashboards always read the project's shared database, <project>.sqlite in its data folder; "
                    "remove the data line.",
}


def _unknown_key(table: str, key: str, allowed: tuple[str, ...]) -> ConfigError:
    close = difflib.get_close_matches(str(key), allowed, n=1)
    did = f' Did you mean "{close[0]}"?' if close else ""
    where = f"under [{table}]" if table else "at the top"
    elsewhere = _ELSEWHERE.get((table, str(key)))
    return ConfigError(
        f'project.toml has a setting "{str(key)[:60]}" {where} that easy-ui does not know, so it cannot be sure '
        f"what you meant.{'' if elsewhere else did}",
        hint=(elsewhere + " " if elsewhere else "") + f"The settings allowed {where} are: {', '.join(allowed)}.",
        code="config_unknown_key",
    )


def _name_list(ui: dict[str, Any], key: str, pattern: re.Pattern[str], example: str, what: str, one: str) -> tuple[str, ...]:
    raw = ui.get(key, [])
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise ConfigError(f"[ui] {key} in project.toml must be a list of {what}.", hint=f"For example: {key} = {example}")
    bad = [x for x in raw if not pattern.match(x)]
    if bad:
        raise ConfigError(f"[ui] {key} in project.toml has {bad[0][:60]!r}, which is not a {one}.",
                          hint=f"For example: {key} = {example}")
    if len(raw) > MAX_DASHBOARDS or len(set(raw)) != len(raw):
        raise ConfigError(f"[ui] {key} in project.toml lists a name more than once (or more than 100).", hint="List each one once.")
    return tuple(raw)


def _names_of(data: dict[str, Any], table: str, what: str, example: str) -> tuple[str, ...]:
    raw = data.get(table, {})
    if not isinstance(raw, dict) or not all(isinstance(v, dict) for v in raw.values()):
        raise ConfigError(f"[{table}] in project.toml must hold one table per {what}, like [{table}.<name>].",
                          hint=f"For example:\n{example}")
    if len(raw) > MAX_SHARED:
        raise ConfigError(f"project.toml declares more than {MAX_SHARED} {what}s under [{table}].",
                          hint=f"Keep at most {MAX_SHARED}.")
    for n in raw:
        if not SHARED_NAME_RE.match(n) or "__" in n or n.startswith(("sqlite_", "mes_")):
            raise ConfigError(
                f"project.toml has a {what} called {n[:60]!r} under [{table}], which cannot be used as a name.",
                hint="Use lowercase letters, digits and _ (up to 41 characters, starting with a letter), without "
                "__ and not starting with sqlite_ or mes_.",
            )
    return tuple(raw)


def parse(data: dict[str, Any], *, app_dir: Path, manifest: Path, sha256: str | None = None) -> AppConfig:
    project = data.get("project")
    if not isinstance(project, dict):
        raise ConfigError(
            "project.toml has no [project] table, so easy-ui cannot tell which project this is.",
            hint=f"Add to project.toml:\n{PROJECT_EXAMPLE}",
            code="config_no_project",
        )
    name = project.get("name")
    if not isinstance(name, str) or not paths.APP_NAME_RE.match(name) or name == "mes":
        raise ConfigError(
            f"The project name in project.toml ({str(name)[:60]!r}) is missing or not valid, so easy-ui cannot find "
            "the project's folders.",
            hint="Set [project] name to 2 to 31 lowercase letters, digits and dashes, starting with a letter (not mes).",
        )
    for key in data:
        if key not in TOP_KEYS:
            raise _unknown_key("", key, TOP_KEYS)
    for key in project:
        if key not in PROJECT_KEYS:
            raise _unknown_key("project", key, PROJECT_KEYS)
    title = project.get("title", name)
    if not isinstance(title, str) or not title.strip() or len(title) > 80 or "\n" in title:
        raise ConfigError(
            "The [project] title in project.toml must be a short piece of text on one line (up to 80 characters).",
            hint='For example: title = "My fitness".',
        )
    version = project.get("version")
    if version is not None and (not isinstance(version, str) or not VERSION_RE.match(version)):
        raise ConfigError(
            "The [project] version in project.toml must be a short version text such as \"0.1.0\".",
            hint='For example: version = "0.1.0", or remove the line.',
        )
    if "apps" not in project:
        raise ConfigError(
            "project.toml does not list its apps, so nothing knows which commands, jobs and dashboards the project "
            "has.",
            hint='Add apps = ["<package>"] under [project].',
            code="config_no_apps",
        )
    apps = project["apps"]
    if not isinstance(apps, list) or not all(isinstance(a, str) for a in apps) or not 1 <= len(apps) <= MAX_APPS:
        raise ConfigError(
            f"[project] apps in project.toml must be a list of 1 to {MAX_APPS} package names.",
            hint=f'For example: apps = ["{name.replace("-", "_")}_main"] (the order decides where dashboards and '
            "sections appear).",
        )
    bad = [a for a in apps if not PACKAGE_RE.match(a)]
    if bad:
        raise ConfigError(
            f"[project] apps in project.toml has {bad[0][:60]!r}, which is not a Python package name.",
            hint="Package names use lowercase letters, digits and _, starting with a letter (2 to 41 characters), "
            'for example "fitlab_walks".',
        )
    dup = sorted({a for a in apps if apps.count(a) > 1})
    if dup:
        raise ConfigError(f"[project] apps in project.toml lists {dup[0]!r} more than once.", hint="List each app once.")

    shared = _names_of(data, "tables", "shared table", '[tables.days]\nkey = ["date"]\ncolumns = { date = "text" }')
    views = _names_of(data, "views", "view", '[views.weekly]\nsql = "SELECT ..."')
    clash = sorted(set(shared) & set(views))
    if clash:
        raise ConfigError(f"project.toml has both a shared table and a view called {clash[0]!r}.",
                          hint="Give the view another name.")

    ui = data.get("ui", {})
    if not isinstance(ui, dict):
        raise ConfigError("[ui] in project.toml must be a table.", hint='For example:\n[ui]\ndashboards = ["overview"]')
    for key in ui:
        if key not in UI_KEYS:
            raise _unknown_key("ui", key, UI_KEYS)

    ui_tables: tuple[str, ...] | None = None
    if "tables" in ui:
        tables_raw = ui["tables"]
        if not isinstance(tables_raw, list) or not all(isinstance(t, str) for t in tables_raw):
            raise ConfigError(
                "[ui] tables in project.toml must be a list of table names.", hint='For example: tables = ["days", "activities"]'
            )
        bad = [t for t in tables_raw if not TABLE_NAME_RE.match(t) or "__" in t]
        if bad:
            raise ConfigError(
                f"[ui] tables in project.toml has a name easy-ui cannot use: {bad[0][:60]!r}.",
                hint="Name shared tables, [views] and app tables (lowercase letters, digits and _, starting with a "
                "letter). Dashboards read a shared table through its view, never <name>__rows.",
            )
        if len(tables_raw) > MAX_TABLES:
            raise ConfigError(f"[ui] tables lists more than {MAX_TABLES} tables.", hint="List only what dashboards need.")
        dupes = sorted({t for t in tables_raw if tables_raw.count(t) > 1})
        if dupes:
            raise ConfigError(f"[ui] tables lists {dupes[0]!r} more than once.", hint="List each table once.")
        ui_tables = tuple(tables_raw)

    accent = ui.get("accent")
    if accent is not None and (not isinstance(accent, str) or not ACCENT_RE.match(accent)):
        raise ConfigError(
            f"[ui] accent in project.toml is {str(accent)[:40]!r}, which is not a colour easy-ui understands.",
            hint='Use a colour written as # and six hex digits, for example accent = "#2f6fdf", or remove the line.',
        )
    order = _name_list(
        ui, "dashboards", SLUG_RE, '["overview", "sleep"] (the first one is the page that opens first)',
        "dashboard names", "dashboard name (a file name without .yaml)",
    )
    refresh_after = _name_list(ui, "refresh_after", JOB_ID_RE, '["myproject-main.rebuild"]',
                               "job ids (<app>.<job>)", "job id (<app name>.<job name>)")
    return AppConfig(
        name=name,
        title=title.strip(),
        app_dir=app_dir,
        manifest=manifest,
        packages=tuple(apps),
        shared_tables=shared,
        views=views,
        ui_tables=ui_tables,
        accent=accent.lower() if accent else None,
        dashboards=order,
        version=version,
        refresh_after=refresh_after,
        manifest_sha256=sha256,
    )


def _no_project(where: Path | None) -> NotFound:
    app_toml = paths.find_app_manifest(where)
    if app_toml is not None:
        return NotFound(
            f"{paths.display_path(app_toml.parent)} holds an app (its app.toml), not a project, and no project.toml "
            "was found above it, so easy-ui does not know which project's dashboards to use.",
            hint="Run the command in the project's folder (the one holding project.toml and listing this app under "
            "[project] apps), or pass --project-dir with that folder.",
            code="not_a_project",
        )
    return NotFound(
        "There is no project here (no project.toml was found in this folder or above), so easy-ui does not know "
        "which project's dashboards to use.",
        hint="Run the command inside the project's folder, or pass --project-dir with the folder that holds "
        "project.toml.",
    )


def load_snapshot(ui_dir: str | Path) -> AppConfig:
    """The configuration from ``<ui_dir>/project.json`` (written by ``data build`` and ``publish``)."""
    root = Path(ui_dir).expanduser().resolve()
    f = root / paths.PROJECT_FILE
    try:
        raw = paths.read_nofollow(f, 1, MAX_SNAPSHOT_BYTES)  # the project's account writes this folder
        data = json.loads(raw) if len(raw) <= MAX_SNAPSHOT_BYTES else None
    except FileNotFoundError:
        data = {}
        guess = root.parent.name if root.name == "ui" else root.name
        if paths.APP_NAME_RE.match(guess):
            data = {"name": guess}
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("name"), str) or not paths.APP_NAME_RE.match(data["name"]):
        raise NotFound(
            f"The dashboard folder {paths.display_path(root)} does not say which project it belongs to yet, so "
            "easy-ui cannot serve it.",
            hint="Run easy-ui data build or easy-ui publish for the project first (they write project.json there), "
            "or pass --project-dir.",
        )
    title = data.get("title")
    title = title.strip() if isinstance(title, str) and title.strip() and len(title) <= 80 and "\n" not in title \
        else data["name"]
    accent = data.get("accent")
    accent = accent.lower() if isinstance(accent, str) and ACCENT_RE.match(accent) else None
    order = data.get("dashboards")
    order = tuple(order) if isinstance(order, list) and len(order) <= MAX_DASHBOARDS and all(
        isinstance(x, str) and SLUG_RE.match(x) for x in order) and len(set(order)) == len(order) else ()
    version = data.get("version")
    version = version if isinstance(version, str) and VERSION_RE.match(version) else None
    return AppConfig(name=data["name"], title=title, app_dir=root, manifest=f, accent=accent, dashboards=order,
                     version=version, ui_dir=root, from_ui_dir=True)


def write_snapshot(config: AppConfig) -> None:
    """Write ``project.json`` into the ui folder, so the server knows the project without reading its code."""
    if config.from_ui_dir:
        return
    st = config.state
    st.root.mkdir(mode=0o700, parents=True, exist_ok=True)
    body = json.dumps(config.snapshot(), ensure_ascii=False, sort_keys=True, indent=1).encode()
    try:
        if st.project_file.read_bytes() == body:
            return
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=st.root)
    try:
        os.fchmod(fd, 0o440)  # the owner and, on a server, the web account's group
        with os.fdopen(fd, "wb") as fh:
            fh.write(body)
        os.replace(tmp, st.project_file)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def load(app_dir: str | Path | None = None, *, ui_dir: str | Path | None = None,
         auth_dir: str | Path | None = None) -> AppConfig:
    cfg = _load(app_dir, ui_dir=ui_dir)
    if auth_dir is not None:
        cfg = dataclasses.replace(cfg, auth_dir=Path(os.path.abspath(Path(auth_dir).expanduser())))
    return cfg


def _load(app_dir: str | Path | None = None, *, ui_dir: str | Path | None = None) -> AppConfig:
    if app_dir is None:
        found = paths.find_app_dir()
        if found is None:
            if ui_dir is not None:
                return load_snapshot(ui_dir)
            raise _no_project(None)
        root = found
    else:
        root = Path(app_dir).expanduser()
        if root.name == paths.MANIFEST_FILE and root.is_file():
            root = root.parent
    root = root.resolve()
    manifest = root / paths.MANIFEST_FILE
    if not manifest.is_file():
        if (root / paths.APP_MANIFEST_FILE).is_file():
            raise _no_project(root)
        raise NotFound(
            f"There is no project.toml in {paths.display_path(root)}, so easy-ui cannot tell which project this is.",
            hint="Pass --project-dir with the project's folder (the one holding project.toml).",
        )
    try:
        raw = paths.read_nofollow(manifest, 1, MAX_PROJECT_TOML_BYTES)
    except OSError as e:
        raise ConfigError(
            "project.toml could not be read (it must be a plain file, not a link), so easy-ui cannot tell which "
            "project this is.",
            hint="Make project.toml an ordinary file in the project's folder.",
            detail=str(e),
        ) from None
    if len(raw) > MAX_PROJECT_TOML_BYTES:
        raise ConfigError(
            f"project.toml is larger than {MAX_PROJECT_TOML_BYTES // 1000} KB, so easy-ui did not read it.",
            hint="A project.toml is a short settings file; move long text elsewhere.",
        )
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(
            f"project.toml has a typing mistake{_where(e)}, so easy-ui cannot read it.",
            hint="Fix that line (your assistant can do it), then try again.",
            detail=str(e),
        ) from None
    except UnicodeDecodeError:
        raise ConfigError("project.toml is not a text file easy-ui can read.", hint="Save it as UTF-8 text.") from None
    cfg = parse(data, app_dir=root, manifest=manifest, sha256=hashlib.sha256(raw).hexdigest())
    if ui_dir is not None:
        cfg = dataclasses.replace(cfg, ui_dir=Path(ui_dir).expanduser().resolve())
    return cfg

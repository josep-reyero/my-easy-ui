"""Reading the parts of a project's ``project.toml`` that easy-ui uses.

easy-ui reads ``project.toml`` itself (it does not depend on my-easy-software).
It reads ``[project]`` (``name``, ``title``, ``version``) and ``[ui]``; every other
table (``[apps]``, ``[jobs]``, ``[storage]``, ...) belongs to other tools and is
ignored. Unknown keys inside ``[ui]`` are errors, so a typo never silently
changes what is shown::

    [project]
    name = "fitness"
    title = "My fitness"

    [ui]
    data = "fitness.sqlite"          # relative to the project's data folder
    tables = ["days", "activities"]  # only these tables/views reach dashboards
    accent = "#2f6fdf"               # optional
    dashboards = ["overview", "sleep"]  # optional: menu order; the first is the landing page
    refresh_after = ["sync"]         # read by mes: rebuild the dashboard data after these jobs

The older ``app.toml`` (with ``[app]``) is no longer read; easy-ui says how to
rename it.

On a server the dashboard server's account cannot read the project's folder at
all, so ``data build`` and ``publish`` also write ``project.json`` into the ui
folder, and ``load(ui_dir=...)`` builds the configuration from that file alone
when there is no ``project.toml`` to read.

Public API
----------
``UI_KEYS``  the keys allowed in ``[ui]``.
``AppConfig`` (frozen dataclass; ``ProjectConfig`` is the same class)
    ``name``, ``title``, ``app_dir`` (Path: the project folder, or the ui folder when
    loaded from ``project.json``), ``manifest`` (Path of project.toml or project.json),
    ``data`` (str | None, the ``[ui] data`` value), ``tables`` (tuple of str),
    ``accent`` (str | None), ``dashboards`` (tuple of slugs: the menu order; the first one
    is the landing page; dashboards not listed follow by name), ``version`` (str | None),
    ``refresh_after`` (tuple of job names, for mes), ``ui_dir`` (Path | None: a ui folder
    given with ``--ui-dir``), ``from_ui_dir`` (bool), ``auth_dir`` (Path | None: a sign-in folder
    given with ``--auth-dir``). Properties: ``dashboards_dir``
    (``<app_dir>/ui/dashboards``), ``data_dir`` (the project's data folder),
    ``state`` (``paths.StatePaths`` of the ui folder; its ``auth`` is the sign-in folder,
    see ``easyui.paths``). Methods: ``source_db()`` (Path of the
    project's SQLite file; raises ``ConfigError`` when ``[ui] data`` is missing),
    ``require_tables()`` (raises ``ConfigError`` when the allowlist is empty),
    ``to_dict()``, ``public()`` (``{name, title, accent}`` for the browser),
    ``snapshot()`` (what ``project.json`` holds).
``load(app_dir=None, *, ui_dir=None, auth_dir=None) -> AppConfig``
    ``app_dir`` is the project's folder or its ``project.toml``; ``None`` means the
    nearest folder at or above the current directory holding ``project.toml``, and
    when there is none and ``ui_dir`` is given, the ``project.json`` in that folder.
    Raises ``NotFound`` when there is none and ``ConfigError`` when it is wrong.
``parse(data, *, app_dir, manifest) -> AppConfig``
    Checks an already-parsed TOML table.
``write_snapshot(config)``  writes ``project.json`` into the ui folder (atomically).
"""

from __future__ import annotations

import contextlib
import dataclasses
import difflib
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

UI_KEYS = ("data", "tables", "accent", "dashboards", "refresh_after")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}\Z")
MAX_DASHBOARDS = 100
TABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
ACCENT_RE = re.compile(r"^#[0-9a-fA-F]{6}\Z")
JOB_RE = re.compile(r"^[a-z][a-z0-9_-]{0,40}\Z")
VERSION_RE = re.compile(r"^[0-9A-Za-z.+_-]{1,40}\Z")
MAX_TABLES = 64
MAX_SNAPSHOT_BYTES = 64_000

UI_EXAMPLE = '[ui]\ndata = "app.sqlite"\ntables = ["days"]'
PROJECT_EXAMPLE = '[project]\nname = "myproject"\ntitle = "My project"'


@dataclass(frozen=True)
class AppConfig:
    name: str
    title: str
    app_dir: Path
    manifest: Path
    data: str | None = None
    tables: tuple[str, ...] = field(default_factory=tuple)
    accent: str | None = None
    dashboards: tuple[str, ...] = field(default_factory=tuple)
    version: str | None = None
    refresh_after: tuple[str, ...] = field(default_factory=tuple)
    ui_dir: Path | None = None
    from_ui_dir: bool = False
    auth_dir: Path | None = None

    @property
    def dashboards_dir(self) -> Path:
        return self.app_dir / "ui" / "dashboards"

    @property
    def data_dir(self) -> Path:
        return paths.app_data_dir(self.name)

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
        if not self.data:
            raise ConfigError(
                "project.toml does not say which data file the dashboards read, so easy-ui has no data to show.",
                hint=f"Add to project.toml:\n{UI_EXAMPLE}\n(the file name is relative to the project's data folder "
                f"{paths.display_path(self.data_dir)}).",
            )
        return paths.safe_join(self.data_dir, self.data, what="[ui] data in project.toml")

    def require_tables(self) -> tuple[str, ...]:
        if not self.tables:
            raise ConfigError(
                "project.toml does not list any tables for the dashboards, so no data would be copied for them.",
                hint='List the tables or views dashboards may read in project.toml, for example: tables = ["days"] '
                "under [ui]. Run: easy-ui tables --source to see what the project's data file has.",
            )
        return self.tables

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
            "data": self.data,
            "tables": list(self.tables),
            "accent": self.accent,
            "dashboards": list(self.dashboards),
            "version": self.version,
            "refresh_after": list(self.refresh_after),
        }


ProjectConfig = AppConfig


def _where(e: tomllib.TOMLDecodeError) -> str:
    m = re.search(r"line (\d+)", str(e))
    return f" on line {m.group(1)}" if m else ""


def _unknown_key(table: str, key: str, allowed: tuple[str, ...]) -> ConfigError:
    close = difflib.get_close_matches(key, allowed, n=1)
    did = f' Did you mean "{close[0]}"?' if close else ""
    return ConfigError(
        f'project.toml has a setting "{key}" under [{table}] that easy-ui does not know, so it cannot be sure '
        f"what you meant.{did}",
        hint=f"The settings allowed under [{table}] are: {', '.join(allowed)}.",
    )


def _name_list(ui: dict[str, Any], key: str, pattern: re.Pattern[str], example: str, what: str, one: str) -> tuple[str, ...]:
    raw = ui.get(key, [])
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise ConfigError(f"[ui] {key} in project.toml must be a list of {what}.", hint=f"For example: {key} = {example}")
    bad = [x for x in raw if not pattern.match(x)]
    if bad:
        raise ConfigError(f"[ui] {key} in project.toml has {bad[0]!r}, which is not a {one}.", hint=f"For example: {key} = {example}")
    if len(raw) > MAX_DASHBOARDS or len(set(raw)) != len(raw):
        raise ConfigError(f"[ui] {key} in project.toml lists a name more than once (or more than 100).", hint="List each one once.")
    return tuple(raw)


def parse(data: dict[str, Any], *, app_dir: Path, manifest: Path) -> AppConfig:
    project = data.get("project")
    if not isinstance(project, dict):
        if isinstance(data.get("app"), dict):
            raise ConfigError(
                "project.toml has an [app] table, but easy-ui now reads [project], so it cannot tell which project "
                "this is.",
                hint="Rename the [app] table to [project] (the keys name and title stay the same).",
            )
        raise ConfigError(
            "project.toml has no [project] table, so easy-ui cannot tell which project this is.",
            hint=f"Add to project.toml:\n{PROJECT_EXAMPLE}",
        )
    name = project.get("name")
    if not isinstance(name, str) or not paths.APP_NAME_RE.match(name):
        raise ConfigError(
            f"The project name in project.toml ({name!r}) is missing or not valid, so easy-ui cannot find the "
            "project's folders.",
            hint="Set [project] name to 2 to 31 lowercase letters, digits and dashes, starting with a letter.",
        )
    title = project.get("title", name)
    if not isinstance(title, str) or not title.strip() or len(title) > 80:
        raise ConfigError(
            "The [project] title in project.toml must be a short piece of text (up to 80 characters).",
            hint='For example: title = "My fitness".',
        )
    version = project.get("version")
    if version is not None and (not isinstance(version, str) or not VERSION_RE.match(version)):
        raise ConfigError(
            "The [project] version in project.toml must be a short version text such as \"0.1.0\".",
            hint='For example: version = "0.1.0", or remove the line.',
        )

    ui = data.get("ui", {})
    if not isinstance(ui, dict):
        raise ConfigError("[ui] in project.toml must be a table.", hint=f"For example:\n{UI_EXAMPLE}")
    for key in ui:
        if key not in UI_KEYS:
            raise _unknown_key("ui", key, UI_KEYS)

    db = ui.get("data")
    if db is not None:
        if not isinstance(db, str):
            raise ConfigError('[ui] data in project.toml must be a file name in quotes.', hint='For example: data = "app.sqlite"')
        paths.safe_join(Path("/"), db, what="[ui] data in project.toml")

    tables_raw = ui.get("tables", [])
    if not isinstance(tables_raw, list) or not all(isinstance(t, str) for t in tables_raw):
        raise ConfigError(
            "[ui] tables in project.toml must be a list of table names.", hint='For example: tables = ["days", "activities"]'
        )
    bad = [t for t in tables_raw if not TABLE_NAME_RE.match(t)]
    if bad:
        raise ConfigError(
            f"[ui] tables in project.toml has a name easy-ui cannot use: {bad[0]!r}.",
            hint="Table names use letters, digits and underscores, starting with a letter or underscore.",
        )
    if len(tables_raw) > MAX_TABLES:
        raise ConfigError(f"[ui] tables lists more than {MAX_TABLES} tables.", hint="List only what dashboards need.")
    dupes = sorted({t for t in tables_raw if tables_raw.count(t) > 1})
    if dupes:
        raise ConfigError(f"[ui] tables lists {dupes[0]!r} more than once.", hint="List each table once.")

    accent = ui.get("accent")
    if accent is not None and (not isinstance(accent, str) or not ACCENT_RE.match(accent)):
        raise ConfigError(
            f"[ui] accent in project.toml is {accent!r}, which is not a colour easy-ui understands.",
            hint='Use a colour written as # and six hex digits, for example accent = "#2f6fdf", or remove the line.',
        )
    order = _name_list(
        ui, "dashboards", SLUG_RE, '["overview", "sleep"] (the first one is the page that opens first)',
        "dashboard names (the file names without .yaml)", "dashboard name (the file name without .yaml)",
    )
    refresh_after = _name_list(ui, "refresh_after", JOB_RE, '["sync"]', "job names", "job name")
    return AppConfig(
        name=name,
        title=title.strip(),
        app_dir=app_dir,
        manifest=manifest,
        data=db,
        tables=tuple(tables_raw),
        accent=accent.lower() if accent else None,
        dashboards=order,
        version=version,
        refresh_after=refresh_after,
    )


def _no_project(where: Path | None) -> NotFound:
    old = paths.find_old_manifest(where)
    if old is not None:
        return NotFound(
            f"There is an app.toml in {paths.display_path(old.parent)}, but easy-ui now reads project.toml, so it "
            "does not know which project this is.",
            hint="Rename app.toml to project.toml and its [app] table to [project]; everything else stays the same.",
            code="old_manifest",
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
    fake = {"project": {"name": data["name"], "title": data.get("title") or data["name"]}, "ui": {}}
    if isinstance(data.get("accent"), str):
        fake["ui"]["accent"] = data["accent"]
    if isinstance(data.get("dashboards"), list):
        fake["ui"]["dashboards"] = data["dashboards"]
    if isinstance(data.get("version"), str):
        fake["project"]["version"] = data["version"]
    try:
        cfg = parse(fake, app_dir=root, manifest=f)
    except ConfigError:
        cfg = parse({"project": {"name": data["name"]}}, app_dir=root, manifest=f)
    return dataclasses.replace(cfg, ui_dir=root, from_ui_dir=True)


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
        if (root / paths.OLD_MANIFEST_FILE).is_file():
            raise _no_project(root)
        raise NotFound(
            f"There is no project.toml in {paths.display_path(root)}, so easy-ui cannot tell which project this is.",
            hint="Pass --project-dir with the project's folder (the one holding project.toml).",
        )
    try:
        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(
            f"project.toml has a typing mistake{_where(e)}, so easy-ui cannot read it.",
            hint="Fix that line (your assistant can do it), then try again.",
            detail=str(e),
        ) from None
    except UnicodeDecodeError:
        raise ConfigError("project.toml is not a text file easy-ui can read.", hint="Save it as UTF-8 text.") from None
    cfg = parse(data, app_dir=root, manifest=manifest)
    if ui_dir is not None:
        cfg = dataclasses.replace(cfg, ui_dir=Path(ui_dir).expanduser().resolve())
    return cfg

"""Reading the parts of an app's ``app.toml`` that easy-ui uses.

easy-ui reads ``app.toml`` itself (it does not depend on my-easy-software).
It reads ``[app]`` (``name``, ``title``) and ``[ui]``; every other table (for
example ``[jobs]``) belongs to other tools and is ignored. Unknown keys inside
``[ui]`` are errors, so a typo never silently changes what is shown::

    [app]
    name = "fitness"
    title = "My fitness"

    [ui]
    data = "fitness.sqlite"          # relative to the app's data folder
    tables = ["days", "activities"]  # only these tables/views reach dashboards
    accent = "#2f6fdf"               # optional
    dashboards = ["overview", "sleep"]  # optional: menu order; the first opens at the start

Public API
----------
``UI_KEYS``  the keys allowed in ``[ui]``.
``AppConfig`` (frozen dataclass)
    ``name``, ``title``, ``app_dir`` (Path), ``manifest`` (Path of app.toml),
    ``data`` (str | None, the ``[ui] data`` value), ``tables`` (tuple of str),
    ``accent`` (str | None), ``dashboards`` (tuple of slugs: the menu order; the first one
    opens at the start; dashboards not listed follow by name). Properties: ``dashboards_dir``
    (``<app_dir>/ui/dashboards``), ``data_dir`` (the app's data folder),
    ``state`` (``paths.StatePaths``). Methods: ``source_db()`` (Path of the
    app's SQLite file; raises ``ConfigError`` when ``[ui] data`` is missing),
    ``require_tables()`` (raises ``ConfigError`` when the allowlist is empty),
    ``to_dict()``, ``public()`` (``{name, title, accent}`` for the browser).
``load(app_dir=None) -> AppConfig``
    ``app_dir`` is the app's folder or its ``app.toml``; ``None`` means the
    nearest folder at or above the current directory holding ``app.toml``.
    Raises ``NotFound`` when there is none and ``ConfigError`` when it is wrong.
``parse(data, *, app_dir, manifest) -> AppConfig``
    Checks an already-parsed TOML table.
"""

from __future__ import annotations

import difflib
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from easyui import paths
from easyui.errors import ConfigError, NotFound

UI_KEYS = ("data", "tables", "accent", "dashboards")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}\Z")
MAX_DASHBOARDS = 100
TABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
ACCENT_RE = re.compile(r"^#[0-9a-fA-F]{6}\Z")
MAX_TABLES = 64

UI_EXAMPLE = '[ui]\ndata = "app.sqlite"\ntables = ["days"]'


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

    @property
    def dashboards_dir(self) -> Path:
        return self.app_dir / "ui" / "dashboards"

    @property
    def data_dir(self) -> Path:
        return paths.app_data_dir(self.name)

    @property
    def state(self) -> paths.StatePaths:
        return paths.state_paths(self.name)

    def source_db(self) -> Path:
        if not self.data:
            raise ConfigError(
                "app.toml does not say which data file the dashboards read, so easy-ui has no data to show.",
                hint=f"Add to app.toml:\n{UI_EXAMPLE}\n(the file name is relative to the app's data folder "
                f"{paths.display_path(self.data_dir)}).",
            )
        return paths.safe_join(self.data_dir, self.data, what="[ui] data in app.toml")

    def require_tables(self) -> tuple[str, ...]:
        if not self.tables:
            raise ConfigError(
                "app.toml does not list any tables for the dashboards, so no data would be copied for them.",
                hint='List the tables or views dashboards may read in app.toml, for example: tables = ["days"] '
                "under [ui]. Run: easy-ui tables --source to see what the app's data file has.",
            )
        return self.tables

    def public(self) -> dict[str, Any]:
        return {"name": self.name, "title": self.title, "accent": self.accent}

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "app_dir": str(self.app_dir),
            "data": self.data,
            "tables": list(self.tables),
            "accent": self.accent,
            "dashboards": list(self.dashboards),
        }


def _where(e: tomllib.TOMLDecodeError) -> str:
    m = re.search(r"line (\d+)", str(e))
    return f" on line {m.group(1)}" if m else ""


def _unknown_key(table: str, key: str, allowed: tuple[str, ...]) -> ConfigError:
    close = difflib.get_close_matches(key, allowed, n=1)
    did = f' Did you mean "{close[0]}"?' if close else ""
    return ConfigError(
        f'app.toml has a setting "{key}" under [{table}] that easy-ui does not know, so it cannot be sure '
        f"what you meant.{did}",
        hint=f"The settings allowed under [{table}] are: {', '.join(allowed)}.",
    )


def parse(data: dict[str, Any], *, app_dir: Path, manifest: Path) -> AppConfig:
    app = data.get("app")
    if not isinstance(app, dict):
        raise ConfigError(
            "app.toml has no [app] table, so easy-ui cannot tell which app this is.",
            hint='Add to app.toml:\n[app]\nname = "myapp"\ntitle = "My app"',
        )
    name = app.get("name")
    if not isinstance(name, str) or not paths.APP_NAME_RE.match(name):
        raise ConfigError(
            f"The app name in app.toml ({name!r}) is missing or not valid, so easy-ui cannot find the app's folders.",
            hint="Set [app] name to 2 to 31 lowercase letters, digits and dashes, starting with a letter.",
        )
    title = app.get("title", name)
    if not isinstance(title, str) or not title.strip() or len(title) > 80:
        raise ConfigError(
            "The [app] title in app.toml must be a short piece of text (up to 80 characters).",
            hint='For example: title = "My fitness".',
        )

    ui = data.get("ui", {})
    if not isinstance(ui, dict):
        raise ConfigError("[ui] in app.toml must be a table.", hint=f"For example:\n{UI_EXAMPLE}")
    for key in ui:
        if key not in UI_KEYS:
            raise _unknown_key("ui", key, UI_KEYS)

    db = ui.get("data")
    if db is not None:
        if not isinstance(db, str):
            raise ConfigError('[ui] data in app.toml must be a file name in quotes.', hint='For example: data = "app.sqlite"')
        paths.safe_join(Path("/"), db, what="[ui] data in app.toml")

    tables_raw = ui.get("tables", [])
    if not isinstance(tables_raw, list) or not all(isinstance(t, str) for t in tables_raw):
        raise ConfigError(
            "[ui] tables in app.toml must be a list of table names.", hint='For example: tables = ["days", "activities"]'
        )
    bad = [t for t in tables_raw if not TABLE_NAME_RE.match(t)]
    if bad:
        raise ConfigError(
            f"[ui] tables in app.toml has a name easy-ui cannot use: {bad[0]!r}.",
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
            f"[ui] accent in app.toml is {accent!r}, which is not a colour easy-ui understands.",
            hint='Use a colour written as # and six hex digits, for example accent = "#2f6fdf", or remove the line.',
        )
    order_raw = ui.get("dashboards", [])
    if not isinstance(order_raw, list) or not all(isinstance(d, str) for d in order_raw):
        raise ConfigError(
            "[ui] dashboards in app.toml must be a list of dashboard names (the file names without .yaml).",
            hint='For example: dashboards = ["overview", "sleep"]. The first one opens when the page starts.',
        )
    bad = [d for d in order_raw if not SLUG_RE.match(d)]
    if bad:
        raise ConfigError(
            f"[ui] dashboards in app.toml has {bad[0]!r}, which is not a dashboard name.",
            hint="Use the file name without .yaml: lowercase letters, digits and -, for example \"overview\".",
        )
    if len(order_raw) > MAX_DASHBOARDS or len(set(order_raw)) != len(order_raw):
        raise ConfigError(
            "[ui] dashboards in app.toml lists a dashboard more than once (or more than 100).",
            hint="List each dashboard once, in the order the menu should show them.",
        )
    return AppConfig(
        name=name,
        title=title.strip(),
        app_dir=app_dir,
        manifest=manifest,
        data=db,
        tables=tuple(tables_raw),
        accent=accent.lower() if accent else None,
        dashboards=tuple(order_raw),
    )


def load(app_dir: str | Path | None = None) -> AppConfig:
    if app_dir is None:
        found = paths.find_app_dir()
        if found is None:
            raise NotFound(
                "There is no app here (no app.toml was found in this folder or above), so easy-ui does not know "
                "which app's dashboards to use.",
                hint="Run the command inside the app's folder, or pass --app-dir with the folder that holds app.toml.",
            )
        root = found
    else:
        root = Path(app_dir).expanduser()
        if root.name == paths.MANIFEST_FILE and root.is_file():
            root = root.parent
    root = root.resolve()
    manifest = root / paths.MANIFEST_FILE
    if not manifest.is_file():
        raise NotFound(
            f"There is no app.toml in {paths.display_path(root)}, so easy-ui cannot tell which app this is.",
            hint="Pass --app-dir with the app's folder (the one holding app.toml).",
        )
    try:
        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(
            f"app.toml has a typing mistake{_where(e)}, so easy-ui cannot read it.",
            hint="Fix that line (your assistant can do it), then try again.",
            detail=str(e),
        ) from None
    except UnicodeDecodeError:
        raise ConfigError("app.toml is not a text file easy-ui can read.", hint="Save it as UTF-8 text.") from None
    return parse(data, app_dir=root, manifest=manifest)

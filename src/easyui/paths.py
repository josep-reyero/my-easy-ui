"""Per-machine folders used by easy-ui.

Nothing easy-ui writes lives inside the app's repository:

- app data (read only by easy-ui): ``$MES_DATA_HOME/<app>/``
  (default ``$XDG_DATA_HOME/mes/<app>`` or ``~/.local/share/mes/<app>``)
- UI state: ``$EASYUI_STATE_HOME/<app>/``
  (default ``$XDG_STATE_HOME/easy-ui/<app>`` or ``~/.local/state/easy-ui/<app>``),
  holding ``data/`` (data bundles and the ``current`` link), ``specs/``
  (published dashboard versions) and ``auth/`` (password hash, sessions).

``MES_DATA_HOME`` and ``EASYUI_STATE_HOME`` replace the base folder; the app
name is always appended.

Public API
----------
``APP_NAME_RE``  the app-name pattern (same as mes: ``^[a-z][a-z0-9-]{1,30}$``).
``MANIFEST_FILE`` ``"app.toml"``.
``data_home() -> Path``, ``state_home() -> Path``
    Base folders (not created).
``app_data_dir(app) -> Path``
    ``<data_home>/<app>``; never created by easy-ui (the app owns it).
``StatePaths`` (frozen dataclass)
    ``root``, ``data``, ``specs``, ``auth`` and ``current`` (= ``data/current``).
    ``ensure()`` creates the folders with mode 0700 and returns itself.
``state_paths(app, create=False) -> StatePaths``
``find_app_dir(start=None) -> Path | None``
    Nearest folder at or above ``start`` (default: current directory) holding
    ``app.toml``.
``safe_join(base, relative, *, what) -> Path``
    Joins a relative path below ``base``; raises ``ConfigError`` for absolute
    paths or paths that climb out with ``..``.
``display_path(path, base=None) -> str``
    A short form for messages: relative to ``base`` when inside it, ``~/...``
    when inside the home folder.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from easyui.errors import ConfigError, UsageError

MANIFEST_FILE = "app.toml"
APP_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}\Z")


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else None


def data_home() -> Path:
    return _env_path("MES_DATA_HOME") or (_env_path("XDG_DATA_HOME") or Path.home() / ".local/share") / "mes"


def state_home() -> Path:
    return _env_path("EASYUI_STATE_HOME") or (
        _env_path("XDG_STATE_HOME") or Path.home() / ".local/state"
    ) / "easy-ui"


def _check_app(app: str) -> str:
    if not isinstance(app, str) or not APP_NAME_RE.match(app):
        raise UsageError(
            f"{app!r} is not a valid app name, so easy-ui will not use it as a folder name.",
            hint="App names use 2 to 31 lowercase letters, digits and dashes, starting with a letter. "
            "Fix [app] name in app.toml.",
        )
    return app


def app_data_dir(app: str) -> Path:
    return data_home() / _check_app(app)


@dataclass(frozen=True)
class StatePaths:
    root: Path

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def specs(self) -> Path:
        return self.root / "specs"

    @property
    def auth(self) -> Path:
        return self.root / "auth"

    @property
    def current(self) -> Path:
        return self.data / "current"

    def ensure(self) -> StatePaths:
        for d in (self.root, self.data, self.specs, self.auth):
            d.mkdir(mode=0o700, parents=True, exist_ok=True)
        return self


def state_paths(app: str, create: bool = False) -> StatePaths:
    sp = StatePaths(state_home() / _check_app(app))
    return sp.ensure() if create else sp


def find_app_dir(start: str | Path | None = None) -> Path | None:
    cur = Path(start or Path.cwd()).resolve()
    if cur.is_file():
        cur = cur.parent
    for d in (cur, *cur.parents):
        if (d / MANIFEST_FILE).is_file():
            return d
    return None


def safe_join(base: Path, relative: str, *, what: str) -> Path:
    p = PurePosixPath(relative)
    if not relative or p.is_absolute() or relative.startswith("~") or ".." in p.parts or "\\" in relative:
        raise ConfigError(
            f"{what} is {relative!r}, which is not a plain path inside {display_path(base)}, so easy-ui "
            "will not read it (it only reads files inside the app's own data folder).",
            hint='Use a file name relative to the app\'s data folder, for example "app.sqlite".',
        )
    return base.joinpath(*p.parts)


def display_path(path: str | Path, base: str | Path | None = None) -> str:
    p = Path(path)
    if base is not None:
        try:
            return str(p.resolve().relative_to(Path(base).resolve()))
        except (ValueError, OSError):
            pass
    try:
        return "~/" + str(p.resolve().relative_to(Path.home().resolve()))
    except (ValueError, OSError):
        return str(p)

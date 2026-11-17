"""A project's apps: finding them (``apps.lock.json`` or ``<project>/apps/<package>/``) and reading their ``app.toml``.

easy-ui never imports my-easy-software or any app. It reads, itself, what the
apps contract (my-easy-software ``docs/apps-contract.md``, sections 2.5 and
6.5) gives it:

- ``apps.lock.json`` in the project folder, written by ``mes apps sync``. It is
  refused unless it matches ``project.toml`` exactly (its sha-256, the package
  list in order) and every path in it is an absolute, normalised, real folder
  holding a regular ``app.toml`` whose ``[app] name`` is the one recorded.
  In-project apps must be at ``<project>/apps/<package>``.
- Without a lock, only in-project apps: ``<project>/apps/<package>/app.toml``.
- From each ``app.toml``: ``[app] name`` (and ``title``), ``[data] tables``
  and ``[ui]`` (``dashboards``, ``tables``, ``contributes``). ``[jobs]`` and the
  rest of ``[data]`` belong to my-easy-software, which checks them.

Every file inside an app folder is read through ``AppFolder``: the folder is
opened without following a link at its own name, and everything below it is
reached from that open folder one name at a time, never following a link and
never climbing out (``..``, absolute names and links are refused), with a size
limit per file. An app can therefore never make easy-ui read outside its own
folder.

Public API
----------
``LOCK_FILE`` (``"apps.lock.json"``), ``MAX_LOCK_BYTES``, ``MAX_APP_TOML_BYTES``, ``RESERVED_SHORT_NAMES``,
``APP_KEYS``, ``APP_UI_KEYS``, ``SECTIONS_PATH_RE``, ``APP_TABLE_RE``.
``short_name(app, project) -> str``     the app name without ``<project>-``.
``table_prefix(short) -> str``          ``short`` with ``-`` as ``_``, then ``_``.
``App`` (frozen dataclass): ``package``, ``name``, ``title``, ``short``, ``prefix``, ``folder`` (Path),
    ``in_project``, ``label`` (how its files are named in messages: ``apps/<package>`` or
    ``<package> (installed)``), ``tables`` ([data] tables), ``ui_tables``, ``dashboards`` (slugs),
    ``contributes`` (``((slug, "dashboards/<file>.yaml"), ...)``); ``file_label(rel)``,
    ``contributes_to(slug) -> str | None``, ``open() -> AppFolder``, ``to_dict()``.
``AppFolder(app)`` a context manager: ``read(rel, limit, what) -> bytes | problem dict``,
    ``exists(rel)``, ``fd`` (the open folder), ``stat_tree(names) -> str`` (a fingerprint for the preview).
``load_apps(config) -> tuple[App, ...]``  raises ``ConfigError`` (plain words, naming the file).
``lock_state(config) -> "lock" | "files"``  how the apps were found.
``allowlist(config, apps) -> tuple[str, ...]``  contract 4.6; raises ``ConfigError``.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import stat
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from easyui import paths
from easyui.errors import ConfigError

LOCK_FILE = "apps.lock.json"
APPS_DIR = "apps"
APP_TOML = "app.toml"
MAX_LOCK_BYTES = 64_000
MAX_APP_TOML_BYTES = 64_000
RESERVED_SHORT_NAMES = ("describe", "guide", "apps", "help", "sqlite", "mes")
# Table names that belong to SQLite or to mes itself: no app's tables may start like this.
RESERVED_TABLE_PREFIXES = ("sqlite_", "mes_")
APP_KEYS = ("app", "data", "jobs", "ui")
APP_UI_KEYS = ("dashboards", "tables", "contributes")
APP_TABLE_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}\Z")
SECTIONS_PATH_RE = re.compile(r"^dashboards/[a-z0-9][a-z0-9-]{0,60}\.yaml\Z")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}\Z")
MAX_APP_DASHBOARDS = 100
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

STALE_LOCK = "The list of installed apps (apps.lock.json) is out of date, so the apps cannot be found safely."
SYNC_HINT = "Run: mes apps sync"


def short_name(app: str, project: str) -> str:
    prefix = f"{project}-"
    return app[len(prefix):] if app.startswith(prefix) and len(app) > len(prefix) else app


def table_prefix(short: str) -> str:
    return short.replace("-", "_") + "_"


@dataclass(frozen=True)
class App:
    package: str
    name: str
    title: str
    short: str
    prefix: str
    folder: Path
    in_project: bool
    label: str
    tables: tuple[str, ...] = field(default_factory=tuple)
    ui_tables: tuple[str, ...] = field(default_factory=tuple)
    dashboards: tuple[str, ...] = field(default_factory=tuple)
    contributes: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    def file_label(self, rel: str) -> str:
        return f"{self.label}/{rel}"

    def contributes_to(self, slug: str) -> str | None:
        return next((p for s, p in self.contributes if s == slug), None)

    def open(self) -> AppFolder:
        return AppFolder(self)

    def to_dict(self) -> dict[str, Any]:
        return {"package": self.package, "name": self.name, "title": self.title, "short": self.short,
                "prefix": self.prefix, "location": "in-project" if self.in_project else "installed",
                "folder": self.label, "tables": list(self.tables), "ui_tables": list(self.ui_tables),
                "dashboards": list(self.dashboards), "contributes": {s: p for s, p in self.contributes}}


# ---------------------------------------------------------------- reading inside an app folder


def _problem(label: str, message: str, hint: str | None = None) -> dict[str, Any]:
    d: dict[str, Any] = {"file": label, "line": None, "where": "", "problem": message}
    if hint:
        d["hint"] = hint
    d["level"] = "error"
    return d


def _open_folder(path: Path) -> int:
    """The folder ``path``, opened without following a link at its own name (raises OSError)."""
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise OSError(f"{path} is a link or not a folder")
    return os.open(path, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC)


def _rel_parts(rel: str) -> list[str] | None:
    if not rel or "\\" in rel or "\x00" in rel or rel.startswith("/") or len(rel) > 300:
        return None
    parts = rel.split("/")
    if any(p in ("", ".", "..") or p.startswith(".") for p in parts):
        return None
    return parts


class AppFolder:
    """Every read of one app's folder goes through here: no links, no climbing out, a size limit per file."""

    def __init__(self, app: App) -> None:
        self.app = app
        self.fd = -1

    def __enter__(self) -> AppFolder:
        try:
            self.fd = _open_folder(self.app.folder)
        except OSError as e:
            raise ConfigError(
                f"The folder of the app {self.app.name} ({self.app.label}) could not be opened, so its dashboards "
                "and styles cannot be read.",
                hint="Check that the app is installed (uv sync) and that its folder is a real folder, not a link; "
                "then run: mes apps sync",
                detail=str(e),
            ) from None
        return self

    def __exit__(self, *exc: Any) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def open_dir(self, rel: str) -> int | None:
        """A sub-folder of the app, opened one name at a time without following links; None when missing."""
        parts = _rel_parts(rel)
        if parts is None:
            return None
        fd = os.dup(self.fd)
        try:
            for name in parts:
                try:
                    nfd = os.open(name, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC, dir_fd=fd)
                except OSError:
                    os.close(fd)
                    return None
                os.close(fd)
                fd = nfd
        except BaseException:
            os.close(fd)
            raise
        return fd

    def exists(self, rel: str) -> bool:
        parts = _rel_parts(rel)
        if parts is None:
            return False
        folder = "/".join(parts[:-1])
        dfd = self.open_dir(folder) if folder else os.dup(self.fd)
        if dfd is None:
            return False
        try:
            os.stat(parts[-1], dir_fd=dfd, follow_symlinks=False)
            return True
        except OSError:
            return False
        finally:
            os.close(dfd)

    def read(self, rel: str, limit: int, what: str) -> bytes | dict[str, Any]:
        """A plain file of the app (``dashboards/x.yaml``), or a problem dict saying why it was refused."""
        from easyui import styles  # noqa: PLC0415 - styles uses this module

        label = self.app.file_label(rel)
        parts = _rel_parts(rel)
        if parts is None:
            return _problem(label, f"{label} is not a file name inside the app's folder, so easy-ui does not read it.",
                            "Name files relative to the app's folder, without .. or a leading /.")
        folder = "/".join(parts[:-1])
        dfd = self.open_dir(folder) if folder else os.dup(self.fd)
        if dfd is None:
            return _problem(label, f"{label} does not exist (or a folder on the way is a link, which easy-ui does "
                            "not follow).", "Check the app's app.toml and its files.")
        try:
            try:
                os.stat(parts[-1], dir_fd=dfd, follow_symlinks=False)
            except FileNotFoundError:
                return _problem(label, f"{label} does not exist.", "Check the file name in the app's app.toml.")
            except OSError:
                pass
            return styles.read_at(dfd, parts[-1], label, limit, what)
        finally:
            os.close(dfd)

    def stat_tree(self, names: tuple[str, ...]) -> str:
        """Names, sizes and times of the given top-level entries and everything below them (no links followed)."""
        h = hashlib.sha256()

        def walk(fd: int, depth: int, only: tuple[str, ...] | None) -> None:
            try:
                with os.scandir(fd) as it:
                    entries = sorted(it, key=lambda e: e.name)
            except OSError:
                h.update(b"unreadable")
                return
            for e in entries:
                if only is not None and e.name not in only:
                    continue
                try:
                    s = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                h.update(f"{depth}\0{e.name}\0{s.st_size}\0{s.st_mtime_ns}\0{s.st_ino}\0{s.st_mode}\n".encode())
                if stat.S_ISDIR(s.st_mode) and depth < 4:
                    try:
                        sub = os.open(e.name, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC, dir_fd=fd)
                    except OSError:
                        continue
                    try:
                        walk(sub, depth + 1, None)
                    finally:
                        os.close(sub)

        top = os.dup(self.fd)
        try:
            walk(top, 0, names)
        finally:
            os.close(top)
        return h.hexdigest()


# ---------------------------------------------------------------- finding the apps


_LOCK_CHANGED = ("project.toml changed after apps.lock.json was written",
                 "the apps in apps.lock.json are not the ones [project] apps lists, in that order")


def _stale(why: str) -> ConfigError:
    if why in _LOCK_CHANGED:
        return ConfigError("project.toml changed after the list of apps (apps.lock.json) was written, so easy-ui will "
                           "not guess which apps to read.", hint="Run: mes apps sync (it takes a second), then run "
                           "this command again.", detail=why, code="apps_lock_stale")
    return ConfigError(STALE_LOCK, hint=SYNC_HINT, detail=why, code="apps_lock_stale")


def _in_project_ok(config: Any, pkg: str) -> bool:
    try:
        folder = _in_project_folder(config, pkg)
        st = os.lstat(folder / APP_TOML)
    except (OSError, ConfigError):
        return False
    return stat.S_ISREG(st.st_mode)


def lock_state(config: Any) -> str:
    try:
        os.lstat(Path(config.app_dir) / LOCK_FILE)
    except OSError:
        return "files"
    return "lock"


def _read_lock(config: Any) -> dict[str, Any] | None:
    lock = Path(config.app_dir) / LOCK_FILE
    try:
        os.lstat(lock)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise _stale(f"apps.lock.json could not be read: {e}") from None
    try:
        raw = paths.read_nofollow(lock, 1, MAX_LOCK_BYTES)
    except OSError as e:
        raise _stale(f"apps.lock.json is not a plain file: {e}") from None
    if len(raw) > MAX_LOCK_BYTES:
        raise _stale("apps.lock.json is larger than 64 KB")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise _stale("apps.lock.json is not valid JSON") from None
    if not isinstance(data, dict) or data.get("v") != 1:
        raise _stale('apps.lock.json does not have "v": 1')
    if data.get("project") != config.name:
        raise _stale(f"apps.lock.json is for the project {str(data.get('project'))[:40]!r}, not {config.name!r}")
    if not config.manifest_sha256 or data.get("project_toml_sha256") != config.manifest_sha256:
        raise _stale("project.toml changed after apps.lock.json was written")
    entries = data.get("apps")
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        raise _stale('apps.lock.json has no "apps" list')
    if [e.get("package") for e in entries] != list(config.packages):
        raise _stale("the apps in apps.lock.json are not the ones [project] apps lists, in that order")
    return data


def _check_lock_entry(config: Any, e: dict[str, Any]) -> tuple[Path, bool, str]:
    pkg = e["package"]
    path, name, in_project = e.get("path"), e.get("name"), e.get("in_project")
    if not isinstance(path, str) or not path or _CONTROL_RE.search(path) or not os.path.isabs(path) \
            or os.path.normpath(path) != path or len(path) > 4096:
        raise _stale(f"the path of {pkg} in apps.lock.json is not an absolute, normalised path")
    if not isinstance(name, str) or not paths.APP_NAME_RE.match(name):
        raise _stale(f"the app name of {pkg} in apps.lock.json is not valid")
    if not isinstance(in_project, bool) or not isinstance(e.get("program", ""), str):
        raise _stale(f"the entry of {pkg} in apps.lock.json is incomplete")
    folder = Path(path)
    if in_project and folder != Path(config.app_dir) / APPS_DIR / pkg:
        raise _stale(f"the in-project app {pkg} is not at apps/{pkg} in apps.lock.json")
    return folder, in_project, name


def _in_project_folder(config: Any, pkg: str) -> Path:
    """``<project>/apps/<package>``, checked without following links (the apps folder included)."""
    root = Path(config.app_dir)
    try:
        rfd = os.open(root, os.O_RDONLY | _O_DIRECTORY | _O_CLOEXEC)
    except OSError as e:
        raise ConfigError(f"The project folder {paths.display_path(root)} could not be opened.", detail=str(e)) from None
    try:
        try:
            afd = os.open(APPS_DIR, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC, dir_fd=rfd)
        except OSError:
            afd = -1
        ok = False
        if afd >= 0:
            try:
                pfd = os.open(pkg, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC, dir_fd=afd)
            except OSError:
                pfd = -1
            if pfd >= 0:
                try:
                    ok = stat.S_ISREG(os.stat(APP_TOML, dir_fd=pfd, follow_symlinks=False).st_mode)
                except OSError:
                    ok = False
                finally:
                    os.close(pfd)
            os.close(afd)
    finally:
        os.close(rfd)
    if not ok:
        raise ConfigError(
            f"The app {pkg} is not inside this project's apps folder and the list of installed apps has not been "
            "written yet.",
            hint=SYNC_HINT,
            detail=f"looked for apps/{pkg}/app.toml (a real folder and file, not links)",
            code="app_not_found",
        )
    return root / APPS_DIR / pkg


def _read_app_toml(folder: Path, label: str, *, stale_on_error: bool) -> dict[str, Any]:
    from easyui import styles  # noqa: PLC0415

    try:
        fd = _open_folder(folder)
    except OSError as e:
        if stale_on_error:
            raise _stale(f"{label} is not a real folder: {e}") from None
        raise ConfigError(f"The app folder {label} could not be opened.", hint=SYNC_HINT, detail=str(e)) from None
    try:
        got = styles.read_at(fd, APP_TOML, f"{label}/{APP_TOML}", MAX_APP_TOML_BYTES, "an app.toml")
    finally:
        os.close(fd)
    if isinstance(got, dict):
        if stale_on_error:
            raise _stale(got["problem"])
        raise ConfigError(got["problem"], hint=got.get("hint"))
    try:
        data = tomllib.loads(got.decode("utf-8"))
    except tomllib.TOMLDecodeError as e:
        m = re.search(r"line (\d+)", str(e))
        raise ConfigError(
            f"{label}/{APP_TOML} has a typing mistake{f' on line {m.group(1)}' if m else ''}, so easy-ui cannot read "
            "the app's dashboards.",
            hint="Fix that line (your assistant can do it), then try again.",
            detail=str(e),
        ) from None
    except UnicodeDecodeError:
        raise ConfigError(f"{label}/{APP_TOML} is not UTF-8 text.", hint="Save it as UTF-8 text.") from None
    return data


def _unknown(file: str, table: str, key: Any, allowed: tuple[str, ...]) -> ConfigError:
    close = difflib.get_close_matches(str(key), allowed, n=1)
    did = f' Did you mean "{close[0]}"?' if close else ""
    where = f"under [{table}]" if table else "at the top"
    return ConfigError(
        f'{file} has a setting "{str(key)[:60]}" {where} that easy-ui does not know, so it cannot be sure what was '
        f"meant.{did}",
        hint=f"The settings allowed {where} are: {', '.join(allowed)}.",
        code="config_unknown_key",
    )


def _str_list(file: str, where: str, raw: Any, pattern: re.Pattern[str], what: str, example: str,
              limit: int) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise ConfigError(f"{where} in {file} must be a list of {what}.", hint=f"For example: {example}")
    bad = [x for x in raw if not pattern.match(x)]
    if bad:
        raise ConfigError(f"{where} in {file} has {bad[0][:60]!r}, which is not a valid name.", hint=f"For example: {example}")
    if len(raw) > limit or len(set(raw)) != len(raw):
        raise ConfigError(f"{where} in {file} lists a name more than once (or more than {limit}).", hint="List each one once.")
    return tuple(raw)


def _parse_app(config: Any, pkg: str, folder: Path, in_project: bool, data: dict[str, Any],
               expected_name: str | None) -> App:
    label = f"{APPS_DIR}/{pkg}" if in_project else f"{pkg} (installed)"
    file = f"{label}/{APP_TOML}"
    for key in data:
        if key not in APP_KEYS:
            raise _unknown(file, "", key, APP_KEYS)
    app = data.get("app")
    if not isinstance(app, dict):
        raise ConfigError(f"{file} has no [app] table, so easy-ui cannot tell which app it is.",
                          hint=f'Add:\n[app]\nname = "{config.name}-{pkg.split("_")[-1]}"')
    name = app.get("name")
    if not isinstance(name, str) or not paths.APP_NAME_RE.match(name) or name == "mes":
        raise ConfigError(f"The [app] name in {file} ({str(name)[:60]!r}) is missing or not valid.",
                          hint="Use 2 to 31 lowercase letters, digits and dashes, starting with a letter (not mes).")
    if name == config.name:
        raise ConfigError(f"The app in {file} is called {name!r}, like the project itself, which is not allowed.",
                          hint=f'Name it after what it does, for example "{config.name}-main".')
    if expected_name is not None and name != expected_name:
        raise _stale(f"{file} names the app {name!r}, but apps.lock.json says {expected_name!r}")
    title = app.get("title", name)
    if not isinstance(title, str) or not title.strip() or len(title) > 80 or "\n" in title:
        raise ConfigError(f"The [app] title in {file} must be one line of text (up to 80 characters).",
                          hint='For example: title = "Walks"')
    short = short_name(name, config.name)
    prefix = table_prefix(short)
    if short in RESERVED_SHORT_NAMES:
        raise ConfigError(
            f"The app in {file} would have the short name {short!r}, which the project command or the database keeps "
            "for itself.",
            hint=f"Rename the app, for example {config.name}-{short}-app.",
        )
    if prefix.startswith(RESERVED_TABLE_PREFIXES):
        raise ConfigError(
            f"The tables of the app in {file} would start with {prefix!r}, and names that start with sqlite_ or mes_ "
            "belong to the database itself.",
            hint=f"Rename the app, for example {config.name}-{short.replace('-', '')}x.",
        )
    data_t = data.get("data", {})
    if not isinstance(data_t, dict):
        raise ConfigError(f"[data] in {file} must be a table.", hint=f'For example:\n[data]\ntables = ["{prefix}log"]')
    tables = _str_list(file, "[data] tables", data_t.get("tables"), APP_TABLE_RE, "table names",
                       f'tables = ["{prefix}log"]', 64)
    for t in tables:
        if not t.startswith(prefix) or "__" in t:
            raise ConfigError(
                f"[data] tables in {file} has {t!r}, but the tables of the app {name} must start with {prefix} (so "
                "two apps never use the same name).",
                hint=f"Rename it, for example {prefix}{t.split('_', 1)[-1] if '_' in t else t}.",
            )
    ui = data.get("ui", {})
    if not isinstance(ui, dict):
        raise ConfigError(f"[ui] in {file} must be a table.", hint='For example:\n[ui]\ndashboards = ["walks"]')
    for key in ui:
        if key not in APP_UI_KEYS:
            raise _unknown(file, "ui", key, APP_UI_KEYS)
    dashboards = _str_list(file, "[ui] dashboards", ui.get("dashboards"), SLUG_RE, "dashboard names",
                           f'dashboards = ["{short}"] (each is dashboards/<name>.yaml in the app\'s folder)',
                           MAX_APP_DASHBOARDS)
    ui_tables = _str_list(file, "[ui] tables", ui.get("tables"), APP_TABLE_RE, "table names",
                          f'tables = ["{prefix}log"]', 64)
    for t in ui_tables:
        if t not in tables:
            raise ConfigError(
                f"[ui] tables in {file} lists {t!r}, which is not one of the app's own tables ([data] tables), so "
                "dashboards may not read it.",
                hint=f"Add it to [data] tables, or remove it. The app's tables are: {', '.join(tables) or '(none)'}.",
            )
    contributes_raw = ui.get("contributes", {})
    if not isinstance(contributes_raw, dict):
        raise ConfigError(f"[ui.contributes] in {file} must hold one table per dashboard.",
                          hint='For example:\n[ui.contributes.overview]\nsections = "dashboards/overview-walks.yaml"')
    contributes: list[tuple[str, str]] = []
    own_files = {f"dashboards/{d}.yaml" for d in dashboards}
    for slug, v in contributes_raw.items():
        where = f"[ui.contributes.{str(slug)[:40]}]"
        if not SLUG_RE.match(str(slug)):
            raise ConfigError(f"{where} in {file} does not name a dashboard (lowercase letters, digits and -).",
                              hint="Use the target dashboard's name, for example [ui.contributes.overview].")
        if not isinstance(v, dict):
            raise ConfigError(f"{where} in {file} must be a table with sections = \"dashboards/<file>.yaml\".",
                              hint=f'For example:\n{where}\nsections = "dashboards/{slug}-{short}.yaml"')
        for key in v:
            if key != "sections":
                raise _unknown(file, f"ui.contributes.{slug}", key, ("sections",))
        rel = v.get("sections")
        if not isinstance(rel, str) or not SECTIONS_PATH_RE.match(rel):
            raise ConfigError(
                f"{where} sections in {file} must be a file in the app's dashboards/ folder, like "
                f'"dashboards/{slug}-{short}.yaml".',
                hint="Write the path relative to the app's folder: dashboards/<name>.yaml (lowercase letters, digits "
                "and -).",
            )
        if rel in own_files:
            raise ConfigError(
                f"{where} sections in {file} is {rel}, which is also one of the app's own dashboards.",
                hint="Keep the sections in a file of their own, for example "
                f"dashboards/{slug}-{short}.yaml.",
            )
        contributes.append((slug, rel))
    return App(package=pkg, name=name, title=title.strip(), short=short, prefix=prefix, folder=folder,
               in_project=in_project, label=label, tables=tables, ui_tables=ui_tables, dashboards=dashboards,
               contributes=tuple(contributes))


def _check_together(config: Any, apps: list[App]) -> None:
    seen: dict[str, str] = {}
    for a in apps:
        for word in dict.fromkeys((a.name, a.short)):
            if word in seen:
                raise ConfigError(
                    f"The apps {seen[word]} and {a.name} both use the word {word!r} (as a name or short name), so "
                    "commands and dashboards could not tell them apart.",
                    hint="Rename one of the apps in its app.toml.",
                )
            seen[word] = a.name
    for a in apps:
        for b in apps:
            if a is not b and a.prefix.startswith(b.prefix):
                raise ConfigError(
                    f"The table names of the app {a.name} ({a.prefix}...) start like those of {b.name} ({b.prefix}...), "
                    "so it would be unclear which app a table belongs to.",
                    hint="Rename one of the apps.",
                )


_NOTED: set[tuple[str, str, str]] = set()


def _note_once(config: Any, text: str) -> None:
    """A one-line note on stderr, said once per project and project.toml (the apps are read often)."""
    key = (str(config.app_dir), str(config.manifest_sha256), text)
    if key not in _NOTED:
        _NOTED.add(key)
        sys.stderr.write(text + "\n")


def load_apps(config: Any) -> tuple[App, ...]:
    """The project's apps in ``[project] apps`` order (through apps.lock.json, or the in-project fallback)."""
    try:
        lock = _read_lock(config)
    except ConfigError as e:
        # Out of date only because project.toml changed, and every app is in apps/: read the apps
        # exactly as without a lock (the same rule as mes). A damaged lock is still refused.
        if e.code != "apps_lock_stale" or e.detail not in _LOCK_CHANGED or not all(
                _in_project_ok(config, pkg) for pkg in config.packages):
            raise
        _note_once(config, "Note: apps.lock.json is out of date (project.toml changed), so easy-ui read the apps "
                   "[project] apps lists from apps/ in the project folder. To update the list, run: mes apps sync")
        lock = None
    apps: list[App] = []
    if lock is not None:
        for e in lock["apps"]:
            folder, in_project, name = _check_lock_entry(config, e)
            label = f"{APPS_DIR}/{e['package']}" if in_project else f"{e['package']} (installed)"
            data = _read_app_toml(folder, label, stale_on_error=True)
            apps.append(_parse_app(config, e["package"], folder, in_project, data, name))
    else:
        for pkg in config.packages:
            folder = _in_project_folder(config, pkg)
            data = _read_app_toml(folder, f"{APPS_DIR}/{pkg}", stale_on_error=False)
            apps.append(_parse_app(config, pkg, folder, True, data, None))
    _check_together(config, apps)
    return tuple(apps)


def allowlist(config: Any, apps: tuple[App, ...]) -> tuple[str, ...]:
    """The shared views (file order), then [views], then each app's [ui] tables; or [ui] tables, a subset of it."""
    full: list[str] = list(dict.fromkeys([*config.shared_tables, *config.views,
                                          *(t for a in apps for t in a.ui_tables)]))
    if config.ui_tables is None:
        return tuple(full)
    for t in config.ui_tables:
        if t not in full:
            close = difflib.get_close_matches(t, full, n=1)
            raise ConfigError(
                f"[ui] tables in project.toml lists {t!r}, which is not a shared table, a view or a table an app lets "
                "dashboards read, so it cannot be copied for the dashboards."
                + (f' Did you mean "{close[0]}"?' if close else ""),
                hint=f"Dashboards may read: {', '.join(full) or '(nothing yet)'}. An app's own table needs to be in "
                "its app.toml [ui] tables first.",
                code="config_table_not_allowed",
            )
    return tuple(config.ui_tables)

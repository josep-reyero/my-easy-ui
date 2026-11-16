"""Per-machine folders used by easy-ui, for one project at a time.

A project is one folder with a ``project.toml`` at its root (see
``easyui.appconfig``). Nothing easy-ui writes lives inside the project's
repository.

On a personal machine (no accounts involved):

- the project's data (only read by easy-ui): ``$MES_DATA_HOME/<project>/``
  (default ``$XDG_DATA_HOME/mes/<project>`` or ``~/.local/share/mes/<project>``),
  unless this machine's ``~/.config/mes/config.toml`` (or ``$MES_CONFIG``) says
  ``[projects.<project>] data_dir = "..."``: then that folder, as it is (this is
  how an existing archive is adopted without moving it). ``mes`` honours the
  same setting.
- easy-ui's own folder for the project (the *ui folder*):
  ``$EASYUI_STATE_HOME/<project>/`` when that variable is set, else
  ``$MES_STATE_HOME/<project>/ui`` (default ``$XDG_STATE_HOME/mes/<project>/ui`` or
  ``~/.local/state/mes/<project>/ui``).

On a server (``MES_ROOT`` is set, normally to ``/var/lib/mes``), everything of a
project is under ``$MES_ROOT/<project>/``: the data in ``data/`` and the ui
folder in ``ui/``. The ``data_dir`` override still wins for the data.

The ui folder holds ``data/`` (dashboard data copies and the ``current``
link), ``specs/`` (published dashboard versions) and ``project.json`` (what the
server needs to know about the project, written by ``data build`` and
``publish``, so the server never has to read the project's code). On a server
they are written by the project's account (``mes-<project>``) and only read by
the web account (``mes-<project>-web``), which opens them without following
links (``open_nofollow``): the project's account could otherwise point a link
at a file only the web account may read.

The sign-in folder (password hash, passkeys, recovery codes, sessions) is never
inside the ui folder, because the project's account could rename or replace
anything there. It is, in this order: ``--auth-dir`` when given; on a server
``$MES_ROOT/<project>/auth`` (beside ``ui/``, in the project folder, which only
root may change); next to a ui folder named ``ui`` (``<project>/auth``, the
default layout on a personal machine too); and only for a ui folder chosen
with ``EASYUI_STATE_HOME`` (tests, personal machines) ``<ui>/auth``. Before
using it, the commands that sign people in refuse a sign-in folder that is a
link, is not owned by the account running them, is not mode 0700, or sits in a
folder another account (other than root) can change (``check_auth_dir``). An
old ``ui/auth`` is never moved or read: its contents cannot be trusted, so the
owner sets the password (and adds passkeys) again.

Public API
----------
``APP_NAME_RE`` / ``PROJECT_NAME_RE``  ``^[a-z][a-z0-9-]{1,30}$`` (same as mes).
``MANIFEST_FILE`` ``"project.toml"``; ``OLD_MANIFEST_FILE`` ``"app.toml"`` (no longer read).
``data_home() -> Path``, ``state_home() -> Path``, ``server_root() -> Path | None``
    Base folders (not created).
``config_path() -> Path``                 this machine's mes config file (may not exist).
``machine_config() -> dict``              the parsed file, ``{}`` when missing; ``ConfigError`` when broken.
``data_dir_override(project) -> Path | None``  ``[projects.<project>] data_dir``, checked.
``app_data_dir(project) -> Path``         the project's data folder (override, server layout or base).
``ui_dir(project) -> Path``               the project's ui folder.
``StatePaths(root, auth_root=None)`` (frozen dataclass)
    ``root``, ``data``, ``specs``, ``auth`` (``auth_root``, else ``<root>/auth``), ``current``
    (= ``data/current``), ``project_file`` (= ``project.json``). ``ensure()`` creates the missing
    folders (mode 0700, or 0750 when the ui folder itself lets its group in, as on a server; the
    sign-in folder is always 0700) and returns itself.
``state_paths(project, create=False) -> StatePaths``
``dir_mode(root) -> int``                 0750 for a group-readable ui folder, else 0700.
``make_dir(path, mode)``                  one folder with exactly that mode (whatever the umask).
``find_app_dir(start=None) -> Path | None``
    Nearest folder at or above ``start`` (default: current directory) holding
    ``project.toml``.
``find_old_manifest(start=None) -> Path | None``  the nearest ``app.toml`` (for a helpful message).
``open_nofollow(path, depth, *, directory=False) -> int``
    Opens ``path`` without following a link in its last ``depth`` components (the folder ``depth``
    levels up is the trusted start). Used for everything the server reads under the ui folder,
    which another account (the project's) writes on a server. Raises ``OSError``.
``readlink_nofollow(path, depth) -> str``   the target of the link ``path``, same rule for its folders.
``lstat_nofollow(path, depth) -> os.stat_result``  ``lstat`` of ``path``, same rule for its folders.
``read_nofollow(path, depth, limit) -> bytes``  a regular file's bytes (at most ``limit + 1``).
``auth_dir(project, ui=None) -> Path``     the sign-in folder (see below).
``check_auth_dir(path) -> Path``           refuses an unsafe sign-in folder (``Refused``, plain words).
``safe_join(base, relative, *, what) -> Path``
    Joins a relative path below ``base``; raises ``ConfigError`` for absolute
    paths or paths that climb out with ``..``.
``display_path(path, base=None) -> str``
    A short form for messages: relative to ``base`` when inside it, ``~/...``
    when inside the home folder.
"""

from __future__ import annotations

import contextlib
import os
import re
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from easyui.errors import ConfigError, Refused, UsageError

MANIFEST_FILE = "project.toml"
OLD_MANIFEST_FILE = "app.toml"
PROJECT_FILE = "project.json"
APP_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}\Z")
PROJECT_NAME_RE = APP_NAME_RE


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else None


def data_home() -> Path:
    return _env_path("MES_DATA_HOME") or (_env_path("XDG_DATA_HOME") or Path.home() / ".local/share") / "mes"


def _mes_state_home() -> Path:
    return _env_path("MES_STATE_HOME") or (_env_path("XDG_STATE_HOME") or Path.home() / ".local/state") / "mes"


def state_home() -> Path:
    """The base of the ui folders: ``EASYUI_STATE_HOME``, or the mes state base (then ``<base>/<project>/ui``)."""
    return _env_path("EASYUI_STATE_HOME") or _mes_state_home()


def server_root() -> Path | None:
    """``MES_ROOT`` (the server layout, normally ``/var/lib/mes``), or None on a personal machine."""
    root = _env_path("MES_ROOT")
    if root is None:
        return None
    if not root.is_absolute():
        raise ConfigError(
            f"MES_ROOT is {str(root)!r}, which is not an absolute folder, so easy-ui does not know where the "
            "project's folders are.",
            hint="Set MES_ROOT to an absolute folder such as /var/lib/mes, or unset it on a personal machine.",
        )
    return root


def config_path() -> Path:
    return _env_path("MES_CONFIG") or (_env_path("XDG_CONFIG_HOME") or Path.home() / ".config") / "mes" / "config.toml"


def machine_config() -> dict[str, Any]:
    p = config_path()
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(
            f"This computer's settings file ({display_path(p)}) has a typing mistake, so easy-ui cannot tell where "
            "the project's data is.",
            hint="Fix that file (your assistant can do it), or move it away to use the default folders.",
            detail=str(e),
        ) from None
    return data if isinstance(data, dict) else {}


def _check_app(app: str) -> str:
    if not isinstance(app, str) or not APP_NAME_RE.match(app):
        raise UsageError(
            f"{app!r} is not a valid project name, so easy-ui will not use it as a folder name.",
            hint="Project names use 2 to 31 lowercase letters, digits and dashes, starting with a letter. "
            "Fix [project] name in project.toml.",
        )
    return app


def data_dir_override(project: str) -> Path | None:
    """``[projects.<project>] data_dir`` from this machine's config file, or None."""
    _check_app(project)
    projects = machine_config().get("projects")
    section = projects.get(project) if isinstance(projects, dict) else None
    if not isinstance(section, dict) or "data_dir" not in section:
        return None
    raw = section["data_dir"]
    where = f"[projects.{project}] data_dir in {display_path(config_path())}"
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        raise ConfigError(
            f"{where} must be a folder name in quotes.",
            hint=f'For example:\n[projects.{project}]\ndata_dir = "~/archive/{project}"',
        )
    p = Path(raw).expanduser()
    if not p.is_absolute():
        raise ConfigError(
            f"{where} is {raw!r}, which is not an absolute folder, so easy-ui cannot be sure which folder is meant.",
            hint='Write the whole path, starting with / or ~/, for example data_dir = "~/archive/fitness".',
        )
    return p


def app_data_dir(app: str) -> Path:
    _check_app(app)
    override = data_dir_override(app)
    if override is not None:
        return override
    root = server_root()
    if root is not None:
        return root / app / "data"
    return data_home() / app


def ui_dir(app: str) -> Path:
    _check_app(app)
    root = server_root()
    if root is not None:
        return root / app / "ui"
    base = _env_path("EASYUI_STATE_HOME")
    if base is not None:
        return base / app
    return _mes_state_home() / app / "ui"


def dir_mode(root: Path) -> int:
    """Folders easy-ui creates in a ui folder: 0750 when the folder lets its group in (a server), else 0700."""
    try:
        m = os.stat(root).st_mode
    except OSError:
        return 0o700
    return 0o750 if stat.S_IMODE(m) & 0o050 == 0o050 else 0o700


def make_dir(path: Path, mode: int) -> None:
    """Create one folder with exactly ``mode`` (a umask such as 0077 would otherwise take the group bits away)."""
    try:
        path.mkdir(mode=mode)
    except FileExistsError:
        return
    os.chmod(path, mode)


@dataclass(frozen=True)
class StatePaths:
    root: Path
    auth_root: Path | None = None

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def specs(self) -> Path:
        return self.root / "specs"

    @property
    def auth(self) -> Path:
        return self.auth_root if self.auth_root is not None else self.root / "auth"

    @property
    def current(self) -> Path:
        return self.data / "current"

    @property
    def project_file(self) -> Path:
        return self.root / PROJECT_FILE

    def ensure(self) -> StatePaths:
        if not self.root.exists():
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        shared = dir_mode(self.root)
        for d in (self.data, self.specs):
            make_dir(d, shared)
        # The sign-in folder is always private to the account that runs the server.
        if not self.auth.exists() and not self.auth.is_symlink():
            if not self.auth.parent.exists():
                self.auth.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with contextlib.suppress(OSError):  # e.g. a server's project folder: the administrator makes it
                make_dir(self.auth, 0o700)
        return self


def state_paths(app: str, create: bool = False) -> StatePaths:
    ui = ui_dir(app)
    sp = StatePaths(ui, auth_dir(app, ui))
    return sp.ensure() if create else sp


def auth_dir(app: str, ui: Path | None = None) -> Path:
    """The sign-in folder when ``--auth-dir`` is not given (see the module docstring)."""
    _check_app(app)
    root = server_root()
    if root is not None:
        return root / app / "auth"
    ui = ui if ui is not None else ui_dir(app)
    if ui.name == "ui":
        return ui.parent / "auth"
    return ui / "auth"


def _auth_refused(path: Path, why: str, fix: str) -> Refused:
    return Refused(
        f"The sign-in folder {display_path(path)} {why}, so easy-ui does not trust what is in it and stopped. "
        "Someone else could have put a password, passkeys or sessions there.",
        hint=fix,
        code="auth_dir_unsafe",
    )


def _changeable_by_others(st: os.stat_result) -> bool:
    """A folder another account (not root, not this one) could change: owned by it, or group/other writable
    without the sticky bit (with it, others cannot rename what is not theirs)."""
    if st.st_uid not in (0, os.geteuid()):
        return True
    return bool(st.st_mode & 0o022) and not st.st_mode & stat.S_ISVTX


def check_auth_dir(path: Path) -> Path:
    """Refuse a sign-in folder that is a link, not this account's, not 0700, or in a folder others can change."""
    path = Path(path)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise _auth_refused(path, "does not exist", "Create it, owned by the account that runs the dashboards, with "
                            "mode 0700 (the files from easy-ui units list it), then set the password with "
                            "easy-ui set-password.") from None
    if stat.S_ISLNK(st.st_mode):
        raise _auth_refused(path, "is a link to another place", "Replace the link with a real folder (mode 0700, owned "
                            "by the account that runs the dashboards) and set the password again.")
    if not stat.S_ISDIR(st.st_mode):
        raise _auth_refused(path, "is not a folder", "Replace it with a real folder (mode 0700) and set the password again.")
    if st.st_uid != os.geteuid():
        raise _auth_refused(path, "belongs to another account", "Give it to the account that runs the dashboards "
                            "(chown), make it mode 0700, and set the password again.")
    if stat.S_IMODE(st.st_mode) != 0o700:
        raise _auth_refused(path, f"has mode {stat.S_IMODE(st.st_mode):04o} instead of 0700", f"Run: chmod 700 {path}")
    parent = Path(os.path.realpath(path.parent))
    for d in (parent, *parent.parents):
        try:
            dst = os.stat(d)
        except OSError:
            continue
        if _changeable_by_others(dst):
            raise _auth_refused(path, f"is inside {display_path(d)}, which another account can change",
                                "Keep the sign-in folder in a folder only root or this account can change, for "
                                "example /var/lib/mes/<project>/auth on a server (see easy-ui units).")
    return path


# ---------------------------------------------------------------- reading without following links

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


def _open_dir_chain(path: Path, depth: int) -> tuple[int, str]:
    """An open folder for ``path``'s parent, reached without following links below ``depth`` levels up."""
    path = Path(path)
    parts = path.parts[-depth:] if depth > 0 else ()
    if depth < 1 or len(path.parts) <= depth or any(p in ("", ".", "..") for p in parts):
        raise OSError(f"cannot open {path} safely")
    start = Path(*path.parts[:-depth])
    fd = os.open(start, os.O_RDONLY | _DIRECTORY | _CLOEXEC)
    try:
        for name in parts[:-1]:
            nfd = os.open(name, os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = nfd
    except BaseException:
        os.close(fd)
        raise
    return fd, parts[-1]


def open_nofollow(path: str | Path, depth: int, *, directory: bool = False) -> int:
    dfd, name = _open_dir_chain(Path(path), depth)
    try:
        flags = os.O_RDONLY | _NOFOLLOW | _CLOEXEC | (_DIRECTORY if directory else os.O_NONBLOCK)
        return os.open(name, flags, dir_fd=dfd)
    finally:
        os.close(dfd)


def readlink_nofollow(path: str | Path, depth: int) -> str:
    dfd, name = _open_dir_chain(Path(path), depth)
    try:
        return os.readlink(name, dir_fd=dfd)
    finally:
        os.close(dfd)


def lstat_nofollow(path: str | Path, depth: int) -> os.stat_result:
    dfd, name = _open_dir_chain(Path(path), depth)
    try:
        return os.stat(name, dir_fd=dfd, follow_symlinks=False)
    finally:
        os.close(dfd)


def read_nofollow(path: str | Path, depth: int, limit: int) -> bytes:
    fd = open_nofollow(path, depth)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{path} is not a plain file")
        chunks, total = [], 0
        while total <= limit:
            chunk = os.read(fd, min(65536, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _find(name: str, start: str | Path | None) -> Path | None:
    cur = Path(start or Path.cwd()).resolve()
    if cur.is_file():
        cur = cur.parent
    for d in (cur, *cur.parents):
        if (d / name).is_file():
            return d
    return None


def find_app_dir(start: str | Path | None = None) -> Path | None:
    return _find(MANIFEST_FILE, start)


def find_old_manifest(start: str | Path | None = None) -> Path | None:
    d = _find(OLD_MANIFEST_FILE, start)
    return d / OLD_MANIFEST_FILE if d is not None else None


def safe_join(base: Path, relative: str, *, what: str) -> Path:
    p = PurePosixPath(relative)
    if not relative or p.is_absolute() or relative.startswith("~") or ".." in p.parts or "\\" in relative:
        raise ConfigError(
            f"{what} is {relative!r}, which is not a plain path inside {display_path(base)}, so easy-ui "
            "will not read it (it only reads files inside the project's own data folder).",
            hint='Use a file name relative to the project\'s data folder, for example "app.sqlite".',
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

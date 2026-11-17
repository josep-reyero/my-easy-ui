"""The dashboard data: an immutable copy of the allowlisted tables.

``easy-ui data build`` copies only the tables and views dashboards may read
(contract 4.6: the shared tables' views, the ``[views]``, then each app's
``[ui] tables``; or ``[ui] tables`` in project.toml) from the project's shared
database ``<data folder>/<project>.sqlite`` into a new file under the UI
state folder (``<state>/data/bundle-<time>-<hash>.sqlite``), makes it
read-only (0440; the folders are 0750 or 0700), writes a small JSON description next to it and then atomically
switches the ``current`` link to it. Views are copied as plain tables (their
rows, not their definition), so a dashboard can never reach the tables a view
is built from: a shared table's ``<name>__rows`` never reaches dashboards, only
its merged view does (copied with an index on its key columns, made by easy-ui
from the column names of ``<name>__rows_key``). Old bundles are pruned (the newest ``keep``, default 3, stay;
the current one always stays). When the data has not changed, the current
bundle is kept and nothing is switched.

The project's file is not trusted while it is copied: the connection to it is
read-only, in SQLite's defensive mode, with untrusted schema (functions in its
views must be harmless ones), no extension loading and no attached
databases; an authorizer lets it only read, and only the listed tables and
views (and what a listed view itself reads); SQLite's memory is capped as in the
query sandbox (``sandbox.HEAP_LIMIT``, with ``temp_store = MEMORY``, so sorting
never spills into files); and the copy stops after ``BUILD_TIME_LIMIT_S``
(``DataBuildError``). This reading runs in the easy-ui process itself (not in a
killable worker): the authorizer, the time limit and the memory cap are its limits.
``describe_tables`` (``easy-ui tables``) reads the same way (the same authorizer, letting it read every table
and view of the file; one that uses anything else is listed with ``refused`` and not read), with a time limit
of ``DESCRIBE_TIME_LIMIT_S`` (60 s) and the same memory cap. The new file may grow to at most
``2 x`` the project's file (at least ``MIN_SIZE_LIMIT``). Indexes are made
again from their column lists (never by running the SQL stored in the
project's file); indexes on expressions and partial indexes are left out.
Temporary files of a build that was stopped are removed when the next build
starts, and at once when the build is stopped with SIGTERM, SIGHUP or Ctrl-C.

Public API
----------
``DEFAULT_KEEP``  3.
``BUNDLE_RE``     the bundle file-name pattern.
``BUILD_TIME_LIMIT_S`` (600), ``MIN_SIZE_LIMIT`` (64 MB), ``FILE_MODE`` (0440)
``build(config, *, keep=DEFAULT_KEEP, now=None, time_limit_s=BUILD_TIME_LIMIT_S) -> dict``
    Builds and switches. Returns ``{"bundle", "path", "built_at", "unchanged",
    "previous", "tables": {name: {"rows", "columns", "kind"}}, "pruned",
    "size", "sha256", "tables_added", "tables_removed"}`` (the last two compare
    the tables with those of the previous data, so a widened list of tables is
    shown). Raises ``ConfigError`` (nothing to copy, or the apps cannot be read),
    ``NotFound`` (no data file, or a listed table is missing),
    ``TemporaryFailure`` (another build is running), ``DataBuildError``.
``current_bundle(where) -> Path | None``
    The file the ``current`` link points to, or None. ``where`` is an
    ``AppConfig`` or ``paths.StatePaths``. Only bundle file names inside the
    data folder are ever followed, and neither ``data/`` nor the bundle may be
    a link (the project's account writes this folder; the server only reads it).
``require_current(where) -> Path``
    Like ``current_bundle`` but raises ``NoData`` (plain message + hint).
``list_bundles(where) -> list[dict]``
    Newest first: ``{"bundle", "path", "built_at", "size", "current"}``.
``prune(where, keep=DEFAULT_KEEP) -> list[str]``
    Removes old bundles (never the current one) and stale temporary files.
``bundle_info(path) -> dict``
    The JSON description written next to a bundle (``{}`` when missing).
``describe_tables(db_path, *, samples=3, only=None, immutable=True, samples_for=None) -> list[dict]``
    ``[{"name", "kind", "rows", "columns": [{"name", "type", "samples": [...]}]}]``; a table or view that uses
    anything but plain reading has ``rows: None``, no columns and ``refused`` (why, in plain words).
    for the CLI ``tables`` command. ``only`` limits it to those names; ``samples_for`` (names) gives
    sample values only for those tables (the others get ``"samples_hidden": true``).
``table_change(before, after) -> {"added", "removed"}``  compares two lists of table names.
``data_rev(where) -> str``
    The current bundle's id, or ``"none"``.
``rev(where) -> str``
    16 hex characters that change whenever the data bundle or anything under
    the ``specs/`` folder (published dashboards) changes. ``GET /api/rev``.
    ``specs/`` is opened without following a link and walked through open
    folders, at most ``MAX_REV_ENTRIES`` entries and 4 levels deep.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import difflib
import fcntl
import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from easyui import paths
from easyui.appconfig import AppConfig, write_snapshot
from easyui.errors import DataBuildError, NoData, NotFound, TemporaryFailure

DEFAULT_KEEP = 3
BUILD_TIME_LIMIT_S = 600.0
MIN_SIZE_LIMIT = 64 * 1024 * 1024
FILE_MODE = 0o440
_SOURCE_PRAGMAS = frozenset({"table_info", "table_xinfo", "index_list", "index_xinfo"})
_COLLATIONS = frozenset({"BINARY", "NOCASE", "RTRIM"})
BUNDLE_RE = re.compile(r"^bundle-(\d{8}T\d{12}Z)-([0-9a-f]{12})\.sqlite\Z")
_TYPE_RE = re.compile(r"^[A-Za-z0-9_ (),]{0,40}\Z")
_TMP_PREFIX = ".tmp-"
_BATCH = 1000


def _state(where: AppConfig | paths.StatePaths) -> paths.StatePaths:
    return where.state if isinstance(where, AppConfig) else where


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _fsync_dir(d: Path) -> None:
    with contextlib.suppress(OSError):
        fd = os.open(d, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _write_json_atomic(path: Path, data: Any) -> None:
    tmp = path.with_name(f"{_TMP_PREFIX}{secrets.token_hex(4)}.json")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp, FILE_MODE)
    os.replace(tmp, path)


def _harden(conn: sqlite3.Connection, *, defensive: bool = True) -> sqlite3.Connection:
    """Defensive mode, untrusted schema, no extensions, no attached files: for a file easy-ui does not trust.
    (The new file is made with ``defensive=False``: defensive mode refuses ``journal_mode = OFF``.)"""
    if hasattr(conn, "enable_load_extension"):
        conn.enable_load_extension(False)
    if hasattr(conn, "setconfig"):
        for key, value in (
            ("SQLITE_DBCONFIG_DEFENSIVE", defensive),
            ("SQLITE_DBCONFIG_ENABLE_LOAD_EXTENSION", False),
            ("SQLITE_DBCONFIG_TRUSTED_SCHEMA", False),
            ("SQLITE_DBCONFIG_ENABLE_TRIGGER", False),
            ("SQLITE_DBCONFIG_ENABLE_VIEW", True),
            ("SQLITE_DBCONFIG_DQS_DML", False),
            ("SQLITE_DBCONFIG_DQS_DDL", False),
            ("SQLITE_DBCONFIG_WRITABLE_SCHEMA", False),
            ("SQLITE_DBCONFIG_ENABLE_FTS3_TOKENIZER", False),
        ):
            op = getattr(sqlite3, key, None)
            if op is not None:
                conn.setconfig(op, value)
    op = getattr(sqlite3, "SQLITE_LIMIT_ATTACHED", None)
    if op is not None and defensive:  # (VACUUM of the new file attaches a temporary database itself)
        conn.setlimit(op, 0)
    return conn


def _connect_ro(db: Path, *, immutable: bool) -> sqlite3.Connection:
    """A hardened read-only connection to a file easy-ui does not trust, in this process. Its memory is capped like
    the query sandbox's (``sandbox.HEAP_LIMIT``, process-wide; it only ever lowers the cap) and its sorting and
    temporary results stay in that memory (``temp_store = MEMORY``), never in files."""
    from easyui.sandbox import ensure_heap_limit  # noqa: PLC0415

    ensure_heap_limit()
    uri = "file:" + quote(str(db.resolve())) + "?mode=ro" + ("&immutable=1" if immutable else "")
    conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    try:
        _harden(conn)
        conn.execute("PRAGMA temp_store = MEMORY")
        return conn
    except BaseException:
        conn.close()
        raise


def _out_of_memory(e: BaseException) -> bool:
    return isinstance(e, MemoryError) or "out of memory" in str(e).lower()


class _SourceGuard:
    """The authorizer and time limit for reading the project's own file during a build.

    Only reading is allowed: the listed tables and views, SQLite's list of them (``sqlite_schema``),
    what a listed view itself reads, and the column and index lists of the listed tables. Functions on
    ``sandbox.DENIED_FUNCTIONS`` are refused."""

    def __init__(self, allowed: list[str] | tuple[str, ...], time_limit_s: float) -> None:
        from easyui.sandbox import DENIED_FUNCTIONS  # noqa: PLC0415

        self.allowed = {_lower(t) for t in allowed}
        self.denied_functions = DENIED_FUNCTIONS
        self.deadline = time.monotonic() + float(time_limit_s)
        self.time_limit_s = float(time_limit_s)
        self.refused: list[str] = []

    def authorize(self, action: int, a1: str | None, a2: str | None, db: str | None, src: str | None) -> int:
        ok, deny = sqlite3.SQLITE_OK, sqlite3.SQLITE_DENY
        if action in (sqlite3.SQLITE_SELECT, getattr(sqlite3, "SQLITE_RECURSIVE", -1)):
            return ok
        if action == sqlite3.SQLITE_FUNCTION:
            if (a2 or "").lower() in self.denied_functions:
                self.refused.append(f"the function {a2}()")
                return deny
            return ok
        if action == sqlite3.SQLITE_READ:
            if db not in (None, "main"):
                self.refused.append(f'"{a1}" in another database')
                return deny
            table = _lower(a1 or "")
            if table in self.allowed or table in ("sqlite_master", "sqlite_schema"):
                return ok
            if table.startswith("pragma_") and table[len("pragma_"):] in _SOURCE_PRAGMAS:
                return ok  # the column and index lists, checked again as PRAGMA below
            if src:
                return ok  # read by a view: our own statements name only listed tables and views
            self.refused.append(f'"{a1}", which is not in [ui] tables')
            return deny
        if action == sqlite3.SQLITE_PRAGMA and (a1 or "").lower() in _SOURCE_PRAGMAS:
            return ok
        if action == sqlite3.SQLITE_TRANSACTION:
            return ok  # BEGIN / COMMIT of the build itself: one consistent snapshot
        self.refused.append("something other than reading")
        return deny

    def progress(self) -> int:
        return 1 if time.monotonic() > self.deadline else 0


def _lower(name: str) -> str:
    from easyui.sandbox import ascii_lower  # noqa: PLC0415

    return ascii_lower(name)


# ---------------------------------------------------------------- current link


def current_bundle(where: AppConfig | paths.StatePaths) -> Path | None:
    st = _state(where)
    try:  # data/ and the bundle are reached without following links (another account writes this folder)
        target = paths.readlink_nofollow(st.current, 2)
    except OSError:
        return None
    if not BUNDLE_RE.match(target):
        return None
    p = st.data / target
    try:
        return p if stat.S_ISREG(paths.lstat_nofollow(p, 2).st_mode) else None
    except OSError:
        return None


def require_current(where: AppConfig | paths.StatePaths) -> Path:
    p = current_bundle(where)
    if p is None:
        raise NoData(
            "There is no dashboard data yet, so the dashboards have nothing to show.",
            hint="Build it with: easy-ui data build (it copies the tables dashboards may read from the project's "
            "shared database).",
        )
    return p


def data_rev(where: AppConfig | paths.StatePaths) -> str:
    p = current_bundle(where)
    return p.stem if p else "none"


MAX_REV_ENTRIES = 4000
_REV_DEPTH = 4


def rev(where: AppConfig | paths.StatePaths) -> str:
    st = _state(where)
    h = hashlib.sha256(data_rev(st).encode())
    # specs/ is opened without following a link (another account writes the ui folder on a server) and
    # walked through open folders only, at most MAX_REV_ENTRIES entries and _REV_DEPTH levels deep.
    try:
        top = paths.open_nofollow(st.specs, 1, directory=True)
    except OSError:
        return h.hexdigest()[:16]
    seen = 0
    try:
        for root, dirs, files, rfd in os.fwalk(".", dir_fd=top, follow_symlinks=False):
            depth = 0 if root == "." else root.count("/")
            if depth >= _REV_DEPTH:
                dirs.clear()
            dirs.sort()
            for name in sorted(dirs + files):
                seen += 1
                if seen > MAX_REV_ENTRIES:
                    h.update(b"\0(more)")
                    return h.hexdigest()[:16]
                try:
                    s = os.stat(name, dir_fd=rfd, follow_symlinks=False)
                except OSError:
                    continue
                rel = os.path.normpath(os.path.join(root, name))
                h.update(f"\0{rel}\0{s.st_size}\0{s.st_mtime_ns}".encode())
                if stat.S_ISLNK(s.st_mode):
                    with contextlib.suppress(OSError):
                        h.update(os.readlink(name, dir_fd=rfd).encode())
    except OSError:
        pass
    finally:
        os.close(top)
    return h.hexdigest()[:16]


def bundle_info(path: str | Path) -> dict[str, Any]:
    side = Path(path).with_suffix(".json")
    try:
        return json.loads(paths.read_nofollow(side, 2, 1_000_000).decode("utf-8"))
    except (OSError, ValueError):
        return {}


def list_bundles(where: AppConfig | paths.StatePaths) -> list[dict[str, Any]]:
    st = _state(where)
    cur = current_bundle(st)
    out = []
    if st.data.is_dir():
        for p in sorted(st.data.iterdir(), reverse=True):
            m = BUNDLE_RE.match(p.name)
            if not m or not p.is_file():
                continue
            out.append(
                {
                    "bundle": p.stem,
                    "path": str(p),
                    "built_at": bundle_info(p).get("built_at"),
                    "size": p.stat().st_size,
                    "current": cur is not None and p.name == cur.name,
                }
            )
    return out


def prune(where: AppConfig | paths.StatePaths, keep: int = DEFAULT_KEEP) -> list[str]:
    st = _state(where)
    if not st.data.is_dir():
        return []
    keep = max(1, int(keep))
    cur = current_bundle(st)
    removed: list[str] = []
    bundles = sorted((p for p in st.data.iterdir() if BUNDLE_RE.match(p.name)), reverse=True)
    for p in bundles[keep:]:
        if cur is not None and p.name == cur.name:
            continue
        with contextlib.suppress(OSError):
            p.unlink()
            removed.append(p.stem)
        with contextlib.suppress(OSError):
            p.with_suffix(".json").unlink()
    cutoff = time.time() - 3600
    for p in st.data.iterdir():
        if p.name.startswith(_TMP_PREFIX):
            with contextlib.suppress(OSError):
                if p.lstat().st_mtime < cutoff:
                    p.unlink()
    return removed


# ---------------------------------------------------------------- describing


DESCRIBE_TIME_LIMIT_S = 60.0


def _describe_one(conn: sqlite3.Connection, name: str, kind: str, samples: int, show: set[str] | None) -> dict[str, Any]:
    types = {r[1]: r[2] for r in conn.execute("SELECT * FROM pragma_table_info(?)", (name,))}
    cur = conn.execute(f"SELECT * FROM {_q(name)} LIMIT 0")
    cols = [d[0] for d in cur.description or ()]
    columns = []
    hidden = show is not None and _lower(name) not in show
    for col in cols:
        vals: list[Any] = []
        if samples > 0 and not hidden:
            for (v,) in conn.execute(
                f"SELECT DISTINCT {_q(col)} FROM {_q(name)} WHERE {_q(col)} IS NOT NULL LIMIT ?", (samples,)
            ):
                if isinstance(v, (bytes, bytearray)):
                    v = f"<{len(v)} bytes>"
                elif isinstance(v, str) and len(v) > 60:
                    v = v[:57] + "..."
                vals.append(v)
        columns.append({"name": col, "type": types.get(col) or "", "samples": vals})
    count = conn.execute(f"SELECT count(*) FROM {_q(name)}").fetchone()[0]
    entry: dict[str, Any] = {"name": name, "kind": kind, "rows": count, "columns": columns}
    if hidden:
        entry["samples_hidden"] = True
    return entry


def describe_tables(
    db_path: str | Path,
    *,
    samples: int = 3,
    only: list[str] | tuple[str, ...] | None = None,
    immutable: bool = True,
    samples_for: list[str] | tuple[str, ...] | set[str] | None = None,
) -> list[dict[str, Any]]:
    conn = _connect_ro(Path(db_path), immutable=immutable)
    # The same read-only authorizer as data build: only reading, no function on sandbox.DENIED_FUNCTIONS, no
    # other pragma than the column and index lists. Every table and view of the file may be listed here.
    guard = _SourceGuard((), DESCRIBE_TIME_LIMIT_S)
    conn.set_authorizer(guard.authorize)
    conn.set_progress_handler(guard.progress, 10_000)
    show = {_lower(n) for n in samples_for} if samples_for is not None else None
    try:
        rows = conn.execute(
            "SELECT name, type FROM sqlite_schema WHERE type IN ('table', 'view') "
            "AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' ORDER BY name"
        ).fetchall()
        guard.allowed = {_lower(name) for name, _kind in rows}
        wanted = {n.lower() for n in only} if only is not None else None
        out = []
        for name, kind in rows:
            if wanted is not None and name.lower() not in wanted:
                continue
            try:
                out.append(_describe_one(conn, name, kind, samples, show))
            except sqlite3.DatabaseError as e:
                msg = str(e).lower()
                if not (guard.refused or "not authorized" in msg or "prohibited" in msg or "unsafe use" in msg):
                    raise
                what = guard.refused[0] if guard.refused else "something that is not plain reading"
                guard.refused.clear()
                out.append({"name": name, "kind": kind, "rows": None, "columns": [],
                            "refused": f"it uses {what}, so easy-ui did not read it"})
        return out
    except (sqlite3.OperationalError, MemoryError) as e:
        if _out_of_memory(e):
            raise DataBuildError(
                f"Looking at the data file needed more than {_heap_mb()} MB of memory, so it was stopped.",
                hint="A view in the file may make very large values or sort a great many rows; list fewer tables, or "
                "check the file with the project's own commands.",
            ) from None
        if "interrupt" in str(e).lower():
            raise DataBuildError(
                f"Looking at the data file took longer than {DESCRIBE_TIME_LIMIT_S:g} seconds, so it was stopped.",
                hint="A view in the file may be very slow; list fewer tables, or check the file with the project's "
                "own commands.",
            ) from None
        raise
    finally:
        conn.close()


def table_change(before: Any, after: Any) -> dict[str, list[str]]:
    """Which table names ``after`` adds to or drops from ``before`` (compared without case)."""
    b = {_lower(str(t)): str(t) for t in before or ()}
    a = {_lower(str(t)): str(t) for t in after or ()}
    return {"added": sorted(a[k] for k in a.keys() - b.keys()), "removed": sorted(b[k] for k in b.keys() - a.keys())}


# ---------------------------------------------------------------- building


def _source_objects(src: sqlite3.Connection) -> dict[str, tuple[str, str]]:
    """lower-case name -> (real name, kind) for every table and view in the source."""
    rows = src.execute(
        "SELECT name, type FROM sqlite_schema WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'"
    ).fetchall()
    return {name.lower(): (name, kind) for name, kind in rows}


def _copy_table(src: sqlite3.Connection, dst: sqlite3.Connection, name: str, kind: str) -> dict[str, Any]:
    types = {r[1]: (r[2] or "") for r in src.execute("SELECT * FROM pragma_table_info(?)", (name,))}
    cur = src.execute(f"SELECT * FROM {_q(name)}")
    cols = [d[0] for d in cur.description or ()]
    if not cols:
        raise DataBuildError(f'The table "{name}" has no columns, so it cannot be copied.', hint="Remove it from the tables dashboards read.")
    if len({c.lower() for c in cols}) != len(cols):
        raise DataBuildError(
            f'The {kind} "{name}" has two columns with the same name, so it cannot be copied for the dashboards.',
            hint="Give every column its own name (in a view: use AS), then build again.",
        )
    col_defs = []
    columns = []
    for c in cols:
        t = types.get(c, "")
        if not _TYPE_RE.match(t):
            t = ""
        if not t:  # views have no declared types: use the type of the first value
            row = src.execute(f"SELECT typeof({_q(c)}) FROM {_q(name)} WHERE {_q(c)} IS NOT NULL LIMIT 1").fetchone()
            t = {"integer": "INTEGER", "real": "REAL", "text": "TEXT", "blob": "BLOB"}.get(row[0] if row else "", "")
        col_defs.append(f"{_q(c)} {t}".rstrip())
        columns.append({"name": c, "type": t})
    dst.execute(f"CREATE TABLE {_q(name)} ({', '.join(col_defs)})")
    insert = f"INSERT INTO {_q(name)} VALUES ({', '.join('?' * len(cols))})"
    count = 0
    while True:
        chunk = cur.fetchmany(_BATCH)
        if not chunk:
            break
        dst.executemany(insert, chunk)
        count += len(chunk)
    indexes = 0
    if kind == "view":
        indexes += _index_shared_view(src, dst, name, cols)
    if kind == "table":
        for sql in _index_statements(src, name):
            try:
                dst.execute(sql)
                indexes += 1
            except sqlite3.Error:
                pass  # an index is only a speed-up; the data is complete without it
    return {"kind": kind, "rows": count, "columns": columns, "indexes": indexes}


def _index_shared_view(src: sqlite3.Connection, dst: sqlite3.Connection, name: str, cols: list[str]) -> int:
    """A shared table's view is copied with an index on its key columns (those of ``<name>__rows_key``).

    Only the column names are read (``pragma_index_xinfo``, one of the column and index lists the build's
    authorizer allows); the index is written by easy-ui itself, never from SQL stored in the project's file.
    """
    try:
        key = [
            r[0]
            for r in src.execute(
                "SELECT name FROM pragma_index_xinfo(?) WHERE key = 1 ORDER BY seqno", (f"{name}__rows_key",)
            )
        ]
    except sqlite3.Error:
        return 0
    if not key or len(key) > 4 or any(k is None or k not in cols for k in key):
        return 0
    try:
        dst.execute(f"CREATE INDEX {_q(name + '_key')} ON {_q(name)} ({', '.join(_q(k) for k in key)})")
    except sqlite3.Error:
        return 0
    return 1


def _index_statements(src: sqlite3.Connection, table: str) -> list[str]:
    """CREATE INDEX statements made by easy-ui from the column lists of ``table``'s own indexes.

    The SQL stored in the project's file is never run. Indexes made by a PRIMARY KEY or UNIQUE
    constraint, partial indexes and indexes on expressions (or with other collations) are left out."""
    out = []
    for _seq, iname, unique, origin, partial in src.execute(
        'SELECT seq, name, "unique", origin, partial FROM pragma_index_list(?)', (table,)
    ).fetchall():
        if origin != "c" or partial:
            continue
        parts = []
        for cid, cname, desc, coll in src.execute(
            "SELECT cid, name, desc, coll FROM pragma_index_xinfo(?) WHERE key = 1 ORDER BY seqno", (iname,)
        ).fetchall():
            if cid is None or cid < 0 or cname is None or str(coll).upper() not in _COLLATIONS:
                parts = []
                break
            parts.append(f"{_q(cname)} COLLATE {str(coll).upper()}{' DESC' if desc else ''}")
        if parts:
            out.append(f"CREATE {'UNIQUE ' if unique else ''}INDEX {_q(iname)} ON {_q(table)} ({', '.join(parts)})")
    return out


def _missing_tables_error(missing: list[str], available: list[str]) -> NotFound:
    parts = []
    for t in missing:
        close = difflib.get_close_matches(t, available, n=1)
        parts.append(f'"{t}"' + (f' (did you mean "{close[0]}"?)' if close else ""))
    return NotFound(
        f"Dashboards may read {', '.join(parts)} (a shared table, a view or an app's table), but the project's data "
        "file has no table or view with that name yet, so the dashboard data was not built (the previous data stays "
        "in use).",
        hint=f"Tables and views in the data file: {', '.join(available) or '(none)'}. With my-easy-software, run: mes db "
        "sync (it creates the shared tables and views), then the app's rebuild; or fix the name in project.toml or the "
        "app's app.toml. Then run: easy-ui data build",
    )


def build(
    config: AppConfig,
    *,
    keep: int = DEFAULT_KEEP,
    now: _dt.datetime | None = None,
    time_limit_s: float = BUILD_TIME_LIMIT_S,
) -> dict[str, Any]:
    src_path = config.source_db()
    tables = config.require_tables()
    if not src_path.is_file():
        raise NotFound(
            f"The project's data file {paths.display_path(src_path)} does not exist yet, so there is nothing to copy "
            "for the dashboards.",
            hint="Run the project's jobs that write its data first (with my-easy-software: mes db sync, then the "
            "apps' rebuild jobs).",
        )
    st = config.state.ensure()
    write_snapshot(config)
    lock_path = st.data / ".build.lock"
    lock = open(lock_path, "a")  # noqa: SIM115 - held for the whole build
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TemporaryFailure(
                "Another data build for this project is running right now, so this one did not start.",
                hint="Wait a moment and run easy-ui data build again.",
            ) from None
        _remove_stale_temp(st)  # left by a build that was killed; no other build can be running now
        with paths.cleanup_on_signals():
            return _build_locked(config, src_path, tables, st, keep, now, time_limit_s)
    finally:
        lock.close()


def _remove_stale_temp(st: paths.StatePaths) -> None:
    with contextlib.suppress(OSError):
        for e in os.scandir(st.data):
            if e.name.startswith(_TMP_PREFIX) and (e.is_symlink() or e.is_file(follow_symlinks=False)):
                with contextlib.suppress(OSError):
                    os.unlink(e.path)


def _build_locked(
    config: AppConfig,
    src_path: Path,
    tables: tuple[str, ...],
    st: paths.StatePaths,
    keep: int,
    now: _dt.datetime | None,
    time_limit_s: float = BUILD_TIME_LIMIT_S,
) -> dict[str, Any]:
    now = (now or _dt.datetime.now(_dt.UTC)).astimezone(_dt.UTC)
    tmp = st.data / f"{_TMP_PREFIX}{secrets.token_hex(6)}.sqlite"
    previous = current_bundle(st)
    before_tables = list((bundle_info(previous).get("tables") or {}).keys()) if previous is not None else None
    guard = _SourceGuard(tables, time_limit_s)
    try:
        try:
            src = _connect_ro(src_path, immutable=False)
            src.set_authorizer(guard.authorize)
            src.set_progress_handler(guard.progress, 10_000)
        except sqlite3.Error as e:
            raise DataBuildError(
                f"The project's data file {paths.display_path(src_path)} could not be opened, so the dashboard data "
                "was not built (the previous data stays in use).",
                hint="Check that it is a SQLite file and that this account can read it.",
                detail=str(e),
            ) from None
        try:
            src.execute("BEGIN")  # one consistent snapshot of every table
            objects = _source_objects(src)
            missing = [t for t in tables if t.lower() not in objects]
            if missing:
                raise _missing_tables_error(missing, sorted(n for n, _ in objects.values()))
            dst = _harden(sqlite3.connect(tmp, isolation_level=None), defensive=False)
            try:
                dst.execute("PRAGMA journal_mode = OFF")
                dst.execute("PRAGMA synchronous = OFF")
                # The copy can never grow past twice the project's file (at least MIN_SIZE_LIMIT).
                page = int(dst.execute("PRAGMA page_size").fetchone()[0]) or 4096
                cap = max(MIN_SIZE_LIMIT, 2 * src_path.stat().st_size)
                dst.execute(f"PRAGMA max_page_count = {max(16, cap // page)}")
                dst.execute("BEGIN")
                summary = {}
                for t in tables:
                    real, kind = objects[t.lower()]
                    summary[real] = _copy_table(src, dst, real, kind)
                dst.execute("COMMIT")
                dst.execute("VACUUM")
            finally:
                dst.close()
            src.execute("COMMIT")
        except (sqlite3.Error, MemoryError) as e:
            raise _copy_error(e, guard, cap_mb=max(MIN_SIZE_LIMIT, 2 * src_path.stat().st_size) // 1_000_000) from None
        finally:
            src.close()

        digest = hashlib.sha256(tmp.read_bytes()).hexdigest()
        if previous is not None and BUNDLE_RE.match(previous.name)[2] == digest[:12]:  # type: ignore[index]
            tmp.unlink()
            info = bundle_info(previous)
            return {
                "bundle": previous.stem,
                "path": str(previous),
                "built_at": info.get("built_at"),
                "unchanged": True,
                "previous": previous.stem,
                "tables": summary,
                "pruned": prune(st, keep),
                "size": previous.stat().st_size,
                "sha256": digest,
                "tables_added": [],
                "tables_removed": [],
            }

        stamp = now.strftime("%Y%m%dT%H%M%S") + f"{now.microsecond:06d}Z"
        final = st.data / f"bundle-{stamp}-{digest[:12]}.sqlite"
        os.chmod(tmp, FILE_MODE)
        with open(tmp, "rb") as fh:
            os.fsync(fh.fileno())
        os.replace(tmp, final)
        info = {
            "bundle": final.stem,
            "app": config.name,
            "built_at": now.isoformat().replace("+00:00", "Z"),
            "source": config.data,
            "tables": summary,
            "sha256": digest,
            "size": final.stat().st_size,
        }
        _write_json_atomic(final.with_suffix(".json"), info)
        link_tmp = st.data / f"{_TMP_PREFIX}{secrets.token_hex(4)}.link"
        os.symlink(final.name, link_tmp)
        os.replace(link_tmp, st.current)
        _fsync_dir(st.data)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    change = table_change(before_tables, summary.keys()) if before_tables is not None else {"added": [], "removed": []}
    return {
        **{k: info[k] for k in ("bundle", "built_at", "tables", "size", "sha256")},
        "path": str(final),
        "unchanged": False,
        "previous": previous.stem if previous else None,
        "pruned": prune(st, keep),
        "tables_added": change["added"],
        "tables_removed": change["removed"],
    }


def _heap_mb() -> int:
    from easyui.sandbox import HEAP_LIMIT  # noqa: PLC0415

    return HEAP_LIMIT // 1_000_000


def _copy_error(e: sqlite3.Error | MemoryError, guard: _SourceGuard, *, cap_mb: int) -> DataBuildError:
    if _out_of_memory(e):
        return DataBuildError(
            f"Copying the dashboard data needed more than {_heap_mb()} MB of memory, so it was stopped and nothing was "
            "changed (the previous data stays in use).",
            hint="A listed view may make very large values or sort a great many rows. Make it simpler, or list its "
            "tables instead, then run: easy-ui data build",
            detail=str(e) or "out of memory",
        )
    msg = str(e).lower()
    if guard.refused or "not authorized" in msg or "prohibited" in msg or "unsafe use" in msg:
        what = guard.refused[0] if guard.refused else "something that is not plain reading"
        return DataBuildError(
            f"A listed table or view in the project's data file tried to use {what}, so the dashboard data was not "
            "built (the previous data stays in use). easy-ui only reads the tables and views listed in [ui] tables.",
            hint="Check the view's definition in the project's data file (it may use a function SQLite does not trust "
            "in views), or remove it from [ui] tables in project.toml.",
            detail=str(e),
        )
    if "interrupt" in msg:
        return DataBuildError(
            f"Copying the dashboard data took longer than {guard.time_limit_s:g} seconds, so it was stopped and nothing "
            "was changed (the previous data stays in use).",
            hint="A listed view may be very slow. Make it simpler, or list its tables instead, then run: easy-ui data build",
            detail=str(e),
        )
    if "full" in msg:
        return DataBuildError(
            f"The dashboard data would be larger than {cap_mb} MB (twice the project's data file), so it was not built "
            "(the previous data stays in use).",
            hint="A listed view may make many more rows than its tables have. Make it simpler or remove it from "
            "[ui] tables, then run: easy-ui data build",
            detail=str(e),
        )
    return DataBuildError(
        "The dashboard data could not be copied from the project's data file, so nothing was changed (the "
        "previous data stays in use).",
        hint="Try again; if it keeps failing, check the data file with the project's own commands.",
        detail=str(e),
    )

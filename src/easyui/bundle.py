"""The dashboard data: an immutable copy of the allowlisted tables.

``easy-ui data build`` copies only the tables and views dashboards may read
(contract 4.6: the shared tables' views, the ``[views]``, then each app's
``[ui] tables``; or ``[ui] tables`` in project.toml) from the project's shared
database ``<data folder>/<project>.sqlite`` into a new file under the UI
state folder (``<state>/data/bundle-<time>-<hash>.sqlite``), makes it
read-only, writes a small JSON description next to it and then atomically
switches the ``current`` link to it. Views are copied as plain tables (their
rows, not their definition), so a dashboard can never reach the tables a view
is built from: a shared table's ``<name>__rows`` never reaches dashboards, only
its merged view does (copied with an index on its key columns, made by easy-ui
from the column names of ``<name>__rows_key``). Old bundles are pruned (the newest ``keep``, default 3, stay;
the current one always stays). When the data has not changed, the current
bundle is kept and nothing is switched.

Public API
----------
``DEFAULT_KEEP``  3.
``BUNDLE_RE``     the bundle file-name pattern.
``build(config, *, keep=DEFAULT_KEEP, now=None) -> dict``
    Builds and switches. Returns ``{"bundle", "path", "built_at", "unchanged",
    "previous", "tables": {name: {"rows", "columns", "kind"}}, "pruned",
    "size", "sha256"}``. Raises ``ConfigError`` (nothing to copy, or the apps cannot be read),
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
``describe_tables(db_path, *, samples=3, only=None, immutable=True) -> list[dict]``
    ``[{"name", "kind", "rows", "columns": [{"name", "type", "samples": [...]}]}]``
    for the CLI ``tables`` command. ``only`` limits it to those names.
``data_rev(where) -> str``
    The current bundle's id, or ``"none"``.
``rev(where) -> str``
    16 hex characters that change whenever the data bundle or anything under
    the ``specs/`` folder (published dashboards) changes. ``GET /api/rev``.
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
    os.chmod(tmp, 0o444)
    os.replace(tmp, path)


def _connect_ro(db: Path, *, immutable: bool) -> sqlite3.Connection:
    uri = "file:" + quote(str(db.resolve())) + "?mode=ro" + ("&immutable=1" if immutable else "")
    return sqlite3.connect(uri, uri=True, isolation_level=None)


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


def rev(where: AppConfig | paths.StatePaths) -> str:
    st = _state(where)
    h = hashlib.sha256(data_rev(st).encode())
    if st.specs.is_dir():
        for root, dirs, files in os.walk(st.specs, followlinks=False):
            dirs.sort()
            for name in sorted(dirs + files):
                p = Path(root) / name
                rel = str(p.relative_to(st.specs))
                try:
                    s = p.lstat()
                except OSError:
                    continue
                h.update(f"\0{rel}\0{s.st_size}\0{s.st_mtime_ns}".encode())
                if p.is_symlink():
                    with contextlib.suppress(OSError):
                        h.update(os.readlink(p).encode())
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


def describe_tables(
    db_path: str | Path, *, samples: int = 3, only: list[str] | tuple[str, ...] | None = None, immutable: bool = True
) -> list[dict[str, Any]]:
    conn = _connect_ro(Path(db_path), immutable=immutable)
    try:
        rows = conn.execute(
            "SELECT name, type FROM sqlite_schema WHERE type IN ('table', 'view') "
            "AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' ORDER BY name"
        ).fetchall()
        wanted = {n.lower() for n in only} if only is not None else None
        out = []
        for name, kind in rows:
            if wanted is not None and name.lower() not in wanted:
                continue
            types = {r[1]: r[2] for r in conn.execute("SELECT * FROM pragma_table_info(?)", (name,))}
            cur = conn.execute(f"SELECT * FROM {_q(name)} LIMIT 0")
            cols = [d[0] for d in cur.description or ()]
            columns = []
            for col in cols:
                vals: list[Any] = []
                if samples > 0:
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
            out.append({"name": name, "kind": kind, "rows": count, "columns": columns})
        return out
    finally:
        conn.close()


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
        for (sql,) in src.execute(
            "SELECT sql FROM sqlite_schema WHERE type = 'index' AND tbl_name = ? AND sql IS NOT NULL", (name,)
        ):
            try:
                dst.execute(sql)
                indexes += 1
            except sqlite3.Error:
                pass  # an index is only a speed-up; the data is complete without it
    return {"kind": kind, "rows": count, "columns": columns, "indexes": indexes}


def _index_shared_view(src: sqlite3.Connection, dst: sqlite3.Connection, name: str, cols: list[str]) -> int:
    """A shared table's view is copied with an index on its key columns (those of ``<name>__rows_key``).

    Only the column names are read (``pragma_index_info``); the index is written by easy-ui itself.
    """
    try:
        key = [r[0] for r in src.execute("SELECT name FROM pragma_index_info(?) ORDER BY seqno", (f"{name}__rows_key",))]
    except sqlite3.Error:
        return 0
    if not key or len(key) > 4 or any(k is None or k not in cols for k in key):
        return 0
    try:
        dst.execute(f"CREATE INDEX {_q(name + '_key')} ON {_q(name)} ({', '.join(_q(k) for k in key)})")
    except sqlite3.Error:
        return 0
    return 1


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


def build(config: AppConfig, *, keep: int = DEFAULT_KEEP, now: _dt.datetime | None = None) -> dict[str, Any]:
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
        return _build_locked(config, src_path, tables, st, keep, now)
    finally:
        lock.close()


def _build_locked(
    config: AppConfig,
    src_path: Path,
    tables: tuple[str, ...],
    st: paths.StatePaths,
    keep: int,
    now: _dt.datetime | None,
) -> dict[str, Any]:
    now = (now or _dt.datetime.now(_dt.UTC)).astimezone(_dt.UTC)
    tmp = st.data / f"{_TMP_PREFIX}{secrets.token_hex(6)}.sqlite"
    previous = current_bundle(st)
    try:
        try:
            src = _connect_ro(src_path, immutable=False)
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
            dst = sqlite3.connect(tmp, isolation_level=None)
            try:
                dst.execute("PRAGMA journal_mode = OFF")
                dst.execute("PRAGMA synchronous = OFF")
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
        except sqlite3.Error as e:
            raise DataBuildError(
                "The dashboard data could not be copied from the project's data file, so nothing was changed (the "
                "previous data stays in use).",
                hint="Try again; if it keeps failing, check the data file with the project's own commands.",
                detail=str(e),
            ) from None
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
            }

        stamp = now.strftime("%Y%m%dT%H%M%S") + f"{now.microsecond:06d}Z"
        final = st.data / f"bundle-{stamp}-{digest[:12]}.sqlite"
        os.chmod(tmp, 0o444)
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
    return {
        **{k: info[k] for k in ("bundle", "built_at", "tables", "size", "sha256")},
        "path": str(final),
        "unchanged": False,
        "previous": previous.stem if previous else None,
        "pruned": prune(st, keep),
    }

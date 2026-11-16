"""The query sandbox: the only way easy-ui runs SQL for a dashboard.

Every dashboard query runs against an immutable data bundle (see
``easyui.bundle``), never against the project's own database, and only through
these layers:

1. Static check of the SQL text (``scan_sql``): exactly one statement that
   starts with SELECT, WITH or VALUES; parameters only as ``:name``; no
   unclosed quotes or comments.
2. The file is opened read-only and immutable (``mode=ro&immutable=1``),
   extension loading is switched off, the connection is put in SQLite's
   defensive mode with untrusted schema, double-quoted strings are refused,
   no other database may be attached (``SQLITE_LIMIT_ATTACHED = 0``) and the
   statement size, value size, expression depth and so on are limited.
3. An authorizer allows only SELECT, READ, FUNCTION and RECURSIVE (the
   ``WITH RECURSIVE`` part of a SELECT). READ is allowed only on the bundle's
   own tables plus the ``json_each`` / ``json_tree`` table functions.
   Everything else is denied: ATTACH, PRAGMA (including ``pragma_*`` table
   functions), any write or schema change, transactions, ``sqlite_master``,
   ``dbstat``, ``sqlite_dbpage`` and a short deny-list of functions
   (``DENIED_FUNCTIONS``).
4. Parameters are always bound, never pasted into the SQL. Values must be
   None, int, float or text.
5. A progress handler stops a query that runs longer than the time limit
   (default 2 s), and results stop at the row limit (default 5000) or the
   size limit (default 5 MB); then ``truncated`` is true.

Public API
----------
``Limits(timeout_s=2.0, max_rows=5000, max_bytes=5_000_000)`` (frozen dataclass)
``DEFAULT_LIMITS``
``DENIED_FUNCTIONS``, ``SAFE_TABLE_FUNCTIONS``, ``PARAM_NAME_RE``
``SqlScan`` (dataclass): ``params`` (names in first-use order), ``keyword``
    (first word, upper case), ``problems`` (list of plain-language strings).
``scan_sql(sql) -> SqlScan``
    Static checks only; does not need a bundle. Never raises.
``check_sql(sql, *, name=None) -> list[str]``
    Like ``scan_sql`` but raises ``QueryRefused`` on the first problem; returns
    the parameter names.
``QueryResult`` (dataclass): ``columns``, ``rows`` (list of lists, JSON-safe:
    BLOBs and non-finite floats become None), ``truncated`` (bool),
    ``truncated_reason`` (``"rows"`` | ``"size"`` | None), ``elapsed_ms``, ``size``
    (approximate bytes counted against the size limit).
    ``to_dict()`` gives ``{"columns", "rows", "truncated"}``.
``Sandbox(bundle_path, limits=DEFAULT_LIMITS)``
    A context manager holding one sandboxed connection. Methods:
    ``run(sql, params=None, *, name=None) -> QueryResult`` and
    ``columns(sql, params=None, *, name=None) -> list[str]`` (prepares and
    starts the query to learn its column names, fetches nothing; missing
    parameters are bound as NULL). ``tables`` is the set of readable tables.
``set_heap_limit(nbytes) -> int``
    Sets SQLite's process-wide hard heap limit (``PRAGMA hard_heap_limit``),
    so one heavy query cannot take all memory; returns the previous limit.
    The server should call it once at start-up (e.g. 256 MB). 0 removes it.
``run_query(bundle_path, sql, params=None, *, name=None, limits=DEFAULT_LIMITS) -> QueryResult``
``query_columns(bundle_path, sql, params=None, *, name=None, limits=DEFAULT_LIMITS) -> list[str]``
    One-shot helpers (open, run, close). ``query_columns`` is what validation
    uses to check a dashboard query against the current data.

Errors raised: ``QueryRefused`` (exit 5 / HTTP 400) when a layer refuses the
query, ``QueryTimeout`` (exit 75 / HTTP 504) when the time limit is hit,
``QueryError`` (exit 1 / HTTP 400) when SQLite cannot run it (typo, missing
table or column), ``ParamError`` (HTTP 400) for unusable parameter values,
``NoData`` when the bundle file does not exist.
"""

from __future__ import annotations

import contextlib
import difflib
import math
import os
import re
import sqlite3
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from easyui import paths
from easyui.errors import NoData, ParamError, QueryError, QueryRefused, QueryTimeout

PARAM_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,30}\Z")
DENIED_FUNCTIONS = frozenset(
    {
        "load_extension",
        "readfile",
        "writefile",
        "edit",
        "fts3_tokenizer",
        "zipfile",
        "sqlar_compress",
        "sqlar_uncompress",
        "sqlite_compileoption_get",
        "sqlite_compileoption_used",
    }
)
SAFE_TABLE_FUNCTIONS = frozenset({"json_each", "json_tree"})
ALLOWED_KEYWORDS = ("SELECT", "WITH", "VALUES")
MAX_SQL_LENGTH = 20_000
MAX_PARAM_TEXT = 10_000
PROGRESS_STEPS = 1_000

_LIMITS = {
    "SQLITE_LIMIT_ATTACHED": 0,
    "SQLITE_LIMIT_LENGTH": 5_000_000,
    "SQLITE_LIMIT_SQL_LENGTH": MAX_SQL_LENGTH * 2,
    "SQLITE_LIMIT_COLUMN": 500,
    "SQLITE_LIMIT_EXPR_DEPTH": 200,
    "SQLITE_LIMIT_COMPOUND_SELECT": 50,
    "SQLITE_LIMIT_VDBE_OP": 250_000,
    "SQLITE_LIMIT_FUNCTION_ARG": 32,
    "SQLITE_LIMIT_LIKE_PATTERN_LENGTH": 1_000,
    "SQLITE_LIMIT_VARIABLE_NUMBER": 100,
    "SQLITE_LIMIT_TRIGGER_DEPTH": 1,
    "SQLITE_LIMIT_WORKER_THREADS": 0,
}
_CONFIG = {
    "SQLITE_DBCONFIG_DEFENSIVE": True,
    "SQLITE_DBCONFIG_ENABLE_LOAD_EXTENSION": False,
    "SQLITE_DBCONFIG_TRUSTED_SCHEMA": False,
    "SQLITE_DBCONFIG_DQS_DML": False,
    "SQLITE_DBCONFIG_DQS_DDL": False,
    "SQLITE_DBCONFIG_ENABLE_TRIGGER": False,
    "SQLITE_DBCONFIG_WRITABLE_SCHEMA": False,
    "SQLITE_DBCONFIG_ENABLE_FTS3_TOKENIZER": False,
}

_AUTHORIZER_ACTIONS = (
    "CREATE_INDEX", "CREATE_TABLE", "CREATE_TEMP_INDEX", "CREATE_TEMP_TABLE", "CREATE_TEMP_TRIGGER",
    "CREATE_TEMP_VIEW", "CREATE_TRIGGER", "CREATE_VIEW", "DELETE", "DROP_INDEX", "DROP_TABLE",
    "DROP_TEMP_INDEX", "DROP_TEMP_TABLE", "DROP_TEMP_TRIGGER", "DROP_TEMP_VIEW", "DROP_TRIGGER", "DROP_VIEW",
    "INSERT", "PRAGMA", "READ", "SELECT", "TRANSACTION", "UPDATE", "ATTACH", "DETACH", "ALTER_TABLE",
    "REINDEX", "ANALYZE", "CREATE_VTABLE", "DROP_VTABLE", "FUNCTION", "SAVEPOINT", "RECURSIVE",
)  # fmt: skip
# Authorizer action codes only (result codes such as SQLITE_INTERRUPT share the same numbers).
_ACTION_NAMES = {getattr(sqlite3, f"SQLITE_{n}"): n for n in _AUTHORIZER_ACTIONS if hasattr(sqlite3, f"SQLITE_{n}")}
_WRITE_ACTIONS = {"INSERT", "UPDATE", "DELETE"}


@dataclass(frozen=True)
class Limits:
    timeout_s: float = 2.0
    max_rows: int = 5000
    max_bytes: int = 5_000_000


DEFAULT_LIMITS = Limits()


# ---------------------------------------------------------------- static check


@dataclass
class SqlScan:
    params: list[str] = field(default_factory=list)
    keyword: str = ""
    problems: list[str] = field(default_factory=list)
    ctes: list[str] = field(default_factory=list)


def _cte_names(tokens: list[tuple[str, str]]) -> list[str]:
    """Names defined as ``name [(cols)] AS [NOT] [MATERIALIZED] (``."""
    names: list[str] = []
    for i, (kind, text) in enumerate(tokens):
        if kind not in ("word", "name"):
            continue
        j = i + 1
        if j < len(tokens) and tokens[j] == ("punct", "("):
            depth = 0
            while j < len(tokens):
                if tokens[j] == ("punct", "("):
                    depth += 1
                elif tokens[j] == ("punct", ")"):
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            j += 1
        if j >= len(tokens) or tokens[j][0] != "word" or tokens[j][1].upper() != "AS":
            continue
        j += 1
        while j < len(tokens) and tokens[j][0] == "word" and tokens[j][1].upper() in ("NOT", "MATERIALIZED"):
            j += 1
        if j < len(tokens) and tokens[j] == ("punct", "(") and text.lower() not in names:
            names.append(text.lower())
    return names


_IDENT_START = re.compile(r"[A-Za-z_]")
_IDENT = re.compile(r"[A-Za-z0-9_]*")


def scan_sql(sql: Any) -> SqlScan:
    out = SqlScan()
    if not isinstance(sql, str) or not sql.strip():
        out.problems.append("The query is empty. Write one SELECT statement.")
        return out
    if len(sql) > MAX_SQL_LENGTH:
        out.problems.append(f"The query is longer than {MAX_SQL_LENGTH} characters. Make it shorter.")
        return out
    if "\x00" in sql:
        out.problems.append("The query contains a NUL character, which is not allowed.")
        return out
    i, n = 0, len(sql)
    ended = False  # a ';' has been seen
    other_after_end = False
    seen: list[str] = []
    tokens: list[tuple[str, str]] = []
    while i < n:
        ch = sql[i]
        if ch in " \t\r\n\f\v":
            i += 1
            continue
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        if sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            if j < 0:
                out.problems.append("The query has a comment that starts with /* but never ends with */.")
                return out
            i = j + 2
            continue
        if ended:
            other_after_end = True
            break
        if ch in "'\"`":
            j = i + 1
            while True:
                j = sql.find(ch, j)
                if j < 0:
                    what = "text in single quotes" if ch == "'" else "a quoted name"
                    out.problems.append(f"The query has {what} that is never closed (a missing {ch}).")
                    return out
                if j + 1 < n and sql[j + 1] == ch:
                    j += 2
                    continue
                break
            body = sql[i + 1 : j].replace(ch * 2, ch)
            if ch == "'" and i > 0 and sql[i - 1] in "xX" and tokens and tokens[-1][0] == "word":
                tokens.pop()  # x'ABCD' blob literal
            tokens.append(("str", body) if ch == "'" else ("name", body))
            i = j + 1
            continue
        if ch == "[":
            j = sql.find("]", i + 1)
            if j < 0:
                out.problems.append("The query has a [ that is never closed with ].")
                return out
            tokens.append(("name", sql[i + 1 : j]))
            i = j + 1
            continue
        if ch == ";":
            ended = True
            i += 1
            continue
        if ch == ":" and i + 1 < n and _IDENT_START.match(sql[i + 1]):
            m = _IDENT.match(sql, i + 1)
            name = sql[i + 1 : m.end()]
            if not PARAM_NAME_RE.match(name):
                out.problems.append(
                    f"The query uses the parameter :{name}, but parameter names use lowercase letters, digits "
                    "and _ and start with a letter."
                )
            elif name not in seen:
                seen.append(name)
            i = m.end()
            continue
        if ch in "?@$" or ch == ":":
            m = re.match(r"[?@$:][A-Za-z0-9_]*", sql[i:])
            token = m.group(0) if m else ch
            out.problems.append(
                f"The query uses {token!r} as a parameter. Dashboard queries name their parameters as :name "
                "(a colon and the parameter's name)."
            )
            i += max(1, len(token))
            continue
        if _IDENT_START.match(ch):
            m = _IDENT.match(sql, i)
            word = sql[i : m.end()]
            if not out.keyword:
                out.keyword = word.upper()
            tokens.append(("word", word))
            i = m.end()
            continue
        if not out.keyword:
            out.keyword = ch
        tokens.append(("punct", ch))
        i += 1
    if other_after_end:
        out.problems.append(
            "The query has more than one statement (something follows a ';'). A dashboard query is exactly one "
            "SELECT."
        )
    if out.keyword and out.keyword not in ALLOWED_KEYWORDS:
        out.problems.append(
            f"The query starts with {out.keyword}, but dashboard queries only read data: they must start with "
            "SELECT or WITH."
        )
    elif not out.keyword:
        out.problems.append("The query has no statement in it (only comments). Write one SELECT statement.")
    out.params = seen
    out.ctes = _cte_names(tokens)
    return out


def check_sql(sql: Any, *, name: str | None = None) -> list[str]:
    scan = scan_sql(sql)
    if scan.problems:
        raise QueryRefused(
            f"{_label(name)}cannot be used: {scan.problems[0]}",
            hint="Fix the query in the dashboard file, then run: easy-ui validate",
        )
    return scan.params


def _label(name: str | None) -> str:
    return f'The query "{name}" ' if name else "The query "


# ---------------------------------------------------------------- results


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool = False
    truncated_reason: str | None = None
    elapsed_ms: float = 0.0
    size: int = 0  # approximate bytes of the rows, as counted against max_bytes

    def to_dict(self) -> dict[str, Any]:
        return {"columns": self.columns, "rows": self.rows, "truncated": self.truncated}


def _clean(v: Any) -> Any:
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, (bytes, bytearray, memoryview)):
        return None
    return v


def _size(v: Any) -> int:
    if isinstance(v, str):
        return len(v.encode("utf-8", "surrogatepass")) + 3
    if v is None:
        return 4
    return 12


# ---------------------------------------------------------------- the sandbox


def _check_params(params: dict[str, Any] | None, needed: list[str], name: str | None) -> dict[str, Any]:
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ParamError("The query parameters must be a set of named values.", hint="Send params as a JSON object.")
    out: dict[str, Any] = {}
    for key in needed:
        if key not in params:
            raise QueryError(
                f"{_label(name)}uses the parameter :{key}, but no value was given for it.",
                hint="Declare it under params: in the dashboard file (with a default).",
            )
        v = params[key]
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float, str))):
            raise ParamError(
                f"The value for :{key} is not a number or text, so it cannot be used in a query.",
                hint="Send a number, a piece of text or null.",
            )
        if isinstance(v, int) and not -(2**63) <= v < 2**63:
            raise ParamError(f"The number given for :{key} is too large.", hint="Use a smaller number.")
        if isinstance(v, float) and not math.isfinite(v):
            raise ParamError(f"The number given for :{key} is not a finite number.", hint="Use a normal number.")
        if isinstance(v, str) and (len(v) > MAX_PARAM_TEXT or "\x00" in v):
            raise ParamError(f"The text given for :{key} is too long or not plain text.", hint="Use shorter text.")
        out[key] = v
    return out


def _fd_path(fd: int) -> str:
    """A name for an open file that SQLite can open: the descriptor's entry under /dev/fd (or /proc/self/fd)."""
    return f"/dev/fd/{fd}" if os.path.isdir("/dev/fd") else f"/proc/self/fd/{fd}"


class Sandbox:
    """One sandboxed, read-only connection to a data bundle."""

    def __init__(self, bundle_path: str | Path, limits: Limits = DEFAULT_LIMITS) -> None:
        path = Path(os.path.abspath(bundle_path))
        # The bundle and its folder are opened without following links (on a server another account writes
        # them), and SQLite reads the file through that open descriptor.
        try:
            self._fd = paths.open_nofollow(path, 2)
        except OSError:
            self._fd = -1
        if self._fd < 0 or not stat.S_ISREG(os.fstat(self._fd).st_mode):
            if self._fd >= 0:
                os.close(self._fd)
            raise NoData(
                "There is no dashboard data yet, so the dashboards have nothing to show.",
                hint="Build it with: easy-ui data build",
            )
        self.path = path
        self.limits = limits
        self._deadline = math.inf
        self._denied: list[str] = []
        self._ctes: frozenset[str] = frozenset()
        uri = "file:" + quote(_fd_path(self._fd)) + "?mode=ro&immutable=1"
        try:
            self.conn = sqlite3.connect(uri, uri=True, isolation_level=None, check_same_thread=False)
        except BaseException:
            os.close(self._fd)
            raise
        try:
            if hasattr(self.conn, "enable_load_extension"):
                self.conn.enable_load_extension(False)
            if hasattr(self.conn, "setconfig"):
                for key, value in _CONFIG.items():
                    op = getattr(sqlite3, key, None)
                    if op is not None:
                        self.conn.setconfig(op, value)
            for key, value in _LIMITS.items():
                op = getattr(sqlite3, key, None)
                if op is not None:
                    self.conn.setlimit(op, value)
            self.conn.execute("PRAGMA query_only = ON")
            self.tables = frozenset(
                r[0]
                for r in self.conn.execute(
                    "SELECT name FROM sqlite_schema WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'"
                )
            )
            self._readable = frozenset(t.lower() for t in self.tables) | SAFE_TABLE_FUNCTIONS
            # Column names, for "did you mean" in messages (read before the authorizer is set).
            self.columns_by_table: dict[str, list[str]] = {}
            for t in sorted(self.tables):
                with contextlib.suppress(sqlite3.Error):
                    self.columns_by_table[t] = [r[0] for r in self.conn.execute("SELECT name FROM pragma_table_info(?)", (t,))]
            self.conn.set_authorizer(self._authorize)
            self.conn.set_progress_handler(self._progress, PROGRESS_STEPS)
        except BaseException:
            self.conn.close()
            os.close(self._fd)
            raise

    # --- callbacks -----------------------------------------------------------

    def _authorize(self, action: int, a1: str | None, a2: str | None, db: str | None, _src: str | None) -> int:
        what = _ACTION_NAMES.get(action, str(action))
        if what in ("SELECT", "RECURSIVE"):
            return sqlite3.SQLITE_OK
        if what == "FUNCTION":
            fn = (a2 or "").lower()
            if fn in DENIED_FUNCTIONS:
                self._denied.append(f"use the function {fn}(), which dashboards may not use")
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        if what == "READ":
            table = (a1 or "").lower()
            if db in (None, "main") and table in self._readable:
                return sqlite3.SQLITE_OK
            # "count(*) FROM <cte>" reports a READ of the CTE's name with no column and no database.
            if a2 == "" and db is None and table in self._ctes:
                return sqlite3.SQLITE_OK
            if table.startswith("pragma_"):
                self._denied.append("use a PRAGMA (a database setting), which dashboards may not use")
            else:
                self._denied.append(f'read "{a1}", which is not part of the dashboard data')
            return sqlite3.SQLITE_DENY
        if what == "ATTACH" or what == "DETACH":
            self._denied.append("open another database file (ATTACH)")
        elif what == "PRAGMA":
            self._denied.append(f"use PRAGMA {a1 or ''} (a database setting), which dashboards may not use".replace("  ", " "))
        elif what in _WRITE_ACTIONS:
            self._denied.append(f'change data in "{a1}" ({what}); dashboards only read')
        elif what == "TRANSACTION" or what == "SAVEPOINT":
            self._denied.append("start or end a transaction; dashboards only read")
        else:
            self._denied.append(f"run a {what.replace('_', ' ')} statement; dashboards only read")
        return sqlite3.SQLITE_DENY

    def _progress(self) -> int:
        return 1 if time.monotonic() > self._deadline else 0

    # --- running ---------------------------------------------------------------

    def close(self) -> None:
        self.conn.close()
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def __enter__(self) -> Sandbox:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _execute(self, sql: str, params: dict[str, Any] | None, name: str | None, *, fill_missing: bool) -> sqlite3.Cursor:
        scan = scan_sql(sql)
        if scan.problems:
            raise QueryRefused(
                f"{_label(name)}cannot be used: {scan.problems[0]}",
                hint="Fix the query in the dashboard file, then run: easy-ui validate",
            )
        needed = scan.params
        self._ctes = frozenset(scan.ctes)
        if fill_missing:
            params = {k: (params or {}).get(k) for k in needed}
        bound = _check_params(params, needed, name)
        self._denied = []
        self._deadline = time.monotonic() + self.limits.timeout_s
        try:
            return self.conn.execute(sql, bound)
        except (sqlite3.Error, OverflowError) as e:
            raise self._translate(e, name) from None

    def _translate(self, e: Exception, name: str | None) -> Exception:
        msg = str(e)
        low = msg.lower()
        if self._denied or "not authorized" in low or "prohibited" in low:
            reason = self._denied[0] if self._denied else "do something dashboards are not allowed to do"
            return QueryRefused(
                f"{_label(name)}was refused for safety: it tries to {reason}. Dashboard queries can only read "
                "the dashboard data.",
                hint=f"Rewrite it as one SELECT over these tables: {', '.join(sorted(self.tables)) or '(none)'}.",
                detail=msg,
            )
        if "interrupted" in low:
            return QueryTimeout(
                f"{_label(name)}took longer than {self.limits.timeout_s:g} seconds, so it was stopped to keep the "
                "dashboard responsive.",
                hint="Ask for less data (for example a shorter period) or simplify the query. Try again.",
                detail=msg,
            )
        if isinstance(e, sqlite3.ProgrammingError) and "one statement" in low:
            return QueryRefused(
                f"{_label(name)}has more than one statement. A dashboard query is exactly one SELECT.",
                hint="Remove everything after the first statement.",
                detail=msg,
            )
        m = re.match(r"no such table: (?:main\.)?(.+)", msg)
        if m:
            table = m.group(1)
            close = difflib.get_close_matches(table, sorted(self.tables), n=1)
            did = f' Did you mean "{close[0]}"?' if close else ""
            return QueryError(
                f'{_label(name)}reads a table called "{table}", which is not in the dashboard data.{did}',
                hint=f"The tables available are: {', '.join(sorted(self.tables)) or '(none)'}. To add one, list it "
                "under [ui] tables in project.toml and run: easy-ui data build",
                detail=msg,
            )
        m = re.match(r'no such column: "(.*)" - should this be a string literal', msg, re.S)
        if m:
            return QueryError(
                f'{_label(name)}has "{m.group(1)[:40]}" in double quotes. In SQL, double quotes mean a column '
                "name, and there is no column with that name.",
                hint=f"If it is a piece of text, write it in single quotes: '{m.group(1)[:40]}'.",
                detail=msg,
            )
        m = re.match(r"no such column: (.+)", msg)
        if m:
            col = m.group(1)
            base = col.rsplit(".", 1)[-1]
            known = sorted({c for cols in self.columns_by_table.values() for c in cols})
            close = difflib.get_close_matches(base, known, n=1, cutoff=0.6)
            did = f' Did you mean "{close[0]}"?' if close and close[0] != base else ""
            where = [t for t, cols in self.columns_by_table.items() if close and close[0] in cols]
            hint = (f'"{close[0]}" is a column of {", ".join(where[:3])}. ' if did and where else "") + \
                "See every table and its columns with: easy-ui tables"
            return _with_token(QueryError(
                f'{_label(name)}uses a column called "{col}", which does not exist.{did}',
                hint=hint,
                detail=msg,
            ), col)
        m = re.match(r'near "(.*)": syntax error', msg, re.S)
        if m:
            return _with_token(QueryError(
                f'{_label(name)}has a mistake near "{m.group(1)[:40]}", so it cannot run.',
                hint="Fix the SQL in the dashboard file, then run: easy-ui validate",
                detail=msg,
            ), m.group(1))
        if "out of memory" in low or "too big" in low or "too many" in low or "too large" in low or "too complex" in low or "depth" in low:
            return QueryRefused(
                f"{_label(name)}is too large or complex for a dashboard ({msg}).",
                hint="Simplify the query or ask for less data.",
                detail=msg,
            )
        return QueryError(
            f"{_label(name)}could not run: {msg}.",
            hint="Fix the SQL in the dashboard file, then run: easy-ui validate",
            detail=msg,
        )

    def run(self, sql: str, params: dict[str, Any] | None = None, *, name: str | None = None) -> QueryResult:
        started = time.monotonic()
        cur = self._execute(sql, params, name, fill_missing=False)
        try:
            columns = [d[0] for d in cur.description or ()]
            rows: list[list[Any]] = []
            size = 0
            truncated, reason = False, None
            batch = min(500, self.limits.max_rows + 1)
            while True:
                chunk = cur.fetchmany(batch)
                if not chunk:
                    break
                for row in chunk:
                    if len(rows) >= self.limits.max_rows:
                        truncated, reason = True, "rows"
                        break
                    clean = [_clean(v) for v in row]
                    size += 2 + sum(_size(v) for v in clean)
                    if size > self.limits.max_bytes:
                        truncated, reason = True, "size"
                        break
                    rows.append(clean)
                if truncated:
                    break
        except sqlite3.Error as e:
            raise self._translate(e, name) from None
        finally:
            cur.close()
            self._deadline = math.inf
        elapsed = round((time.monotonic() - started) * 1000, 2)
        return QueryResult(columns, rows, truncated, reason, elapsed, size if reason != "size" else self.limits.max_bytes)

    def columns(self, sql: str, params: dict[str, Any] | None = None, *, name: str | None = None) -> list[str]:
        cur = self._execute(sql, params, name, fill_missing=True)
        try:
            return [d[0] for d in cur.description or ()]
        finally:
            cur.close()
            self._deadline = math.inf


def _with_token(e: QueryError, token: str) -> QueryError:
    """Remember the piece of SQL a message is about, so validation can point at its line."""
    e.token = token  # type: ignore[attr-defined]
    return e


def set_heap_limit(nbytes: int) -> int:
    conn = sqlite3.connect(":memory:")
    try:
        previous = conn.execute("PRAGMA hard_heap_limit").fetchone()[0]
        conn.execute(f"PRAGMA hard_heap_limit = {int(nbytes)}")
        return int(previous)
    finally:
        conn.close()


def run_query(
    bundle_path: str | Path,
    sql: str,
    params: dict[str, Any] | None = None,
    *,
    name: str | None = None,
    limits: Limits = DEFAULT_LIMITS,
) -> QueryResult:
    with Sandbox(bundle_path, limits) as sb:
        return sb.run(sql, params, name=name)


def query_columns(
    bundle_path: str | Path,
    sql: str,
    params: dict[str, Any] | None = None,
    *,
    name: str | None = None,
    limits: Limits = DEFAULT_LIMITS,
) -> list[str]:
    with Sandbox(bundle_path, limits) as sb:
        return sb.columns(sql, params, name=name)

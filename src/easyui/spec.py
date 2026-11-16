"""Dashboard files: loading, validating and compiling ``ui/dashboards/<slug>.yaml``.

A dashboard file is declarative YAML (see ``docs/SPEC.md``): ``spec``,
``title``, optional ``description``, ``params`` (the controls' values),
``queries`` (named read-only SELECTs using ``:param`` placeholders) and
``layout`` (rows of components from a closed set). It never contains
JavaScript, HTML or CSS.

Validation produces problems an agent can act on: the file, the line, the
location (``layout[0][1].query``), what is wrong, the allowed values and the
closest match. Unknown keys are always errors.

Public API
----------
Constants: ``SPEC_VERSION`` (``"easy-ui/v1"``), ``SLUG_RE``, ``NAME_RE``,
``TOP_KEYS``, ``PARAM_TYPES`` (type -> allowed keys), ``COMPONENTS`` (type ->
{"description", "settings": {name: Setting}}), ``LIMITS`` (size limits).

``Problem`` (dataclass): ``file``, ``where``, ``line``, ``message``, ``hint``,
    ``allowed``, ``did_you_mean``, ``level`` (``"error"`` | ``"warning"``), ``place``
    (the location in words: "row 2, first component (line), setting y").
    ``to_dict()``; ``text()`` gives one readable line.
``plain_place(path, data=None)``, ``allowed_text(values)``  helpers for messages.
``Param``, ``Query``, ``Dashboard`` (dataclasses). ``Dashboard`` has ``slug``,
    ``title``, ``description``, ``params`` (dict, file order), ``queries``
    (dict), ``layout`` (list of rows of normalised component dicts),
    ``file``, ``warnings``; ``query(name)`` raises ``NotFound``.

``slug_for(path) -> str``  the slug a file name gives (raises ``SpecError`` when invalid).
``loads(text, *, slug, file="<text>") -> Dashboard``  raises ``SpecError`` (``problems``).
``load_file(path) -> Dashboard``                       raises ``SpecError`` / ``NotFound``.
``check(text, *, slug, file="<text>") -> (Dashboard | None, errors, warnings)``  never raises.
``check_partial(...)``  the same, but with mistakes it gives the parts that passed (for data checks).
``check_with_data(dashboard, bundle_path, *, limits=..., vary=True, pool=None) -> (errors, warnings)``
    Runs every query in the sandbox with the default values to learn its columns,
    then checks every column a component names. With ``vary`` it also runs each
    query with every choice option, each int param's min and max and, for text
    params, empty and the longest allowed text (at most ``VARY_LIMIT_S`` seconds
    in all), so a query that is too slow or fails for another control value is
    usually caught before it is published. (A query that is slow only for one
    particular text cannot be found this way; at run time it is still stopped
    at its time limit.) The queries run in a worker process (``easyui.workers``;
    ``pool`` defaults to ``workers.default_pool()``).
``validate_file(path, *, bundle=None, file_label=None) -> dict``
    ``{"file", "slug", "title", "ok", "errors": [...], "warnings": [...]}``.
``validate_app(config, files=None, *, check_data=True) -> dict``
    Validates the project's dashboards (or ``files``) and, when there is current
    data, checks them against it. ``{"ok", "app", "data": {"checked", "bundle",
    "note"}, "dashboards": [...], "errors", "warnings"}``.
``resolve_params(dashboard, given=None, *, today=None) -> dict``
    Turns values from the browser into typed values; missing ones use the
    default. Raises ``ParamError`` (HTTP 400) for unknown names or bad values.
``compile_dashboard(dashboard, *, today=None) -> dict``
    The form sent to the browser (no SQL). See docs/SPEC.md, "Compiled form".
``run_queries(dashboard, names, params=None, *, bundle_path, limits=..., today=None, errors="raise", total_time_s=None, pool=None) -> dict``
    Runs named queries with typed, bound params: ``{"results": {name: {"columns",
    "rows", "truncated"}}}``. ``total_time_s`` is one time budget shared by all
    the queries (each also keeps ``limits.timeout_s``). They run in a worker process from ``pool``
    (default ``workers.default_pool()``) that is killed when a query outlives its time. With ``errors="inline"`` a failing query gives
    ``{"error": {"code", "message", "hint"}}`` in its place instead of raising.
``default_values(dashboard, *, today=None) -> dict``  every param's default, date rules worked out.
``resolve_date_rule(rule, today) -> date``            ``"today-90d"`` etc.
``parse_markdown(text) -> (blocks, problems)``
    The markdown subset as a JSON tree for DOM rendering (no HTML anywhere).
``catalog() -> dict``  the whole vocabulary for ``easy-ui components``.
"""

from __future__ import annotations

import copy
import dataclasses
import datetime as _dt
import difflib
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from easyui import formats, paths, sandbox, workers
from easyui.errors import EasyUIError, NotFound, ParamError, QueryTimeout, SpecError

SPEC_VERSION = "easy-ui/v1"
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}\Z")
NAME_RE = sandbox.PARAM_NAME_RE
TOP_KEYS = ("spec", "title", "description", "params", "queries", "layout", "theme", "app")
REQUIRED_TOP = ("spec", "title", "layout")
PARAM_TYPES: dict[str, tuple[str, ...]] = {
    "choice": ("type", "label", "default", "options"),
    "int": ("type", "label", "default", "min", "max"),
    "date": ("type", "label", "default"),
    "text": ("type", "label", "default"),
}
QUERY_KEYS = ("sql",)
OPTION_KEYS = ("value", "label")
TABLE_COLUMN_KEYS = ("column", "label", "format")
LIMITS = {
    "file_bytes": 256_000,
    "rows": 50,
    "components_per_row": 6,
    "params": 20,
    "queries": 30,
    "options": 100,
    "y_columns": 8,
    "table_columns": 30,
    "table_limit": 5000,
    "text_param_length": 200,
    "title_length": 120,
    "long_text_length": 500,
    "markdown_length": 10_000,
    "queries_per_request": 50,
}
_INT_RANGE = (-(2**53) + 1, 2**53 - 1)
_DATE_RULE_RE = re.compile(r"^today(?:\s*([+-])\s*(\d{1,5})\s*([dwmy]))?\Z")


# ---------------------------------------------------------------- vocabulary


@dataclass(frozen=True)
class Setting:
    kind: str
    description: str
    required: bool = False
    default: Any = None
    min: int | None = None
    max: int | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"kind": self.kind, "required": self.required, "description": self.description}
        if not self.required:
            d["default"] = self.default
        if self.min is not None:
            d["min"] = self.min
        if self.max is not None:
            d["max"] = self.max
        return d


_TITLE = Setting("text", "Heading shown on the component.", required=True)
_QUERY = Setting("query", "Name of a query under queries: in this file.", required=True)
_FORMAT = Setting("format", "How values look; one of the formats. Not set: numbers as 'number', text unchanged.")
_UNIT = Setting("unit", "Short unit shown next to values on the axis, e.g. 'km' or 'bpm' (up to 12 characters).")
_X = Setting("column", "Column for the horizontal axis (usually a date or a category).", required=True)
_READOUT = Setting(
    "readout",
    "The value shown in the chart's heading: latest (the newest point, or the one under the pointer), pointer "
    "(only while pointing at the chart) or none.",
    default="latest",
)
_PARTIAL = Setting(
    "partial",
    "Marks a period that is still in progress (this week so far): last (the last row) or the name of a column "
    "that is true (1) for those rows. They are drawn lighter (bars) or dashed (lines) and read 'so far'.",
)
_STEP = Setting(
    "step",
    "The gap between the lines across the chart, in the query's own numbers (for distance in metres, 5000 is a "
    "line every 5 km). Not set: round steps chosen from the values.",
)
_ID = Setting(
    "component_id",
    "A stable name for this component (lowercase letters, digits and -), shown as data-id for stylesheets. "
    "Not set: r<row>c<column>, which changes when the layout changes.",
)
_CLASS = Setting(
    "classes",
    "Space-separated names of your own for stylesheets (lowercase letters, digits and -, not starting with eui-).",
)
_APP = Setting(
    "app_name",
    "The app this component comes from. easy-ui sets it on the sections an app adds to a dashboard, so the app's "
    "own stylesheets apply to them; set it yourself only to let an app's stylesheets style this component.",
)
COMPONENT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,40}\Z")
APP_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}\Z")
ANCHOR_RE = SLUG_RE
_AUTO_ID_RE = re.compile(r"^r\d+c\d+\Z")
MAX_CLASSES = 8
_Y = Setting(
    "columns",
    "Column, or list of up to 8 columns, drawn as series. Name them nicely in the SQL with AS.",
    required=True,
)

COMPONENTS: dict[str, dict[str, Any]] = {
    "stat": {
        "description": "One big number from the first row of a query, with an optional change and hint.",
        "settings": {
            "title": _TITLE,
            "query": _QUERY,
            "value": Setting("column", "Column holding the number to show (first row).", required=True),
            "format": _FORMAT,
            "delta": Setting("column", "Column holding a change to show next to the value (first row)."),
            "delta_format": Setting("format", "Format for the delta. Not set: the same as format."),
            "better": Setting(
                "better",
                "Which way of the delta is good: higher or lower. Good changes are drawn in the accent colour, bad "
                "ones in a muted red. Not set: the delta is drawn in neutral grey.",
            ),
            "hint": Setting("longtext", "A short explanation under the number."),
        },
    },
    "line": {
        "description": "A line chart of one or more columns over x.",
        "settings": {"title": _TITLE, "query": _QUERY, "x": _X, "y": _Y, "format": _FORMAT, "unit": _UNIT,
                     "step": _STEP, "readout": _READOUT, "partial": _PARTIAL},
    },
    "area": {
        "description": "Like line, with the area under each line filled.",
        "settings": {"title": _TITLE, "query": _QUERY, "x": _X, "y": _Y, "format": _FORMAT, "unit": _UNIT,
                     "step": _STEP, "readout": _READOUT, "partial": _PARTIAL},
    },
    "bar": {
        "description": "A bar chart of one or more columns per x value.",
        "settings": {
            "title": _TITLE,
            "query": _QUERY,
            "x": _X,
            "y": _Y,
            "format": _FORMAT,
            "unit": _UNIT,
            "stacked": Setting("bool", "true stacks the series on top of each other.", default=False),
            "step": _STEP,
            "readout": _READOUT,
            "partial": _PARTIAL,
        },
    },
    "table": {
        "description": "Rows of a query as a table.",
        "settings": {
            "title": _TITLE,
            "query": _QUERY,
            "columns": Setting(
                "table_columns",
                "Which columns to show, in order: a column name, or {column, label, format}. Not set: all columns.",
            ),
            "limit": Setting("int", "Show at most this many rows.", min=1, max=LIMITS["table_limit"]),
            "phone": Setting(
                "phone",
                "On narrow screens: cards (each row as a small card of label and value, no sideways scrolling) or "
                "scroll (the table as it is, scrolled sideways).",
                default="cards",
            ),
        },
    },
    "text": {
        "description": "Text written in a small markdown subset: paragraphs, # headings, **bold**, *italic*, "
        "- lists, 1. lists and [links](https://...) (https only).",
        "settings": {
            "title": Setting("text", "Optional heading."),
            "body": Setting("markdown", "The text.", required=True),
        },
    },
    "controls": {
        "description": "The inputs that let a viewer change the params (period, kind, ...).",
        "settings": {
            "title": Setting("text", "Optional heading."),
            "params": Setting("params", "Which params to show, in order. Not set: all params."),
        },
    },
}
for _c in COMPONENTS.values():
    _c["settings"]["id"] = _ID
    _c["settings"]["class"] = _CLASS
    _c["settings"]["app"] = _APP
COMPONENT_TYPES = tuple(COMPONENTS)

_KIND_TEXT = {
    "text": f"text, up to {LIMITS['title_length']} characters",
    "longtext": f"text, up to {LIMITS['long_text_length']} characters",
    "query": "the name of a query in this file",
    "column": "a column name returned by the query",
    "columns": "a column name, or a list of column names",
    "format": "one of: " + ", ".join(formats.FORMAT_NAMES),
    "unit": "text, up to 12 characters",
    "bool": "true or false",
    "int": "a whole number",
    "table_columns": "a list of column names or {column, label, format}",
    "markdown": "text in the markdown subset",
    "params": "a list of param names from params:",
    "better": "higher or lower",
    "readout": "latest, pointer or none",
    "partial": "last, or a column name returned by the query",
    "step": "a number above 0",
    "phone": "cards or scroll",
    "component_id": "lowercase letters, digits and -, starting with a letter (up to 41)",
    "classes": f"up to {MAX_CLASSES} space-separated names of lowercase letters, digits and -",
    "app_name": "an app's name (lowercase letters, digits and -)",
}

EXAMPLE = """\
spec: easy-ui/v1
title: Training
description: Distance and steps over time.
params:
  since:
    type: date
    label: From
    default: today-90d
queries:
  totals:
    sql: SELECT sum(distance_m) AS distance, count(*) AS days FROM days WHERE day >= :since
  daily:
    sql: SELECT day, distance_m AS "Distance" FROM days WHERE day >= :since ORDER BY day
layout:
  - - type: controls
  - - type: stat
      title: Distance
      query: totals
      value: distance
      format: distance
    - type: line
      title: Distance per day
      query: daily
      x: day
      y: Distance
      format: distance
"""


def catalog() -> dict[str, Any]:
    return {
        "spec": SPEC_VERSION,
        "file": "ui/dashboards/<slug>.yaml (one dashboard per file; slug: lowercase letters, digits and -)",
        "top_level": {
            "spec": f'required, exactly "{SPEC_VERSION}"',
            "title": "required, the page title",
            "description": "optional, one or two sentences",
            "params": "optional, name -> param (the values viewers can change)",
            "queries": "name -> {sql: one read-only SELECT using :param placeholders}",
            "layout": "required, a list of rows; each row is a list of up to 6 components",
            "theme": "optional, this dashboard's own look: the settings of ui/theme.yaml except name, description "
            "and font_files (see: easy-ui style-guide)",
            "app": "set by easy-ui on an app's dashboard (the app it comes from, whose stylesheets apply to it)",
        },
        "anchors": "a layout item {anchor: <name>} in place of a row marks where apps' sections may be placed "
        "(position: after:<name>); it shows nothing",
        "sections": {
            "spec": "easy-ui/sections-v1",
            "file": "dashboards/<file>.yaml in an app's folder (or ui/sections/<app>.<dashboard>.yaml in the project "
            "to replace it)",
            "keys": ["spec", "sections"],
            "section_keys": ["id", "position", "queries", "layout"],
            "position": "start | end | after:<anchor>",
            "rules": [
                "at most 10 sections per file, 10 queries and 10 rows per section",
                "query names start with the app's table prefix (the short name with - as _, then _)",
                "sections cannot declare params; their queries may use the target dashboard's params",
            ],
        },
        "names": "param and query names: lowercase letters, digits and _, starting with a letter (up to 31)",
        "params": {
            "choice": {
                "keys": list(PARAM_TYPES["choice"]),
                "about": "pick one of options; options are values or {value, label}; default is one of the values",
            },
            "int": {"keys": list(PARAM_TYPES["int"]), "about": "a whole number between min and max"},
            "date": {
                "keys": list(PARAM_TYPES["date"]),
                "about": "a date sent to SQL as text YYYY-MM-DD; default is YYYY-MM-DD, today, or today-<N><d|w|m|y> "
                "such as today-90d",
            },
            "text": {
                "keys": list(PARAM_TYPES["text"]),
                "about": f"free text up to {LIMITS['text_param_length']} characters",
            },
        },
        "queries": {
            "rules": [
                "exactly one statement starting with SELECT or WITH",
                "parameters only as :name, and every :name must be declared under params",
                "only the tables dashboards may read exist: the shared tables (their merged views), [views] and the "
                "apps' [ui] tables (see: easy-ui tables)",
                "no ATTACH, PRAGMA, writes or extension functions",
                "text values in single quotes; double quotes are for names",
                f"time limit {sandbox.DEFAULT_LIMITS.timeout_s:g} s, at most {sandbox.DEFAULT_LIMITS.max_rows} rows",
            ]
        },
        "components": {
            name: {
                "description": c["description"],
                "settings": {k: {**s.to_dict(), "value": _KIND_TEXT[s.kind]} for k, s in c["settings"].items()},
            }
            for name, c in COMPONENTS.items()
        },
        "formats": formats.vocabulary(),
        "limits": dict(LIMITS),
        "example": EXAMPLE,
    }


# ---------------------------------------------------------------- problems


@dataclass
class Problem:
    file: str
    where: str
    message: str
    line: int | None = None
    hint: str | None = None
    allowed: list[Any] | None = None
    did_you_mean: str | None = None
    level: str = "error"
    place: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"file": self.file, "line": self.line, "where": self.where, "problem": self.message}
        if self.place:
            d["place"] = self.place
        if self.allowed is not None:
            d["allowed"] = self.allowed
        if self.did_you_mean is not None:
            d["did_you_mean"] = self.did_you_mean
        if self.hint:
            d["hint"] = self.hint
        d["level"] = self.level
        return d

    def text(self) -> str:
        loc = f"{self.file}:{self.line}" if self.line else self.file
        where = self.place or self.where
        out = f"{loc}  {where}: {self.message}" if where else f"{loc}  {self.message}"
        if self.did_you_mean is not None:
            out += f' Did you mean "{self.did_you_mean}"?'
        if self.hint:
            out += f" {self.hint}"
        if self.allowed is not None and len(self.allowed) <= 20:
            out += f" Allowed: {allowed_text(self.allowed)}."
        return out


def allowed_text(allowed: list[Any]) -> str:
    """The allowed values as one list; quoted when a value has a comma or a space, so it stays readable."""
    items = [str(a) for a in allowed]
    if any("," in a or " " in a or not a for a in items):
        return ", ".join(f'"{a}"' for a in items)
    return ", ".join(items)


_ORDINAL = ("first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth")


def plain_place(path: tuple[Any, ...], data: Any = None) -> str | None:
    """A place in a dashboard file in words: 'row 2, first component (line), setting y'.

    In a sections file (``sections[0].layout[1][0]``) it starts with the section: 'section "walks", row 2, ...'.
    """
    if not path:
        return None
    if path[0] == "sections" and len(path) >= 2 and isinstance(path[1], int):
        sec = None
        try:
            sec = data["sections"][path[1]]
        except (KeyError, IndexError, TypeError):
            sec = None
        sid = sec.get("id") if isinstance(sec, dict) else None
        head_text = f'section "{sid}"' if isinstance(sid, str) and len(sid) < 45 else f"section {path[1] + 1}"
        rest = plain_place(tuple(path[2:]), sec) if len(path) > 2 else None
        return f"{head_text}, {rest}" if rest else head_text
    head = path[0]
    if head == "layout" and len(path) >= 2 and isinstance(path[1], int):
        parts = [f"row {path[1] + 1}"]
        if len(path) >= 3 and isinstance(path[2], int):
            c = path[2]
            nth = _ORDINAL[c] if c < len(_ORDINAL) else f"number {c + 1}"
            ctype = None
            try:
                comp = data["layout"][path[1]][c]
                ctype = comp.get("type") if isinstance(comp, dict) else None
            except (KeyError, IndexError, TypeError):
                ctype = None
            parts.append(f"{nth} component" + (f" ({ctype})" if isinstance(ctype, str) and len(ctype) < 30 else ""))
            rest = path[3:]
            if rest:
                setting = str(rest[0])
                if len(rest) > 1 and isinstance(rest[1], int):
                    setting += f" (item {rest[1] + 1})"
                parts.append(f"setting {setting}")
        return ", ".join(parts)
    if head in ("queries", "params") and len(path) >= 2:
        what = "query" if head == "queries" else "param"
        out = f'{what} "{path[1]}"'
        if len(path) >= 3:
            out += f", setting {path[2]}"
        return out
    if len(path) == 1:
        return f"setting {head}"
    return None


def _where(path: tuple[Any, ...]) -> str:
    out = ""
    for p in path:
        out += f"[{p}]" if isinstance(p, int) else (f".{p}" if out else str(p))
    return out


def _closest(word: Any, options: list[str] | tuple[str, ...]) -> str | None:
    if not isinstance(word, str) or not options:
        return None
    lower = {o.lower(): o for o in options}
    if word.lower() in lower and lower[word.lower()] != word:
        return lower[word.lower()]
    m = difflib.get_close_matches(word, list(options), n=1, cutoff=0.6)
    return m[0] if m else None


class _Ctx:
    """Collects problems. ``origin`` (composed dashboards) maps a path to (file, path in that file, its data)."""

    def __init__(self, file: str, lines: dict[tuple[Any, ...], int], data: Any = None, origin: Any = None) -> None:
        self.file = file
        self.lines = lines
        self.data = data
        self.origin = origin
        self.errors: list[Problem] = []
        self.warnings: list[Problem] = []

    def line(self, path: tuple[Any, ...]) -> int | None:
        while True:
            if path in self.lines:
                return self.lines[path]
            if not path:
                return None
            path = path[:-1]

    def _p(self, path, message, hint, allowed, got, level) -> Problem:
        allowed_list = list(allowed) if allowed is not None else None
        did = _closest(got, [str(a) for a in allowed_list]) if allowed_list and got is not None else None
        file, where_path, data = self.file, tuple(path), self.data
        if self.origin is not None:
            file, where_path, data = self.origin(tuple(path))
        return Problem(file, _where(where_path), message, self.line(path), hint, allowed_list, did, level,
                       plain_place(where_path, data))

    def err(self, path, message, *, hint=None, allowed=None, got=None) -> None:
        self.errors.append(self._p(path, message, hint, allowed, got, "error"))

    def warn(self, path, message, *, hint=None, allowed=None, got=None) -> None:
        self.warnings.append(self._p(path, message, hint, allowed, got, "warning"))


# ---------------------------------------------------------------- YAML


class _YamlProblem(Exception):
    def __init__(self, message: str, line: int | None, hint: str | None = None) -> None:
        super().__init__(message)
        self.message, self.line, self.hint = message, line, hint


MAX_DEPTH = 40


def _yaml_plain(problem: str, context: str, line: int | None) -> tuple[str, str]:
    """PyYAML's parser words, in plain language."""
    at = "This line" if line else "A line"  # the line number is shown next to the file name
    p = problem.lower()
    if "found character '\\t'" in p or "\\t" in p or "tab" in p:
        return (f"{at} starts with a tab, so the file cannot be read (YAML only accepts spaces for indenting).",
                "Replace the tab with spaces.")
    if "mapping values are not allowed" in p:
        return (f"{at} has ': ' where a new setting cannot start, so the file cannot be read. Often a piece of text "
                "contains ': ' and needs quotes, or the line is indented too far.",
                'Put text that contains ": " in quotes, for example title: "Sleep: last nights", or line the setting '
                "up with the ones above it.")
    if "expected <block end>" in p or "expected <block mapping start>" in p or "block sequence" in p \
            or "did not find expected" in p and "key" in p:
        return (f"{at} is not lined up with the lines around it, so the file cannot be read.",
                "Each setting of a component must start in the same column as its 'type:' (and each row starts with "
                "'- -' or '- '). Use spaces, not tabs.")
    if "could not find expected ':'" in p or "simple key" in p:
        return (f"{at} looks like a setting without a ':' after its name (or a quote that is not closed), so the "
                "file cannot be read.", "Write it as name: value, and close every quote.")
    if "found unexpected end of stream" in p or "found unexpected end" in p or "while scanning a quoted" in context:
        return (f"{at} has a quote that is never closed, so the file cannot be read.", "Close the quote on the same line.")
    if "constructor" in p or "could not determine" in p:
        return (f"{at} uses a YAML tag (a word starting with !), which dashboard files do not allow, so the file "
                "cannot be read.", "Remove the tag and write the value as plain text, a number, a list or settings.")
    if "found undefined alias" in p or "found character '*'" in p or "found character '&'" in p:
        return (f"{at} starts a value with * or &, which YAML reads as a special mark, so the file cannot be read.",
                "Put that text in quotes.")
    if "found character '@'" in p or "found character '`'" in p or "found character '%'" in p:
        return (f"{at} starts a value with a character YAML reserves (@, ` or %), so the file cannot be read.",
                "Put that text in quotes.")
    return (f"{at} could not be read as YAML ({problem}), so the file cannot be read.",
            "Check the indentation (spaces, not tabs, lined up with the settings around it) and put text containing "
            "': ' or '#' in quotes.")


def _parse_yaml(text: str) -> tuple[Any, dict[tuple[Any, ...], int]]:
    loader = yaml.SafeLoader(text)
    lines: dict[tuple[Any, ...], int] = {}
    try:
        node = loader.get_single_node()
        if node is None:
            return None, lines
        seen: set[int] = set()

        def walk(n: yaml.Node, path: tuple[Any, ...]) -> None:
            if len(path) > MAX_DEPTH:
                raise _YamlProblem(
                    f"The file is nested more than {MAX_DEPTH} levels deep, which no dashboard needs, so it was not read.",
                    n.start_mark.line + 1,
                    "Write the settings out plainly (see: easy-ui components).",
                )
            if id(n) in seen:
                used_at = lines.get(path) or lines.get(path[:-1]) or n.start_mark.line + 1
                raise _YamlProblem(
                    "The file uses a YAML alias (*name), which dashboard files do not allow.",
                    used_at,
                    "Write the value out in full instead of using & and *.",
                )
            seen.add(id(n))
            lines.setdefault(path, n.start_mark.line + 1)
            if isinstance(n, yaml.ScalarNode):
                # Where the text of a value starts: the line after "key: |" for a block, else the same line.
                first = n.start_mark.line + (2 if n.style in ("|", ">") else 1)
                lines.setdefault(("#value",) + path, first)
            if isinstance(n, yaml.ScalarNode) and n.tag == "tag:yaml.org,2002:timestamp":
                try:
                    loader.construct_object(n)
                except (ValueError, OverflowError):
                    raise _YamlProblem(
                        f"{n.value!r} looks like a date, but that date does not exist.",
                        n.start_mark.line + 1,
                        "Use a real date written as YYYY-MM-DD.",
                    ) from None
            if isinstance(n, yaml.MappingNode):
                keys: set[Any] = set()
                for k, v in n.value:
                    if k.tag == "tag:yaml.org,2002:merge":
                        raise _YamlProblem(
                            "The file uses a YAML merge key (<<), which dashboard files do not allow.",
                            k.start_mark.line + 1,
                            "Write the settings out in full.",
                        )
                    if not isinstance(k, yaml.ScalarNode):
                        raise _YamlProblem("A key in the file is not a plain word.", k.start_mark.line + 1)
                    key = loader.construct_object(k)
                    try:
                        hash(key)
                    except TypeError:
                        raise _YamlProblem("A key in the file is not a plain word.", k.start_mark.line + 1) from None
                    if key in keys:
                        raise _YamlProblem(
                            f'The key "{key}" appears twice in the same place, so it is unclear which one counts.',
                            k.start_mark.line + 1,
                            "Keep only one of them.",
                        )
                    keys.add(key)
                    lines.setdefault(path + (key,), k.start_mark.line + 1)
                    walk(v, path + (key,))
            elif isinstance(n, yaml.SequenceNode):
                for i, item in enumerate(n.value):
                    walk(item, path + (i,))

        walk(node, ())
        try:
            data = loader.construct_document(node)
        except (ValueError, TypeError, OverflowError) as e:
            raise _YamlProblem(f"The file has a value that cannot be read ({e}).", None, "Check dates and numbers.") from None
        return data, lines
    except RecursionError:
        raise _YamlProblem(
            f"The file is nested more than {MAX_DEPTH} levels deep, which no dashboard needs, so it was not read.",
            None,
            "Write the settings out plainly (see: easy-ui components).",
        ) from None
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None) or getattr(e, "context_mark", None)
        line = mark.line + 1 if mark is not None else None
        problem = getattr(e, "problem", None) or str(e)
        message, hint = _yaml_plain(str(problem), getattr(e, "context", None) or "", line)
        raise _YamlProblem(message, line, hint) from None
    finally:
        loader.dispose()


# ---------------------------------------------------------------- markdown


def _safe_href(href: str) -> bool:
    if not href or len(href) > 2000 or re.search(r"[\x00-\x20\"'<>\\^`{|}]", href):
        return False
    try:
        p = urlsplit(href)
    except ValueError:
        return False
    return p.scheme == "https" and bool(p.netloc)


def _merge_text(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for n in nodes:
        if n["t"] == "text" and out and out[-1]["t"] == "text":
            out[-1] = {"t": "text", "v": out[-1]["v"] + n["v"]}
        elif n["t"] != "text" or n["v"]:
            out.append(n)
    return out


def _link_at(s: str, i: int) -> tuple[str, str, int] | None:
    """``[label](target)`` starting at ``s[i]``; the target may hold balanced parentheses."""
    m = re.match(r"\[([^\[\]]*)\]\(", s[i:])
    if not m:
        return None
    j = i + m.end()
    depth = 1
    k = j
    while k < len(s):
        if s[k] == "(":
            depth += 1
        elif s[k] == ")":
            depth -= 1
            if depth == 0:
                return m.group(1), s[j:k].strip(), k + 1
        k += 1
    return None


def _inline(s: str, problems: list[str], depth: int = 0) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            out.append({"t": "text", "v": "".join(buf)})
            buf.clear()

    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n and s[i + 1] in "\\*_[]()#-+.!`":
            buf.append(s[i + 1])
            i += 2
            continue
        if depth < 4 and s.startswith("**", i):
            j = s.find("**", i + 2)
            if j > i + 2:
                flush()
                out.append({"t": "b", "c": _inline(s[i + 2 : j], problems, depth + 1)})
                i = j + 2
                continue
        if depth < 4 and c in "*_" and not s.startswith("**", i):
            if c == "_" and i > 0 and s[i - 1].isalnum():
                buf.append(c)
                i += 1
                continue
            j = i + 1
            while j < n:
                if s[j] == c and not (c == "*" and s.startswith("**", j)):
                    if c == "_" and j + 1 < n and s[j + 1].isalnum():
                        j += 1
                        continue
                    break
                j += 1
            if j < n and j > i + 1:
                flush()
                out.append({"t": "i", "c": _inline(s[i + 1 : j], problems, depth + 1)})
                i = j + 1
                continue
        if c == "[":
            m = _link_at(s, i)
            if m:
                label, href, end = m
                flush()
                if _safe_href(href):
                    out.append({"t": "a", "href": href, "c": _inline(label, problems, depth + 1) if depth < 4 else [{"t": "text", "v": label}]})
                else:
                    problems.append(
                        f'The link "{label}" goes to "{href[:60]}", but links must start with https:// '
                        "(other kinds of links are not allowed)."
                    )
                    out.append({"t": "text", "v": label})
                i = end
                continue
        buf.append(c)
        i += 1
    flush()
    return _merge_text(out)


def parse_markdown(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    problems: list[str] = []
    blocks: list[dict[str, Any]] = []
    para: list[str] = []
    kind: str | None = None
    items: list[list[dict[str, Any]]] = []

    def flush_para() -> None:
        if para:
            blocks.append({"t": "p", "c": _inline(" ".join(para), problems)})
            para.clear()

    def flush_list() -> None:
        nonlocal kind
        if kind:
            blocks.append({"t": kind, "items": list(items)})
            items.clear()
            kind = None

    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line:
            flush_para()
            flush_list()
            continue
        m = re.match(r"^(#{1,4})\s+(.*?)\s*#*\s*$", line)
        if m:
            flush_para()
            flush_list()
            blocks.append({"t": "h", "level": len(m.group(1)), "c": _inline(m.group(2), problems)})
            continue
        m = re.match(r"^[-*+]\s+(.*)$", line) or None
        mo = None if m else re.match(r"^\d{1,3}[.)]\s+(.*)$", line)
        if m or mo:
            want = "ul" if m else "ol"
            flush_para()
            if kind != want:
                flush_list()
                kind = want
            items.append(_inline((m or mo).group(1), problems))  # type: ignore[union-attr]
            continue
        if kind and raw[:1] in (" ", "\t") and items:
            items[-1] = _merge_text(items[-1] + [{"t": "text", "v": " "}] + _inline(line, problems))
            continue
        flush_list()
        para.append(line)
    flush_para()
    flush_list()
    return blocks, problems


# ---------------------------------------------------------------- model


@dataclass
class Param:
    name: str
    type: str
    label: str
    default: Any
    options: list[dict[str, Any]] | None = None
    min: int | None = None
    max: int | None = None
    default_rule: str | None = None  # for date params: "today-90d" etc.


@dataclass
class Query:
    name: str
    sql: str
    params: list[str] = field(default_factory=list)


@dataclass
class Dashboard:
    slug: str
    title: str
    description: str | None
    params: dict[str, Param]
    queries: dict[str, Query]
    layout: list[list[dict[str, Any]]]
    file: str = "<text>"
    warnings: list[Problem] = field(default_factory=list)
    column_refs: list[tuple[tuple[Any, ...], str, str]] = field(default_factory=list)
    lines: dict[tuple[Any, ...], int] = field(default_factory=dict, repr=False)
    theme: dict[str, dict[str, str]] | None = None  # this dashboard's theme: as custom properties
    app: str | None = None  # the app the dashboard comes from (its stylesheets apply to it)
    origin: Any = field(default=None, repr=False)  # composed dashboards: path -> (file, path, data) of the source

    def query(self, name: str) -> Query:
        q = self.queries.get(name) if isinstance(name, str) else None
        if q is None:
            raise NotFound(
                f'The dashboard "{self.slug}" has no query called "{name}", so it cannot be run.',
                hint=f"Its queries are: {', '.join(self.queries) or '(none)'}.",
            )
        return q

    def used_queries(self) -> list[str]:
        out: list[str] = []
        for row in self.layout:
            for comp in row:
                q = comp.get("query")
                if q and q not in out:
                    out.append(q)
        return out


# ---------------------------------------------------------------- validation helpers


_BAD = object()


def _is_text_like(v: Any) -> bool:
    return isinstance(v, (str, int, float, _dt.date)) and not isinstance(v, bool)


def _text(ctx: _Ctx, path, v, *, limit: int, what: str, allow_empty: bool = False) -> Any:
    if isinstance(v, bool):
        ctx.err(path, f"{what} is {v!r}, which YAML reads as true/false, not as text.", hint="Put the text in quotes.")
        return _BAD
    if not _is_text_like(v):
        ctx.err(path, f"{what} must be text.", hint="For example: \"My title\".")
        return _BAD
    s = v.isoformat() if isinstance(v, _dt.date) else str(v)
    if not allow_empty and not s.strip():
        ctx.err(path, f"{what} is empty.", hint="Write some text, or remove the setting.")
        return _BAD
    if len(s) > limit:
        ctx.err(path, f"{what} is {len(s)} characters long; the most allowed is {limit}.", hint="Make it shorter.")
        return _BAD
    return s.strip() if not allow_empty else s


def _mapping(ctx: _Ctx, path, v, what: str, example: str) -> dict | None:
    if not isinstance(v, dict):
        ctx.err(path, f"{what} must be a set of settings (key: value lines).", hint=f"For example:\n{example}")
        return None
    return v


def _keys(ctx: _Ctx, path, d: dict, allowed: tuple[str, ...], what: str) -> None:
    for k in d:
        if k not in allowed:
            ctx.err(
                path + (k,),
                f'"{k}" is not a setting of {what}.',
                allowed=list(allowed),
                got=str(k),
                hint="Remove it or use one of the allowed settings.",
            )


def _label_of(name: str) -> str:
    s = name.replace("_", " ").strip()
    return s[:1].upper() + s[1:]


def add_months(d: _dt.date, months: int) -> _dt.date:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    for day in (d.day, 30, 29, 28):
        try:
            return _dt.date(year, month, min(d.day, day))
        except ValueError:
            continue
    raise ValueError("bad date")  # pragma: no cover


def resolve_date_rule(rule: str, today: _dt.date) -> _dt.date:
    m = _DATE_RULE_RE.match(rule.strip())
    if not m:
        raise ValueError(rule)
    if not m.group(1):
        return today
    n = int(m.group(2)) * (1 if m.group(1) == "+" else -1)
    unit = m.group(3)
    if unit == "d":
        return today + _dt.timedelta(days=n)
    if unit == "w":
        return today + _dt.timedelta(weeks=n)
    if unit == "m":
        return add_months(today, n)
    return add_months(today, 12 * n)


def _parse_date(v: Any) -> _dt.date | None:
    if isinstance(v, _dt.datetime):
        return None
    if isinstance(v, _dt.date):
        return v
    if isinstance(v, str) and re.match(r"^\d{4}-\d{2}-\d{2}\Z", v):
        try:
            return _dt.date.fromisoformat(v)
        except ValueError:
            return None
    return None


def _check_param(ctx: _Ctx, name: Any, raw: Any) -> Param | None:
    path = ("params", name)
    if not isinstance(name, str) or not NAME_RE.match(name):
        ctx.err(
            path,
            f"The param name {name!r} cannot be used.",
            hint="Param names use lowercase letters, digits and _, start with a letter and are at most 31 long "
            "(they are used in SQL as :name).",
        )
        return None
    d = _mapping(ctx, path, raw, f'The param "{name}"', "  type: choice\n  label: Period\n  default: 30\n  options: [7, 30, 90]")
    if d is None:
        return None
    ptype = d.get("type")
    if ptype not in PARAM_TYPES:
        if ptype is None:
            ctx.err(path, f'The param "{name}" has no type.', allowed=list(PARAM_TYPES), hint="Add type: with one of the allowed types.")
        else:
            ctx.err(path + ("type",), f'The param type "{ptype}" is not known.', allowed=list(PARAM_TYPES), got=str(ptype))
        return None
    _keys(ctx, path, d, PARAM_TYPES[ptype], f'a {ptype} param')
    label = _label_of(name)
    if "label" in d:
        lv = _text(ctx, path + ("label",), d["label"], limit=60, what=f'The label of "{name}"')
        if lv is not _BAD:
            label = lv
    if "default" not in d:
        examples = {"choice": "one of the option values", "int": "a whole number", "date": "today-30d or 2026-01-01", "text": '""'}
        ctx.err(
            path,
            f'The param "{name}" has no default value, so the dashboard would not know what to show at first.',
            hint=f"Add default: with {examples[ptype]}.",
        )
        return None
    default = d["default"]
    p = Param(name=name, type=ptype, label=label, default=None)
    dpath = path + ("default",)

    if ptype == "choice":
        opts = d.get("options")
        if not isinstance(opts, list) or not opts:
            ctx.err(path + ("options",), f'The choice param "{name}" needs a list of options.', hint="For example: options: [7, 30, 90] or options: [{value: run, label: Running}].")
            return None
        if len(opts) > LIMITS["options"]:
            ctx.err(path + ("options",), f"There are more than {LIMITS['options']} options.", hint="Use fewer options.")
            return None
        out = []
        seen: set[str] = set()
        for i, o in enumerate(opts):
            opath = path + ("options", i)
            if isinstance(o, dict):
                _keys(ctx, opath, o, OPTION_KEYS, "an option")
                if "value" not in o:
                    ctx.err(opath, "This option has no value.", hint="Write {value: ..., label: ...}.")
                    continue
                value, olabel = o["value"], o.get("label")
                vpath = opath + ("value",)
            else:
                value, olabel, vpath = o, None, opath
            if isinstance(value, bool) or not isinstance(value, (str, int, float, _dt.date)):
                ctx.err(
                    vpath,
                    f"The option {value!r} is not plain text or a number"
                    + (" (YAML reads yes/no/on/off as true/false)" if isinstance(value, bool) else "")
                    + ".",
                    hint="Put the option in quotes, for example: 'no'.",
                )
                continue
            if isinstance(value, _dt.date):
                value = value.isoformat()
            if isinstance(value, str) and len(value) > LIMITS["text_param_length"]:
                ctx.err(vpath, "This option value is too long.", hint=f"Use at most {LIMITS['text_param_length']} characters.")
                continue
            if str(value) in seen:
                ctx.err(vpath, f"The option {value!r} appears twice.", hint="List each option once.")
                continue
            seen.add(str(value))
            if olabel is None:
                olabel = str(value)
            else:
                olabel = _text(ctx, opath + ("label",), olabel, limit=60, what="The option label")
                if olabel is _BAD:
                    continue
            out.append({"value": value, "label": olabel})
        p.options = out
        values = [o["value"] for o in out]
        match = [v for v in values if str(v) == str(default.isoformat() if isinstance(default, _dt.date) else default)]
        if not match:
            if out:
                ctx.err(
                    dpath,
                    f'The default {default!r} of "{name}" is not one of its options.',
                    allowed=values,
                    got=str(default),
                )
            return None
        p.default = match[0]
    elif ptype == "int":
        lo, hi = _INT_RANGE
        for key in ("min", "max"):
            if key in d:
                v = d[key]
                if isinstance(v, bool) or not isinstance(v, int):
                    ctx.err(path + (key,), f'{key} of "{name}" must be a whole number.', hint=f"For example: {key}: 1")
                    return None
                if not _INT_RANGE[0] <= v <= _INT_RANGE[1]:
                    ctx.err(path + (key,), f'{key} of "{name}" is too large.', hint="Use a smaller number.")
                    return None
        p.min, p.max = d.get("min"), d.get("max")
        if p.min is not None and p.max is not None and p.min > p.max:
            ctx.err(path + ("min",), f'min of "{name}" is larger than its max.', hint="Swap them.")
            return None
        if isinstance(default, bool) or not isinstance(default, int):
            ctx.err(dpath, f'The default of "{name}" must be a whole number.', hint="For example: default: 30")
            return None
        if (p.min is not None and default < p.min) or (p.max is not None and default > p.max) or not lo <= default <= hi:
            ctx.err(dpath, f'The default {default} of "{name}" is outside min..max.', hint="Change the default or the limits.")
            return None
        p.default = default
    elif ptype == "date":
        if isinstance(default, str) and _DATE_RULE_RE.match(default.strip()):
            p.default_rule = default.strip()
            p.default = None
        else:
            dd = _parse_date(default)
            if dd is None:
                ctx.err(
                    dpath,
                    f'The default {default!r} of "{name}" is not a date easy-ui understands.',
                    hint="Use a date like 2026-01-31, or today, or today-<number><d|w|m|y> such as today-90d.",
                )
                return None
            p.default = dd.isoformat()
    else:  # text
        dv = _text(ctx, dpath, default, limit=LIMITS["text_param_length"], what=f'The default of "{name}"', allow_empty=True)
        if dv is _BAD:
            return None
        p.default = dv
    return p


def _check_setting(ctx: _Ctx, path, kind: str, s: Setting, v: Any, ctype: str, dash: dict) -> Any:
    what = f'"{path[-1]}"'
    if kind == "text":
        return _text(ctx, path, v, limit=LIMITS["title_length"], what=what)
    if kind == "longtext":
        return _text(ctx, path, v, limit=LIMITS["long_text_length"], what=what)
    if kind == "unit":
        return _text(ctx, path, v, limit=12, what=what)
    if kind == "query":
        if not isinstance(v, str) or v not in dash["queries"]:
            names = list(dash["queries"])
            if not names:
                ctx.err(path, f"This {ctype} names the query {v!r}, but the file has no queries.", hint="Add it under queries:.")
            else:
                ctx.err(path, f"This {ctype} names the query {v!r}, which is not under queries:.", allowed=names, got=v)
            return _BAD
        return v
    if kind == "column":
        if not isinstance(v, str) or not v:
            ctx.err(path, f"{what} must be a column name.", hint="Write the name of a column the query returns.")
            return _BAD
        return v
    if kind == "columns":
        items = [v] if isinstance(v, str) else v
        if not isinstance(items, list) or not items or not all(isinstance(c, str) and c for c in items):
            ctx.err(path, f"{what} must be a column name or a list of column names.", hint="For example: y: distance or y: [distance, steps].")
            return _BAD
        if len(items) > LIMITS["y_columns"]:
            ctx.err(path, f"{what} lists more than {LIMITS['y_columns']} columns.", hint="Show fewer series.")
            return _BAD
        return list(items)
    if kind == "format":
        if v not in formats.FORMATS:
            hint = None
            if isinstance(v, str) and len(v) <= 12 and not _closest(v, formats.FORMAT_NAMES):
                if "unit" in COMPONENTS.get(ctype, {}).get("settings", {}):
                    hint = f"{v!r} looks like a unit, not a format: write unit: {v} and pick a format for the numbers."
                else:
                    hint = f"{v!r} looks like a unit, not a format. A {ctype} has no unit setting: put the unit in its title."
            ctx.err(path, f"The format {v!r} is not known.", allowed=list(formats.FORMAT_NAMES), got=str(v), hint=hint)
            return _BAD
        return v
    if kind == "better":
        if v not in ("higher", "lower"):
            ctx.err(path, f"better is {v!r}, but it can only be higher or lower.", allowed=["higher", "lower"], got=str(v),
                    hint="Write better: higher when a rise is good news (HRV, steps), better: lower when a fall is (resting heart rate).")
            return _BAD
        return v
    if kind in ("readout", "phone"):
        allowed = ["latest", "pointer", "none"] if kind == "readout" else ["cards", "scroll"]
        if v not in allowed:
            ctx.err(path, f"{what} is {v!r}, but it can only be one of: {', '.join(allowed)}.", allowed=allowed, got=str(v))
            return _BAD
        return v
    if kind == "partial":
        if not isinstance(v, str) or not v:
            ctx.err(path, f"{what} must be last or a column name.",
                    hint="partial: last marks the last row as still in progress; or name a column that is 1 for such rows.")
            return _BAD
        return v
    if kind == "step":
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not (v > 0) or v != v or v == float("inf"):
            ctx.err(path, f"{what} must be a number above 0.", hint="For example step: 5000 for a line every 5 km of metres.")
            return _BAD
        return v
    if kind == "app_name":
        if not isinstance(v, str) or not APP_NAME_RE.match(v):
            ctx.err(path, f"app is {str(v)[:45]!r}, which is not an app's name.",
                    hint="Use the name of one of the project's apps (lowercase letters, digits and -), or remove it.")
            return _BAD
        return v
    if kind == "component_id":
        if not isinstance(v, str) or not COMPONENT_ID_RE.match(v):
            ctx.err(path, f"id is {str(v)[:45]!r}, which cannot be used as a component's name.",
                    hint="Use lowercase letters, digits and -, starting with a letter, for example id: weekly-distance.")
            return _BAD
        if _AUTO_ID_RE.match(v):
            ctx.err(path, f'id "{v}" looks like the names easy-ui gives components without an id (r0c1).',
                    hint="Pick a name that says what the component shows, for example id: weekly-distance.")
            return _BAD
        ids = dash.setdefault("ids", set())
        if v in ids:
            ctx.err(path, f'Two components have the id "{v}", so a stylesheet could not tell them apart.',
                    hint="Give each component its own id.")
            return _BAD
        ids.add(v)
        return v
    if kind == "classes":
        words = v.split() if isinstance(v, str) else v if isinstance(v, list) else None
        if not words or not all(isinstance(w, str) for w in words):
            ctx.err(path, "class must be one or more names separated by spaces.", hint="For example: class: wide highlight")
            return _BAD
        if len(words) > MAX_CLASSES:
            ctx.err(path, f"class has {len(words)} names; at most {MAX_CLASSES} are allowed.", hint="Use fewer names.")
            return _BAD
        for w in words:
            if w.startswith("eui-"):
                ctx.err(path, f'The class name "{w[:45]}" starts with eui-, which is kept for easy-ui\'s own names.',
                        hint="Use a name of your own, for example class: highlight.")
                return _BAD
            if not COMPONENT_ID_RE.match(w):
                ctx.err(path, f"The class name {w[:45]!r} cannot be used.",
                        hint="Use lowercase letters, digits and -, starting with a letter, for example class: highlight.")
                return _BAD
        return list(dict.fromkeys(words))
    if kind == "bool":
        if not isinstance(v, bool):
            ctx.err(path, f"{what} must be true or false.")
            return _BAD
        return v
    if kind == "int":
        if isinstance(v, bool) or not isinstance(v, int) or (s.min is not None and v < s.min) or (s.max is not None and v > s.max):
            ctx.err(path, f"{what} must be a whole number from {s.min} to {s.max}.")
            return _BAD
        return v
    if kind == "markdown":
        if not isinstance(v, str) or not v.strip():
            ctx.err(path, f"{what} must be text.", hint="Use a YAML block: body: |  followed by indented lines.")
            return _BAD
        if len(v) > LIMITS["markdown_length"]:
            ctx.err(path, f"The text is longer than {LIMITS['markdown_length']} characters.", hint="Make it shorter.")
            return _BAD
        blocks, problems = parse_markdown(v)
        for msg in problems:
            ctx.err(path, msg, hint="Change the link to an https:// address or remove it.")
        return blocks if not problems else _BAD
    if kind == "params":
        names = list(dash["params"])
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            ctx.err(path, f"{what} must be a list of param names.", allowed=names)
            return _BAD
        ok = True
        for i, x in enumerate(v):
            if x not in dash["params"]:
                ctx.err(path + (i,), f"There is no param called {x!r}.", allowed=names, got=x)
                ok = False
        if len(set(v)) != len(v):
            ctx.err(path, "A param is listed twice.", hint="List each param once.")
            ok = False
        return list(v) if ok else _BAD
    if kind == "table_columns":
        if not isinstance(v, list) or not v:
            ctx.err(path, f"{what} must be a list of columns.", hint="For example: columns: [day, {column: distance_m, label: Distance, format: distance}].")
            return _BAD
        if len(v) > LIMITS["table_columns"]:
            ctx.err(path, f"{what} lists more than {LIMITS['table_columns']} columns.", hint="Show fewer columns.")
            return _BAD
        out = []
        ok = True
        for i, item in enumerate(v):
            ipath = path + (i,)
            if isinstance(item, str) and item:
                out.append({"column": item, "label": item, "format": None})
                continue
            if not isinstance(item, dict):
                ctx.err(ipath, "Each column is a column name or {column, label, format}.")
                ok = False
                continue
            _keys(ctx, ipath, item, TABLE_COLUMN_KEYS, "a table column")
            col = item.get("column")
            if not isinstance(col, str) or not col:
                ctx.err(ipath, "This table column has no column: name.", hint="Add column: <name>.")
                ok = False
                continue
            label = col
            if "label" in item:
                label = _text(ctx, ipath + ("label",), item["label"], limit=60, what="The column label")
                if label is _BAD:
                    ok = False
                    continue
            fmt = None
            if "format" in item and item["format"] is not None:
                fmt = _check_setting(ctx, ipath + ("format",), "format", _FORMAT, item["format"], ctype, dash)
                if fmt is _BAD:
                    ok = False
                    continue
            out.append({"column": col, "label": label, "format": fmt})
        return out if ok else _BAD
    raise AssertionError(kind)  # pragma: no cover


def _check_component(ctx: _Ctx, path, raw: Any, dash: dict, refs: list, r: int, c: int) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        ctx.err(
            path,
            "Each component is a set of settings starting with type:.",
            hint="For example:\n- type: stat\n  title: Distance\n  query: totals\n  value: distance",
        )
        return None
    ctype = raw.get("type")
    if ctype not in COMPONENTS:
        if ctype is None:
            ctx.err(path, "This component has no type.", allowed=list(COMPONENT_TYPES), hint="Add type: with one of the allowed types.")
        else:
            ctx.err(path + ("type",), f"The component type {ctype!r} is not known.", allowed=list(COMPONENT_TYPES), got=str(ctype))
        return None
    settings: dict[str, Setting] = COMPONENTS[ctype]["settings"]
    _keys(ctx, path, raw, ("type", *settings), f"a {ctype}")
    out: dict[str, Any] = {"id": f"r{r}c{c}", "type": ctype}
    ok = True
    for key, s in settings.items():
        if key == "id" and raw.get("id") is None:
            continue  # keeps the automatic r<row>c<column>
        if key not in raw or raw[key] is None:
            if s.required:
                about = s.description[:1].lower() + s.description[1:].rstrip(".")
                ctx.err(path, f'This {ctype} has no "{key}" ({about}).', hint=f"Add {key}: ...")
                ok = False
            out[key] = s.default
            continue
        v = _check_setting(ctx, path + (key,), s.kind, s, raw[key], ctype, dash)
        if v is _BAD:
            ok = False
            out[key] = s.default
        else:
            out[key] = v
    if ctype == "text":
        out["blocks"] = out.pop("body")
    if ctype == "controls" and out.get("params") is None:
        out["params"] = list(dash["params"])
    if not ok:
        return None
    q = out.get("query")
    if q:
        for key, s in settings.items():
            val = out.get(key)
            if (s.kind == "column" or (s.kind == "partial" and val != "last")) and val:
                refs.append((path + (key,), q, val))
            elif s.kind == "columns" and val:
                for i, col in enumerate(val):
                    refs.append((path + (key, i) if isinstance(raw[key], list) else path + (key,), q, col))
            elif s.kind == "table_columns" and val:
                for i, col in enumerate(val):
                    refs.append((path + (key, i), q, col["column"]))
    return out


def slug_for(path: str | Path) -> str:
    p = Path(path)
    slug = p.stem
    if p.suffix != ".yaml" or not SLUG_RE.match(slug):
        raise SpecError(
            f'The dashboard file name "{p.name}" cannot be used: its name becomes the page address.',
            hint="Name it <slug>.yaml where the slug uses lowercase letters, digits and - (up to 41 characters, "
            "starting with a letter or digit), for example training-overview.yaml.",
        )
    return slug


def _check(text: str, *, slug: str, file: str = "<text>") -> tuple[Dashboard | None, Dashboard | None, list[Problem], list[Problem]]:
    ctx = _Ctx(file, {})
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
        ctx.err((), f"The dashboard name {slug!r} cannot be used as a page address.", hint="Use lowercase letters, digits and -.")
        return None, None, ctx.errors, ctx.warnings
    if len(text.encode("utf-8", "surrogatepass")) > LIMITS["file_bytes"]:
        ctx.err((), f"The file is larger than {LIMITS['file_bytes'] // 1000} KB.", hint="Split it into several dashboards.")
        return None, None, ctx.errors, ctx.warnings
    try:
        data, lines = _parse_yaml(text)
    except _YamlProblem as e:
        ctx.errors.append(Problem(file, "", e.message, e.line, e.hint))
        return None, None, ctx.errors, ctx.warnings
    return _check_data(data, lines, slug=slug, file=file)


def check_data(data: Any, lines: dict[tuple[Any, ...], int], *, slug: str, file: str,
               origin: Any = None) -> tuple[Dashboard | None, Dashboard | None, list[Problem], list[Problem]]:
    """Check an already-parsed dashboard (composition uses it): (dashboard, partial, errors, warnings).

    ``origin(path) -> (file, path, data)`` names, for each place, the file it came from (problems then point there).
    """
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
        ctx = _Ctx(file, {})
        ctx.err((), f"The dashboard name {slug!r} cannot be used as a page address.", hint="Use lowercase letters, digits and -.")
        return None, None, ctx.errors, ctx.warnings
    return _check_data(data, lines, slug=slug, file=file, origin=origin)


def _check_data(data: Any, lines: dict[tuple[Any, ...], int], *, slug: str, file: str,
                origin: Any = None) -> tuple[Dashboard | None, Dashboard | None, list[Problem], list[Problem]]:
    ctx = _Ctx(file, lines, data, origin)
    if not isinstance(data, dict):
        ctx.err((), "The file must be a set of settings (spec:, title:, queries:, layout: ...).", hint=f"Start from this example:\n{EXAMPLE}")
        return None, None, ctx.errors, ctx.warnings

    for k in data:
        if k not in TOP_KEYS:
            ctx.err((k,), f'"{k}" is not a top-level setting of a dashboard.', allowed=list(TOP_KEYS), got=str(k), hint="Remove it or fix the spelling.")
    for k in REQUIRED_TOP:
        if k not in data:
            hint = {"spec": f"Add spec: {SPEC_VERSION} as the first line.", "title": "Add title: <page title>.", "layout": "Add layout: with a list of rows of components."}[k]
            ctx.err((), f'The file has no "{k}".', hint=hint)
    if "spec" in data and data["spec"] != SPEC_VERSION:
        ctx.err(("spec",), f'spec is {data["spec"]!r}, but this version of easy-ui reads "{SPEC_VERSION}".', allowed=[SPEC_VERSION], hint=f"Write spec: {SPEC_VERSION}.")
    title = _text(ctx, ("title",), data["title"], limit=LIMITS["title_length"], what="The title") if "title" in data else _BAD
    description = None
    if data.get("description") is not None:
        description = _text(ctx, ("description",), data["description"], limit=LIMITS["long_text_length"], what="The description")
        if description is _BAD:
            description = None

    app_name = data.get("app")
    if app_name is not None and (not isinstance(app_name, str) or not APP_NAME_RE.match(app_name)):
        ctx.err(("app",), f"app is {str(app_name)[:45]!r}, which is not an app's name.",
                hint="easy-ui sets app: on the dashboards an app ships; remove the line from your own dashboards.")
        app_name = None

    # theme: this dashboard's own look (easyui.theme)
    theme_tokens = None
    if data.get("theme") is not None:
        from easyui import theme as _theme  # noqa: PLC0415 - theme uses this module's helpers

        theme_tokens = _theme.check_overrides(ctx, ("theme",), data["theme"])

    # params
    params: dict[str, Param] = {}
    raw_params = data.get("params") or {}
    if not isinstance(raw_params, dict):
        ctx.err(("params",), "params must be a set of named params.", hint="For example:\nparams:\n  since:\n    type: date\n    default: today-30d")
        raw_params = {}
    if len(raw_params) > LIMITS["params"]:
        ctx.err(("params",), f"There are more than {LIMITS['params']} params.", hint="Use fewer params.")
    bad_params: set[str] = set()
    for name, raw in raw_params.items():
        p = _check_param(ctx, name, raw)
        if p is not None:
            params[name] = p
        elif isinstance(name, str):
            bad_params.add(name)

    # queries
    queries: dict[str, Query] = {}
    raw_queries = data.get("queries") or {}
    if not isinstance(raw_queries, dict):
        ctx.err(("queries",), "queries must be a set of named queries.", hint="For example:\nqueries:\n  totals:\n    sql: SELECT count(*) AS days FROM days")
        raw_queries = {}
    if len(raw_queries) > LIMITS["queries"]:
        ctx.err(("queries",), f"There are more than {LIMITS['queries']} queries.", hint="Use fewer queries.")
    used_params: set[str] = set()
    known_query_names: list[str] = []
    for name, raw in raw_queries.items():
        path = ("queries", name)
        if not isinstance(name, str) or not NAME_RE.match(name):
            ctx.err(path, f"The query name {name!r} cannot be used.", hint="Query names use lowercase letters, digits and _, start with a letter and are at most 31 long.")
            continue
        known_query_names.append(name)
        if isinstance(raw, str):
            ctx.err(path, f'The query "{name}" must be written as sql: under its name.', hint=f"Write:\n  {name}:\n    sql: SELECT ...")
            continue
        d = _mapping(ctx, path, raw, f'The query "{name}"', "    sql: SELECT ...")
        if d is None:
            continue
        _keys(ctx, path, d, QUERY_KEYS, "a query")
        if "sql" not in d:
            ctx.err(path, f'The query "{name}" has no sql.', hint="Add sql: SELECT ...")
            continue
        scan = sandbox.scan_sql(d["sql"])
        for msg in scan.problems:
            ctx.err(path + ("sql",), msg, hint="Dashboard queries are one read-only SELECT. See: easy-ui components")
        missing = [p for p in scan.params if p not in params and p not in bad_params]
        for p in missing:
            ctx.err(
                path + ("sql",),
                f"The query uses :{p}, but there is no param called {p!r} under params:.",
                allowed=list(params) or None,
                got=p,
                hint="Declare it under params: (with a type and a default), or fix the name.",
            )
        used_params.update(scan.params)
        if not scan.problems and not missing:
            queries[name] = Query(name=name, sql=d["sql"], params=scan.params)

    # layout
    layout: list[list[dict[str, Any]]] = []
    refs: list[tuple[tuple[Any, ...], str, str]] = []
    # Names that exist but failed their own checks still count, so one mistake is reported once.
    dash_ctx = {
        "queries": dict.fromkeys(known_query_names, True),
        "params": dict.fromkeys(list(params) + sorted(bad_params), True),
        "ids": set(),
    }
    raw_layout = data.get("layout")
    if "layout" in data:
        if not isinstance(raw_layout, list) or not raw_layout:
            ctx.err(("layout",), "layout must be a list of rows, and each row a list of components.", hint="For example:\nlayout:\n  - - type: stat\n      ...")
        else:
            if len(raw_layout) > LIMITS["rows"]:
                ctx.err(("layout",), f"layout has more than {LIMITS['rows']} rows.", hint="Split it into several dashboards.")
            anchors: set[str] = set()
            for r, row in enumerate(raw_layout):
                rpath = ("layout", r)
                if isinstance(row, dict) and "anchor" in row:
                    # {anchor: name}: where apps' sections may go (composition removes it; it shows nothing)
                    name = row.get("anchor")
                    extra = [k for k in row if k != "anchor"]
                    if extra:
                        ctx.err(rpath, f"An anchor item holds only anchor: <name>, but this one also has {extra[0]!r}.",
                                hint="Write it as - anchor: walks (on a line of its own, in place of a row).")
                    elif not isinstance(name, str) or not ANCHOR_RE.match(name):
                        ctx.err(rpath + ("anchor",), f"The anchor name {str(name)[:45]!r} cannot be used.",
                                hint="Use lowercase letters, digits and -, for example anchor: activity.")
                    elif name in anchors:
                        ctx.err(rpath + ("anchor",), f'There are two anchors called "{name}" in this file.',
                                hint="Give each anchor its own name.")
                    else:
                        anchors.add(name)
                    continue
                if isinstance(row, dict):
                    ctx.err(
                        rpath,
                        f"Row {r + 1} is a single component, but each row must be a list of components.",
                        hint="Start the row with '- -' (a list inside the list), e.g.\nlayout:\n  - - type: stat\n      title: ...",
                    )
                    continue
                if not isinstance(row, list) or not row:
                    ctx.err(rpath, f"Row {r + 1} must be a list of components.")
                    continue
                if len(row) > LIMITS["components_per_row"]:
                    ctx.err(rpath, f"Row {r + 1} has {len(row)} components; at most {LIMITS['components_per_row']} fit in a row.", hint="Move some to a new row.")
                out_row = []
                for c, comp in enumerate(row):
                    res = _check_component(ctx, rpath + (c,), comp, dash_ctx, refs, len(layout), c)
                    if res is not None:
                        out_row.append(res)
                layout.append(out_row)
            if not layout and anchors:
                ctx.err(("layout",), "The layout has only anchors, so the page would be empty.",
                        hint="Add at least one row of components (anchors only mark where apps' sections go).")

    if ctx.errors:
        # What could be checked so far: lets validation also check the data of the parts that are fine,
        # so a typo in one component and a wrong column in another are both reported in one round.
        partial = Dashboard(
            slug=slug, title=title if isinstance(title, str) else slug, description=description, params=params,
            queries=queries, layout=layout, file=file, warnings=[], column_refs=refs, lines=lines,
            theme=theme_tokens, app=app_name, origin=origin,
        )
        return None, partial, ctx.errors, ctx.warnings

    used_q = {comp.get("query") for row in layout for comp in row if comp.get("query")}
    for name in queries:
        if name not in used_q:
            ctx.warn(("queries", name), f'The query "{name}" is not used by any component.', hint="Use it in the layout or remove it.")
    for name in params:
        if name not in used_params:
            ctx.warn(("params", name), f'The param "{name}" is not used by any query, so changing it changes nothing.', hint=f"Use :{name} in a query or remove it.")
    shown = {p for row in layout for comp in row if comp["type"] == "controls" for p in comp["params"]}
    unshown = [p for p in params if p not in shown]
    if unshown:
        ctx.warn(
            ("params",),
            f"These params have no control on the page, so viewers cannot change them: {', '.join(unshown)}.",
            hint="Add a component of type: controls to the layout (it shows every param by default).",
        )
    dash = Dashboard(
        slug=slug,
        title=title,  # type: ignore[arg-type]
        description=description,
        params=params,
        queries=queries,
        layout=layout,
        file=file,
        warnings=list(ctx.warnings),
        column_refs=refs,
        lines=lines,
        theme=theme_tokens,
        app=app_name,
        origin=origin,
    )
    return dash, dash, ctx.errors, ctx.warnings


def check(text: str, *, slug: str, file: str = "<text>") -> tuple[Dashboard | None, list[Problem], list[Problem]]:
    dash, _partial, errors, warnings = _check(text, slug=slug, file=file)
    return dash, errors, warnings


def check_partial(text: str, *, slug: str, file: str = "<text>") -> tuple[Dashboard | None, list[Problem], list[Problem]]:
    """Like ``check``, but with mistakes it returns the parts that passed (``None`` when nothing could be read)."""
    _dash, partial, errors, warnings = _check(text, slug=slug, file=file)
    return partial, errors, warnings


def _raise(file: str, errors: list[Problem]) -> SpecError:
    first = errors[0]
    more = f" (and {len(errors) - 1} more problem{'s' if len(errors) > 2 else ''})" if len(errors) > 1 else ""
    return SpecError(
        f"The dashboard file {file} has a mistake{more}: {first.text()}",
        hint="Fix the file, then run: easy-ui validate",
        problems=[e.to_dict() for e in errors],
    )


def loads(text: str, *, slug: str, file: str = "<text>") -> Dashboard:
    dash, errors, _warnings = check(text, slug=slug, file=file)
    if errors or dash is None:
        raise _raise(file, errors)
    return dash


def _read(path: Path) -> str:
    """A dashboard file, read like publish reads it: no links, no FIFOs or devices, the size checked first."""
    try:
        data = paths.read_plain_file(path, LIMITS["file_bytes"])
    except FileNotFoundError:
        raise NotFound(f"There is no dashboard file {paths.display_path(path)}.", hint="Check the file name; dashboards live in ui/dashboards/.") from None
    except paths.NotPlainFile as e:
        raise SpecError(f"The dashboard file {paths.display_path(path)} {e.reason}, so easy-ui does not read it.",
                        hint="Dashboards must be ordinary text files (not links), each at most "
                        f"{LIMITS['file_bytes']} bytes.") from None
    except OSError as e:
        raise SpecError(f"The dashboard file {paths.display_path(path)} could not be opened ({e.strerror}).",
                        hint="Check that the file is readable.") from None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise SpecError(f"The dashboard file {paths.display_path(path)} is not UTF-8 text.", hint="Save it as UTF-8 text.") from None


def load_file(path: str | Path, *, file_label: str | None = None) -> Dashboard:
    p = Path(path)
    slug = slug_for(p)
    return loads(_read(p), slug=slug, file=file_label or paths.display_path(p))


# ---------------------------------------------------------------- data checks


def default_values(dash: Dashboard, *, today: _dt.date | None = None) -> dict[str, Any]:
    today = today or _dt.date.today()
    out: dict[str, Any] = {}
    for p in dash.params.values():
        if p.type == "date" and p.default_rule:
            out[p.name] = resolve_date_rule(p.default_rule, today).isoformat()
        else:
            out[p.name] = p.default
    return out


def _sql_line(dash: Dashboard, qname: str, token: str | None) -> int | None:
    """The line of the dashboard file where ``token`` first appears in a query's SQL."""
    start = dash.lines.get(("#value", "queries", qname, "sql"))
    q = dash.queries.get(qname)
    if not start or q is None or not token:
        return None
    needle = token.rsplit(".", 1)[-1] if "." in token and not token.startswith(".") else token
    pattern = re.compile(r"(?<![\w])" + re.escape(needle) + r"(?![\w])", re.I)
    for i, text in enumerate(q.sql.splitlines()):
        if pattern.search(text):
            return start + i
    return None


VARY_LIMIT_S = 20.0  # at most this long trying the other control values of all queries together


def _variations(dash: Dashboard, q: Query, base: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Other control values worth trying at publish: each choice option, each int's min and max, and for text
    the empty text and the longest text allowed."""
    out = []
    for name in q.params:
        p = dash.params.get(name)
        if p is None:
            continue
        values: list[Any] = []
        if p.type == "choice":
            values = [o["value"] for o in (p.options or [])][:12]
        elif p.type == "int":
            values = [v for v in (p.min, p.max) if v is not None]
        elif p.type == "text":
            values = ["", "x" * LIMITS["text_param_length"]]
        for v in values:
            if v == base.get(name):
                continue
            label = next((o["label"] for o in (p.options or []) if o["value"] == v), v) if p.type == "choice" else v
            if p.type == "text":
                label = "empty text" if v == "" else f"a text of {len(v)} characters"
            out.append((f'With "{p.label}" set to {label}, ', {**base, name: v}))
    return out


def check_with_data(
    dash: Dashboard,
    bundle_path: str | Path,
    *,
    limits: sandbox.Limits = sandbox.DEFAULT_LIMITS,
    vary: bool = True,
    pool: workers.WorkerPool | None = None,
) -> tuple[list[Problem], list[Problem]]:
    ctx = _Ctx(dash.file, dash.lines, origin=dash.origin)
    values = default_values(dash)
    cols: dict[str, list[str]] = {}
    started = time.monotonic()
    skipped = 0
    # Preparing a query can take long or a lot of memory without SQLite checking the time, so even the
    # checks run in a worker process that is stopped when its time is up (easyui.workers).
    with (pool or workers.default_pool()).session(bundle_path, limits) as sb:
        for name, q in dash.queries.items():
            try:
                cols[name] = sb.columns(q.sql, {k: values.get(k) for k in q.params}, name=name)
            except EasyUIError as e:
                problem = ctx._p(("queries", name, "sql"), e.message, e.hint, None, None, "error")
                problem.line = _sql_line(dash, name, getattr(e, "token", None)) or problem.line
                ctx.errors.append(problem)
                continue
            if not vary:
                continue
            # The defaults work; a slow or broken query for another control value would only show on the page.
            for prefix, other in _variations(dash, q, values):
                if time.monotonic() - started > VARY_LIMIT_S:
                    skipped += 1
                    continue
                try:
                    sb.run(q.sql, {k: other.get(k) for k in q.params}, name=name)
                except EasyUIError as e:
                    ctx.err(("queries", name, "sql"), prefix + e.message[:1].lower() + e.message[1:], hint=e.hint)
                    break
    if skipped:
        ctx.warn((), f"Not every control value could be tried in the time allowed ({skipped} left out).",
                 hint="Try the dashboard with easy-ui preview, changing each control.")
    for name, c in cols.items():
        dupes = sorted({x for x in c if c.count(x) > 1})
        if dupes:
            ctx.warn(("queries", name, "sql"), f'The query "{name}" returns more than one column called {", ".join(dupes)}.', hint="Give each column its own name with AS.")
    for path, qname, col in dash.column_refs:
        if qname not in cols:
            continue
        if col not in cols[qname]:
            ctx.err(path, f'The query "{qname}" does not return a column called "{col}".', allowed=cols[qname], got=col, hint="Use one of the columns it returns, or change the query.")
    return ctx.errors, ctx.warnings


def validate_file(path: str | Path, *, bundle: str | Path | None = None, file_label: str | None = None) -> dict[str, Any]:
    p = Path(path)
    label = file_label or paths.display_path(p)
    entry: dict[str, Any] = {"file": label, "slug": None, "title": None, "ok": False, "errors": [], "warnings": []}
    try:
        slug = slug_for(p)
        text = _read(p)
    except EasyUIError as e:
        entry["errors"] = [Problem(label, "", e.message, None, e.hint).to_dict()]
        return entry
    return _validate_text(text, slug, label, bundle)


def _validate_text(text: str, slug: str, label: str, bundle: str | Path | None) -> dict[str, Any]:
    entry: dict[str, Any] = {"file": label, "slug": slug, "title": None, "ok": False, "errors": [], "warnings": []}
    dash, errors, warnings = check_partial(text, slug=slug, file=label)
    if dash is not None and bundle is not None:
        e2, w2 = check_with_data(dash, bundle, vary=not errors)
        errors, warnings = errors + e2, warnings + w2
    if dash is not None:
        entry["title"] = dash.title
    entry["errors"] = [e.to_dict() for e in errors]
    entry["warnings"] = [w.to_dict() for w in warnings]
    entry["ok"] = not errors
    return entry


def _validate_any_file(path: Path, *, bundle: Path | None, label: str) -> dict[str, Any]:
    """One file named on the command line: a dashboard, or a sections file (checked on its own)."""
    try:  # read like every dashboard file: no links, no FIFOs or devices, the size checked first
        head = paths.read_plain_file(path, LIMITS["file_bytes"])[:4096].decode("utf-8", "replace")
    except OSError:
        head = ""  # validate_file then says why the file cannot be read
    if re.search(r"^spec:\s*['\"]?easy-ui/sections-v1", head, re.M):
        from easyui import compose  # noqa: PLC0415

        entry: dict[str, Any] = {"file": label, "slug": None, "title": None, "ok": False, "errors": [], "warnings": [],
                                 "kind": "sections"}
        try:
            text = _read(path)
        except EasyUIError as e:
            entry["errors"] = [Problem(label, "", e.message, None, e.hint).to_dict()]
            return entry
        entry["errors"], entry["warnings"] = compose.check_sections_text(text, file=label)
        entry["ok"] = not entry["errors"]
        return entry
    return validate_file(path, bundle=bundle, file_label=label)


def validate_app(config: Any, files: list[str | Path] | None = None, *, check_data: bool = True) -> dict[str, Any]:
    """The project's dashboards as composed from the project and its apps (or only ``files``), checked.

    Without ``files``, every dashboard is composed first (``easyui.compose``) and each entry says where it came
    from (``from``, ``replaces``, ``sections``); problems name the file each part came from.
    """
    from easyui import bundle as _bundle
    from easyui import compose  # noqa: PLC0415

    current = _bundle.current_bundle(config) if check_data else None
    data = {"checked": current is not None, "bundle": current.stem if current else None, "note": None}
    if check_data and current is None:
        data["note"] = "Queries and column names were not checked against data, because no data has been built yet. Run: easy-ui data build"
    elif not check_data:
        data["note"] = "Checking against data was switched off."
    notes: list[str] = []
    if files:
        entries = [_validate_any_file(Path(f), bundle=current, label=paths.display_path(Path(f), config.app_dir))
                   for f in files]
        order: list[str] = []
    else:
        comp = compose.compose(config, bundle_path=current)
        notes += comp.notes
        entries = []
        for d in comp.dashboards:
            entries.append({"file": d.file, "slug": d.slug, "title": d.title, "ok": d.ok, "errors": d.errors,
                            "warnings": d.warnings, "from": d.origin, "replaces": d.replaces,
                            "sections": d.sections})
        by_file: dict[str, dict[str, Any]] = {}
        for p in comp.problems + comp.warnings:
            e = by_file.setdefault(p["file"], {"file": p["file"], "slug": None, "title": None, "ok": True,
                                                "errors": [], "warnings": []})
            (e["errors"] if p.get("level") == "error" else e["warnings"]).append(p)
            e["ok"] = not e["errors"]
        entries += list(by_file.values())
        order = comp.order
        if not comp.dashboards and not comp.problems:
            notes.append("There are no dashboards yet: add one in ui/dashboards/ (for example ui/dashboards/overview.yaml, "
                         "see: easy-ui components), or an app that ships dashboards.")
    n_err = sum(len(e["errors"]) for e in entries)
    n_warn = sum(len(e["warnings"]) for e in entries)
    return {
        "ok": n_err == 0,
        "app": config.name,
        "data": data,
        "dashboards": entries,
        "order": order,
        "errors": n_err,
        "warnings": n_warn,
        "notes": notes,
    }


# ---------------------------------------------------------------- runtime


def resolve_params(dash: Dashboard, given: dict[str, Any] | None = None, *, today: _dt.date | None = None) -> dict[str, Any]:
    if given is None:
        given = {}
    if not isinstance(given, dict):
        raise ParamError("The control values must be sent as named values.", hint="Send params as a JSON object.")
    for k in given:
        if k not in dash.params:
            raise ParamError(
                f'The dashboard "{dash.slug}" has no control called {str(k)[:40]!r}.',
                hint=f"Its params are: {', '.join(dash.params) or '(none)'}.",
            )
    values = default_values(dash, today=today)
    for name, raw in given.items():
        if raw is None:
            continue
        p = dash.params[name]
        bad = ParamError(f'The value {str(raw)[:40]!r} is not allowed for "{p.label}".', hint="Pick a value from the control and try again.")
        if p.type == "choice":
            match = [o["value"] for o in p.options or [] if not isinstance(raw, (dict, list, bool)) and str(o["value"]) == str(raw)]
            if not match:
                raise bad
            values[name] = match[0]
        elif p.type == "int":
            v: Any = raw
            if isinstance(v, str) and re.match(r"^-?\d{1,16}\Z", v.strip()):
                v = int(v.strip())
            if isinstance(v, float) and v.is_integer():
                v = int(v)
            if isinstance(v, bool) or not isinstance(v, int):
                raise ParamError(f'"{p.label}" must be a whole number.', hint="Enter a whole number.")
            lo = p.min if p.min is not None else _INT_RANGE[0]
            hi = p.max if p.max is not None else _INT_RANGE[1]
            if not lo <= v <= hi:
                raise ParamError(f'"{p.label}" must be between {lo} and {hi}.', hint="Enter a number in that range.")
            values[name] = v
        elif p.type == "date":
            d = _parse_date(raw) if isinstance(raw, str) else None
            if d is None:
                raise ParamError(f'"{p.label}" must be a date written as YYYY-MM-DD.', hint="Pick a date with the control.")
            values[name] = d.isoformat()
        else:
            if not isinstance(raw, str) or len(raw) > LIMITS["text_param_length"] or "\x00" in raw:
                raise ParamError(
                    f'"{p.label}" must be text of at most {LIMITS["text_param_length"]} characters.',
                    hint="Use shorter text.",
                )
            values[name] = raw
    return values


def compile_dashboard(dash: Dashboard, *, today: _dt.date | None = None) -> dict[str, Any]:
    defaults = default_values(dash, today=today)
    params = []
    for p in dash.params.values():
        d: dict[str, Any] = {"name": p.name, "type": p.type, "label": p.label, "default": defaults[p.name]}
        if p.type == "choice":
            d["options"] = [dict(o) for o in p.options or []]
        elif p.type == "int":
            d["min"], d["max"] = p.min, p.max
        elif p.type == "date":
            d["default_rule"] = p.default_rule
        else:
            d["max_length"] = LIMITS["text_param_length"]
        params.append(d)
    used = dash.used_queries()
    return {
        "spec": SPEC_VERSION,
        "slug": dash.slug,
        "title": dash.title,
        "description": dash.description,
        "params": params,
        "queries": {name: {"params": list(dash.queries[name].params)} for name in used},
        "layout": copy.deepcopy(dash.layout),
        "theme": copy.deepcopy(dash.theme),
        "app": dash.app,
    }


def run_queries(
    dash: Dashboard,
    names: list[str],
    params: dict[str, Any] | None = None,
    *,
    bundle_path: str | Path,
    limits: sandbox.Limits = sandbox.DEFAULT_LIMITS,
    today: _dt.date | None = None,
    errors: str = "raise",
    total_time_s: float | None = None,
    pool: workers.WorkerPool | None = None,
) -> dict[str, Any]:
    if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
        raise ParamError("Say which queries to run, as a list of names.", hint=f"This dashboard's queries are: {', '.join(dash.queries)}.")
    if len(names) > LIMITS["queries_per_request"]:
        raise ParamError("Too many queries were asked for at once.", hint=f"Ask for at most {LIMITS['queries_per_request']}.")
    unique = list(dict.fromkeys(names))
    for n in unique:
        dash.query(n)
    values = resolve_params(dash, params, today=today)
    results: dict[str, Any] = {}
    budget = limits.max_bytes
    # One request shares one time budget, the same way it shares the size budget, so a page
    # with many slow queries cannot hold the server for (queries x time limit).
    deadline = time.monotonic() + total_time_s if total_time_s is not None else None
    # Every query runs in a worker process that is killed when its time is up (easyui.workers).
    with (pool or workers.default_pool()).session(bundle_path, limits) as sb:
        for n in unique:
            q = dash.queries[n]
            timeout = limits.timeout_s
            shared = False
            if deadline is not None:
                left = deadline - time.monotonic()
                if left < timeout:
                    timeout, shared = left, True
            try:
                if timeout <= 0:
                    raise _out_of_time(n, total_time_s)
                sb.limits = dataclasses.replace(limits, max_bytes=max(budget, 0), timeout_s=timeout)
                try:
                    res = sb.run(q.sql, {k: values[k] for k in q.params}, name=n)
                except QueryTimeout:
                    if shared:
                        raise _out_of_time(n, total_time_s) from None
                    raise
            except EasyUIError as e:
                if errors != "inline":
                    raise
                results[n] = {"error": {"code": e.code, "message": e.message, "hint": e.hint}}
                continue
            budget -= res.size
            results[n] = res.to_dict()
    return {"results": results}


def _out_of_time(name: str, total: float | None) -> QueryTimeout:
    return QueryTimeout(
        f'The query "{name}" did not run to the end, because the queries of this page together took longer '
        f"than {total or 0:g} seconds. The limit keeps one slow page from holding up every other dashboard.",
        hint="Try again, or ask for less data (for example a shorter period). If it keeps happening, the "
        "dashboard's queries need to be made simpler.",
    )

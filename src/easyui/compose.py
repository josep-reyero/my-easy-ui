"""Dashboards from a project and its apps: discovery, contributed sections, overrides and composition.

This is section 6 of the apps contract (my-easy-software ``docs/apps-contract.md``).

Where dashboards come from, in discovery order:

1. each app in ``[project] apps`` order, each slug in its ``[ui] dashboards``
   order: ``<app folder>/dashboards/<slug>.yaml``;
2. the project's ``ui/dashboards/*.yaml`` (by file name).

A project file replaces an app's dashboard with the same name (a note, not a
warning). Two apps shipping the same name is an error unless the project has
that file, which then decides.

Contributed sections: for each final dashboard D and each app (in order) with
``[ui.contributes.D]``, the sections come from the project's
``ui/sections/<app name>.<D>.yaml`` when it exists (it replaces the app's;
``sections: []`` removes them), else from the app's file. They are placed at
``start``, ``end`` or ``after:<anchor>`` (an ``{anchor: name}`` item of D's
layout), in apps order then file order; the anchors are then removed and the
result is checked as an ordinary ``easy-ui/v1`` dashboard. Every problem names
the file the part came from (``apps/fitlab_walks/dashboards/overview-walks.yaml,
sections[0].layout[0][0]``), never the composed file.

The composed file is what gets published, so the server never composes
anything. Sections cannot declare params (they may use D's), and their query
names must start with the app's table prefix. Each component a section adds,
and each app dashboard, carries ``app: <app name>``: the page marks them with
``data-app`` and the app's own stylesheets apply only there.

Public API
----------
``SECTIONS_SPEC`` (``"easy-ui/sections-v1"``), ``OVERRIDE_RE``, ``MAX_SECTIONS``, ``MAX_SECTION_QUERIES``,
``MAX_SECTION_ROWS``.
``Part`` (dataclass): one file read: ``label``, ``origin`` ("project" | app name), ``rel``, ``data``, ``sha256``.
``Composed`` (dataclass): one dashboard: ``slug``, ``title``, ``description``, ``origin``, ``replaces``, ``file``,
    ``sections`` ([{"app", "id", "file", "placed"}]), ``sources`` ([{"file", "from", "sha256", "size"}]),
    ``data`` (the composed file's bytes, or None), ``sha256``, ``errors``, ``warnings``, ``dashboard``, ``ok``,
    ``to_dict()``.
``Composition`` (dataclass): ``dashboards`` (discovery order), ``order`` (menu order), ``problems`` (errors not
    tied to one dashboard), ``warnings``, ``notes``, ``project_files`` (the project's own dashboard and sections
    files), ``apps``; ``ok``, ``errors()``, ``get(slug)``, ``to_dict()`` (the ``easy-ui compose --json`` shape).
``compose(config, *, bundle_path=None, vary=True, apps=None) -> Composition``  never raises for file mistakes
    (they are problems); raises ``ConfigError`` when the project or an app.toml cannot be read.
``dump(data, sources=()) -> str``   a composed dashboard as YAML text.
``check_sections_text(text, *, file, prefix=None) -> (errors, warnings)``   a sections file on its own.
``register(subparsers)``  ``easy-ui compose`` and ``easy-ui dashboards``.
"""

from __future__ import annotations

import argparse
import copy
import datetime as _dt
import hashlib
import json
import os
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from easyui import paths, spec
from easyui.errors import EasyUIError, Refused

SECTIONS_SPEC = "easy-ui/sections-v1"
SECTIONS_KEYS = ("spec", "sections")
SECTION_KEYS = ("id", "position", "queries", "layout")
OVERRIDE_RE = re.compile(r"^(?P<app>[a-z][a-z0-9-]{1,30})\.(?P<slug>[a-z0-9][a-z0-9-]{0,40})\Z")
POSITION_RE = re.compile(r"^(?:start|end|after:(?P<anchor>[a-z0-9][a-z0-9-]{0,40}))\Z")
MAX_SECTIONS = 10
MAX_SECTION_QUERIES = 10
MAX_SECTION_ROWS = 10
MAX_FILE_BYTES = spec.LIMITS["file_bytes"]
_TOP_ORDER = ("spec", "title", "description", "app", "theme", "params", "queries", "layout")


@dataclass(frozen=True)
class Part:
    label: str
    origin: str
    rel: str
    data: bytes
    sha256: str

    def source(self) -> dict[str, Any]:
        return {"file": self.label, "from": self.origin, "sha256": self.sha256, "size": len(self.data)}


@dataclass
class Composed:
    slug: str
    origin: str
    file: str
    replaces: str | None = None
    title: str | None = None
    description: str | None = None
    sections: list[dict[str, Any]] = field(default_factory=list)
    sources: list[dict[str, Any]] = field(default_factory=list)
    data: bytes | None = None
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    dashboard: spec.Dashboard | None = None
    replaced_file: str | None = None

    @property
    def ok(self) -> bool:
        return not self.errors and self.dashboard is not None and self.data is not None

    @property
    def sha256(self) -> str | None:
        return hashlib.sha256(self.data).hexdigest() if self.data is not None else None

    def to_dict(self) -> dict[str, Any]:
        return {"slug": self.slug, "title": self.title, "from": self.origin, "replaces": self.replaces,
                "file": self.file, "sections": list(self.sections), "sources": list(self.sources), "ok": self.ok,
                "errors": list(self.errors), "warnings": list(self.warnings)}


@dataclass
class Composition:
    dashboards: list[Composed] = field(default_factory=list)
    order: list[str] = field(default_factory=list)
    problems: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    project_files: list[Part] = field(default_factory=list)
    apps: tuple[Any, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems and all(d.ok for d in self.dashboards)

    def errors(self) -> list[dict[str, Any]]:
        return list(self.problems) + [e for d in self.dashboards for e in d.errors]

    def get(self, slug: str) -> Composed | None:
        return next((d for d in self.dashboards if d.slug == slug), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dashboards": [{"slug": d.slug, "from": d.origin, "replaces": d.replaces,
                            "sections": [{k: s[k] for k in ("app", "id", "file", "placed")} for s in d.sections]}
                           for d in self.dashboards],
            "order": list(self.order),
            "warnings": list(self.warnings) + [w for d in self.dashboards for w in d.warnings],
            "errors": self.errors(),
            "ok": self.ok,
            "notes": list(self.notes),
        }


def _problem(label: str, message: str, hint: str | None = None, level: str = "error") -> dict[str, Any]:
    return spec.Problem(label, "", message, None, hint, level=level).to_dict()


def _override_name(stem: str, apps: Any) -> str | None:
    """``<app>.<dashboard>`` with the app's full name, when ``stem`` names an app by its short name or
    its package name (``moves.overview`` or ``demo_moves.overview`` -> ``demo-moves.overview``)."""
    word, dot, slug = stem.partition(".")
    if not dot or not spec.SLUG_RE.match(slug):
        return None
    for a in apps:
        if word in (a.short, a.package, a.name.replace("-", "_")) and word != a.name:
            return f"{a.name}.{slug}"
    return None


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- reading


def _read_project(config: Any, folder: str, name_re: re.Pattern[str], problems: list[dict[str, Any]],
                  notes: list[str]) -> dict[str, Part]:
    from easyui import publish  # noqa: PLC0415 - publish uses this module

    ui = Path(config.app_dir) / "ui"
    d = ui / folder
    for p in (ui, d):
        if p.is_symlink():
            label = paths.display_path(p, config.app_dir)
            problems.append(_problem(label, f"{label} is a link, so easy-ui does not read dashboards through it.",
                                     "Make it a real folder inside the project."))
            return {}
    files, probs, nts = publish.read_dashboard_dir(d, label_prefix=f"ui/{folder}/", name_re=name_re)
    problems.extend(probs)
    notes.extend(nts)
    return {f.slug: Part(f.label, "project", f"ui/{folder}/{f.name}", f.data, f.sha256) for f in files}


def _parse(part: Part) -> tuple[Any, dict[tuple[Any, ...], int], dict[str, Any] | None]:
    try:
        text = part.data.decode("utf-8")
    except UnicodeDecodeError:
        return None, {}, _problem(part.label, f"{part.label} is not UTF-8 text.", "Save it as UTF-8 text.")
    if len(part.data) > MAX_FILE_BYTES:
        return None, {}, _problem(part.label, f"{part.label} is larger than {MAX_FILE_BYTES // 1000} KB.",
                                  "Split it into smaller files.")
    try:
        data, lines = spec._parse_yaml(text)
    except spec._YamlProblem as e:
        return None, {}, spec.Problem(part.label, "", e.message, e.line, e.hint).to_dict()
    return data, lines, None


# ---------------------------------------------------------------- sections files


@dataclass
class _Section:
    app: Any
    part: Part
    index: int
    id: str
    position: str
    anchor: str | None
    queries: dict[str, Any]
    layout: list[Any]
    data: Any
    lines: dict[tuple[Any, ...], int]
    n: int = -1  # its number among the placed sections


def _check_sections(ctx: spec._Ctx, data: Any, *, prefix: str | None, app_name: str | None) -> list[dict[str, Any]]:
    """The structure of a sections file; returns the sections that can be placed (as plain dicts)."""
    if not isinstance(data, dict):
        ctx.err((), "A sections file is a set of settings: spec: and sections:.",
                hint=f"Start with:\nspec: {SECTIONS_SPEC}\nsections:\n  - id: walks\n    position: end\n    layout: ...")
        return []
    for k in data:
        if k not in SECTIONS_KEYS:
            ctx.err((k,), f'"{str(k)[:40]}" is not a setting of a sections file.', allowed=list(SECTIONS_KEYS),
                    got=str(k), hint="Sections files hold only spec: and sections:.")
    if data.get("spec") != SECTIONS_SPEC:
        if "spec" not in data:
            ctx.err((), "The file has no spec.", hint=f"Add spec: {SECTIONS_SPEC} as the first line.")
        else:
            ctx.err(("spec",), f'spec is {str(data["spec"])[:40]!r}, but a sections file must say "{SECTIONS_SPEC}".',
                    allowed=[SECTIONS_SPEC], hint=f"Write spec: {SECTIONS_SPEC}.")
    raw = data.get("sections")
    if "sections" not in data:
        ctx.err((), "The file has no sections.", hint="Add sections: with a list (sections: [] adds nothing).")
        return []
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        ctx.err(("sections",), "sections must be a list of sections.", hint="For example:\nsections:\n  - id: walks\n    ...")
        return []
    if len(raw) > MAX_SECTIONS:
        ctx.err(("sections",), f"There are more than {MAX_SECTIONS} sections in this file.", hint="Use fewer sections.")
    out: list[dict[str, Any]] = []
    ids: set[str] = set()
    for i, sec in enumerate(raw[:MAX_SECTIONS]):
        path = ("sections", i)
        if not isinstance(sec, dict):
            ctx.err(path, "Each section is a set of settings: id, position, queries and layout.",
                    hint="For example:\n  - id: walks\n    position: end\n    layout:\n      - - type: stat ...")
            continue
        good = True
        for k in sec:
            if k not in SECTION_KEYS:
                hint = ("Sections cannot declare params; their queries may use the dashboard's own params."
                        if k == "params" else "Remove it or fix the spelling.")
                ctx.err(path + (k,), f'"{str(k)[:40]}" is not a setting of a section.', allowed=list(SECTION_KEYS),
                        got=str(k), hint=hint)
                good = False
        sid = sec.get("id")
        if not isinstance(sid, str) or not spec.SLUG_RE.match(sid):
            ctx.err(path + ("id",) if "id" in sec else path,
                    "This section has no id." if "id" not in sec else f"The section id {str(sid)[:45]!r} cannot be used.",
                    hint="Give it a name of lowercase letters, digits and -, for example id: walks.")
            good = False
        elif sid in ids:
            ctx.err(path + ("id",), f'Two sections in this file are called "{sid}".', hint="Give each section its own id.")
            good = False
        else:
            ids.add(sid)
        pos = sec.get("position")
        m = POSITION_RE.match(pos) if isinstance(pos, str) else None
        if m is None:
            ctx.err(path + ("position",) if "position" in sec else path,
                    "This section has no position." if "position" not in sec else
                    f"The position {str(pos)[:45]!r} is not one easy-ui knows.",
                    allowed=["start", "end", "after:<anchor>"], hint="Use start, end or after:<anchor name>.")
            good = False
        queries = sec.get("queries") or {}
        if not isinstance(queries, dict):
            ctx.err(path + ("queries",), "queries must be a set of named queries.",
                    hint=f"For example:\n    queries:\n      {prefix or 'app_'}per_week:\n        sql: SELECT ...")
            good = False
            queries = {}
        if len(queries) > MAX_SECTION_QUERIES:
            ctx.err(path + ("queries",), f"A section may have at most {MAX_SECTION_QUERIES} queries.",
                    hint="Split it into two sections.")
            good = False
        for q in queries:
            if prefix is not None and (not isinstance(q, str) or not q.startswith(prefix)):
                ctx.err(path + ("queries", q), f'The query name "{str(q)[:40]}" does not start with {prefix}, the '
                        "prefix of this app's names, so it could clash with the dashboard's own queries.",
                        hint=f"Rename it, for example {prefix}{str(q)[:30]}.")
                good = False
        layout = sec.get("layout")
        if not isinstance(layout, list) or not layout:
            ctx.err(path + ("layout",) if "layout" in sec else path,
                    "This section has no layout." if "layout" not in sec else
                    "layout must be a list of rows, and each row a list of components.",
                    hint="For example:\n    layout:\n      - - type: stat\n          ...")
            good = False
            layout = []
        if len(layout) > MAX_SECTION_ROWS:
            ctx.err(path + ("layout",), f"A section may have at most {MAX_SECTION_ROWS} rows.",
                    hint="Split it into two sections.")
            good = False
        for r, row in enumerate(layout):
            if isinstance(row, dict) and "anchor" in row:
                ctx.err(path + ("layout", r), "Anchors belong in dashboards, not in sections.",
                        hint="Remove the anchor item; place the section with position: instead.")
                good = False
            elif not isinstance(row, list):
                ctx.err(path + ("layout", r), f"Row {r + 1} of this section must be a list of components.",
                        hint="Start the row with '- -' (a list inside the list).")
                good = False
            else:
                for c, comp in enumerate(row):
                    if isinstance(comp, dict) and app_name is not None and comp.get("app") not in (None, app_name):
                        ctx.err(path + ("layout", r, c, "app"),
                                f"This component says it comes from the app {str(comp.get('app'))[:40]!r}, but the "
                                f"section belongs to {app_name}.", hint="Remove the app: setting; easy-ui sets it.")
                        good = False
        if good:
            out.append({"index": i, "id": sid, "position": pos, "anchor": m.group("anchor") if m else None,
                        "queries": queries, "layout": layout})
    return out


def check_sections_text(text: str, *, file: str, prefix: str | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """A sections file checked on its own (its structure; the components are checked in their dashboard)."""
    try:
        data, lines = spec._parse_yaml(text)
    except spec._YamlProblem as e:
        return [spec.Problem(file, "", e.message, e.line, e.hint).to_dict()], []
    ctx = spec._Ctx(file, lines, data)
    _check_sections(ctx, data, prefix=prefix, app_name=None)
    return [p.to_dict() for p in ctx.errors], [p.to_dict() for p in ctx.warnings]


# ---------------------------------------------------------------- composing one dashboard


def _translate_lines(src: dict[tuple[Any, ...], int], mapper: Any, out: dict[tuple[Any, ...], int]) -> None:
    for key, line in src.items():
        marked = bool(key) and key[0] == "#value"
        body = key[1:] if marked else key
        got = mapper(body)
        if got is None:
            continue
        out.setdefault((("#value",) + got) if marked else got, line)


def _compose_one(slug: str, base: Part, origin: str, contributions: list[tuple[Any, Part]],
                 bundle_path: Path | None, vary: bool) -> Composed:
    entry = Composed(slug=slug, origin=origin, file=base.label)
    entry.sources.append(base.source())
    base_data, base_lines, bad = _parse(base)
    sec_errors: list[spec.Problem] = []
    sec_warnings: list[spec.Problem] = []
    sections: list[_Section] = []
    used_ids: set[str] = set()
    for app, part in contributions:
        entry.sources.append(part.source())
        data, lines, sbad = _parse(part)
        if sbad is not None:
            entry.errors.append(sbad)
            continue
        ctx = spec._Ctx(part.label, lines, data)
        for s in _check_sections(ctx, data, prefix=app.prefix, app_name=app.name):
            if s["id"] in used_ids:
                ctx.err(("sections", s["index"], "id"), f'The dashboard "{slug}" already has a section called '
                        f'"{s["id"]}" (from another app), so this one cannot be told apart.',
                        hint="Give the section another id.")
                continue
            used_ids.add(s["id"])
            sections.append(_Section(app, part, s["index"], s["id"], s["position"], s["anchor"], s["queries"],
                                     s["layout"], data, lines))
        sec_errors.extend(ctx.errors)
        sec_warnings.extend(ctx.warnings)
    if bad is not None:
        entry.errors.insert(0, bad)
        entry.errors.extend(p.to_dict() for p in sec_errors)
        entry.warnings.extend(p.to_dict() for p in sec_warnings)
        return entry

    base_ctx = spec._Ctx(base.label, base_lines, base_data)
    composed: dict[str, Any]
    row_origin: dict[int, tuple[str, tuple[Any, ...], Any]] = {}
    query_origin: dict[str, tuple[str, tuple[Any, ...], Any]] = {}
    base_row_map: dict[int, int] = {}
    sec_row_map: dict[tuple[int, int, int], int] = {}  # (section n, row in its file) -> composed row
    changed = False
    if isinstance(base_data, dict):
        raw_layout = base_data.get("layout")
        raw_queries = base_data.get("queries")
        queries_ok = isinstance(raw_queries, dict) or raw_queries is None
        merged_queries: dict[Any, Any] = dict(raw_queries or {}) if queries_ok else raw_queries
        placeable = isinstance(raw_layout, list) and queries_ok
        anchors: list[str] = []
        if isinstance(raw_layout, list):
            for item in raw_layout:
                if _is_anchor(item) and item["anchor"] not in anchors:
                    anchors.append(item["anchor"])
                elif _is_anchor(item):
                    placeable = False  # two anchors with one name: the dashboard's own check reports them
        buckets: dict[str, list[_Section]] = {"start": [], "end": []}
        for n, s in enumerate(sections):
            if not placeable:
                break
            clash = [q for q in s.queries if q in merged_queries]
            if clash:
                sctx = spec._Ctx(s.part.label, s.lines, s.data)
                sctx.err(("sections", s.index, "queries", clash[0]), f'The dashboard "{slug}" already has a query '
                         f'called "{clash[0]}", so this section\'s query cannot use that name.',
                         hint="Rename the section's query (keep the app's prefix).")
                sec_errors.extend(sctx.errors)
                continue
            for q, v in s.queries.items():
                merged_queries[q] = v
                query_origin[q] = (s.part.label, ("sections", s.index, "queries", q), s.data)
            if s.anchor is not None:
                if s.anchor in anchors:
                    key, placed = f"after:{s.anchor}", f"after:{s.anchor}"
                else:
                    key, placed = "end", "end"
                    sctx = spec._Ctx(s.part.label, s.lines, s.data)
                    sctx.warn(("sections", s.index, "position"),
                              f'The section "{s.id}" goes after the anchor "{s.anchor}", but the dashboard "{slug}" '
                              "has no such anchor, so it was put at the end.",
                              hint=f"The dashboard's anchors are: {', '.join(anchors) or '(none)'}. A dashboard marks a "
                              "place with a layout item - anchor: <name>.")
                    sec_warnings.extend(sctx.warnings)
            else:
                key = placed = s.position
            buckets.setdefault(key, []).append(s)
            s.n = n
            entry.sections.append({"app": s.app.name, "id": s.id, "file": s.part.label, "placed": placed,
                                   "from": s.part.origin})
        if placeable:
            rows: list[Any] = []

            def emit(bucket: list[_Section]) -> None:
                for s in bucket:
                    for r, row in enumerate(s.layout):
                        sec_row_map[(s.n, r)] = len(rows)
                        row_origin[len(rows)] = (s.part.label, ("sections", s.index, "layout", r), s.data)
                        rows.append([_mark(c, s.app.name) for c in row])

            emit(buckets["start"])
            seen_anchors: set[str] = set()
            for r, item in enumerate(raw_layout):
                if _is_anchor(item) and item["anchor"] not in seen_anchors:
                    seen_anchors.add(item["anchor"])
                    emit(buckets.get(f"after:{item['anchor']}", []))
                    changed = True
                    continue
                base_row_map[r] = len(rows)
                row_origin[len(rows)] = (base.label, ("layout", r), base_data)
                rows.append(item)
            emit(buckets["end"])
            changed = changed or bool(sections)
            layout_value: Any = rows
        else:
            layout_value = raw_layout
            if isinstance(raw_layout, list):
                for r in range(len(raw_layout)):
                    base_row_map[r] = r
        composed = {}
        for k in _TOP_ORDER:
            if k == "app" and origin != "project":
                if base_data.get("app") not in (None, origin):
                    base_ctx.err(("app",), f"app is {str(base_data.get('app'))[:40]!r}, but this dashboard comes from "
                                 f"the app {origin}.", hint="Remove the app: line; easy-ui sets it.")
                composed["app"] = origin
                changed = changed or base_data.get("app") != origin
            elif k == "queries" and (merged_queries or "queries" in base_data):
                composed["queries"] = merged_queries
            elif k == "layout" and "layout" in base_data:
                composed["layout"] = layout_value
            elif k in base_data and k not in ("queries", "layout"):
                composed[k] = base_data[k]
        for k, v in base_data.items():
            if k not in composed:
                composed[k] = v
    else:
        composed = base_data  # type: ignore[assignment]

    def base_map(p: tuple[Any, ...]) -> tuple[Any, ...] | None:
        if len(p) >= 2 and p[0] == "layout" and isinstance(p[1], int):
            return ("layout", base_row_map[p[1]], *p[2:]) if p[1] in base_row_map else None
        return p

    lines: dict[tuple[Any, ...], int] = {}
    _translate_lines(base_lines, base_map, lines)
    for s in sections:
        if s.n < 0:
            continue

        def sec_map(p: tuple[Any, ...], s: _Section = s) -> tuple[Any, ...] | None:
            if len(p) >= 4 and p[0] == "sections" and p[1] == s.index:
                if p[2] == "queries" and p[3] in s.queries:
                    return ("queries", *p[3:])
                if p[2] == "layout" and isinstance(p[3], int) and (s.n, p[3]) in sec_row_map:
                    return ("layout", sec_row_map[(s.n, p[3])], *p[4:])
            return None

        _translate_lines(s.lines, sec_map, lines)

    def origin_of(p: tuple[Any, ...]) -> tuple[str, tuple[Any, ...], Any]:
        if len(p) >= 2 and p[0] == "layout" and isinstance(p[1], int) and p[1] in row_origin:
            file, prefix, data = row_origin[p[1]]
            return file, prefix + tuple(p[2:]), data
        if len(p) >= 2 and p[0] == "queries" and p[1] in query_origin:
            file, prefix, data = query_origin[p[1]]
            return file, prefix + tuple(p[2:]), data
        return base.label, p, base_data

    dash, partial, errors, warnings = spec.check_data(composed, lines, slug=slug, file=base.label, origin=origin_of)
    errors = base_ctx.errors + sec_errors + errors
    warnings = sec_warnings + warnings
    complete = not errors
    if partial is not None and bundle_path is not None:
        try:
            e2, w2 = spec.check_with_data(partial, bundle_path, vary=vary and complete)
        except EasyUIError as e:
            e2, w2 = [spec.Problem(base.label, "", e.message, None, e.hint)], []
        errors, warnings = errors + e2, warnings + w2
    entry.errors.extend(p.to_dict() for p in errors)
    entry.warnings.extend(p.to_dict() for p in warnings)
    if partial is not None:
        entry.title, entry.description = partial.title, partial.description
    if dash is None or errors:
        return entry
    if not changed:
        entry.data = base.data
    else:
        text = dump(composed, [x["file"] for x in entry.sources])
        back, _lines = spec._parse_yaml(text)
        if _canon(back) != _canon(composed):  # pragma: no cover - a safety net: the written file means the same
            entry.errors.append(_problem(base.label, f'The composed dashboard "{slug}" could not be written exactly, '
                                         "so it was left out.", "Please report this; meanwhile simplify the values "
                                         "in the dashboard or its sections."))
            return entry
        entry.data = text.encode("utf-8")
    entry.dashboard = dash
    return entry


def _is_anchor(item: Any) -> bool:
    return isinstance(item, dict) and set(item) == {"anchor"} and isinstance(item["anchor"], str) \
        and bool(spec.ANCHOR_RE.match(item["anchor"]))


def _mark(comp: Any, app: str) -> Any:
    if isinstance(comp, dict):
        c = dict(comp)
        c["app"] = app
        return c
    return comp


def _canon(v: Any) -> str:
    return json.dumps(v, sort_keys=True, default=lambda o: o.isoformat() if isinstance(o, _dt.date) else repr(o))


# ---------------------------------------------------------------- YAML out


class _Dumper(yaml.SafeDumper):
    pass


def _str(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    style = "|" if "\n" in value and not value.startswith((" ", "\t")) and value == value.rstrip(" \t") else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_Dumper.add_representer(str, _str)
_Dumper.ignore_aliases = lambda self, data: True  # type: ignore[method-assign]


def dump(data: Any, sources: list[str] | tuple[str, ...] = ()) -> str:
    """A composed dashboard as YAML text (no anchors or aliases), with a comment naming its source files."""
    head = ""
    if sources:
        head = ("# Composed by easy-ui from " + ", ".join(sources) + ".\n"
                "# Edit those files, not this one: easy-ui makes it again on validate, preview and publish.\n")
    body = yaml.dump(copy.deepcopy(data), Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=100,
                     default_flow_style=False)
    return head + body


# ---------------------------------------------------------------- the whole project


def compose(config: Any, *, bundle_path: Path | None = None, vary: bool = True, apps: Any = None) -> Composition:
    """Every final dashboard of the project, composed and checked (against the data when ``bundle_path`` is given)."""
    out = Composition()
    apps = tuple(config.apps if apps is None else apps)
    out.apps = apps
    project_dash = _read_project(config, "dashboards", spec.SLUG_RE, out.problems, out.notes)
    first = len(out.problems)
    project_sec = _read_project(config, "sections", OVERRIDE_RE, out.problems, out.notes)
    for prob in out.problems[first:]:  # a file name that is not <app name>.<dashboard>: name the right one
        stem = str(prob.get("file", "")).removeprefix("ui/sections/").removesuffix(".yaml")
        better = _override_name(stem, apps)
        if better:
            prob["hint"] = f"Rename it to ui/sections/{better}.yaml."
        elif apps:
            prob["hint"] = (f"Name it ui/sections/<app name>.<dashboard>.yaml with the app's full name, for example "
                            f"{apps[0].name}.overview.yaml.")
    out.project_files = list(project_dash.values()) + list(project_sec.values())
    app_dash: dict[str, list[tuple[Any, Part]]] = {}
    app_sec: dict[tuple[str, str], Part] = {}
    discovered: list[str] = []
    for app in apps:
        with app.open() as folder:
            for slug in app.dashboards:
                rel = f"dashboards/{slug}.yaml"
                got = folder.read(rel, MAX_FILE_BYTES, "a dashboard file")
                if slug not in discovered:
                    discovered.append(slug)
                if isinstance(got, dict):
                    out.problems.append(got)
                    continue
                app_dash.setdefault(slug, []).append((app, Part(app.file_label(rel), app.name, rel, got, _sha(got))))
            for slug, rel in app.contributes:
                got = folder.read(rel, MAX_FILE_BYTES, "a sections file")
                if isinstance(got, dict):
                    out.problems.append(got)
                    continue
                app_sec[(app.name, slug)] = Part(app.file_label(rel), app.name, rel, got, _sha(got))
    for slug in project_dash:
        if slug not in discovered:
            discovered.append(slug)

    finals: list[tuple[str, Part, str, str | None, str | None]] = []
    for slug in discovered:
        shipped = app_dash.get(slug, [])
        if slug in project_dash:
            replaces = shipped[0][0].name if shipped else None
            if shipped:
                names = " and ".join(a.name for a, _ in shipped)
                out.notes.append(f"ui/dashboards/{slug}.yaml replaces the dashboard {slug} of {names}.")
            finals.append((slug, project_dash[slug], "project", replaces, shipped[0][1].label if shipped else None))
        elif len(shipped) > 1:
            a, b = shipped[0][0].name, shipped[1][0].name
            out.problems.append(_problem(
                shipped[1][1].label, f"The apps {a} and {b} both have a dashboard called {slug}.",
                f"Add ui/dashboards/{slug}.yaml to the project to decide what it shows."))
        elif shipped:
            app, part = shipped[0]
            finals.append((slug, part, app.name, None, None))
    final_slugs = {f[0] for f in finals}

    for app in apps:
        for slug, rel in app.contributes:
            if slug not in final_slugs:
                out.warnings.append(_problem(
                    app.file_label(rel),
                    f'The app {app.name} adds sections to the dashboard "{slug}", but this project has no dashboard '
                    "with that name, so they are left out.",
                    f"That is fine if the project does not need them; to show them, add ui/dashboards/{slug}.yaml.",
                    level="warning"))
    for key, part in project_sec.items():
        m = OVERRIDE_RE.match(key)
        app_name, slug = m.group("app"), m.group("slug")  # type: ignore[union-attr]
        app = next((a for a in apps if a.name == app_name), None)
        why = None
        if app is None:
            why = f"this project has no app called {app_name}"
        elif app.contributes_to(slug) is None:
            why = f'the app {app_name} adds no sections to "{slug}"'
        elif slug not in final_slugs:
            why = f'this project has no dashboard called "{slug}"'
        if why:
            better = _override_name(key, apps) if app is None else None
            if better:
                hint = (f"{app_name} is not the app's full name: rename the file to ui/sections/{better}.yaml.")
            else:
                hint = ("Name it ui/sections/<app name>.<dashboard>.yaml for an app that adds sections to that "
                        "dashboard, or remove it." + (f" This project's apps: {', '.join(a.name for a in apps)}."
                                                     if apps else ""))
            out.warnings.append(_problem(
                part.label, f"{part.label} replaces sections, but {why}, so it changes nothing.", hint,
                level="warning"))

    for slug, base, origin, replaces, replaced_file in finals:
        contributions = []
        for app in apps:
            if app.contributes_to(slug) is None:
                continue
            part = project_sec.get(f"{app.name}.{slug}") or app_sec.get((app.name, slug))
            if part is not None:
                contributions.append((app, part))
        entry = _compose_one(slug, base, origin, contributions, bundle_path, vary)
        entry.replaces = replaces
        entry.replaced_file = replaced_file
        out.dashboards.append(entry)

    present = [d.slug for d in out.dashboards]
    missing = [s for s in getattr(config, "dashboards", ()) if s not in present and s not in app_dash] if present else []
    for s in missing:
        out.problems.append(_problem(
            "project.toml", f'[ui] dashboards lists "{s}", but neither the project (ui/dashboards/) nor any of its apps '
            "has a dashboard with that name.",
            "Fix the name in project.toml, or add the dashboard. See every dashboard with: easy-ui dashboards"))
    rank = {s: i for i, s in enumerate(getattr(config, "dashboards", ()))}
    out.order = sorted(present, key=lambda s: (rank.get(s, len(rank)), present.index(s)))
    return out


# ---------------------------------------------------------------- command line


def _origin_text(origin: str, file: str) -> str:
    return f"the project ({file})" if origin == "project" else f"the app {origin} ({file})"


def _placed_text(placed: str) -> str:
    if placed == "start":
        return "at the start"
    if placed == "end":
        return "at the end"
    return f'after the anchor "{placed.split(":", 1)[1]}"'


def _listing(comp: Composition) -> list[str]:
    lines = []
    if comp.apps:
        lines.append("Apps, in the order of [project] apps:")
        for a in comp.apps:
            own = ", ".join(a.dashboards) if a.dashboards else "none"
            adds = ", ".join(s for s, _ in a.contributes) or "none"
            lines.append(f"  {a.name} ({a.label}): its own dashboards: {own}; adds sections to: {adds}")
        lines.append("")
    lines.append("Dashboards, in menu order:")
    by_slug = {d.slug: d for d in comp.dashboards}
    for slug in comp.order:
        d = by_slug[slug]
        state = "" if d.ok else f"  [{len(d.errors)} problem(s): see easy-ui validate]"
        lines.append(f"  {slug}: from {_origin_text(d.origin, d.file)}{state}")
        if d.replaces:
            lines.append(f"      replaces the app {d.replaces}'s dashboard ({d.replaced_file})")
        for s in d.sections:
            how = " (the project's replacement)" if s.get("from") == "project" else ""
            lines.append(f'      + section "{s["id"]}" of {s["app"]}, {_placed_text(s["placed"])}: {s["file"]}{how}')
    if not comp.dashboards:
        lines.append("  (none yet: add ui/dashboards/<name>.yaml, or an app with [ui] dashboards)")
    return lines


def _write_out(out_dir: Path, comp: Composition) -> list[str]:
    if out_dir.is_symlink() or (out_dir.exists() and not out_dir.is_dir()):
        raise Refused(f"{out_dir} is a link or not a folder, so the composed dashboards were not written there.",
                      hint="Pick a new folder with --out.", code="out_refused")
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for d in comp.dashboards:
        if d.data is None:
            continue
        target = out_dir / f"{d.slug}.yaml"
        if target.is_symlink():
            raise Refused(f"{target} is a link, so easy-ui does not write through it.", hint="Remove the link first.",
                          code="out_refused")
        tmp = out_dir / f".{d.slug}.yaml.{secrets.token_hex(4)}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
        try:
            os.write(fd, d.data)
        finally:
            os.close(fd)
        os.replace(tmp, target)
        written.append(str(target))
    return written


def _cmd_compose(args: argparse.Namespace) -> Any:
    from easyui import bundle  # noqa: PLC0415
    from easyui.cli import Result, load_config  # noqa: PLC0415

    cfg = load_config(args)
    comp = compose(cfg, bundle_path=None if args.no_data else bundle.current_bundle(cfg), vary=False)
    data = comp.to_dict()
    lines = _listing(comp)
    if args.out:
        data["written"] = _write_out(Path(args.out).expanduser(), comp)
        lines.append(f"Wrote {len(data['written'])} composed dashboard file(s) to {args.out}.")
    for p in data["errors"][:20]:
        lines.append("  error:   " + _problem_text(p))
    for p in data["warnings"][:20]:
        lines.append("  warning: " + _problem_text(p))
    lines += comp.notes
    return Result(data, "\n".join(lines), 0 if comp.ok else 1)


def _problem_text(p: dict[str, Any]) -> str:
    return spec.Problem(p["file"], p.get("where", ""), p["problem"], p.get("line"), p.get("hint"), p.get("allowed"),
                        p.get("did_you_mean"), p.get("level", "error"), p.get("place")).text()


def _cmd_dashboards(args: argparse.Namespace) -> Any:
    from easyui.cli import Result, load_config  # noqa: PLC0415

    cfg = load_config(args)
    comp = compose(cfg, vary=False)
    overrides = []
    for d in comp.dashboards:
        if d.replaces:
            overrides.append({"file": d.file, "replaces": d.replaced_file, "app": d.replaces, "dashboard": d.slug})
        for s in d.sections:
            if s.get("from") == "project":
                app = next(a for a in comp.apps if a.name == s["app"])
                overrides.append({"file": s["file"], "replaces": app.file_label(app.contributes_to(d.slug) or ""),
                                  "app": s["app"], "dashboard": d.slug})
    for d in comp.dashboards:
        for app in comp.apps:
            rel = app.contributes_to(d.slug)
            key = f"ui/sections/{app.name}.{d.slug}.yaml"
            if rel and not any(s["app"] == app.name for s in d.sections) and any(
                    p.label == key for p in comp.project_files):
                overrides.append({"file": key, "replaces": app.file_label(rel), "app": app.name, "dashboard": d.slug,
                                  "removes": True})
    data = {
        "apps": [a.to_dict() for a in comp.apps],
        "dashboards": [{**d.to_dict(), "replaced_file": d.replaced_file} for d in comp.dashboards],
        "order": comp.order,
        "overrides": overrides,
        "warnings": comp.to_dict()["warnings"],
        "errors": comp.errors(),
        "notes": comp.notes,
    }
    lines = _listing(comp)
    if overrides:
        lines += ["", "What the project overrides:"]
        for o in overrides:
            verb = "removes the sections of" if o.get("removes") else "replaces"
            lines.append(f"  {o['file']} {verb} {o['replaces']}")
    if data["warnings"]:
        lines += ["", "Warnings:"] + ["  " + _problem_text(w) for w in data["warnings"][:20]]
    if comp.problems:
        lines += ["", "Problems:"] + ["  " + _problem_text(p) for p in comp.problems[:20]]
    return Result(data, "\n".join(lines))


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "compose", help="Show (or write) the dashboards as they are made from the project and its apps.",
        description="Compose every dashboard from the project's ui/dashboards/, its apps' dashboards and the sections "
        "apps add (with the project's replacements from ui/sections/), check them, and list where each part comes "
        "from. With --out DIR, also write the composed files there (what easy-ui publish stores).")
    p.add_argument("--out", metavar="DIR", help="write the composed dashboards as <name>.yaml files into this folder")
    p.add_argument("--no-data", action="store_true", help="do not check queries against the current data")
    p.set_defaults(func=_cmd_compose)
    p = subparsers.add_parser(
        "dashboards", help="List every dashboard, where it and each of its sections come from, and what overrides what.",
        description="List the project's dashboards in menu order: which come from the project and which from an app, "
        "the sections apps add to each and where they are placed, and which project files replace an app's.")
    p.set_defaults(func=_cmd_dashboards)


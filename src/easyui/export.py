"""``easy-ui export``: a static, self-contained copy of chosen dashboards, for sharing without a server.

What goes into the copy
-----------------------
Only what the chosen dashboards show, for one set of control values:

- the project's title, name and accent colour, and the dashboards' compiled form
  (titles, descriptions, texts, hints, units, components, and each control
  with its exported value as its only value: a choice keeps only the chosen
  option, a number has no min or max). Compiled dashboards never hold SQL, and
  only the queries a component uses are named;
- the result rows of those queries, run once now against the current
  dashboard data with those values, cut down to what the components show:
  only the columns they name (a table without ``columns`` shows them all),
  and only as many rows as they show (a number: the first row; a table with
  ``limit``: that many; a chart: all). No table, no database file, no other rows;
- the renderer (the same page code as the server's, with its data source
  swapped for the data in the page) and its style sheet and chart library.

``describe_payload(payload)`` lists exactly that, from the payload itself, and
is what the command prints before "and nothing else" (a test compares the
two). A picture (``easy-ui screenshot``, never shared as a page) keeps every
option of a control, so its controls look as they do on the live page.

Nothing of the sign-in (password hash, passkeys, sessions) and nothing of the
project's own data folder is read. A query that fails stops the export, so a
copy never carries an error message instead of data.

The folder::

    <out>/index.html                   the page, with the data in a <script type="application/json"> block
    <out>/assets/easyui-export.js      the renderer as one classic script (works when opened from a file)
    <out>/assets/style.css, icon.svg
    <out>/assets/vendor/echarts.min.js (+ its licence and notice)
    <out>/.easyui-export               a marker, so --force only ever replaces an earlier export

The page loads nothing from anywhere else and makes no network request (its
Content-Security-Policy says ``connect-src 'none'``) and asks search engines
not to index it. It works from any static host and when opened straight from
the disk.

``--single-file`` writes one HTML file instead (``--out FILE.html``), with the
style sheet, icon, chart library and renderer inside it (its policy allows
exactly those inline parts by their hashes), to open from the disk or send
directly.

Link mode (``--encrypt``)
-------------------------
The data (titles included) is encrypted with AES-256-GCM (a fresh random
256-bit key and 96-bit nonce, additional data ``easy-ui export v1``). The key
is never written into the folder: it is returned once, as the part of the link
after ``#`` (``#k=<43 characters>``), which browsers never send to the host.
The page decrypts it with WebCrypto, keeps the key in the running page only
(never in the browser's storage) and takes it out of the address bar; the
browser's history may still hold the address it was opened with. Sharing
again with a new key (a new export) makes old links stop working.

What link mode protects against: someone who finds or copies the files (on a
disk, a backup, a host's listing) without the link. What it does NOT protect
against: whoever serves the files. A host can change the page so that it
sends the key away the next time someone opens the link; only share a locked
copy through a host you trust, or send the single file directly.

The modules of the renderer are joined into one classic script by
``bundle_js()``, a deliberately small step that understands only the plain
``import { a, b } from "./x.js";`` / ``export ...`` forms the page code uses
and refuses anything else (no build tool, no third-party code).

Public API
----------
``MARKER``, ``EXPORT_CSP``, ``AAD``
``bundle_js(web_dir=WEB_DIR) -> str``
``parse_values(items) -> dict``          ``["days=30", "overview.kind=run"]`` -> ``{(slug|None, name): text}``.
``build_payload(config, *, dashboards=None, values=None, source=None, today=None) -> (payload, visible)``
``describe_payload(payload) -> list[str]``  every piece of the payload, in words (rows as counts).
``encrypt_payload(payload) -> (box, key_text)``
``export(config, *, out, dashboards=None, values=None, encrypt=False, working=False, fixture=False,
force=False, today=None, single_file=False) -> dict``
    ``{"out", "files", "encrypted", "fragment" (link mode: "#k=..."), "dashboards", "visible", "lines",
    "bytes", "single_file"}``. ``lines`` is ``describe_payload`` of what was written.
``register(subparsers)``  the ``export`` command.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import datetime as _dt
import json
import os
import re
import secrets
import shutil
import tempfile
from pathlib import Path
from typing import Any

from easyui import paths, security, spec
from easyui.appconfig import AppConfig
from easyui.errors import EasyUIError, NotFound, Refused, UsageError

WEB_DIR = Path(__file__).resolve().parent / "web"
MARKER = ".easyui-export"
AAD = b"easy-ui export v1"
EXPORT_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'none'; "
    "font-src 'self'; base-uri 'none'; form-action 'none'"
)
ENTRY = "app.js"
SWAP = {"api.js": "static-api.js"}  # the server's data source -> the data in the page
_IMPORT_RE = re.compile(r'^import \{([^}]*)\} from "\./([a-z][a-z0-9-]*\.js)";\s*$')
_EXPORT_DECL_RE = re.compile(r"^export (async function|function|class|const|let) ([A-Za-z_$][\w$]*)")
_EXPORT_LIST_RE = re.compile(r"^export \{([^}]*)\};\s*$")
_NAME_RE = re.compile(r"^[A-Za-z_$][\w$]*\Z")
_VALUE_RE = re.compile(r"^(?:([a-z0-9][a-z0-9-]{0,40})\.)?([a-z][a-z0-9_]{0,30})=(.*)\Z", re.S)
ASSETS = ("style.css", "icon.svg")
SINGLE_MARKER = "<!-- easy-ui export (single file) -->"
# Keys of the payload that only say how something is drawn, never what is shown.
_STRUCTURE = frozenset({"picture", "fixed", "id", "type", "format", "delta_format", "better", "stacked",
                        "readout", "phone", "spec", "t", "level", "truncated", "encrypted"})
VENDOR = ("echarts.min.js", "echarts.LICENSE.txt", "echarts.NOTICE.txt")


# ---------------------------------------------------------------- the renderer as one classic script


def _bundle_error(what: str) -> EasyUIError:
    return EasyUIError(
        f"The page code could not be prepared for the export ({what}), so nothing was written.",
        hint="Reinstall my-easy-ui (for example: uv sync); this is a problem in easy-ui, not in your dashboards.",
        code="export_bundle",
    )


def _names(text: str) -> list[str]:
    out = [n.strip() for n in text.split(",") if n.strip()]
    if not out or not all(_NAME_RE.match(n) for n in out):
        raise _bundle_error(f"an import or export list it does not understand: {text.strip()[:60]}")
    return out


def _module(name: str, source: str) -> tuple[list[tuple[list[str], str]], list[str], str]:
    imports: list[tuple[list[str], str]] = []
    exports: list[str] = []
    body: list[str] = []
    for line in source.split("\n"):
        stripped = line.strip()
        if line.startswith("import "):
            m = _IMPORT_RE.match(line)
            if not m:
                raise _bundle_error(f"an import in {name} it does not understand")
            imports.append((_names(m.group(1)), m.group(2)))
            continue
        if line.startswith("export "):
            m = _EXPORT_LIST_RE.match(line)
            if m:
                exports += _names(m.group(1))
                continue
            m = _EXPORT_DECL_RE.match(line)
            if not m:
                raise _bundle_error(f"an export in {name} it does not understand")
            exports.append(m.group(2))
            body.append(line[len("export "):])
            continue
        if re.search(r"\bimport\s*\(|\bimport\.meta\b(?!\.url)|^\s*export\b", stripped):
            raise _bundle_error(f"a module feature in {name} it does not support")
        body.append(line.replace("import.meta.url", "__base"))
    return imports, exports, "\n".join(body)


def bundle_js(web_dir: Path = WEB_DIR) -> str:
    """The page code as one classic script (modules cannot load from a file:// page)."""
    assets = Path(web_dir) / "assets"
    order: list[str] = []
    parsed: dict[str, tuple[list[tuple[list[str], str]], list[str], str]] = {}
    visiting: set[str] = set()

    def visit(name: str) -> None:
        name = SWAP.get(name, name)
        if name in parsed:
            return
        if name in visiting:
            raise _bundle_error(f"a circular import through {name}")
        visiting.add(name)
        try:
            source = (assets / name).read_text(encoding="utf-8")
        except OSError:
            raise _bundle_error(f"{name} is missing") from None
        imports, exports, body = _module(name, source)
        for _names_, dep in imports:
            visit(dep)
        parsed[name] = (imports, exports, body)
        visiting.discard(name)
        order.append(name)

    visit(ENTRY)
    parts = [
        "// easy-ui renderer for a static export: the page modules joined into one classic script by easy-ui export.",
        "(function () {",
        '"use strict";',
        "const __base = (document.currentScript && document.currentScript.src) || document.baseURI;",
        "const __m = Object.create(null);",
    ]
    for name in order:
        imports, exports, body = parsed[name]
        lines = [f"const {{ {', '.join(names)} }} = __m[{json.dumps(SWAP.get(dep, dep))}];" for names, dep in imports]
        parts.append(f"// ---- {name}")
        parts.append(f"__m[{json.dumps(name)}] = (function () {{")
        parts.extend(lines)
        parts.append(body)
        parts.append(f"return {{ {', '.join(dict.fromkeys(exports))} }};")
        parts.append("})();")
    parts.append("})();")
    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------- control values


def parse_values(items: list[str] | None) -> dict[tuple[str | None, str], str]:
    out: dict[tuple[str | None, str], str] = {}
    for item in items or []:
        m = _VALUE_RE.match(item or "")
        if not m:
            raise UsageError(
                f"--set {str(item)[:60]!r} is not understood.",
                hint="Write --set NAME=VALUE for every exported dashboard that has that control, or "
                "--set DASHBOARD.NAME=VALUE for one, for example --set days=30 --set overview.kind=run.",
            )
        out[(m.group(1), m.group(2))] = m.group(3)
    return out


def _value_for(p: spec.Param, raw: str) -> Any:
    if p.type == "int":
        try:
            return int(raw)
        except ValueError:
            return raw  # resolve_params refuses it with a plain message
    if p.type == "choice":
        for o in p.options or []:
            if str(o["value"]) == raw:
                return o["value"]
    return raw


# ---------------------------------------------------------------- the payload


def _source(config: AppConfig, *, working: bool, fixture: bool) -> tuple[Any, Any]:
    if working or fixture:
        from easyui import preview  # noqa: PLC0415

        prepared = preview.prepare(config, fixture=fixture)
        return prepared.source, prepared
    from easyui.publish import PublishedSource  # noqa: PLC0415

    return PublishedSource(config), None


def _used_queries(compiled: dict[str, Any]) -> list[str]:
    seen: dict[str, None] = {}
    for row in compiled.get("layout") or []:
        for comp in row:
            q = comp.get("query")
            if q:
                seen[q] = None
    return list(seen)


def _needs(compiled: dict[str, Any]) -> dict[str, tuple[set[str] | None, int | None]]:
    """query -> (the columns the components show, None = all; the rows they show, None = all)."""
    need: dict[str, tuple[set[str] | None, int | None]] = {}
    for row in compiled.get("layout") or []:
        for comp in row:
            q = comp.get("query")
            if not q:
                continue
            kind = comp.get("type")
            cols: set[str] | None
            rows: int | None
            if kind == "stat":
                cols, rows = {comp.get("value"), comp.get("delta")}, 1
            elif kind in ("line", "area", "bar"):
                cols, rows = {comp.get("x"), *(comp.get("y") or [])}, None
                if comp.get("partial") not in (None, "last"):
                    cols.add(comp["partial"])
            elif kind == "table":
                cols = {c["column"] for c in comp["columns"]} if comp.get("columns") else None
                rows = comp.get("limit") if isinstance(comp.get("limit"), int) else None
            else:  # a component this step does not know: keep everything it might show
                cols, rows = None, None
            have_cols, have_rows = need.get(q, (set(), 0))
            cols = None if have_cols is None or cols is None else have_cols | {c for c in cols if c}
            rows = None if have_rows is None or rows is None else max(have_rows, rows)
            need[q] = (cols, rows)
    return need


def _project(result: dict[str, Any], cols: set[str] | None, rows: int | None) -> dict[str, Any]:
    keep = [i for i, c in enumerate(result["columns"]) if cols is None or c in cols]
    picked = result["rows"] if rows is None else result["rows"][:rows]
    return {"columns": [result["columns"][i] for i in keep], "rows": [[r[i] for i in keep] for r in picked],
            "truncated": result["truncated"]}


def build_payload(
    config: AppConfig,
    *,
    dashboards: list[str] | None = None,
    values: dict[tuple[str | None, str], str] | None = None,
    source: Any,
    today: _dt.date | None = None,
    picture: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    today = today or _dt.date.today()
    values = values or {}
    from easyui.server import order_dashboards  # noqa: PLC0415 - the same order as the server's menu

    listed = order_dashboards(source.list(), config.dashboards)
    known = [d["slug"] for d in listed if d.get("slug") != "easy-ui-problems"]
    if dashboards:
        missing = [s for s in dashboards if s not in known]
        if missing:
            raise NotFound(
                f"There is no dashboard called {missing[0]!r} to export.",
                hint=("The dashboards are: " + ", ".join(known) + ".") if known else "Publish dashboards first: easy-ui publish",
            )
        chosen = [s for s in known if s in dashboards]
    else:
        chosen = known
    if not chosen:
        raise NotFound("There are no dashboards to export.", hint="Publish them first with: easy-ui publish")
    used_names: set[tuple[str | None, str]] = set()
    bundle_path = source.bundle_path()
    payload: dict[str, Any] = {
        "v": 1,
        "app": config.public(),
        "made": f"{today.day} {today.strftime('%b')} {today.year}",
        "picture": picture,
        "dashboards": [],
        "specs": {},
        "data": {},
    }
    visible: list[dict[str, Any]] = []
    for slug in chosen:
        dash = source.dashboard(slug)
        given = {}
        for name, p in dash.params.items():
            for key in ((slug, name), (None, name)):
                if key in values:
                    given[name] = _value_for(p, values[key])
                    used_names.add(key)
                    break
        resolved = spec.resolve_params(dash, given, today=today)
        compiled = spec.compile_dashboard(dash, today=today)
        queries = _used_queries(compiled)
        out = spec.run_queries(dash, queries, resolved, bundle_path=bundle_path, today=today, errors="inline")
        results = out["results"]
        failed = [q for q in queries if "error" in results.get(q, {})]
        if failed:
            err = results[failed[0]]["error"]
            raise Refused(
                f"The query {failed[0]!r} of the dashboard {slug!r} did not run ({err.get('message')}), so no copy was "
                "made: a shared copy never carries an error instead of numbers.",
                hint="Fix the dashboard (easy-ui validate), publish it, then export again.",
                code="export_query_failed",
            )
        for p in compiled["params"]:
            p["default"] = resolved[p["name"]]
            p.pop("default_rule", None)
            if not picture:  # the copy shows one value per control: no other options, no range
                if p["type"] == "choice":
                    p["options"] = [o for o in p.get("options") or [] if o["value"] == p["default"]][:1]
                for key in ("min", "max", "max_length"):
                    p.pop(key, None)
        compiled["queries"] = {q: compiled["queries"][q] for q in queries if q in compiled.get("queries", {})}
        compiled["fixed"] = "picture" if picture else True
        needs = _needs(compiled)
        shown = {q: _project(results[q], *needs.get(q, (None, None))) for q in queries}
        payload["dashboards"].append({"slug": slug, "title": dash.title, "description": dash.description})
        payload["specs"][slug] = compiled
        payload["data"][slug] = {"values": resolved, "results": shown}
        visible.append({
            "slug": slug,
            "title": dash.title,
            "values": resolved,
            "queries": [
                {"name": q, "columns": shown[q]["columns"], "rows": len(shown[q]["rows"]), "truncated": shown[q]["truncated"]}
                for q in queries
            ],
        })
    unused = [f"{s + '.' if s else ''}{n}" for (s, n) in values if (s, n) not in used_names]
    if unused:
        raise UsageError(
            f"--set {unused[0]} does not match a control of the exported dashboards, so nothing was exported.",
            hint="Use the control names from the dashboard files (under params:), for example --set days=30.",
        )
    return payload, visible


# ---------------------------------------------------------------- what the copy holds, in words


def _leaves(node: Any, skip: frozenset[str] = _STRUCTURE) -> list[str]:
    """Every text or number in ``node`` (lists and dicts walked), except under keys that are only structure."""
    out: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k not in skip:
                out += _leaves(v, skip)
    elif isinstance(node, list):
        for v in node:
            out += _leaves(v, skip)
    elif isinstance(node, (str, int, float)) and not isinstance(node, bool):
        out.append(str(node))
    return out


def _text_of(blocks: Any) -> str:
    parts = []
    for block in blocks or []:
        words = [w for w in _leaves(block, _STRUCTURE | {"href"}) if w]
        links = [w for w in _leaves(_only(block, "href")) if w]
        parts.append(" ".join(words) + (f" (link: {', '.join(links)})" if links else ""))
    return " / ".join(p for p in parts if p)


def _only(node: Any, key: str) -> Any:
    if isinstance(node, dict):
        return [node[key] if k == key else _only(v, key) for k, v in node.items()]
    if isinstance(node, list):
        return [_only(v, key) for v in node]
    return None


def _component_line(comp: dict[str, Any]) -> str:
    kind = comp.get("type")
    title = f' "{comp["title"]}"' if comp.get("title") else ""
    if kind == "text":
        return f"text{title}: {_text_of(comp.get('blocks'))}"
    rest = []
    for key, value in comp.items():
        if key in _STRUCTURE or key in ("title", "blocks") or value is None or value == [] or isinstance(value, bool):
            continue
        if key == "columns" and isinstance(value, list):
            value = ", ".join(f"{c['column']} as \"{c['label']}\"" for c in value if isinstance(c, dict))
        elif isinstance(value, list):
            value = ", ".join(str(v) for v in value)
        rest.append(f"{key} {value}")
    return f"{kind}{title}" + (f": {'; '.join(rest)}" if rest else "")


def describe_payload(payload: dict[str, Any]) -> list[str]:
    """Every piece of a copy's payload, in words. Rows are counted, not listed."""
    app = payload.get("app") or {}
    accent = f", accent colour {app['accent']}" if app.get("accent") else ""
    lines = [f'The project title "{app.get("title")}" (name {app.get("name")}{accent}) and the date {payload.get("made")}.']
    for d in payload.get("dashboards") or []:
        slug = d["slug"]
        spec_ = payload["specs"][slug]
        data = payload["data"][slug]
        about = f': "{d["description"]}"' if d.get("description") else ""
        lines.append(f'Dashboard "{d["title"]}" ({slug}){about}')
        for p in spec_.get("params") or []:
            value = p.get("default")
            options = p.get("options") or []
            chosen = next((o for o in options if o.get("value") == value), None)
            shown = json.dumps(value, ensure_ascii=False) + (f' (shown as "{chosen["label"]}")' if chosen else "")
            others = [f'{json.dumps(o.get("value"), ensure_ascii=False)} "{o.get("label")}"' for o in options if o is not chosen]
            extra = [f"{k} {p[k]}" for k in ("min", "max", "max_length") if p.get(k) is not None]
            lines.append(f'  control "{p.get("label")}" ({p.get("name")}) set to {shown}'
                         + (f"; other choices {', '.join(others)}" if others else "")
                         + (f"; {', '.join(extra)}" if extra else ""))
        for row in spec_.get("layout") or []:
            for comp in row:
                lines.append("  " + _component_line(comp))
        for name, res in (data.get("results") or {}).items():
            cut = ", cut short" if res.get("truncated") else ""
            cols = ", ".join(res.get("columns") or []) or "no columns"
            lines.append(f"  numbers of {name}: {len(res.get('rows') or [])} row(s) of {cols}{cut}")
    lines.append("No SQL, no tables, no other rows or columns and nothing of the sign-in are in the copy.")
    return lines


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def encrypt_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: PLC0415

    key = AESGCM.generate_key(bit_length=256)
    nonce = secrets.token_bytes(12)
    plain = json.dumps({**payload, "encrypted": True}, ensure_ascii=False, separators=(",", ":")).encode()
    ct = AESGCM(key).encrypt(nonce, plain, AAD)
    return {"v": 1, "enc": "A256GCM", "iv": _b64url(nonce), "ct": _b64url(ct)}, _b64url(key)


# ---------------------------------------------------------------- writing


def _json_for_html(data: Any) -> str:
    """JSON that is safe inside a <script type="application/json"> block (no </script>, no <!--)."""
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return text.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e").replace(
        "\u2028", "\\u2028").replace("\u2029", "\\u2029")


def _html(title: str, data_json: str, *, locked: bool) -> str:
    from html import escape  # noqa: PLC0415

    robots = '<meta name="robots" content="noindex, nofollow">\n'  # a shared copy is never meant for search engines
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<meta http-equiv="Content-Security-Policy" content="{EXPORT_CSP}">\n'
        '<meta name="referrer" content="no-referrer">\n'
        f"{robots}"
        '<meta name="color-scheme" content="light dark">\n'
        f"<title>{escape(title)}</title>\n"
        '<link rel="icon" href="assets/icon.svg" type="image/svg+xml">\n'
        '<link rel="stylesheet" href="assets/style.css">\n'
        f'<script type="application/json" id="easyui-export">{data_json}</script>\n'
        '<script defer src="assets/easyui-export.js"></script>\n'
        "</head>\n<body>\n"
        '<div id="app" class="app">\n  <p class="boot" role="status">Opening…</p>\n</div>\n'
        "<noscript>\n  <p class=\"noscript\">This page needs JavaScript to show the dashboards. Turn it on, then reload "
        "the page.</p>\n</noscript>\n</body>\n</html>\n"
    )


def _prepare_out(out: Path, force: bool) -> None:
    if out.is_symlink():
        raise Refused(f"{out} is a link, so easy-ui does not write the export through it.", hint="Choose a real folder.")
    if out.exists():
        if not out.is_dir():
            raise Refused(f"{out} exists and is not a folder, so nothing was written.", hint="Choose a new folder for the export.")
        entries = list(out.iterdir())
        # --force replaces only an earlier export: the marker, and nothing but what an export writes.
        ours = {e.name for e in entries} <= {MARKER, "index.html", "assets"} and (out / MARKER).is_file() and not any(
            e.is_symlink() for e in entries)
        if entries and not (force and ours):
            raise Refused(
                f"{paths.display_path(out)} is not empty, so the export was not written there (nothing is overwritten).",
                hint="Choose a new folder, or add --force to replace an earlier export in that folder (only an earlier "
                "easy-ui export is ever replaced).",
                code="not_empty",
            )


def _inline_script(js: str) -> str:
    # Inside <script>, only "</script" (any case) and "<!--" could end or change it.
    return re.sub(r"</(script)", r"<\\/\1", js, flags=re.I).replace("<!--", "<\\!--")


def _sha256_b64(text: str) -> str:
    import hashlib  # noqa: PLC0415

    return base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode("ascii")


def _single_html(title: str, data_json: str, js: str, web_dir: Path) -> str:
    """One self-contained page: style sheet, icon, chart library and renderer inside it."""
    from html import escape  # noqa: PLC0415

    assets = web_dir / "assets"
    css = (assets / "style.css").read_text(encoding="utf-8").replace("</", "<\\/")
    icon = base64.b64encode((assets / "icon.svg").read_bytes()).decode("ascii")
    charts = _inline_script((assets / "vendor" / "echarts.min.js").read_text(encoding="utf-8"))
    app = _inline_script(js)
    csp = (
        f"default-src 'none'; script-src 'sha256-{_sha256_b64(charts)}' 'sha256-{_sha256_b64(app)}'; "
        f"style-src 'sha256-{_sha256_b64(css)}'; img-src data:; connect-src 'none'; font-src 'none'; "
        "base-uri 'none'; form-action 'none'"
    )
    return (
        f"<!doctype html>\n{SINGLE_MARKER}\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<meta http-equiv="Content-Security-Policy" content="{csp}">\n'
        '<meta name="referrer" content="no-referrer">\n'
        '<meta name="robots" content="noindex, nofollow">\n'
        '<meta name="color-scheme" content="light dark">\n'
        f"<title>{escape(title)}</title>\n"
        f'<link rel="icon" href="data:image/svg+xml;base64,{icon}" type="image/svg+xml">\n'
        f"<style>{css}</style>\n"
        f'<script type="application/json" id="easyui-export">{data_json}</script>\n'
        "</head>\n<body>\n"
        '<div id="app" class="app">\n  <p class="boot" role="status">Opening…</p>\n</div>\n'
        "<noscript>\n  <p class=\"noscript\">This page needs JavaScript to show the dashboards. Turn it on, then reload "
        "the page.</p>\n</noscript>\n"
        f"<script>{charts}</script>\n<script>{app}</script>\n</body>\n</html>\n"
    )


def _prepare_single(out: Path, force: bool) -> None:
    if out.suffix.lower() != ".html":
        raise UsageError("With --single-file, --out names the HTML file to write.", hint="For example: --out share.html")
    if out.is_symlink():
        raise Refused(f"{out} is a link, so easy-ui does not write the export through it.", hint="Choose a real file.")
    if out.exists():
        ours = False
        if out.is_file() and force:
            with contextlib.suppress(OSError), open(out, "rb") as fh:
                ours = SINGLE_MARKER.encode() in fh.read(200)
        if not ours:
            raise Refused(
                f"{paths.display_path(out)} already exists, so the export was not written there (nothing is overwritten).",
                hint="Choose a new file name, or add --force to replace an earlier single-file easy-ui export.",
                code="not_empty",
            )


def _write_tree(tmp: Path, html: str, js: str, web_dir: Path) -> list[str]:
    files = []

    def put(rel: str, data: bytes) -> None:
        p = tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        os.chmod(p, 0o644)
        files.append(rel)

    put("index.html", html.encode())
    put("assets/easyui-export.js", js.encode())
    for name in ASSETS:
        put(f"assets/{name}", (web_dir / "assets" / name).read_bytes())
    for name in VENDOR:
        put(f"assets/vendor/{name}", (web_dir / "assets" / "vendor" / name).read_bytes())
    put(MARKER, b"Made by easy-ui export. easy-ui export --force may replace this folder.\n")
    return files


def export(
    config: AppConfig,
    *,
    out: str | Path,
    dashboards: list[str] | None = None,
    values: dict[tuple[str | None, str], str] | None = None,
    encrypt: bool = False,
    working: bool = False,
    fixture: bool = False,
    force: bool = False,
    today: _dt.date | None = None,
    web_dir: Path = WEB_DIR,
    picture: bool = False,
    single_file: bool = False,
) -> dict[str, Any]:
    out = Path(out).expanduser()
    if single_file:
        _prepare_single(out, force)
    else:
        _prepare_out(out, force)
    security.verify_vendored(web_dir)
    source, prepared = _source(config, working=working, fixture=fixture)
    try:
        payload, visible = build_payload(config, dashboards=dashboards, values=values, source=source, today=today,
                                         picture=picture)
    finally:
        if prepared is not None:
            prepared.close()
    js = bundle_js(web_dir)
    fragment = None
    title = "Shared dashboards" if encrypt else config.title
    if encrypt:
        box, key = encrypt_payload(payload)
        data_json = _json_for_html(box)
        fragment = f"#k={key}"
    else:
        data_json = _json_for_html(payload)
    lines = describe_payload(payload)
    out.parent.mkdir(parents=True, exist_ok=True)
    if single_file:
        page = _single_html(title, data_json, js, web_dir).encode()
        fd, tmp_name = tempfile.mkstemp(prefix=".easyui-export-", suffix=".html", dir=out.parent)
        try:
            os.fchmod(fd, 0o644)
            with os.fdopen(fd, "wb") as fh:
                fh.write(page)
            os.replace(tmp_name, out)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise
        return {
            "app": config.name, "out": str(out), "files": [out.name], "encrypted": encrypt, "fragment": fragment,
            "source": "working files" if (working or fixture) else "published", "dashboards": [d["slug"] for d in visible],
            "visible": visible, "lines": lines, "bytes": len(page), "single_file": True,
        }
    html = _html(title, data_json, locked=encrypt)
    tmp = Path(tempfile.mkdtemp(prefix=".easyui-export-", dir=out.parent))
    try:
        os.chmod(tmp, 0o755)
        files = _write_tree(tmp, html, js, web_dir)
        if out.exists():
            old = out.parent / f".easyui-export-old-{secrets.token_hex(4)}"
            os.rename(out, old)
            os.rename(tmp, out)
            shutil.rmtree(old, ignore_errors=True)
        else:
            os.rename(tmp, out)
    except BaseException:
        with contextlib.suppress(OSError):
            shutil.rmtree(tmp)
        raise
    size = sum((out / f).stat().st_size for f in files)
    return {
        "app": config.name,
        "out": str(out),
        "files": sorted(files),
        "encrypted": encrypt,
        "fragment": fragment,
        "source": "working files" if (working or fixture) else "published",
        "dashboards": [d["slug"] for d in visible],
        "visible": visible,
        "lines": lines,
        "bytes": size,
        "single_file": False,
    }


def _visible_lines(res: dict[str, Any]) -> list[str]:
    who = (
        "Anyone who has the whole link (with the part after #) can see"
        if res["encrypted"] else
        "Anyone who can open " + ("this file" if res.get("single_file") else "these files (or the site they are put on)")
        + " can see"
    )
    return [f"{who} exactly this, and nothing else:", *("  " + line for line in res["lines"])]


def _cmd_export(args: argparse.Namespace) -> Any:
    from easyui.cli import Result, load_config  # noqa: PLC0415

    config = load_config(args)
    res = export(
        config,
        out=args.out,
        dashboards=args.dashboard or None,
        values=parse_values(args.set),
        encrypt=args.encrypt,
        working=args.working,
        fixture=args.fixture,
        force=args.force,
        single_file=args.single_file,
    )
    what = "one file" if res["single_file"] else "a static copy"
    lines = [f"Wrote {what} of {len(res['dashboards'])} dashboard(s) to {res['out']} ({res['bytes']:,} bytes; "
             "it needs no server and loads nothing from elsewhere)."]
    if res["encrypted"]:
        lines.append("The data is locked (AES-256-GCM). It opens only with this ending of the link, which is not "
                     "stored anywhere, so keep it now:")
        lines.append(f"  {res['fragment']}")
        where = "file" if res["single_file"] else "folder"
        lines.append(f"Add it to the address where the {where} is put, for example https://example.org/" + res["fragment"])
        lines.append("This protects the numbers from someone who finds or copies the files without the link. It does "
                     "not protect them from whoever serves the files: a host could change the page to send the key "
                     "away when the link is opened. Use a host you trust, or send the single file (--single-file) "
                     "directly. The browser's history may keep the whole link.")
    lines += _visible_lines(res)
    return Result(res, "\n".join(lines))


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "export",
        help="Make a static copy of chosen dashboards (no server needed), optionally locked with a link key.",
        description="Write a self-contained folder (index.html and its files), or with --single-file one HTML file, "
        "with the chosen published dashboards and only the numbers they show for one set of control values. It needs "
        "no server and makes no network request. With --encrypt the data is locked with AES-256-GCM and opens only "
        "with the key at the end of the link: that protects it from someone who finds the files, not from whoever "
        "serves them.",
    )
    p.add_argument("--out", required=True, metavar="DIR", help="a new or empty folder to write the copy into "
                   "(with --single-file: the .html file to write)")
    p.add_argument("--single-file", action="store_true",
                   help="write one self-contained .html file (to open from the disk or send directly)")
    p.add_argument("--dashboard", action="append", metavar="NAME", help="a dashboard to include (repeatable; default: all)")
    p.add_argument("--set", action="append", metavar="[DASHBOARD.]NAME=VALUE",
                   help="a control value to export (default: each control's default); repeatable")
    p.add_argument("--encrypt", action="store_true", help="lock the data; the key is only in the link (after #k=)")
    p.add_argument("--working", action="store_true", help="export the dashboard files as they are now (not the published ones)")
    p.add_argument("--fixture", action="store_true", help="with the made-up sample data in ui/fixtures/ (implies --working)")
    p.add_argument("--force", action="store_true", help="replace an earlier export in --out")
    p.set_defaults(func=_cmd_export)

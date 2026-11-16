"""``easy-ui export``: a static, self-contained copy of chosen dashboards, for sharing without a server.

What goes into the copy
-----------------------
Only what the chosen dashboards show, for one set of control values:

- the dashboards' compiled form (titles, texts, components, the params with the
  exported values as their only values). Compiled dashboards never hold SQL,
  and only the queries a component uses are named;
- the result rows of those queries, run once now against the current
  dashboard data with those values. No table, no database file, no other rows;
- the renderer (the same page code as the server's, with its data source
  swapped for the data in the page) and its style sheet and chart library.

Nothing of the sign-in (password hash, passkeys, sessions) and nothing of the
project's own data folder is read. A query that fails stops the export, so a
copy never carries an error message instead of data.

The folder::

    <out>/index.html                   the page, with the data in a <script type="application/json"> block
    <out>/assets/easyui-export.js      the renderer as one classic script (works when opened from a file)
    <out>/assets/style.css, icon.svg
    <out>/assets/vendor/echarts.min.js (+ its licence and notice)
    <out>/ui/theme.css, ui/styles/*.css, ui/assets/...   the project's look (theme, stylesheets, fonts and
                                       pictures), exactly as the server serves them, when it has one
    <out>/.easyui-export               a marker, so --force only ever replaces an earlier export

The look is never locked by ``--encrypt`` (a page can only use stylesheets, fonts and pictures that are
files); the command says which of these files the copy holds.

The page loads nothing from anywhere else and makes no network request (its
Content-Security-Policy says ``connect-src 'none'``). It works from any
static host and when opened straight from the disk.

Link mode (``--encrypt``)
-------------------------
The data (titles included) is encrypted with AES-256-GCM (a fresh random
256-bit key and 96-bit nonce, additional data ``easy-ui export v1``). The key
is never written into the folder: it is returned once, as the part of the link
after ``#`` (``#k=<43 characters>``), which browsers never send to the host.
The page decrypts it with WebCrypto, keeps the key for that browser tab only
and takes it out of the address bar. Sharing again with a new key (a new
export) makes old links stop working.

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
``encrypt_payload(payload) -> (box, key_text)``
``export(config, *, out, dashboards=None, values=None, encrypt=False, working=False, fixture=False,
force=False, today=None) -> dict``
    ``{"out", "files", "encrypted", "fragment" (link mode: "#k=..."), "dashboards", "visible", "bytes"}``.
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

from easyui import paths, spec, styles
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


def _look(config: AppConfig, source: Any, *, working: bool) -> styles.StyleSet:
    """The theme, stylesheets and assets that go into the copy."""
    if working:
        checked = styles.check(styles.read_project_look(config))
        if not checked.ok:
            first = checked.errors[0]
            raise Refused(
                f"The look of the dashboards has a mistake ({first['file']}: {first['problem']}), so no copy was made.",
                hint="Fix it (easy-ui validate shows every problem), then export again.",
                code="export_look_invalid",
            )
        return checked.styles or styles.EMPTY
    get = getattr(source, "styles", None)
    return get() if callable(get) else styles.EMPTY


def _used_queries(compiled: dict[str, Any]) -> list[str]:
    seen: dict[str, None] = {}
    for row in compiled.get("layout") or []:
        for comp in row:
            q = comp.get("query")
            if q:
                seen[q] = None
    return list(seen)


def build_payload(
    config: AppConfig,
    *,
    dashboards: list[str] | None = None,
    values: dict[tuple[str | None, str], str] | None = None,
    source: Any,
    today: _dt.date | None = None,
    picture: bool = False,
    look: styles.StyleSet | None = None,
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
    look = look if look is not None else styles.EMPTY
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
        "styles": look.public(),
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
        compiled["queries"] = {q: compiled["queries"][q] for q in queries if q in compiled.get("queries", {})}
        compiled["fixed"] = "picture" if picture else True
        payload["dashboards"].append({"slug": slug, "title": dash.title, "description": dash.description})
        payload["specs"][slug] = compiled
        payload["data"][slug] = {"values": resolved, "results": {q: results[q] for q in queries}}
        visible.append({
            "slug": slug,
            "title": dash.title,
            "values": resolved,
            "queries": [
                {"name": q, "columns": results[q]["columns"], "rows": len(results[q]["rows"]), "truncated": results[q]["truncated"]}
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

    robots = '<meta name="robots" content="noindex, nofollow">\n' if locked else ""
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
        ours = {e.name for e in entries} <= {MARKER, "index.html", "assets", "ui"} and (out / MARKER).is_file() and not any(
            e.is_symlink() for e in entries)
        if entries and not (force and ours):
            raise Refused(
                f"{paths.display_path(out)} is not empty, so the export was not written there (nothing is overwritten).",
                hint="Choose a new folder, or add --force to replace an earlier export in that folder (only an earlier "
                "easy-ui export is ever replaced).",
                code="not_empty",
            )


def _write_tree(tmp: Path, html: str, js: str, web_dir: Path, look: styles.StyleSet | None = None) -> list[str]:
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
    for rel, data in (look.files() if look is not None else {}).items():
        put(f"ui/{rel}", data)
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
) -> dict[str, Any]:
    out = Path(out).expanduser()
    _prepare_out(out, force)
    source, prepared = _source(config, working=working, fixture=fixture)
    try:
        look = _look(config, source, working=working or fixture)
        payload, visible = build_payload(config, dashboards=dashboards, values=values, source=source, today=today,
                                         picture=picture, look=look)
    finally:
        if prepared is not None:
            prepared.close()
    js = bundle_js(web_dir)
    fragment = None
    if encrypt:
        box, key = encrypt_payload(payload)
        html = _html("Shared dashboards", _json_for_html(box), locked=True)
        fragment = f"#k={key}"
    else:
        html = _html(config.title, _json_for_html(payload), locked=False)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".easyui-export-", dir=out.parent))
    try:
        os.chmod(tmp, 0o755)
        files = _write_tree(tmp, html, js, web_dir, look)
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
        "look": sorted(f for f in files if f.startswith("ui/")),
        "bytes": size,
    }


def _visible_lines(res: dict[str, Any]) -> list[str]:
    who = (
        "Anyone who has the whole link (with the part after #) can see"
        if res["encrypted"] else
        "Anyone who can open these files (or the site they are put on) can see"
    )
    lines = [f"{who}, and nothing else:"]
    for d in res["visible"]:
        vals = ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in d["values"].items()) or "no controls"
        lines.append(f"  {d['title']} ({d['slug']}), for {vals}:")
        for q in d["queries"]:
            cut = ", cut short" if q["truncated"] else ""
            lines.append(f"    {q['rows']} row(s) of {', '.join(q['columns'])}{cut}")
    look = res.get("look") or []
    if look:
        locked = " These are not locked, even with --encrypt: anyone with the files can see them." if res["encrypted"] else ""
        lines.append(f"  The look: {', '.join(look[:10])}{' and more' if len(look) > 10 else ''}.{locked}")
    lines.append("No SQL, no tables and nothing of the sign-in are in the copy.")
    return lines


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
    )
    lines = [f"Wrote a static copy of {len(res['dashboards'])} dashboard(s) to {res['out']} ({res['bytes']:,} bytes; "
             "it needs no server and loads nothing from elsewhere)."]
    if res["encrypted"]:
        lines.append("The data is locked (AES-256-GCM). It opens only with this ending of the link, which is not "
                     "stored anywhere, so keep it now:")
        lines.append(f"  {res['fragment']}")
        lines.append("Add it to the address where the folder is put, for example https://example.org/" + res["fragment"])
    lines += _visible_lines(res)
    return Result(res, "\n".join(lines))


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "export",
        help="Make a static copy of chosen dashboards (no server needed), optionally locked with a link key.",
        description="Write a self-contained folder (index.html and its files) with the chosen published dashboards "
        "and only their numbers for one set of control values. It needs no server and makes no network request. "
        "With --encrypt the data is locked with AES-256-GCM and opens only with the key at the end of the link.",
    )
    p.add_argument("--out", required=True, metavar="DIR", help="a new or empty folder to write the copy into")
    p.add_argument("--dashboard", action="append", metavar="NAME", help="a dashboard to include (repeatable; default: all)")
    p.add_argument("--set", action="append", metavar="[DASHBOARD.]NAME=VALUE",
                   help="a control value to export (default: each control's default); repeatable")
    p.add_argument("--encrypt", action="store_true", help="lock the data; the key is only in the link (after #k=)")
    p.add_argument("--working", action="store_true", help="export the dashboard files as they are now (not the published ones)")
    p.add_argument("--fixture", action="store_true", help="with the made-up sample data in ui/fixtures/ (implies --working)")
    p.add_argument("--force", action="store_true", help="replace an earlier export in --out")
    p.set_defaults(func=_cmd_export)

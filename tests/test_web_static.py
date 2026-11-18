"""Static checks on the browser renderer in src/easyui/web.

No server and no browser: these read the files. They make sure that
- the page has no inline script or style, and declares a strict CSP;
- our own JavaScript never turns text into markup or code;
- nothing loads from another site;
- every file the page or a module refers to exists, and no module is orphaned;
- the vendored ECharts file matches the hash recorded in vendor/MANIFEST.json;
- the JavaScript formats give exactly the texts of the Python reference
  (easyui.formats), when Node.js is installed.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

from easyui import formats

WEB = Path(__file__).resolve().parents[1] / "src" / "easyui" / "web"
ASSETS = WEB / "assets"
VENDOR = ASSETS / "vendor"
OUR_JS = sorted(p for p in ASSETS.rglob("*.js") if VENDOR not in p.parents)
OUR_CSS = sorted(p for p in ASSETS.rglob("*.css") if VENDOR not in p.parents)
HTML_FILES = sorted(WEB.rglob("*.html"))


# ---- HTML -------------------------------------------------------------------


class _Collect(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.script_text: list[str] = []
        self._in_script = False

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        self._in_script = tag == "script"

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_script = False

    def handle_data(self, data):
        if self._in_script and data.strip():
            self.script_text.append(data)


def _parse(path: Path) -> _Collect:
    p = _Collect()
    p.feed(path.read_text(encoding="utf-8"))
    return p


def test_files_exist():
    assert (WEB / "index.html").is_file()
    assert (ASSETS / "app.js").is_file()
    assert (ASSETS / "style.css").is_file()
    assert OUR_JS, "no JavaScript found"


@pytest.mark.parametrize("path", HTML_FILES, ids=lambda p: p.name)
def test_html_has_no_inline_script_or_style(path):
    page = _parse(path)
    assert not page.script_text, "inline script found"
    for tag, attrs in page.tags:
        assert tag != "style", "a <style> element is inline style"
        assert "style" not in attrs, f"<{tag} style=...> is inline style"
        for name in attrs:
            assert not name.startswith("on"), f"<{tag} {name}=...> is an inline event handler"
        if tag == "script":
            assert attrs.get("src"), "every script must load from a file"
        for name in ("href", "src"):
            value = attrs.get(name) or ""
            assert not value.lower().startswith(("javascript:", "data:", "http:", "//")), f"{tag} {name}={value}"


def test_index_declares_strict_csp():
    page = _parse(WEB / "index.html")
    csp = [a.get("content") or "" for t, a in page.tags if t == "meta" and (a.get("http-equiv") or "").lower() == "content-security-policy"]
    assert len(csp) == 1
    policy = csp[0]
    assert "script-src 'self'" in policy
    assert "style-src 'self'" in policy
    assert "default-src 'none'" in policy
    assert "connect-src 'self'" in policy
    assert "unsafe-inline" not in policy and "unsafe-eval" not in policy
    lang = [a.get("lang") for t, a in page.tags if t == "html"]
    assert lang == ["en"]


def test_server_csp_header_matches_the_page_policy():
    """The server's header is the main policy: it must be exactly the meta policy (plus frame-ancestors)."""
    from easyui import security

    page = _parse(WEB / "index.html")
    meta = next(a.get("content") for t, a in page.tags if t == "meta" and (a.get("http-equiv") or "").lower() == "content-security-policy")

    def directives(policy: str) -> dict[str, str]:
        out = {}
        for part in policy.split(";"):
            name, _, value = part.strip().partition(" ")
            if name:
                out[name] = value.strip()
        return out

    header = directives(security.CSP)
    assert header.pop("frame-ancestors") == "'none'"
    assert header == directives(meta)


# ---- our JavaScript -----------------------------------------------------------

FORBIDDEN = {
    "innerHTML": re.compile(r"\binnerHTML\b"),
    "outerHTML": re.compile(r"\bouterHTML\b"),
    "insertAdjacentHTML": re.compile(r"\binsertAdjacentHTML\b"),
    "document.write": re.compile(r"\bdocument\s*\.\s*write(ln)?\b"),
    "eval": re.compile(r"(?<![\w.])eval\s*\("),
    "new Function": re.compile(r"\bnew\s+Function\b"),
    "Function(": re.compile(r"(?<![\w.])Function\s*\("),
    "string timer": re.compile(r"\bset(Timeout|Interval)\s*\(\s*[\"'`]"),
    "createContextualFragment": re.compile(r"createContextualFragment"),
    "DOMParser": re.compile(r"\bDOMParser\b"),
    "srcdoc": re.compile(r"\bsrcdoc\b"),
    "style attribute": re.compile(r"setAttribute\(\s*[\"']style[\"']"),
    "cssText": re.compile(r"\bcssText\b"),
    "javascript: URL": re.compile(r"javascript:", re.I),
    "import from URL": re.compile(r"""\bimport\s*\(?\s*["'](https?:)?//"""),
    "localStorage": re.compile(r"\b(local|session)Storage\b"),
}


@pytest.mark.parametrize("path", OUR_JS, ids=lambda p: p.name)
def test_js_uses_no_markup_or_code_from_text(path):
    text = path.read_text(encoding="utf-8")
    found = [name for name, rx in FORBIDDEN.items() if rx.search(text)]
    # A locked export keeps its link key in the script only: no storage of any kind, in any file.
    assert not found, f"{path.name} uses {found}"


# The SVG namespace, and http://localhost in text that tells people where passkeys work (never fetched).
ALLOWED_URLS = {"http://www.w3.org/2000/svg", "http://localhost"}


@pytest.mark.parametrize("path", OUR_JS + OUR_CSS + HTML_FILES + [ASSETS / "icon.svg"], ids=lambda p: p.name)
def test_no_requests_to_other_sites(path):
    text = path.read_text(encoding="utf-8")
    urls = set(re.findall(r"""https?://[^\s"'`)<>]+""", text))
    urls |= {m for m in re.findall(r"""["'`(](//[A-Za-z0-9.-]+\.[A-Za-z]{2,}[^\s"'`)]*)""", text)}
    assert urls <= ALLOWED_URLS, f"{path.name} refers to {sorted(urls - ALLOWED_URLS)}"


@pytest.mark.parametrize("path", OUR_CSS, ids=lambda p: p.name)
def test_css_loads_nothing_and_has_no_expressions(path):
    text = path.read_text(encoding="utf-8")
    assert "@import" not in text
    assert not re.search(r"url\(", text), "CSS must not load files"
    assert "expression(" not in text


def test_charts_give_echarts_only_data_and_our_functions():
    text = (ASSETS / "charts.js").read_text(encoding="utf-8")
    # Every formatter is one of our functions, never a template string.
    for m in re.finditer(r"formatter:\s*([^\n]*)", text):
        assert "=>" in m.group(1), f"formatter must be a function: {m.group(0)}"
    assert "rich:" not in text
    # ECharts' own tooltip box (which would render HTML) stays off.
    assert "showContent: false" in text
    assert re.search(r"tooltip:\s*\{", text)


def test_links_only_https():
    dom = (ASSETS / "dom.js").read_text(encoding="utf-8")
    assert 'url.protocol === "https:"' in dom
    # Only safeLink, routeLink and the skip link set an href.
    for path in OUR_JS:
        for line in path.read_text(encoding="utf-8").splitlines():
            if re.search(r"\.href\s*=", line):
                # sheet.href: the project's stylesheets, same-origin addresses built by look.js (ui/...).
                assert any(ok in line for ok in ("a.href = url", "a.href = hash", 'skip.href = "#main"',
                                                 "sheet.href = url", "sheet.href = want")), f"{path.name}: {line.strip()}"


# ---- references -----------------------------------------------------------------

IMPORT_RX = re.compile(r"""(?:^|\n)\s*import\s+(?:[^"';]+?\s+from\s+)?["']([^"']+)["']""")
URL_RX = re.compile(r"""new URL\(\s*["']([^"']+)["']\s*,\s*import\.meta\.url\s*\)""")


def _module_refs(path: Path) -> list[Path]:
    text = path.read_text(encoding="utf-8")
    refs = []
    for spec in IMPORT_RX.findall(text) + URL_RX.findall(text):
        assert spec.startswith("./") or not spec.startswith(("/", "http", "//")), f"{path.name}: {spec}"
        refs.append((path.parent / spec).resolve())
    return refs


def test_every_referenced_asset_exists():
    page = _parse(WEB / "index.html")
    refs = []
    for tag, attrs in page.tags:
        for name in ("href", "src"):
            value = attrs.get(name)
            if value and tag in ("link", "script", "img"):
                assert value.startswith("assets/"), f"{tag} {name}={value} must be a relative asset path"
                refs.append((WEB / value).resolve())
    for js in OUR_JS:
        refs.extend(_module_refs(js))
    missing = [str(r.relative_to(WEB)) for r in refs if not r.is_file()]
    assert not missing, f"missing files: {missing}"


def test_no_orphan_modules():
    seen: set[Path] = set()
    # static-api.js replaces api.js in a static export (easy-ui export), so it is a root too.
    todo = [(ASSETS / "app.js").resolve(), (ASSETS / "static-api.js").resolve()]
    while todo:
        p = todo.pop()
        if p in seen or p.suffix != ".js" or VENDOR in p.parents:
            continue
        seen.add(p)
        todo.extend(_module_refs(p))
    orphans = sorted(p.name for p in OUR_JS if p.resolve() not in seen)
    assert not orphans, f"modules nothing imports: {orphans}"


def test_css_tokens_have_light_and_dark_values():
    css = (ASSETS / "style.css").read_text(encoding="utf-8")
    root, _, rest = css.partition("@media (prefers-color-scheme: dark)")
    assert rest, "no dark scheme"
    dark = rest[: rest.index("\n}\n")]
    light_tokens = set(re.findall(r"(--base-[a-z0-9-]+):", root))
    dark_tokens = set(re.findall(r"(--base-[a-z0-9-]+):", dark))
    assert light_tokens and light_tokens == dark_tokens
    assert ":focus-visible" in css
    assert "prefers-reduced-motion" in css
    # theme.js reads these tokens by name.
    theme = (ASSETS / "theme.js").read_text(encoding="utf-8")
    for token in re.findall(r'cssToken\("(--[a-z0-9-]+)"', theme):
        assert token in light_tokens, token


# ---- vendor ---------------------------------------------------------------------


def test_vendor_files_match_manifest():
    manifest = json.loads((VENDOR / "MANIFEST.json").read_text(encoding="utf-8"))
    listed = manifest["files"]
    present = {p.name for p in VENDOR.iterdir() if p.is_file() and p.name != "MANIFEST.json"}
    assert present == set(listed), "every vendor file must be listed, and every listed file present"
    for name, info in listed.items():
        digest = hashlib.sha256((VENDOR / name).read_bytes()).hexdigest()
        assert digest == info["sha256"], f"{name} changed: {digest}"
        assert re.fullmatch(r"\d+\.\d+\.\d+", info["version"]), "versions are pinned exactly"
        assert info["source"].startswith("https://registry.npmjs.org/"), info["source"]
        assert info["version"] in info["source"]
    echarts = listed["echarts.min.js"]
    assert echarts["package"] == "echarts" and echarts["licence"] == "Apache-2.0"
    assert echarts["source_integrity"].startswith("sha512-")
    licence = (VENDOR / "echarts.LICENSE.txt").read_text(encoding="utf-8")
    assert "Apache License" in licence


def test_echarts_loaded_only_from_our_vendor_folder():
    charts = (ASSETS / "charts.js").read_text(encoding="utf-8")
    assert 'new URL("vendor/echarts.min.js", import.meta.url)' in charts
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert "echarts" not in html, "ECharts loads on demand, not on the sign-in page"


# ---- formats: JavaScript against the Python reference ----------------------------

NODE = shutil.which("node")

RUNNER = """
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
const mod = await import(pathToFileURL(process.argv[2]).href);
const cases = JSON.parse(readFileSync(process.argv[3], "utf8"));
const out = { names: mod.FORMAT_NAMES, null_text: mod.NULL_TEXT, months: mod.MONTHS, bad: [] };
for (const [fmt, value, want] of cases) {
  let got;
  try { got = mod.formatValue(fmt, value); } catch (e) { got = "ERROR " + e.message; }
  if (got !== want) out.bad.push({ fmt, value, want, got });
}
let unknown = false;
try { mod.formatValue("nope", 1); } catch { unknown = true; }
out.unknown_raises = unknown;
console.log(JSON.stringify(out));
"""


def _cases() -> list[list]:
    cases: list[list] = []
    for name, info in formats.FORMATS.items():
        for value, text in info["examples"]:
            cases.append([name, value, text])
    rng = random.Random(11)
    values: list = [
        0, -0.0, 0.5, -0.5, 1.5, 2.5, -2.5, 0.005, 0.015, 0.125, 1e-7, 999.5, 999.4999, 1e15,
        123456789.125, 2.675, 1.005, 0.0285, 0.0005, -0.0004, 59.5, 3599.5, 86399.9, 1e12, -1e12,
        "12.5", " 7 ", "-3", "00042", "abc", "", "2026-03-14", "2026-02-29", "2024-02-29",
        "0000-01-01", "2026-03-14 07:05", "2026-03-14T23:59:59Z", "x2026", 1700000000, 1700000000.9,
        -1, 253402300799,
    ]
    for _ in range(300):
        values.append(round(rng.uniform(-1e6, 1e6), rng.randint(0, 6)))
        values.append(rng.randint(-10**7, 10**7))
        values.append(rng.random())
    for name in (None, *formats.FORMAT_NAMES):
        for v in values:
            cases.append([name, v, formats.format_value(name, v)])
    # Python shows a float timestamp outside years 1..9999 as str(value), e.g.
    # "1000000000000.0"; JSON cannot tell the browser it was a float, so it
    # shows "1000000000000". Real dates never get there.
    for v in (1e15, 1e12, -1e12):
        for name in ("date", "datetime"):
            cases.remove([name, v, formats.format_value(name, v)])
    return cases


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed; the JavaScript formats are not run")
def test_js_formats_match_python_reference(tmp_path):
    runner = tmp_path / "run.mjs"
    runner.write_text(RUNNER, encoding="utf-8")
    data = tmp_path / "cases.json"
    data.write_text(json.dumps(_cases()), encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(runner), str(ASSETS / "format.js"), str(data)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["names"] == list(formats.FORMAT_NAMES)
    assert out["null_text"] == formats.NULL_TEXT
    assert out["months"] == list(formats.MONTHS)
    assert out["unknown_raises"] is True
    assert not out["bad"], f"{len(out['bad'])} mismatches, first: {out['bad'][:5]}"


STEP_RUNNER = """
import { pathToFileURL } from "node:url";
const mod = await import(pathToFileURL(process.argv[2]).href);
const cases = [["distance", 65000], ["distance", 850], ["distance", 3400], ["duration", 5400], ["pace", 400],
               ["number", 65000], ["distance", 0], ["distance", 9e9]];
console.log(JSON.stringify(cases.map(([f, m]) => mod.axisStep(f, m))));
"""


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_js_axis_steps_are_round(tmp_path):
    runner = tmp_path / "steps.mjs"
    runner.write_text(STEP_RUNNER, encoding="utf-8")
    proc = subprocess.run([NODE, str(runner), str(ASSETS / "format.js")], capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    # 20 km lines up to 65 km; 250 m below a kilometre; whole km; 30 min; 2 min; numbers left to the chart.
    assert json.loads(proc.stdout) == [20000, 250, 1000, 1800, 120, None, None, None]


def test_a_rule_on_eui_value_sizes_the_figure():
    """The figure inherits size and font from .eui-value, so `.eui-value { font-size }` works."""
    import re as _re
    from pathlib import Path as _P

    css = (_P(__file__).resolve().parents[1] / "src" / "easyui" / "web" / "assets" / "style.css").read_text()
    fig = _re.search(r"\.stat-figure \.fig,\s*\.readout-value \.fig \{(.*?)\}", css, _re.S).group(1)
    assert "font-size: 1em" in fig and "font-family: inherit" in fig
    value = _re.search(r"\n\.stat-value \{(.*?)\}", css, _re.S).group(1)
    assert "var(--eui-size-figure)" in value and "var(--eui-font-numbers)" in value
    assert not _re.search(r"\.fig \{[^}]*var\(--eui-size-figure\)", css)


# ---- charts: the arithmetic behind them (gaps, axis ranges, date labels, row sharing) ------------------------

CHART_RUNNER = """
import { pathToFileURL } from "node:url";
const m = await import(pathToFileURL(process.argv[2]).href);
const rows = await import(pathToFileURL(process.argv[3]).href);
const days = (from, n, step = 1) => Array.from({ length: n }, (_, i) => new Date(Date.UTC(2026, 0, 1) + (from + i * step) * 864e5).toISOString().slice(0, 10));
const out = {};
// A missing day becomes an empty point; regular weeks too; anything irregular is left alone.
const gappy = ["2026-01-01", "2026-01-02", "2026-01-05", "2026-01-06"];
out.gap = m.fillDateGaps(gappy);
out.weeks = m.fillDateGaps(["2026-01-05", "2026-01-12", "2026-01-26", "2026-02-02"]);
out.none = [m.fillDateGaps(days(0, 10)), m.fillDateGaps(["a", "b", "c"]), m.fillDateGaps(["2026-01-03", "2026-01-02", "2026-01-01"]),
  m.fillDateGaps(["2026-01-01", "2026-01-02", "2026-01-03", "2026-12-31"])];
// Round ends with room above; bars and areas from zero.
out.ranges = [
  m.valueRange({ lo: 48, hi: 62, ticks: 4 }),
  m.valueRange({ lo: 2500, hi: 15800, ticks: 4, zero: true }),
  m.valueRange({ lo: 5.5, hi: 8.5, ticks: 3 }),
  m.valueRange({ lo: -3, hi: 7, ticks: 4, zero: true }),
  m.valueRange({ lo: 7, hi: 7, ticks: 4 }),
  m.valueRange({ lo: 0, hi: 64000, ticks: 4, zero: true, format: "distance" }),
  m.valueRange({ lo: 1, hi: 9, ticks: 4, step: 0.001 }),
  m.valueRange({ lo: null, hi: null }),
];
const ticks = (xs, w) => { const t = m.dateTicks(xs, w, 6.6); return t && { unit: t.unit, labels: [...t.labels] }; };
out.ticks = {
  narrowQuarter: ticks(days(0, 120), 300),
  wideQuarter: ticks(days(0, 120), 1400),
  week: ticks(days(0, 7), 600),
  year: ticks(days(0, 730), 900),
  notDates: m.dateTicks(["run", "ride"], 400),
};
out.cols = [[3, 1200, 304, 24], [3, 640, 304, 24], [6, 1200, 136, 16], [6, 600, 136, 16], [6, 288, 136, 16], [5, 700, 136, 16], [1, 100, 300, 10]]
  .map((a) => rows.columnsFor(...a));
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_chart_math(tmp_path):
    runner = tmp_path / "charts.mjs"
    runner.write_text(CHART_RUNNER, encoding="utf-8")
    proc = subprocess.run([NODE, str(runner), str(ASSETS / "chartmath.js"), str(ASSETS / "rows.js")],
                          capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["gap"] == {"xs": ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05", "2026-01-06"],
                          "rowOf": [0, 1, -1, -1, 2, 3]}
    assert out["weeks"]["rowOf"] == [0, 1, -1, 2, 3] and out["weeks"]["xs"][2] == "2026-01-19"
    assert out["none"] == [None, None, None, None]
    line, bars, short, negative, flat, km, tiny, empty = out["ranges"]
    assert line == {"min": 45, "max": 65, "interval": 5}  # room above and below, round ends
    assert bars["min"] == 0 and bars["max"] >= 15800 * 1.04 and bars["max"] % bars["interval"] == 0
    assert short["min"] <= 5.5 - 0.2 and short["max"] >= 8.5 + 0.2
    assert negative["min"] < -3 and negative["max"] > 7
    assert flat["min"] < 7 < flat["max"]
    assert km["interval"] in (20000, 25000) and km["min"] == 0  # whole kilometres for distances
    assert tiny["interval"] > 0.001  # a step far too small for the values is replaced
    assert empty is None
    t = out["ticks"]
    assert t["narrowQuarter"]["unit"] in ("month", "quarter") and [x[1] for x in t["narrowQuarter"]["labels"]][:1] == ["Jan"]
    assert t["wideQuarter"]["unit"] in ("week", "2 weeks")
    assert all(text[0].isdigit() for _, text in t["wideQuarter"]["labels"])
    assert t["week"]["unit"] == "day" and len(t["week"]["labels"]) == 7
    years = [text for _, text in t["year"]["labels"]]
    assert "Jan 2026" in years[0] and any("2027" in y for y in years)
    assert t["notDates"] is None
    # Never more than fit, balanced lines: 3 charts in 640 px go 2 + 1, 6 numbers go 6, 3 + 3 or 2 + 2 + 2.
    assert out["cols"] == [3, 2, 6, 3, 2, 3, 1]


def test_charts_are_svg_and_follow_their_box():
    charts = (ASSETS / "charts.js").read_text(encoding="utf-8")
    timeline = (ASSETS / "timeline.js").read_text(encoding="utf-8")
    assert 'export const RENDERER = "svg";' in charts
    assert '"canvas"' not in charts and '"canvas"' not in timeline
    assert "renderer: RENDERER" in charts and "renderer: RENDERER" in timeline
    # Every size change redraws (labels are laid out for the new width).
    assert "new ResizeObserver(() => this.resized())" in charts
    # No style attribute is ever written: values go in through the style object.
    for path in OUR_JS:
        assert 'setAttribute("style"' not in path.read_text(encoding="utf-8"), path.name


def test_layout_uses_the_whole_width_and_container_queries():
    css = (ASSETS / "style.css").read_text(encoding="utf-8")
    shell = re.search(r"\.top-inner,\n\.main \{(.*?)\}", css, re.S).group(1)
    assert "max-width" not in shell and "var(--edge-left)" in shell
    assert re.search(r"\n\.c \{\s*container-type: inline-size;", css)
    assert "@container (max-width: 560px)" in css  # tables become cards by their own width
    assert "@media (max-width: 560px) {\n  .t-cards" not in css
    assert "safe-area-inset-left" in css and "cqi" in css
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert "viewport-fit=cover" in html

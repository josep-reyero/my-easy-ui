"""Regression tests for the November 2026 defect review (stylesheet checker, look files, project model, server).

Each test names the defect it guards. All data and addresses are made up (``example.invalid``).
"""

from __future__ import annotations

import time

import pytest

from test_compose import fitlab  # noqa: F401 - the fitlab project fixture

from easyui import css, spec, styles

ASSETS = ("a.png", "fonts/notes.woff2")


def check_css(text, *, assets=ASSETS):
    return css.check_stylesheet(
        text, file="ui/styles/a.css", name="a.css", stylesheets=("a.css",), assets=assets,
        classes=styles.STABLE_CLASSES, properties=styles.CUSTOM_PROPERTIES, data_types=spec.COMPONENT_TYPES,
    )


def problems(res):
    return " | ".join(p["problem"] for p in res.errors)


# ---------------------------------------------------------------- 1. the checker takes linear time


def _fill(pre: str, unit: str, post: str = "") -> str:
    room = css.MAX_STYLESHEET_BYTES - len(pre) - len(post)
    return pre + unit * (room // len(unit)) + post


_DEEP = css.MAX_STYLESHEET_BYTES // 16
WORST_SHEETS = {
    "stray } after (": _fill("", "(}"),
    "stray } after [": _fill("", "[}"),
    "nested blocks, then (}": _fill("a{" * 24, "(}"),
    "unclosed blocks": _fill("", "a{"),
    "nested image()": ".a{background:" + "image(" * _DEEP + '"../assets/a.png"' + ")" * _DEEP + "}",
    "nested image-set( never closed": _fill(".a{background:", "image-set("),
    "font-family: repeated": _fill("@font-face{", "font-family:", "}"),
    "many wrong file names": _fill(".a{background:", "url(../assets/b.pnx)", "}"),
    "many unknown eui- classes": "".join(f".eui-x{i}{{}}" for i in range(30000))[:css.MAX_STYLESHEET_BYTES - 2] + "{}",
    "many quotes never closed": _fill("", "'\n"),
    "23 nested @media": _fill("@media x{" * 23, ".a,.b{color:red}", "}" * 23),
    "nested :is()": ":is(" * _DEEP + ".a" + ")" * _DEEP + "{}",
}


@pytest.mark.parametrize("name", list(WORST_SHEETS))
def test_the_worst_stylesheets_at_the_size_limit_are_checked_quickly(name):
    """Finding: a stray } scanned the whole stack of open brackets, so 200 KB of "(}" took about 80 s; nested
    image() and repeated font-family: were quadratic too, and every wrong name searched for the closest match."""
    text = WORST_SHEETS[name]
    assert len(text.encode("utf-8")) <= css.MAX_STYLESHEET_BYTES
    t = time.perf_counter()
    res = check_css(text)
    took = time.perf_counter() - t
    assert took < 2.0, f"{name}: checking took {took:.1f} s"
    if res.ok:
        t = time.perf_counter()
        css.scope_stylesheet(text, "demo")
        assert time.perf_counter() - t < 2.0
    t = time.perf_counter()
    css.inline_refs(text, lambda ref: "assets/a.png", lambda target: "data:image/png;base64,AAAA")
    assert time.perf_counter() - t < 2.0
    assert len(res.errors) <= css.MAX_PROBLEMS + 1 and len(res.warnings) <= css.MAX_PROBLEMS + 1


def test_a_long_list_of_problems_is_cut_short_and_says_so():
    res = check_css(_fill(".a{background:", "url(../assets/b.pnx)", "}"))
    assert len(res.errors) == css.MAX_PROBLEMS + 1
    assert "more problems" in res.errors[-1]["problem"]


def test_a_stray_closing_brace_still_closes_the_open_block():
    res = check_css(".a { width: calc(1px } .b { color: red; }")
    assert "A ( opened here is never closed" in problems(res)
    assert "A { opened" not in problems(res)
    assert check_css(".a { background: url(../assets/a.png) } .b { color: red; }").ok


# ---------------------------------------------------------------- 2. no address built at run time


@pytest.mark.parametrize("bad", [
    '.a { --x: "https://example.invalid/x.png"; background: image-set(var(--x) 1x); }',
    ".a { background: image-set(var(--x) 1x); }",
    ".a { background: -webkit-image-set(var(--x) 1x); }",
    ".a { background: image(var(--x)); }",
    ".a { background: cross-fade(var(--x), url(../assets/a.png)); }",
    ".a { background: image-set(env(--x) 1x); }",
    ".a { background: image-set(attr(data-x) 1x); }",
    ".a { background: image-set(type(var(--t)) \"../assets/a.png\" 1x); }",
    '.a { background: image-set(if(media(print): "https://example.invalid/x.png"; else: "../assets/a.png") 1x); }',
    '@font-face { font-family: "Notes"; src: "https://example.invalid/x.woff2"; }',
    '@font-face { font-family: "Notes"; src: "../assets/fonts/notes.woff2"; }',
    '@font-face { font-family: "Notes"; src: var(--x); }',
    '.a { --x: "https://example.invalid/x.png"; }',
    '.a { --x: "//example.invalid/x.png"; }',
    '.a { --x: "data:image/png;base64,AAAA"; }',
    '.a { --x: " javascript:alert(1)"; }',
])
def test_addresses_put_together_at_run_time_are_refused(bad):
    """Finding: image-set(var(--x) 1x) was accepted, so a custom property holding "https://..." made the browser
    try to fetch it (the page's policy blocked it, but the checker must refuse it)."""
    res = check_css(bad)
    assert not res.ok, bad


@pytest.mark.parametrize("good", [
    '.a { background: image-set("../assets/a.png" 1x, url(../assets/a.png) 2x); }',
    '.a { background: image-set("../assets/a.png" type("image/png") 1x); }',
    '.a { background: cross-fade(url(../assets/a.png) 50%, "../assets/a.png"); }',
    ".a { color: var(--eui-accent); background: linear-gradient(var(--eui-accent), red); }",
    '.a { --note: "Data from https://example.invalid"; content: var(--note); }',
    '.a { --sep: " / "; --label: "Note: kept"; }',
    '@font-face { font-family: "Notes"; src: local("Notes"), url("../assets/fonts/notes.woff2") format("woff2"); }',
])
def test_ordinary_uses_stay_accepted(good):
    res = check_css(good)
    assert res.ok, problems(res)


# ---------------------------------------------------------------- 3. an SVG is UTF-8, and served as UTF-8

_SVG_BODY = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 4 4"><text>\xc3\xa9</text></svg>'


@pytest.mark.parametrize("encoding", ["ISO-8859-1", "windows-1252", "UTF-16", "us-ascii", "utf-7"])
def test_an_svg_declaring_another_encoding_is_refused(encoding):
    """Finding: the picture was checked as UTF-8 but a declaration like encoding="ISO-8859-1" was accepted, so a
    browser honouring it could read other text than the one checked."""
    data = f'<?xml version="1.0" encoding="{encoding}"?>'.encode() + _SVG_BODY
    ctype, why = styles.asset_type("a.svg", data)
    assert ctype is None and "encoding" in why


@pytest.mark.parametrize("decl", ["", '<?xml version="1.0"?>', '<?xml version="1.0" encoding="UTF-8"?>',
                                  "<?xml version='1.0' encoding='utf-8' standalone='yes'?>"])
def test_a_utf8_svg_is_served_as_utf8(decl):
    ctype, why = styles.asset_type("a.svg", decl.encode() + _SVG_BODY)
    assert why is None and ctype == "image/svg+xml; charset=utf-8"


# ---------------------------------------------------------------- 4. size limits stop the reading


def _count_reads(monkeypatch):
    got = {"bytes": 0}
    real = styles.os.read

    def counting(fd, n):
        data = real(fd, n)
        got["bytes"] += len(data)
        return data

    monkeypatch.setattr(styles.os, "read", counting)
    return got


def test_assets_past_the_total_limit_are_not_read(tmp_path, monkeypatch):
    """Finding: every asset was read before the total was compared with the limit (165 MB in memory, 4 s)."""
    monkeypatch.setattr(styles, "MAX_ASSETS_BYTES", 1000)
    (tmp_path / "ui" / "assets").mkdir(parents=True)
    for i in range(20):
        (tmp_path / "ui" / "assets" / f"p{i:02}.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 392)
    got = _count_reads(monkeypatch)
    files = styles.read_style_dir(tmp_path / "ui")
    assert got["bytes"] <= 1000
    assert len(files.assets) == 2 and files.too_large
    assert any("together" in p["problem"] for p in files.problems)


def test_stylesheets_past_the_total_limit_are_not_read_or_checked(tmp_path, monkeypatch):
    monkeypatch.setattr(styles, "MAX_STYLES_BYTES", 1000)
    (tmp_path / "ui" / "styles").mkdir(parents=True)
    for i in range(10):
        (tmp_path / "ui" / "styles" / f"s{i}.css").write_text(".a { color: red; }\n" * 20)
    got = _count_reads(monkeypatch)
    files = styles.read_style_dir(tmp_path / "ui")
    assert got["bytes"] <= 1000 and len(files.stylesheets) == 2

    def no_checks(*_a, **_k):
        raise AssertionError("the stylesheets were checked although the folder is too large")

    monkeypatch.setattr(css, "check_stylesheet", no_checks)
    res = styles.check(files)
    assert not res.ok and res.styles is None
    assert any("together" in p["problem"] for p in res.errors)


def test_too_many_assets_stops_the_reading(tmp_path, monkeypatch):
    monkeypatch.setattr(styles, "MAX_ASSETS", 3)
    (tmp_path / "ui" / "assets").mkdir(parents=True)
    for i in range(10):
        (tmp_path / "ui" / "assets" / f"p{i}.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
    files = styles.read_style_dir(tmp_path / "ui")
    assert len(files.assets) == 3 and files.too_large
    assert sum("more than 3 files" in p["problem"] for p in files.problems) == 1


# ---------------------------------------------------------------- 5a. a stale apps list, read from apps/ with a note


def test_a_stale_lock_read_from_apps_says_so_once_on_stderr(tmp_path, capsys):
    """Finding: a lock out of date only because project.toml changed was read from apps/ without a word."""
    from test_apps import RIDES, WALKS, make, write_lock

    from easyui import appconfig, project

    root = make(tmp_path, {"fitlab_walks": WALKS, "fitlab_rides": RIDES})
    write_lock(root, tmp_path)
    (root / "project.toml").write_text((root / "project.toml").read_text() + "# changed\n")
    capsys.readouterr()
    cfg = appconfig.load(root)
    assert [a.name for a in cfg.apps] == ["fitlab-walks", "fitlab-rides"]
    assert [a.folder for a in cfg.apps] == [root / "apps" / "fitlab_walks", root / "apps" / "fitlab_rides"]
    assert all(a.in_project for a in cfg.apps)
    cfg.apps  # read again: the note is said once
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "apps.lock.json is out of date" in err and "mes apps sync" in err
    assert project.lock_state(cfg) == "lock"


def test_a_stale_lock_with_an_app_outside_the_project_is_refused(tmp_path):
    from test_apps import RIDES, WALKS, make, write_lock

    from easyui import appconfig
    from easyui.errors import ConfigError

    root = make(tmp_path, {"fitlab_walks": WALKS}, installed={"fitlab_rides": RIDES})
    write_lock(root, tmp_path)
    (root / "project.toml").write_text((root / "project.toml").read_text() + "# changed\n")
    with pytest.raises(ConfigError) as e:
        appconfig.load(root).apps
    assert e.value.code == "apps_lock_stale"


# ---------------------------------------------------------------- 5b. a hand-written app: names the file's own app


def _errors(root, slug):
    from test_compose import cfg_of

    from easyui import compose

    return " | ".join(e["problem"] for e in compose.compose(cfg_of(root)).get(slug).errors)


def _with_component_app(text: str, app: str) -> str:
    return text.replace("  - - type: table\n", f"  - - type: table\n      app: {app}\n", 1)


def test_an_app_dashboard_may_not_tag_a_component_with_another_app(fitlab):
    """Finding: only sections were checked, so an app's own dashboard could put another app's name (and so its
    stylesheets) on a component."""
    f = fitlab / "apps/fitlab_walks/dashboards/walks.yaml"
    original = f.read_text()
    f.write_text(_with_component_app(original, "fitlab-rides"))
    assert "but the file belongs to fitlab-walks" in _errors(fitlab, "walks")
    f.write_text(_with_component_app(original, "fitlab-walks"))  # its own name is fine
    assert _errors(fitlab, "walks") == ""


def test_a_project_dashboard_may_not_set_app(fitlab):
    f = fitlab / "ui/dashboards/overview.yaml"
    original = f.read_text()
    f.write_text("app: fitlab-walks\n" + original)
    assert "The project's own dashboard has an app: line" in _errors(fitlab, "overview")
    f.write_text(_with_component_app(original, "fitlab-walks"))
    assert "This component of the project's own file has an app: setting" in _errors(fitlab, "overview")


def test_a_project_sections_file_may_not_set_app(fitlab):
    from test_compose import FITLAB

    (fitlab / "ui" / "sections").mkdir()
    text = (FITLAB / "installed/fitlab_rides/dashboards/overview-rides.yaml").read_text()
    text = text.replace("      - - type: bar\n", "      - - type: bar\n          app: fitlab-rides\n", 1)
    (fitlab / "ui/sections/fitlab-rides.overview.yaml").write_text(text)
    assert "project's own file has an app: setting" in _errors(fitlab, "overview")



# ---------------------------------------------------------------- 6. small ones


def test_the_publish_summary_counts_the_apps_pictures_and_fonts():
    """Finding: the summary counted an app's stylesheets but not its fonts and pictures."""
    from easyui import publish

    text = publish._look_text([{"path": p} for p in (
        "styles/a.css", "assets/logo.svg", "apps/fitlab-walks/styles/a.css", "apps/fitlab-walks/assets/x.png",
        "apps/fitlab-walks/assets/fonts/y.woff2")])
    assert "1 app stylesheet" in text and "2 app font or picture files" in text
    assert "1 font or picture file" in text


# ---------------------------------------------------------------- 7. easy-ui tables --source reads like data build


def test_tables_source_reads_with_the_read_only_authorizer(built_app):
    """Finding: easy-ui tables --source read the project's file without data build's authorizer, so a view there
    could call a function data build refuses (here sqlite_compileoption_get) and show its result."""
    import sqlite3

    from conftest import run_cli, run_cli_json

    conn = sqlite3.connect(built_app.db_path)
    conn.execute("CREATE VIEW build_facts AS SELECT sqlite_compileoption_get(0) AS fact")
    conn.commit()
    conn.close()
    code, data = run_cli_json("tables", "--source", "--all-tables")
    assert code == 0
    facts = next(t for t in data["tables"] if t["name"] == "build_facts")
    assert facts["rows"] is None and facts["columns"] == [] and "sqlite_compileoption_get" in facts["refused"]
    days = next(t for t in data["tables"] if t["name"] == "days")
    assert days["rows"] > 0 and days["columns"]  # ordinary tables are still described
    code, out, _err = run_cli("tables", "--source")
    assert code == 0 and "build_facts (view, not read:" in out

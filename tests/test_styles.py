"""Themes, project stylesheets and assets: checking, publishing, serving, previewing and exporting.

All data and files here are made up.
"""

from __future__ import annotations

import io
import json
import os
import re
import tarfile
from pathlib import Path

import pytest
from conftest import run_cli, run_cli_json

from easyui import css, export, publish, spec, styles
from easyui import theme as themes
from easyui.errors import NotFound, Refused, SpecError

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 24
WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"\x00" * 12
WOFF2 = b"wOF2\x00\x01\x00\x00" + b"\x00" * 24
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 4 4"><rect width="4" height="4" fill="#123"/></svg>'

GOOD_CSS = """/* synthetic */
@font-face { font-family: "Field Notes"; src: url("../assets/fonts/notes.woff2") format("woff2"); }
.eui-dashboard-title { font-family: "Field Notes", serif; font-size: 3rem; }
.eui-row[data-row="2"] { display: grid; grid-template-columns: 2fr 1fr; }
[data-id="heart"] { --eui-chart-1: #b0383a; --eui-chart-line-width: 3; }
.eui-component[data-type="line"] .eui-title { color: var(--eui-accent); }
.logo { background: url("../assets/logo.svg") no-repeat; }
@media (max-width: 600px) { .eui-row { display: block; } }
"""


def check_css(text, *, assets=("fonts/notes.woff2", "logo.svg"), stylesheets=("a.css", "b.css"), name="a.css"):
    return css.check_stylesheet(
        text, file=f"ui/styles/{name}", name=name, stylesheets=stylesheets, assets=assets,
        classes=styles.STABLE_CLASSES, properties=styles.CUSTOM_PROPERTIES, data_types=spec.COMPONENT_TYPES,
    )


def problems(res):
    return " | ".join(p["problem"] for p in res.errors)


# ---------------------------------------------------------------- the CSS checker


def test_an_ordinary_stylesheet_passes():
    res = check_css(GOOD_CSS)
    assert res.ok, problems(res)
    assert res.warnings == []
    assert len(res.font_faces) == 1 and 'url("assets/fonts/notes.woff2")' in res.font_faces[0]


@pytest.mark.parametrize("bad", [
    '.a { background: url("https://tracker.example/x.png"); }',
    ".a { background: url(https://tracker.example/x.png); }",
    ".a { background: url(//tracker.example/x.png); }",
    '.a { background: url("data:image/png;base64,AAAA"); }',
    '.a { background: url("javascript:alert(1)"); }',
    ".a { background: u\\72l(https://tracker.example/x.png); }",       # an escaped url( is still url(
    ".a { background: \\75 rl(https://tracker.example/x.png); }",
    '.a { background: url("/etc/passwd"); }',
    '.a { background: url("../../outside.png"); }',
    '.a { background: url("../assets/../../outside.png"); }',
    '.a { background: url("../assets/missing.png"); }',
    '.a { background: url("../assets/logo.svg?x=1"); }',
    '.a { background: url("../assets/logo%2esvg"); }',
    '.a { background: image-set("https://tracker.example/x.png" 1x); }',
    '.a { background: src("https://tracker.example/x.png"); }',
    '@import url("https://tracker.example/x.css");',
    '@import "https://tracker.example/x.css";',
    '@import "missing.css";',
    '@import "a.css";',                                                  # itself
    "@namespace svg url(http://www.w3.org/2000/svg);",
    ".a { width: expression(alert(1)); }",
    ".a { behavior: url(x.htc); }",
    ".a { -moz-binding: url(x.xml#a); }",
    '@document url-prefix("") { .a { color: red; } }',
    ".a { color: red; ",                                                 # never closed
    '.a { content: "never closed; }',
    "/* never closed",
    ".a { color: red; }\x00",
    "@unknown-rule x { }",
])
def test_hostile_or_broken_stylesheets_are_refused(bad):
    res = check_css(bad)
    assert not res.ok, bad
    for p in res.errors:
        assert p["problem"] and p["file"] == "ui/styles/a.css"
        assert "hint" in p


def test_import_of_another_project_stylesheet_is_fine():
    assert check_css('@import "b.css";\n@import url("./b.css");').ok


def test_unknown_easy_ui_names_are_reported_with_the_closest_match():
    res = check_css(".eui-dashbord-title { color: red; }\n.a { --eui-acent: red; color: var(--eui-acent); }\n"
                    '[data-type="lien"] { color: red; }\n.eui-dashbord-title { color: blue; }')
    assert res.ok
    found = {w.get("did_you_mean") for w in res.warnings}
    assert {"eui-dashboard-title", "--eui-accent", "line"} <= found
    assert sum(1 for w in res.warnings if w.get("did_you_mean") == "eui-dashboard-title") == 1  # said once


@pytest.mark.parametrize("family", ["system-ui", "Iowan Old Style", "inherit", "a;b"])
def test_font_faces_cannot_take_easy_ui_or_odd_names(family):
    res = check_css(f'@font-face {{ font-family: "{family}"; src: url("../assets/fonts/notes.woff2"); }}')
    assert not res.ok


def test_a_font_face_must_name_a_woff2_in_assets():
    res = check_css('@font-face { font-family: "Notes"; src: url("../assets/logo.svg"); }')
    assert not res.ok


def test_size_limit():
    res = check_css("/*" + "x" * css.MAX_STYLESHEET_BYTES + "*/")
    assert not res.ok and "KB" in problems(res)


@pytest.mark.parametrize(("ref", "base", "want"), [
    ("../assets/a.png", "styles", "assets/a.png"),
    ("b.css", "styles", "styles/b.css"),
    ("./b.css", "styles", "styles/b.css"),
    ("fonts/a.woff2", "assets", "assets/fonts/a.woff2"),
    ("../../a.png", "styles", None),
    ("../x/a.png", "styles", None),
    ("a//b.png", "styles", None),
    ("https://x.example/a.png", "styles", None),
    ("C:\\a.png", "styles", None),
    ("a b.png", "styles", None),
    (".hidden.png", "assets", None),
])
def test_resolve_ref(ref, base, want):
    got, why = css.resolve_ref(ref, base)
    assert got == want
    assert (why is None) == (want is not None)


# ---------------------------------------------------------------- the theme file


def test_ready_made_themes_are_valid_and_distinct():
    made = themes.ready_made()
    assert len(made) >= 4
    accents = set()
    for t in made:
        got, errors, warnings = themes.check_theme(themes.ready_made_text(t["name"]))
        assert got is not None and errors == [] and warnings == [], (t["name"], errors, warnings)
        tokens = got.tokens()
        assert tokens["light"]["--eui-accent"] != tokens["dark"]["--eui-accent"]
        accents.add(tokens["light"]["--eui-accent"])
        assert t["description"]
    assert len(accents) == len(made)
    with pytest.raises(NotFound):
        themes.ready_made_text("no-such-theme")


def test_theme_mistakes_are_plain_and_suggest_the_closest_name():
    _t, errors, _w = themes.check_theme("colours:\n  accent: '#336699'\n")
    assert errors and errors[0].get("did_you_mean") == "colors"
    _t, errors, _w = themes.check_theme("colors:\n  acent: '#336699'\n")
    assert errors and errors[0].get("did_you_mean") == "accent"
    _t, errors, _w = themes.check_theme("colors:\n  accent: #336699\n")  # YAML reads that as a comment
    assert errors and "quote" in (errors[0]["problem"] + errors[0].get("hint", "")).lower()
    _t, errors, _w = themes.check_theme("radius: 400\n")
    assert errors
    _t, errors, _w = themes.check_theme("colors:\n  accent: 'red; background: url(x)'\n")
    assert errors
    _t, errors, _w = themes.check_theme("fonts:\n  body: humanst\n")
    assert errors and errors[0].get("did_you_mean") == "humanist"


def test_low_contrast_is_a_warning_not_an_error():
    got, errors, warnings = themes.check_theme("colors:\n  text: '#dddddd'\n  surface: '#ffffff'\n  background: '#ffffff'\n")
    assert got is not None and errors == []
    assert any("contrast" in w["problem"].lower() for w in warnings)


def test_font_files_must_be_woff2_assets():
    text = "font_files:\n  Notes:\n    - file: fonts/notes.woff2\nfonts:\n  headings: Notes\n"
    got, errors, _w = themes.check_theme(text, assets={"fonts/notes.woff2"})
    assert got is not None and errors == [], errors
    out = themes.theme_css(got)
    assert 'url("assets/fonts/notes.woff2")' in out and '"Notes"' in out
    _g, errors, _w = themes.check_theme(text, assets=set())
    assert errors
    _g, errors, _w = themes.check_theme(text.replace("fonts/notes.woff2", "../../x.woff2"), assets={"fonts/notes.woff2"})
    assert errors


def test_theme_css_is_only_custom_properties_light_and_dark():
    got, _e, _w = themes.check_theme(themes.ready_made_text("trail"))
    out = themes.theme_css(got)
    assert out.startswith(":root{") or ":root {" in out
    assert "prefers-color-scheme: dark" in out
    assert "url(" not in out and "@import" not in out
    for name in re.findall(r"(--eui-[a-z0-9-]+)\s*:", out):
        assert name in styles.CUSTOM_PROPERTIES, name


# ---------------------------------------------------------------- assets


@pytest.mark.parametrize(("name", "data", "ok"), [
    ("a.png", PNG, True),
    ("a.jpg", JPG, True),
    ("a.webp", WEBP, True),
    ("a.woff2", WOFF2, True),
    ("a.svg", SVG, True),
    ("a.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><use href="#x"/><path id="x" fill="url(#g)"/></svg>', True),
    ("a.png", JPG, False),
    ("a.woff2", PNG, False),
    ("a.gif", b"GIF89a", False),
    ("a.html", b"<html></html>", False),
    ("a.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', False),
    ("a.svg", b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>', False),
    ("a.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><foreignObject><p>x</p></foreignObject></svg>', False),
    ("a.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><image href="https://tracker.example/x.png"/></svg>', False),
    ("a.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><a xlink:href="javascript:alert(1)">x</a></svg>', False),
    ("a.svg", b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY x "y">]><svg/>', False),
    ("a.svg", b"<svg><style>@import url(https://tracker.example/x.css);</style></svg>", False),
    ("a.svg", b"not a picture", False),
])
def test_asset_types_by_name_and_first_bytes(name, data, ok):
    ctype, why = styles.asset_type(name, data)
    assert (ctype is not None) == ok, why
    assert (why is None) == ok


# ---------------------------------------------------------------- reading the ui folder


def make_look(ui: Path, *, css_text: str = GOOD_CSS, theme: str | None = "trail") -> None:
    (ui / "styles").mkdir(parents=True, exist_ok=True)
    (ui / "assets" / "fonts").mkdir(parents=True, exist_ok=True)
    (ui / "styles" / "a.css").write_text(css_text)
    (ui / "assets" / "fonts" / "notes.woff2").write_bytes(WOFF2)
    (ui / "assets" / "logo.svg").write_bytes(SVG)
    if theme:
        (ui / "theme.yaml").write_text(themes.ready_made_text(theme))


def test_read_and_check_a_whole_look(tmp_path):
    make_look(tmp_path)
    files = styles.read_style_dir(tmp_path)
    assert files.problems == []
    assert [b.path for b in files.blobs()] == ["theme.yaml", "styles/a.css", "assets/fonts/notes.woff2", "assets/logo.svg"]
    res = styles.check(files)
    assert res.ok, res.errors
    st = res.styles
    assert st.public()["stylesheets"] == ["a.css"] and st.public()["theme_css"] is True
    assert st.get("theme.css")[1].startswith("text/css")
    assert b"@font-face" in st.get("theme.css")[0]  # project fonts are declared for the whole page
    assert st.get("styles/a.css") == (GOOD_CSS.encode(), "text/css; charset=utf-8")
    assert st.get("assets/logo.svg")[1] == "image/svg+xml"
    assert st.get("assets/fonts/notes.woff2")[1] == "font/woff2"
    for bad in ("styles/../theme.yaml", "theme.yaml", "assets/missing.png", "styles/b.css", "../x"):
        assert st.get(bad) is None
    assert styles.EMPTY.public() == {"rev": "none", "theme_css": False, "stylesheets": [], "assets": 0}


def test_links_are_never_followed(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.css").write_text(".a{color:red}")
    (outside / "x.png").write_bytes(PNG)
    ui = tmp_path / "ui"
    make_look(ui, theme=None)
    (ui / "styles" / "b.css").symlink_to(outside / "x.css")
    (ui / "assets" / "x.png").symlink_to(outside / "x.png")
    (ui / "assets" / "linked").symlink_to(outside, target_is_directory=True)
    (ui / "theme.yaml").symlink_to(outside / "x.css")
    files = styles.read_style_dir(ui)
    paths = [b.path for b in files.blobs()]
    assert "styles/b.css" not in paths and "assets/x.png" not in paths and files.theme is None
    assert not any(p.startswith("assets/linked") for p in paths)
    assert len(files.problems) >= 4
    assert not styles.check(files).ok


def test_a_linked_styles_folder_is_refused(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.css").write_text(".a{color:red}")
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "styles").symlink_to(outside, target_is_directory=True)
    files = styles.read_style_dir(ui)
    assert files.stylesheets == [] and files.problems


def test_limits(tmp_path, monkeypatch):
    ui = tmp_path / "ui"
    (ui / "styles").mkdir(parents=True)
    for i in range(styles.MAX_STYLESHEETS + 1):
        (ui / "styles" / f"s{i:02}.css").write_text(".a{}")
    assert styles.read_style_dir(ui).problems
    monkeypatch.setattr(styles, "MAX_ASSET_BYTES", 10)
    ui2 = tmp_path / "ui2"
    (ui2 / "assets" / "a" / "b" / "c").mkdir(parents=True)
    (ui2 / "assets" / "big.png").write_bytes(PNG)
    (ui2 / "assets" / "a" / "b" / "c" / "deep.png").write_bytes(PNG[:8])
    (ui2 / "assets" / "bad name.png").write_bytes(PNG[:8])
    texts = " | ".join(p["problem"] for p in styles.read_style_dir(ui2).problems)
    assert "big.png" in texts and "deep" in texts and "bad name" in texts
    (ui / "styles" / "Bad Name.css").write_text(".a{}")


def test_bad_stylesheet_names_are_refused(tmp_path):
    ui = tmp_path / "ui"
    (ui / "styles").mkdir(parents=True)
    (ui / "styles" / "Bad Name.css").write_text(".a{}")
    (ui / "styles" / "notes.txt").write_text("x")
    files = styles.read_style_dir(ui)
    assert files.stylesheets == [] and len(files.problems) == 1
    assert any("notes.txt" in n for n in files.notes)  # not CSS: left out, and said so


# ---------------------------------------------------------------- dashboards: id, class, theme


DASH = """spec: easy-ui/v1
title: Synthetic
queries:
  q:
    sql: SELECT day, steps FROM days
layout:
  - - type: line
      id: steps-line
      class: [wide, calm]
      title: Steps
      query: q
      x: day
      y: steps
    - type: text
      class: note
      body: Made-up numbers.
theme:
  colors:
    accent: "#a2451d"
  chart:
    line_width: 3
"""


def compile_text(text):
    d = spec.loads(text, slug="x", file="ui/dashboards/x.yaml")
    return spec.compile_dashboard(d)


def test_component_id_class_and_dashboard_theme():
    c = compile_text(DASH)
    line, text = c["layout"][0]
    assert line["id"] == "steps-line" and line["class"] == ["wide", "calm"]
    assert text["id"] == "r0c1" and text["class"] == ["note"]
    assert c["theme"]["light"]["--eui-accent"] == "#a2451d" and c["theme"]["dark"] == {}
    assert c["theme"]["both"]["--eui-chart-line-width"] == "3"


@pytest.mark.parametrize(("old", "new"), [
    ("id: steps-line", "id: r1c1"),                   # looks like an automatic id
    ("id: steps-line", "id: Steps Line"),
    ("class: [wide, calm]", "class: [eui-chart]"),    # easy-ui's own names are not settings
    ("class: [wide, calm]", "class: 'a.b'"),
    ("class: [wide, calm]", "class: [a, b, c, d, e, f, g, h, i]"),
    ("    accent: \"#a2451d\"", "    acent: \"#a2451d\""),
    ("  chart:\n    line_width: 3", "  chart:\n    line_width: 30"),
    ("theme:\n", "theme:\n  name: x\n"),            # a dashboard cannot rename the theme
])
def test_bad_id_class_or_theme_settings(old, new):
    with pytest.raises(SpecError):
        compile_text(DASH.replace(old, new, 1))


def test_ids_are_unique_in_a_dashboard():
    text = DASH.replace("    - type: text\n      class: note", "    - type: text\n      id: steps-line\n      class: note")
    with pytest.raises(SpecError):
        compile_text(text)


# ---------------------------------------------------------------- publishing


@pytest.fixture
def look_app(built_app):
    make_look(built_app.app_dir / "ui")
    return built_app


def test_publish_carries_the_look_and_serves_it_hash_checked(look_app):
    cfg = look_app.config()
    res = publish.publish(cfg)
    assert [s["path"] for s in res["styles"]] == ["theme.yaml", "styles/a.css", "assets/fonts/notes.woff2", "assets/logo.svg"]
    vdir = publish.published_dir(cfg)
    manifest = json.loads((vdir / "manifest.json").read_text())
    assert {e["path"] for e in manifest["styles"]} == {s["path"] for s in res["styles"]}
    for e in manifest["styles"]:
        f = vdir / e["path"]
        assert f.is_file() and not f.is_symlink() and oct(f.stat().st_mode & 0o777) == "0o444"
    got = publish.PublishedSource(cfg).styles()
    assert got.public()["stylesheets"] == ["a.css"]
    assert got.get("styles/a.css")[0] == GOOD_CSS.encode()

    # Unchanged: nothing new to publish. A changed stylesheet is a new version.
    assert publish.publish(cfg).get("unchanged")
    (look_app.app_dir / "ui" / "styles" / "a.css").write_text(GOOD_CSS + "\n.eui-hint { color: red; }\n")
    assert publish.publish(cfg)["version"] == 2


def test_a_tampered_published_style_file_is_refused(look_app):
    cfg = look_app.config()
    publish.publish(cfg)
    f = publish.published_dir(cfg) / "styles" / "a.css"
    os.chmod(f, 0o644)
    f.write_text('.a { background: url("https://tracker.example/x.png"); }')
    publish._styles_cache.clear()
    with pytest.raises(publish.PublishedDamaged):
        publish.load_published_styles(cfg)


def test_a_bad_look_is_never_published(look_app):
    cfg = look_app.config()
    (look_app.app_dir / "ui" / "styles" / "a.css").write_text('.a { background: url("https://tracker.example/x.png"); }')
    with pytest.raises(SpecError) as e:
        publish.publish(cfg)
    assert "ui/styles/a.css" in json.dumps(e.value.to_dict())
    assert publish.current_version(cfg) is None


def test_rollback_and_restore_bring_the_look_back(look_app):
    cfg = look_app.config()
    ui = look_app.app_dir / "ui"
    publish.publish(cfg)
    (ui / "styles" / "a.css").write_text(".eui-hint { color: red; }\n")
    (ui / "styles" / "b.css").write_text(".eui-hint { color: blue; }\n")
    (ui / "theme.yaml").unlink()
    publish.publish(cfg)
    assert publish.load_published_styles(cfg).public()["theme_css"] is False
    publish.rollback(cfg, 1)
    assert publish.load_published_styles(cfg).public()["stylesheets"] == ["a.css"]
    assert publish.load_published_styles(cfg).public()["theme_css"] is True

    res = publish.restore_files(cfg, 1)
    assert (ui / "styles" / "a.css").read_text() == GOOD_CSS
    assert not (ui / "styles" / "b.css").exists() and (ui / "theme.yaml").is_file()
    backup = look_app.app_dir / res["styles_backup"]
    assert (backup / "styles" / "b.css").read_text() == ".eui-hint { color: blue; }\n"


def test_restore_refuses_a_linked_folder_on_the_way(look_app, tmp_path):
    cfg = look_app.config()
    ui = look_app.app_dir / "ui"
    publish.publish(cfg)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (ui / "assets" / "fonts" / "notes.woff2").unlink()
    (ui / "assets" / "fonts").rmdir()
    (ui / "assets" / "fonts").symlink_to(outside, target_is_directory=True)
    with pytest.raises(Refused):
        publish.restore_files(cfg, 1)
    assert list(outside.iterdir()) == []


def tar_of(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_an_archive_can_carry_the_look(built_app):
    from conftest import ACTIVITIES_YAML, OVERVIEW_YAML

    raw = tar_of({
        "overview.yaml": OVERVIEW_YAML.encode(), "activities.yaml": ACTIVITIES_YAML.encode(),
        "theme.yaml": themes.ready_made_text("dusk").encode(),
        "ui/styles/a.css": GOOD_CSS.encode(), "assets/fonts/notes.woff2": WOFF2, "assets/logo.svg": SVG,
    })
    dashboards, files = publish.read_archive_parts(raw)
    assert sorted(d.slug for d in dashboards) == ["activities", "overview"]
    assert [b.path for b in files.blobs()] == ["theme.yaml", "styles/a.css", "assets/fonts/notes.woff2", "assets/logo.svg"]
    res = publish.publish_from_archive(built_app.config(), raw)
    assert len(res["styles"]) == 4


@pytest.mark.parametrize("member", ["styles/../x.css", "assets/../../x.png", "styles/sub/a.css", "assets/a.exe"])
def test_an_archive_with_odd_style_members_is_refused(built_app, member):
    from conftest import OVERVIEW_YAML

    raw = tar_of({"overview.yaml": OVERVIEW_YAML.encode(), member: b".a{}"})
    with pytest.raises((Refused, SpecError)):
        publish.publish_from_archive(built_app.config(), raw)


# ---------------------------------------------------------------- export


def test_export_carries_the_look(look_app, tmp_path):
    cfg = look_app.config()
    publish.publish(cfg)
    out = tmp_path / "site"
    res = export.export(cfg, out=out)
    assert {"ui/theme.css", "ui/styles/a.css", "ui/assets/fonts/notes.woff2", "ui/assets/logo.svg"} <= set(res["files"])
    assert (out / "ui" / "styles" / "a.css").read_text() == GOOD_CSS
    payload = json.loads(re.search(r'id="easyui-export">(.*?)</script>', (out / "index.html").read_text(), re.S).group(1))
    assert payload["styles"]["stylesheets"] == ["a.css"]


def test_export_of_the_working_folder_refuses_a_bad_look(look_app, tmp_path):
    (look_app.app_dir / "ui" / "styles" / "a.css").write_text(".a { width: expression(1); }")
    with pytest.raises(Refused) as e:
        export.export(look_app.config(), out=tmp_path / "site", working=True)
    assert e.value.code == "export_look_invalid"


# ---------------------------------------------------------------- preview


def test_preview_reloads_the_look_and_lists_its_problems(look_app):
    from easyui import preview

    pv = preview.prepare(look_app.config())
    try:
        src = pv.source
        first = src.rev()
        assert src.styles().public()["stylesheets"] == ["a.css"]
        css_file = look_app.app_dir / "ui" / "styles" / "a.css"
        css_file.write_text(".eui-hint { color: red; }\n")
        os.utime(css_file, ns=(1, 1))
        src.refresh()
        assert src.rev() != first
        css_file.write_text('.a { background: url("https://tracker.example/x.png"); }')
        os.utime(css_file, ns=(2, 2))
        src.refresh()
        listed = [d["slug"] for d in src.list()]
        assert "easy-ui-problems" in listed
        page = json.dumps(src.compiled("easy-ui-problems"))
        assert "ui/styles/a.css" in page
        assert src.styles().public()["stylesheets"] == []  # dashboards are shown with easy-ui's own look meanwhile
    finally:
        pv.close()


# ---------------------------------------------------------------- command line


def test_style_guide_and_themes_commands(demo_app):
    code, data = run_cli_json("style-guide")
    assert code == 0
    assert {c.lstrip(".") for c in data["classes"]} == set(styles.STABLE_CLASSES)
    assert set(data["custom_properties"]) == set(styles.CUSTOM_PROPERTIES)
    code, out, _ = run_cli("style-guide")
    assert code == 0 and ".eui-dashboard-title" in out
    code, data = run_cli_json("themes")
    assert code == 0 and len(data["themes"]) >= 4
    code, out, _ = run_cli("themes")
    assert code == 0 and "trail" in out


def test_theme_use_never_overwrites_without_force(demo_app):
    target = demo_app.app_dir / "ui" / "theme.yaml"
    code, out, _ = run_cli("theme", "use", "trail")
    assert code == 0 and target.read_text() == themes.ready_made_text("trail")
    target.write_text(target.read_text() + "# my change\n")
    code, data = run_cli_json("theme", "use", "dusk")
    assert code == 5 and data["error"]["code"] == "theme_exists"
    assert target.read_text().endswith("# my change\n")
    code, _out, _ = run_cli("theme", "use", "dusk", "--force")
    assert code == 0 and target.read_text() == themes.ready_made_text("dusk")
    code, _data = run_cli_json("theme", "use", "no-such-theme")
    assert code != 0


def test_validate_covers_the_look(built_app):
    make_look(built_app.app_dir / "ui")
    code, data = run_cli_json("validate")
    assert code == 0 and data["ok"] and data["styles"]["ok"]
    (built_app.app_dir / "ui" / "styles" / "a.css").write_text(".a { -moz-binding: url(x.xml); }\n.eui-titel{}")
    code, data = run_cli_json("validate")
    assert code != 0 and not data["ok"] and data["styles"]["errors"]
    assert any(w.get("did_you_mean") == "eui-title" for w in data["styles"]["warnings"])
    code, out, _ = run_cli("validate")
    assert code != 0 and "ui/styles/a.css" in out

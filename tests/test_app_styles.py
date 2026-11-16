"""An app's own look (its styles/ and assets/ folders): checked like the project's, scoped to what the app shows
([data-app="<app>"]), served under /ui/apps/<app>/, published with the version, and always overridable by the
project. Everything here is made up."""

from __future__ import annotations

import json
import re

import pytest
from test_server import PW, Clock, Env, login, web_dir  # noqa: F401 - fixtures used by name
from test_styles import SVG, WOFF2, tar_of

from easyui import auth, compose, css, publish, styles
from easyui.errors import Refused, SpecError

APP = "demo-sleep"
SLEEP = "apps/demo_sleep"


def scoped(text: str, app: str = APP) -> str:
    return css.scope_stylesheet(text, app)


W = f':where([data-app="{APP}"])'


@pytest.mark.parametrize("text, want", [
    (".a { color: red; }", f"{W} .a, .a{W} {{ color: red; }}"),
    (".a, .b > .c { color: red; }", f"{W} .a, .a{W}, {W} .b > .c, .b > .c{W} {{ color: red; }}"),
    (".a::before { content: 'x'; }", f"{W} .a::before, .a{W}::before {{ content: 'x'; }}"),
    (".a:hover { color: red; }", f"{W} .a:hover, .a:hover{W} {{ color: red; }}"),
    (".a:before { color: red; }", f"{W} .a:before, .a{W}:before {{ color: red; }}"),
    ("@media (max-width: 40em) { .a { color: red; } }",
     f"@media (max-width: 40em) {{ {W} .a, .a{W} {{ color: red; }} }}"),
    ("@supports (display: grid) { @media print { .a { color: red; } } }",
     f"@supports (display: grid) {{ @media print {{ {W} .a, .a{W} {{ color: red; }} }} }}"),
])
def test_every_rule_is_scoped_to_the_app(text, want):
    assert scoped(text) == want


def test_scoping_leaves_at_rules_without_selectors_alone():
    text = "@font-face { font-family: Notes; src: url(../assets/n.woff2) format('woff2'); }\n" \
           "@keyframes spin { from { opacity: 0; } to { opacity: 1; } }\n"
    assert scoped(text) == text


@pytest.mark.parametrize("rule", ["@scope (.a) { .b { color: red; } }", "@page { margin: 1cm; }"])
def test_an_app_may_not_reach_outside_its_scope(rule):
    probs = css.app_at_rule_problems(rule, file=f"{SLEEP}/styles/sleep.css")
    assert probs and probs[0]["file"] == f"{SLEEP}/styles/sleep.css"


def test_the_demo_apps_stylesheet_is_checked_scoped_and_listed_before_the_projects(demo_app):
    cfg = demo_app.config()
    (demo_app.app_dir / "ui" / "styles").mkdir(parents=True)
    (demo_app.app_dir / "ui" / "styles" / "mine.css").write_text(".eui-title { letter-spacing: 0; }\n")
    res = styles.check(styles.read_project_look(cfg))
    assert res.errors == []
    pub = res.styles.public()
    assert pub["app_stylesheets"] == [f"apps/{APP}/styles/sleep.css"] and pub["stylesheets"] == ["mine.css"]
    text = res.styles.get(f"apps/{APP}/styles/sleep.css")[0].decode()
    assert f'.eui-title{W} {{' in text and f'{W} .eui-component,' in text


def test_mistakes_in_an_app_stylesheet_name_its_file(demo_app):
    f = demo_app.app_dir / SLEEP / "styles" / "sleep.css"
    f.write_text(".eui-titel { color: red; }\n@scope (.x) { .y { color: red; } }\nbody { background: url(https://example.invalid/x.png); }\n")
    res = styles.check(styles.read_project_look(demo_app.config()))
    files = {e["file"] for e in res.errors}
    assert files == {f"{SLEEP}/styles/sleep.css"}
    words = " ".join(e["problem"] for e in res.errors)
    assert "@scope" in words and "example.invalid" not in json.dumps(res.styles.public() if res.styles else {})
    assert f"apps/{APP}/styles/sleep.css" not in (res.styles.public()["app_stylesheets"] if res.styles else [])


def test_app_fonts_go_into_theme_css_before_the_projects(demo_app):
    app_dir = demo_app.app_dir / SLEEP
    (app_dir / "assets" / "fonts").mkdir(parents=True)
    (app_dir / "assets" / "fonts" / "dream.woff2").write_bytes(WOFF2)
    (app_dir / "styles" / "sleep.css").write_text(
        "@font-face { font-family: Dream; src: url(../assets/fonts/dream.woff2) format('woff2'); }\n"
        ".eui-title { font-family: Dream, sans-serif; }\n")
    res = styles.check(styles.read_project_look(demo_app.config()))
    assert res.errors == [], res.errors
    theme_css = res.styles.get("theme.css")[0].decode()
    assert f"url(apps/{APP}/assets/fonts/dream.woff2)" in theme_css or \
        f'url("apps/{APP}/assets/fonts/dream.woff2")' in theme_css
    assert res.styles.get(f"apps/{APP}/assets/fonts/dream.woff2")[0] == WOFF2


def test_an_app_asset_that_is_a_link_is_not_read(demo_app, tmp_path):
    secret = tmp_path / "secret.svg"
    secret.write_bytes(SVG.replace(b"#123", b"#999"))
    (demo_app.app_dir / SLEEP / "assets").mkdir()
    (demo_app.app_dir / SLEEP / "assets" / "logo.svg").symlink_to(secret)
    files = styles.read_project_look(demo_app.config())
    assert not files.app_assets
    assert any("logo.svg" in p["file"] for p in files.problems)


# ---------------------------------------------------------------- served and published


@pytest.fixture
def app_look(built_app, web_dir):  # noqa: F811
    cfg = built_app.config()
    (built_app.app_dir / SLEEP / "assets").mkdir()
    (built_app.app_dir / SLEEP / "assets" / "moon.svg").write_bytes(SVG)
    publish.publish(cfg)
    auth.set_password(cfg.state.auth, PW)
    env = Env(built_app, cfg, Clock(), Clock(1000.0), web_dir)
    c = env.make(source=publish.PublishedSource(cfg))
    c.cfg = cfg
    return c


def test_the_apps_look_is_served_after_sign_in_only(app_look):
    c = app_look
    for path in (f"/ui/apps/{APP}/styles/sleep.css", f"/ui/apps/{APP}/assets/moon.svg"):
        assert c.get(path).status_code == 401
    login(c)
    info = c.get("/api/styles").json()
    assert info["app_stylesheets"] == [f"apps/{APP}/styles/sleep.css"]
    r = c.get(f"/ui/apps/{APP}/styles/sleep.css")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/css")
    assert '[data-app="demo-sleep"]' in r.text
    r = c.get(f"/ui/apps/{APP}/assets/moon.svg")
    assert r.status_code == 200 and r.content == SVG and r.headers["content-disposition"].startswith("attachment")


@pytest.mark.parametrize("path", [
    f"/ui/apps/{APP}/app.toml", f"/ui/apps/{APP}/dashboards/overview-sleep.yaml", f"/ui/apps/{APP}/styles/x.css",
    f"/ui/apps/{APP}/styles/../../../theme.yaml", "/ui/apps/Demo/styles/sleep.css", "/ui/apps/demo-moves/styles/sleep.css",
    f"/ui/apps/{APP}/%2e%2e/{APP}/styles/sleep.css", f"/ui/apps/{APP}/styles/.sleep.css",
])
def test_nothing_else_of_an_app_is_served(app_look, path):
    login(app_look)
    assert app_look.get(path).status_code == 404


def test_the_version_keeps_the_apps_look(app_look):
    cfg = app_look.cfg
    m = json.loads((cfg.state.specs / "versions" / "1" / "manifest.json").read_text())
    paths = [e["path"] for e in m["styles"]]
    assert f"apps/{APP}/styles/sleep.css" in paths and f"apps/{APP}/assets/moon.svg" in paths
    # A change in the app's stylesheet is a change of the look: the working folder no longer matches.
    assert publish.working_matches(cfg, 1)
    f = cfg.app_dir / SLEEP / "styles" / "sleep.css"
    f.write_text(f.read_text().replace("0.02em", "0.03em"))
    assert not publish.working_matches(cfg, 1)


def test_the_page_marks_the_apps_parts(demo_app):
    """data-app marks exactly what an app contributed; the project's own components carry no mark."""
    comp = compose.compose(demo_app.config())
    over = comp.get("overview")
    marks = [[c.get("app") for c in row] for row in over.dashboard.layout]
    assert marks[2] == ["demo-moves"] and marks[-1] == ["demo-sleep", "demo-sleep"]
    assert all(m is None for row in marks[:2] for m in row)


def test_an_archive_can_carry_an_apps_look(built_app):
    from conftest import ACTIVITIES_YAML, OVERVIEW_YAML

    raw = tar_of({
        "overview.yaml": OVERVIEW_YAML.encode(), "activities.yaml": ACTIVITIES_YAML.encode(),
        f"apps/{APP}/styles/sleep.css": b".eui-title { letter-spacing: 0.02em; }\n",
        f"apps/{APP}/assets/moon.svg": SVG,
    })
    _dashboards, files = publish.read_archive_parts(raw)
    assert [b.path for b in files.blobs()] == [f"apps/{APP}/styles/sleep.css", f"apps/{APP}/assets/moon.svg"]
    res = publish.publish_from_archive(built_app.config(), raw)
    assert {s["path"] for s in res["styles"]} == {f"apps/{APP}/styles/sleep.css", f"apps/{APP}/assets/moon.svg"}


@pytest.mark.parametrize("member", [
    "apps/Demo/styles/a.css", "apps/demo-sleep/app.toml", "apps/demo-sleep/styles/../../x.css",
    "apps/demo-sleep/dashboards/a.yaml", "apps/x/styles/a.css/../b.css",
])
def test_an_archive_with_odd_app_members_is_refused(built_app, member):
    from conftest import OVERVIEW_YAML

    raw = tar_of({"overview.yaml": OVERVIEW_YAML.encode(), member: b".a{}"})
    with pytest.raises((Refused, SpecError)):
        publish.publish_from_archive(built_app.config(), raw)


def test_scoping_survives_odd_but_valid_css():
    text = '.a[title="}{"] { content: "}"; }\n/* } */\n.b { color: red; }\n'
    out = scoped(text)
    assert out.count(W) == 4
    assert re.search(r'\[title="\}\{"\]' + re.escape(W), out)


# ---------------------------------------------------------------- themes an app ships


def test_an_apps_theme_is_listed_and_can_be_used(demo_app, monkeypatch):
    from conftest import run_cli, run_cli_json

    from easyui import theme as themes

    tdir = demo_app.app_dir / SLEEP / "themes"
    tdir.mkdir()
    (tdir / "night.yaml").write_text(themes.ready_made_text("dusk").replace("name: Dusk", "name: Night"))
    (tdir / "broken.yaml").write_text("colors: [1, 2\n")
    monkeypatch.chdir(demo_app.app_dir)
    code, data = run_cli_json("themes")
    assert code == 0
    mine = [t for t in data["themes"] if t["app"]]
    assert [(t["name"], t["app"]) for t in mine] == [(f"{APP}/night", APP)]
    assert any("apps/demo_sleep/themes/broken.yaml was left out" in n for n in data["notes"])
    code, out, _ = run_cli("theme", "use", f"{APP}/night")
    assert code == 0, out
    assert (demo_app.app_dir / "ui" / "theme.yaml").read_text() == (tdir / "night.yaml").read_text()
    code, data = run_cli_json("theme", "use", f"{APP}/nope", "--force")
    assert code != 0 and "has a theme called" in data["error"]["message"]

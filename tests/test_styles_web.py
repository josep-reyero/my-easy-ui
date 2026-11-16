"""The look in the server and the browser code: routes under /ui/, headers, and the stable names in the renderer."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from test_server import PW, Clock, Env, fast_hash, login, web_dir  # noqa: F401 - fixtures used by name
from test_styles import GOOD_CSS, SVG, WOFF2, make_look

from easyui import auth, publish, security, styles

WEB = Path(__file__).resolve().parents[1] / "src" / "easyui" / "web"
ASSETS = WEB / "assets"


@pytest.fixture
def look_env(built_app, web_dir):  # noqa: F811
    make_look(built_app.app_dir / "ui")
    cfg = built_app.config()
    publish.publish(cfg)
    auth.set_password(cfg.state.auth, PW)
    env = Env(built_app, cfg, Clock(), Clock(1000.0), web_dir)
    c = env.make(source=publish.PublishedSource(cfg))
    c.cfg = cfg
    return c


@pytest.mark.parametrize("path", ["/api/styles", "/ui/theme.css", "/ui/styles/a.css", "/ui/assets/logo.svg"])
def test_the_look_needs_a_session(look_env, path):
    r = look_env.get(path)
    assert r.status_code == 401
    assert "a { " not in r.text and "@font-face" not in r.text


def test_the_look_after_sign_in(look_env):
    c = look_env
    assert login(c).status_code == 200
    info = c.get("/api/styles").json()
    assert info["stylesheets"] == ["a.css"] and info["theme_css"] is True and info["rev"] != "none"

    r = c.get("/ui/theme.css")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/css")
    assert "--eui-accent" in r.text and "prefers-color-scheme: dark" in r.text
    # The page policy is exactly as strict as before: nothing inline, nothing from elsewhere.
    csp = r.headers["content-security-policy"]
    assert "style-src 'self'" in csp and "unsafe-inline" not in csp and "img-src 'self'" in csp
    assert r.headers["x-content-type-options"] == "nosniff"

    r = c.get("/ui/styles/a.css")
    assert r.status_code == 200 and r.text == GOOD_CSS
    again = c.get("/ui/styles/a.css", headers={"if-none-match": r.headers["etag"]})
    assert again.status_code == 304

    r = c.get("/ui/assets/fonts/notes.woff2")
    assert r.status_code == 200 and r.headers["content-type"] == "font/woff2" and r.content == WOFF2


def test_an_svg_is_only_ever_a_picture(look_env):
    c = look_env
    login(c)
    r = c.get("/ui/assets/logo.svg")
    assert r.status_code == 200 and r.content == SVG
    assert r.headers["content-type"].startswith("image/svg+xml")
    policies = r.headers.get_list("content-security-policy")
    assert any("sandbox" in p and "default-src 'none'" in p for p in policies)
    assert r.headers["content-disposition"].startswith("attachment")


@pytest.mark.parametrize("path", [
    "/ui/theme.yaml", "/ui/styles/../theme.yaml", "/ui/styles/b.css", "/ui/assets/missing.png",
    "/ui/assets/fonts/../../theme.yaml", "/ui/manifest.json", "/ui/dashboards/overview.yaml", "/ui/assets/.hidden.png",
    "/ui/styles/A.css", "/ui/assets/%2e%2e/theme.yaml",
])
def test_nothing_else_is_served_under_ui(look_env, path):
    login(look_env)
    assert look_env.get(path).status_code == 404


def test_a_damaged_published_look_is_not_served(look_env):
    c = look_env
    login(c)
    f = publish.published_dir(c.cfg) / "styles" / "a.css"
    os.chmod(f, 0o644)
    f.write_text(".eui-hint { color: red; }")
    publish._styles_cache.clear()
    info = c.get("/api/styles").json()
    assert info["stylesheets"] == [] and info["problem"]["message"]
    assert c.get("/ui/styles/a.css").status_code == 404


def test_the_page_policy_did_not_change():
    assert "style-src 'self';" in security.CSP and "unsafe" not in security.CSP
    assert "img-src 'self';" in security.CSP and "font-src 'self';" in security.CSP


# ---------------------------------------------------------------- the renderer


def js_text() -> str:
    return "\n".join(p.read_text() for p in sorted(ASSETS.glob("*.js")))


def test_every_documented_class_is_put_on_the_page():
    text = js_text()
    for name in styles.STABLE_CLASSES:
        assert re.search(rf"[\"' ]{re.escape(name)}[\"' ]", text), name


def test_every_documented_custom_property_is_used():
    # ...by the browser code, easy-ui's stylesheet, or the generated theme stylesheet.
    text = js_text() + (ASSETS / "style.css").read_text() + (ASSETS.parents[1] / "theme.py").read_text()
    for name in styles.CUSTOM_PROPERTIES:
        assert name in text, name


def test_every_documented_data_attribute_is_set():
    text = js_text()
    for name in styles.DATA_ATTRIBUTES:
        key = name.removeprefix("data-")
        assert re.search(rf"dataset\.{key}\b|\b{key}: ", text), name


def test_project_stylesheets_only_go_into_the_dashboard_tree():
    look = (ASSETS / "look.js").read_text()
    dash = (ASSETS / "dashboard.js").read_text()
    # The page itself only ever gets ui/theme.css; project stylesheets are added to the shadow root's sheets.
    assert look.count("document.head.append") == 1 and 'address("ui/theme.css")' in look
    assert "attachShadow" in dash and "this.sheets.append" in dash
    for f in ASSETS.glob("*.js"):
        if f.name not in ("look.js", "dashboard.js"):
            assert "ui/styles" not in f.read_text(), f.name


def test_easy_ui_own_screens_drop_the_theme():
    app = (ASSETS / "app.js").read_text()
    assert app.count("useLook(false)") >= 4  # settings, not found, failed load, fatal
    assert "forgetLook()" in app  # signing out

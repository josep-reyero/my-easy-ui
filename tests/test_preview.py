"""The local preview: throwaway data, the working folder as a source, problems as page content, serving."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import threading
import time
import types
from pathlib import Path

import httpx
import pytest
from conftest import OVERVIEW_YAML, make_demo_db, run_cli

from easyui import bundle, paths, preview, spec
from easyui.errors import NotAvailable, NotFound, SpecError, UsageError

TEXT_ONLY = "spec: easy-ui/v1\ntitle: {title}\nlayout:\n  - - type: text\n      body: Hello {title}.\n"
BAD = "spec: easy-ui/v1\ntitle: Broken\nlayout:\n  - - type: stat\n      title: X\n      querry: totals\n"
INLINE_TYPES = {"text", "b", "i", "a"}
BLOCK_TYPES = {"h", "p", "ul", "ol"}


def rewrite(path: Path, text: str) -> None:
    """Write and make sure the change is visible even on file systems with coarse times."""
    st = path.stat() if path.exists() else None
    path.write_text(text, encoding="utf-8")
    if st is not None:
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))


def walk_inline(nodes):
    for n in nodes:
        assert n["t"] in INLINE_TYPES and set(n) <= {"t", "v", "c", "href"}
        if n["t"] == "text":
            assert isinstance(n["v"], str)
        else:
            walk_inline(n["c"])


def assert_safe_blocks(blocks):
    for b in blocks:
        assert b["t"] in BLOCK_TYPES
        if b["t"] in ("ul", "ol"):
            for item in b["items"]:
                walk_inline(item)
        else:
            walk_inline(b["c"])


@pytest.fixture
def pv(demo_app):
    p = preview.prepare(demo_app.config())
    yield p
    p.close()


# ---------------------------------------------------------------- data


def test_prepare_uses_real_data_in_a_throwaway_folder(demo_app):
    p = preview.prepare(demo_app.config())
    try:
        assert p.data["from"] == "app data" and p.data["tables"]["days"] == 120
        assert p.root.is_dir() and not str(p.root).startswith(str(demo_app.state_home))
        assert bundle.current_bundle(p.config) is not None
        assert bundle.current_bundle(demo_app.config()) is None  # the real UI state is untouched
        assert not demo_app.state_home.exists() or not any(demo_app.state_home.rglob("*.sqlite"))
        assert len(p.token) >= 32
        tables = {t["name"] for t in bundle.describe_tables(bundle.current_bundle(p.config))}
        assert tables == {"days", "activities", "weekly"}  # only the allowlist, as in data build
    finally:
        p.close()
    assert not p.root.exists()


def make_fixture(app, name="sample.sqlite") -> Path:
    fx = app.app_dir / "ui" / "fixtures" / name
    make_demo_db(fx)
    return fx


def test_fixture_is_used_when_there_is_no_real_data(demo_app):
    demo_app.db_path.unlink()
    make_fixture(demo_app)
    path, kind = preview.choose_data(demo_app.config())
    assert kind == "fixture" and path.name == "sample.sqlite"
    p = preview.prepare(demo_app.config())
    try:
        assert p.data["from"] == "fixture" and p.data["file"] == "ui/fixtures/sample.sqlite"
        assert p.source.list()[1]["slug"] == "overview"
    finally:
        p.close()


def test_fixture_named_like_the_data_file_wins(demo_app):
    demo_app.db_path.unlink()
    make_fixture(demo_app, "aaa.sqlite")
    make_fixture(demo_app, "demo.sqlite")
    assert preview.choose_data(demo_app.config())[0].name == "demo.sqlite"


def test_fixture_flag_forces_fixture(demo_app):
    make_fixture(demo_app)
    assert preview.choose_data(demo_app.config(), fixture=True)[1] == "fixture"
    assert preview.choose_data(demo_app.config())[1] == "app data"


def test_symlinked_fixture_is_ignored(demo_app, tmp_path):
    demo_app.db_path.unlink()
    d = demo_app.app_dir / "ui" / "fixtures"
    d.mkdir(parents=True)
    other = make_demo_db(tmp_path / "elsewhere.sqlite")
    os.symlink(other, d / "sample.sqlite")
    with pytest.raises(NotFound):
        preview.choose_data(demo_app.config())


def test_no_data_at_all(demo_app):
    demo_app.db_path.unlink()
    with pytest.raises(NotFound) as ei:
        preview.choose_data(demo_app.config())
    assert "ui/fixtures" in ei.value.hint
    code, out, err = run_cli("preview")
    assert code == 3 and "no data yet" in err


def test_fixture_without_allowlisted_tables(demo_app, monkeypatch):
    demo_app.db_path.unlink()
    fx = demo_app.app_dir / "ui" / "fixtures" / "sample.sqlite"
    fx.parent.mkdir(parents=True)
    sqlite3.connect(fx).execute("CREATE TABLE other (x)").connection.close()
    made = []
    real_mkdtemp = preview.tempfile.mkdtemp

    def mkdtemp(**kw):
        made.append(Path(real_mkdtemp(**kw)))
        return str(made[-1])

    monkeypatch.setattr(preview.tempfile, "mkdtemp", mkdtemp)
    with pytest.raises(NotFound) as ei:
        preview.prepare(demo_app.config())
    assert "did you mean" in ei.value.message or "no table" in ei.value.message
    assert made and not made[0].exists()  # the throwaway folder is removed again


# ---------------------------------------------------------------- the working source


def test_lists_and_compiles_from_the_working_folder(pv):
    listing = pv.source.list()
    assert [d["slug"] for d in listing] == ["activities", "overview"]
    assert all(d["ok"] for d in listing)
    c = pv.source.compiled("overview")
    assert c == spec.compile_dashboard(spec.loads(OVERVIEW_YAML, slug="overview"))
    dash = pv.source.dashboard("overview")
    res = spec.run_queries(dash, ["totals"], {"since": "2026-04-01"}, bundle_path=pv.source.bundle_path())
    assert res["results"]["totals"]["rows"][0][3] == 30
    assert pv.source.preview is True


def test_rev_changes_when_a_dashboard_changes(pv, demo_app):
    r1 = pv.source.rev()
    assert pv.source.rev() == r1  # stable while nothing changes
    f = demo_app.config().dashboards_dir / "overview.yaml"
    rewrite(f, OVERVIEW_YAML.replace("title: Overview", "title: Overview v2"))
    r2 = pv.source.rev()
    assert r2 != r1 and pv.source.compiled("overview")["title"] == "Overview v2"
    demo_app.write_dashboard("new-one", TEXT_ONLY.format(title="New"))
    r3 = pv.source.rev()
    assert r3 != r2 and "new-one" in [d["slug"] for d in pv.source.list()]
    f.unlink()
    assert pv.source.rev() != r3 and "overview" not in [d["slug"] for d in pv.source.list()]
    with pytest.raises(NotFound):
        pv.source.compiled("overview")


def test_broken_dashboard_shows_problems_as_the_page(pv, demo_app):
    demo_app.write_dashboard("broken", BAD)
    listing = {d["slug"]: d for d in pv.source.list()}
    assert listing["broken"]["ok"] is False and "needs fixing" in listing["broken"]["title"]
    page = pv.source.compiled("broken")
    assert page["slug"] == "broken" and page["params"] == [] and page["queries"] == {}
    [[comp]] = page["layout"]
    assert comp["type"] == "text" and comp["id"] == "r0c0"
    assert_safe_blocks(comp["blocks"])
    flat = json.dumps(comp["blocks"])
    assert "querry" in flat and "Did you mean" in flat and "line 6" in flat
    assert any(p.get("did_you_mean") == "query" for p in page["problems"])
    with pytest.raises(SpecError) as ei:
        pv.source.dashboard("broken")
    assert ei.value.http_status == 422
    # the good dashboards keep working
    assert pv.source.compiled("overview")["title"] == "Overview"


def test_data_problem_in_a_query_is_shown(pv, demo_app):
    demo_app.write_dashboard("cols", OVERVIEW_YAML.replace("value: distance\n", "value: nope\n"))
    page = pv.source.compiled("cols")
    assert "nope" in json.dumps(page["layout"])


def test_problem_text_cannot_become_markup(pv, demo_app):
    demo_app.write_dashboard("html", "spec: easy-ui/v1\ntitle: <script>alert(1)</script>\nlayout:\n"
                             "  - - type: stat\n      title: <img src=x onerror=alert(1)>\n      bogus: 1\n")
    page = pv.source.compiled("html")
    assert_safe_blocks(page["layout"][0][0]["blocks"])
    assert page["layout"][0][0]["type"] == "text"


def test_file_level_problems_get_their_own_page(pv, demo_app, tmp_path):
    d = demo_app.config().dashboards_dir
    (d / "Bad Name.yaml").write_text(TEXT_ONLY.format(title="X"))
    target = tmp_path / "t.yaml"
    target.write_text(TEXT_ONLY.format(title="T"))
    os.symlink(target, d / "linked.yaml")
    listing = [x["slug"] for x in pv.source.list()]
    assert preview.PROBLEMS_SLUG in listing and "linked" not in listing
    page = pv.source.compiled(preview.PROBLEMS_SLUG)
    flat = json.dumps(page)
    assert "Bad Name.yaml" in flat and "linked.yaml" in flat and "link" in flat
    assert_safe_blocks(page["layout"][0][0]["blocks"])


def test_no_dashboards_page(pv, demo_app):
    for f in demo_app.config().dashboards_dir.iterdir():
        f.unlink()
    assert [d["slug"] for d in pv.source.list()] == [preview.PROBLEMS_SLUG]
    page = pv.source.compiled(preview.PROBLEMS_SLUG)
    assert "no dashboards yet" in json.dumps(page).lower()
    with pytest.raises(NotFound):
        pv.source.dashboard(preview.PROBLEMS_SLUG)


def test_problems_page_is_not_reachable_when_all_is_well(pv):
    with pytest.raises(NotFound):
        pv.source.compiled(preview.PROBLEMS_SLUG)


def test_data_file_change_rebuilds_the_data(pv, demo_app):
    r1 = pv.source.rev()
    b1 = bundle.current_bundle(pv.config)
    con = sqlite3.connect(demo_app.db_path)
    con.execute("INSERT INTO days (day, steps) VALUES ('2026-05-01', 1)")
    con.commit()
    con.close()
    os.utime(demo_app.db_path, ns=(time.time_ns(), time.time_ns() + 2_000_000_000))
    r2 = pv.source.rev()
    assert r2 != r1 and bundle.current_bundle(pv.config) != b1
    res = spec.run_queries(pv.source.dashboard("overview"), ["heart"], {"since": "2026-05-01"},
                           bundle_path=pv.source.bundle_path())
    assert res["results"]["heart"]["rows"] == [["2026-05-01", None]]


def test_broken_data_file_keeps_previous_data(pv, demo_app):
    r1 = pv.source.rev()
    b1 = bundle.current_bundle(pv.config)
    demo_app.db_path.write_bytes(b"not sqlite at all" * 100)
    r2 = pv.source.rev()
    assert r2 != r1 and bundle.current_bundle(pv.config) == b1
    assert preview.PROBLEMS_SLUG in [d["slug"] for d in pv.source.list()]
    assert "could not be" in json.dumps(pv.source.compiled(preview.PROBLEMS_SLUG))
    assert pv.source.compiled("overview")["title"] == "Overview"  # still served from the previous data


def test_status(pv, demo_app):
    demo_app.write_dashboard("broken", BAD)
    st = pv.source.status()
    assert {d["slug"]: d["ok"] for d in st["dashboards"]} == {"activities": True, "broken": False, "overview": True}
    assert "dashboard" not in st["dashboards"][0]


def test_problem_page_shape():
    page = preview.problem_page("x", "X", "ui/dashboards/x.yaml",
                                [{"file": "f", "line": 3, "where": "title", "problem": "Bad.", "hint": "Fix it.",
                                  "allowed": ["a", "b"], "did_you_mean": "a", "level": "error"}],
                                [{"file": "f", "line": None, "where": "", "problem": "Hmm.", "level": "warning"}])
    assert page["spec"] == spec.SPEC_VERSION and page["title"] == "X: needs fixing"
    blocks = page["layout"][0][0]["blocks"]
    assert_safe_blocks(blocks)
    assert blocks[1]["items"][0][0]["v"] == 'line 3, title: Bad. Did you mean "a"? Fix it. Allowed: a, b.'
    assert blocks[2]["t"] == "h"


# ---------------------------------------------------------------- serving


def test_make_url():
    assert preview.make_url("127.0.0.1", 8750, "tok") == "http://127.0.0.1:8750/?token=tok"


@pytest.mark.parametrize("port", [-1, 65536, True, "80"])
def test_bad_port(port):
    with pytest.raises(UsageError):
        preview._bind(port)


def test_port_in_use():
    s = preview._bind(0)
    try:
        with pytest.raises(UsageError) as ei:
            preview._bind(s.getsockname()[1])
        assert "already in use" in ei.value.message
    finally:
        s.close()


def test_binds_loopback_only():
    s = preview._bind(0)
    try:
        assert s.getsockname()[0] == "127.0.0.1"
    finally:
        s.close()


def test_missing_server_part(pv, monkeypatch):
    monkeypatch.setitem(sys.modules, "easyui.server", None)
    with pytest.raises(NotAvailable) as ei:
        preview.serve(pv, port=0)
    assert "server part" in ei.value.message


def test_server_without_hook(pv, monkeypatch):
    monkeypatch.setitem(sys.modules, "easyui.server", types.ModuleType("easyui.server"))
    with pytest.raises(NotAvailable):
        preview.serve(pv, port=0)
    old = types.ModuleType("easyui.server")
    old.create_app = lambda config, settings=None: None
    monkeypatch.setitem(sys.modules, "easyui.server", old)
    with pytest.raises(NotAvailable):
        preview.serve(pv, port=0)


def run_in_thread(pv, **kw):
    holder, ready = [], threading.Event()
    urls = []

    def on_ready(url):
        urls.append(url)
        ready.set()

    t = threading.Thread(target=preview.serve, args=(pv,), kwargs={"port": 0, "on_ready": on_ready,
                                                                   "server_holder": holder, **kw}, daemon=True)
    t.start()
    assert ready.wait(10)
    for _ in range(200):
        if holder and holder[0].started:
            break
        time.sleep(0.02)
    return t, holder[0], urls[0]


def stop(t, server):
    server.should_exit = True
    t.join(10)
    assert not t.is_alive()


def test_serve_with_a_fake_server(pv, monkeypatch):
    seen = {}

    def create_app(config, settings=None, *, source=None, preview_token=None, on_idle=None):
        seen.update(config=config, source=source, token=preview_token, state=os.environ.get("EASYUI_STATE_HOME"))

        async def app(scope, receive, send):
            if scope["type"] != "http":
                return
            body = json.dumps({"rev": source.rev()}).encode()
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": body})

        return app

    fake = types.ModuleType("easyui.server")
    fake.create_app = create_app
    monkeypatch.setitem(sys.modules, "easyui.server", fake)
    before = os.environ.get("EASYUI_STATE_HOME")
    t, server, url = run_in_thread(pv)
    try:
        assert url.startswith("http://127.0.0.1:") and url.endswith(f"/?token={pv.token}")
        assert seen["source"] is pv.source and seen["token"] == pv.token and seen["config"] is pv.config
        assert seen["state"] == str(pv.root)
        r = httpx.get(url, timeout=5)
        assert r.status_code == 200 and r.json()["rev"] == pv.source.rev()
    finally:
        stop(t, server)
    assert os.environ.get("EASYUI_STATE_HOME") == before


def test_serve_with_the_real_server(pv, demo_app):
    pytest.importorskip("easyui.server")
    t, server, url = run_in_thread(pv)
    base = url.split("/?")[0]
    try:
        with httpx.Client(base_url=base, timeout=10, headers={"origin": base}) as c:
            assert c.get("/api/dashboards").status_code == 401
            assert c.get("/?token=wrong").status_code in (200, 503)  # the sign-in page, not signed in
            assert c.get("/api/dashboards").status_code == 401
            r = c.get(url.removeprefix(base))
            assert r.status_code == 303 and "token" not in r.headers["location"]
            listing = c.get("/api/dashboards").json()
            # in the order of [ui] dashboards in app.toml, not by name
            assert [d["slug"] for d in listing] == ["overview", "activities"]
            compiled = c.get("/api/dashboards/overview").json()
            assert compiled["title"] == "Overview" and "SELECT" not in json.dumps(compiled)
            q = c.post("/api/query", json={"dashboard": "overview", "queries": ["totals"], "params": {"since": "2026-04-01"}})
            assert q.status_code == 200 and q.json()["results"]["totals"]["rows"][0][3] == 30
            rev1 = c.get("/api/rev").json()["rev"]
            rewrite(demo_app.config().dashboards_dir / "overview.yaml", BAD.replace("Broken", "Overview"))
            rev2 = c.get("/api/rev").json()["rev"]
            assert rev2 != rev1
            page = c.get("/api/dashboards/overview").json()
            assert page["layout"][0][0]["type"] == "text" and page["problems"]
            q = c.post("/api/query", json={"dashboard": "overview", "queries": ["totals"]})
            assert q.status_code == 422 and q.json()["error"]["code"] == "spec_invalid"
    finally:
        stop(t, server)
    # sessions were kept in the throwaway folder, never in the app's real UI state
    assert not (demo_app.state_home / "demo" / "auth").exists() or not any((demo_app.state_home / "demo" / "auth").iterdir())


def test_cli_prints_url_and_cleans_up(demo_app, monkeypatch):
    calls = {}

    def fake_serve(pv, *, port=0, on_ready=None, server_holder=None, on_link_used=None):
        calls["root"] = pv.root
        calls["port"] = port
        on_ready(preview.make_url("127.0.0.1", 45678, pv.token))

    monkeypatch.setattr(preview, "serve", fake_serve)
    code, out, err = run_cli("preview", "--json", "--port", "45678")
    assert code == 0
    lines = out.strip().split("\n")
    assert len(lines) == 2  # one JSON object per line: the address, then how it stopped
    assert json.loads(lines[1]) == {"stopped": "stopped", "app": "demo"}
    data = json.loads(lines[0])
    assert data["url"].startswith("http://127.0.0.1:45678/?token=") and data["app"] == "demo"
    assert data["data"]["from"] == "app data"
    assert calls["port"] == 45678 and not calls["root"].exists()
    code, out, err = run_cli("preview")
    assert code == 0 and "open http://127.0.0.1:45678/?token=" in out and "Ctrl-C" in out


def test_cli_cleans_up_on_error(demo_app, monkeypatch):
    roots = []

    def failing(pv, **kw):
        roots.append(pv.root)
        raise NotAvailable("The server part is missing.", hint="Reinstall.")

    monkeypatch.setattr(preview, "serve", failing)
    code, out, err = run_cli("preview", "--json")
    assert code == 1 and json.loads(out)["error"]["code"] == "not_available"
    assert roots and not roots[0].exists()


def test_cli_bad_port(demo_app):
    code, out, err = run_cli("preview", "--port", "99999")
    assert code == 2


def test_preview_config_keeps_app_settings(demo_app, tmp_path):
    cfg = demo_app.config()
    pc = preview.PreviewConfig.wrap(cfg, tmp_path / "pv", None)
    assert pc.name == cfg.name and pc.tables == cfg.tables and pc.dashboards_dir == cfg.dashboards_dir
    assert pc.state.root == tmp_path / "pv" / "demo" and pc.source_db() == cfg.source_db()
    assert paths.state_paths("demo").root != pc.state.root
    shutil.rmtree(tmp_path / "pv", ignore_errors=True)

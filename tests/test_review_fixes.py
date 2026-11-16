"""Regression tests for the v0.1 review findings (security, first use, plain language).

Each test names the finding it guards. All data is synthetic (the demo app).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from conftest import make_demo_app, run_cli, run_cli_json

from easyui import appconfig, auth, bundle, cli, preview, publish, security, server, spec, units
from easyui.errors import ConfigError, NotFound, UsageError
from easyui.sessions import SessionStore

PW = "synthetic test password 1"
EASY_UI = Path(sys.executable).parent / "easy-ui"


# ---------------------------------------------------------------- sign-in under attack


def test_new_browser_during_an_attack_is_told_the_way_back_in(tmp_path, caplog):
    """Finding: a scripted attacker keeps new devices out. The message and the log must say what is going on."""
    now = [1000.0]
    clock = lambda: now[0]  # noqa: E731
    d = tmp_path / "auth"
    auth.set_password(d, PW)
    a = auth.Authenticator(d, SessionStore(d, clock=clock), clock)
    caplog.set_level(logging.INFO, logger="easyui.auth")
    for _ in range(auth.FREE_ATTEMPTS):
        with pytest.raises(auth.WrongPassword):
            a.sign_in("not it at all")
    with pytest.raises(auth.TooManyAttempts) as e:
        a.sign_in(PW)  # the owner, on a new browser, while the attacker's wait runs
    assert "signed in with before" in e.value.hint and "set-password" in e.value.hint
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "failed sign-in from a browser that has not signed in before" in logged
    assert "refused while sign-in is paused" in logged
    # once the wait is over the owner gets in and the browser becomes known ...
    now[0] += 200
    ok = a.sign_in(PW)
    # ... so when the attacker starts again, that browser is not slowed down
    for _ in range(auth.FREE_ATTEMPTS):
        with pytest.raises(auth.WrongPassword):
            a.sign_in("still not it")
    with pytest.raises(auth.TooManyAttempts):
        a.sign_in(PW)
    assert a.sign_in(PW, device_secret=ok.device_secret).session_secret


def test_check_reports_a_run_of_wrong_passwords(demo_app):
    st = demo_app.config().state.ensure()
    auth.set_password(st.auth, PW)
    a = auth.Authenticator(st.auth, SessionStore(st.auth))
    for _ in range(auth.FREE_ATTEMPTS):
        with pytest.raises(auth.WrongPassword):
            a.sign_in("wrong wrong wrong")
    item = next(i for i in server.run_checks(demo_app.config())["items"] if i["id"] == "no_guessing")
    assert item["status"] == "warn" and "guess" in item["why"]


# ---------------------------------------------------------------- slow clients


def _serve_in_thread(app, *, header_timeout: float):
    import uvicorn

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(64)
    port = sock.getsockname()[1]
    cfg = server.uvicorn_config(app, log_level="warning", limit_concurrency=4)
    cfg.http = type("Fast", (cfg.http,), {"header_timeout": header_timeout})
    srv = uvicorn.Server(cfg)
    t = threading.Thread(target=srv.run, kwargs={"sockets": [sock]}, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    return srv, t, port


def test_a_connection_that_never_finishes_its_headers_is_closed():
    """Finding: idle half-sent requests filled uvicorn's connection limit for as long as the attacker liked."""

    async def app(scope, receive, send):
        if scope["type"] != "http":
            return
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-length", b"2")]})
        await send({"type": "http.response.body", "body": b"ok"})

    srv, t, port = _serve_in_thread(app, header_timeout=0.4)
    try:
        slow = [socket.create_connection(("127.0.0.1", port)) for _ in range(4)]
        for s in slow:
            s.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\n")
            s.settimeout(3)
        for s in slow:
            assert s.recv(100) == b""  # closed by the server
        # the limit is free again
        with socket.create_connection(("127.0.0.1", port)) as ok:
            ok.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
            ok.settimeout(3)
            assert ok.recv(100).startswith(b"HTTP/1.1 200")
    finally:
        srv.should_exit = True
        t.join(5)


def test_a_body_that_does_not_arrive_in_time_gets_408():
    async def inner(scope, receive, send):  # pragma: no cover - never reached
        raise AssertionError

    mw = security.SecurityMiddleware(inner, body_timeout=0.2, origins=security.Origins(local_port=1))
    sent = []

    async def receive():
        await asyncio.sleep(10)

    async def send(m):
        sent.append(m)

    scope = {
        "type": "http", "method": "POST", "path": "/api/login", "scheme": "http", "client": ("127.0.0.1", 5000),
        "headers": [(b"host", b"127.0.0.1:1"), (b"origin", b"http://127.0.0.1:1"), (b"content-type", b"application/json")],
    }
    asyncio.run(asyncio.wait_for(mw(scope, receive, send), 3))
    assert sent[0]["status"] == 408 and b"too slowly" in sent[1]["body"]


# ---------------------------------------------------------------- one time budget per request

SLOW = """\
spec: easy-ui/v1
title: Slow
params:
  n: {{type: int, label: N, default: 0, min: 0, max: 200}}
queries:
{queries}
layout:
  - - type: controls
{stats}
"""


def _slow_dashboard(count: int) -> str:
    queries = "\n".join(
        f"  q{i}:\n    sql: WITH RECURSIVE r(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM r WHERE i < :n * 100000000) "
        "SELECT count(*) AS a FROM r" for i in range(count)
    )
    stats = "\n".join(f"  - - {{type: stat, title: q{i}, query: q{i}, value: a}}" for i in range(count))
    return SLOW.format(queries=queries, stats=stats)


def test_all_queries_of_a_request_share_one_time_budget(bundle_path):
    """Finding: 30 slow queries held a query slot for 60 s per request."""
    dash = spec.loads(_slow_dashboard(6), slug="slow")
    started = time.monotonic()
    out = spec.run_queries(dash, [f"q{i}" for i in range(6)], {"n": 200}, bundle_path=bundle_path,
                           errors="inline", total_time_s=0.6)
    took = time.monotonic() - started
    assert took < 2.0
    errs = [r["error"] for r in out["results"].values()]
    assert len(errs) == 6 and all(e["code"] == "query_timeout" for e in errs)
    assert "together took longer than 0.6 seconds" in errs[-1]["message"]


def test_publish_tries_the_other_control_values(built_app):
    """A query that is fine with the defaults but too slow for another value is not published."""
    built_app.write_dashboard("slow", _slow_dashboard(1))
    with pytest.raises(Exception) as e:
        publish.publish(built_app.config())
    assert 'With "N" set to 200' in e.value.message


# ---------------------------------------------------------------- the terminal


def test_control_characters_in_a_dashboard_never_reach_the_terminal(built_app):
    built_app.write_dashboard("esc", 'spec: easy-ui/v1\n"\\e[2Jbogus\\e]0;owned\\a": 1\n')
    code, out, err = run_cli("validate", "ui/dashboards/esc.yaml")
    assert code == 1 and "\x1b" not in out + err and "\x07" not in out + err and "\\x1b[2Jbogus" in out
    code, out, err = run_cli("publish")
    assert code == 1 and "\x1b" not in out + err and "\\x1b" in err
    assert cli.safe_text("a‮b\nc\td") == "a\\u202eb\nc\td"


# ---------------------------------------------------------------- deep nesting


def test_deeply_nested_yaml_is_a_plain_mistake(built_app):
    built_app.write_dashboard("deep", "spec: easy-ui/v1\ntitle: " + "[" * 5000 + "]" * 5000 + "\nlayout: []\n")
    code, data = run_cli_json("validate", "ui/dashboards/deep.yaml")
    assert code == 1
    assert "nested more than" in data["dashboards"][0]["errors"][0]["problem"]
    code, data = run_cli_json("publish")
    assert code == 1 and data["error"]["code"] == "spec_invalid"


def test_deeply_nested_json_is_a_400(demo_app):
    from starlette.testclient import TestClient

    cfg = demo_app.config()
    c = TestClient(server.create_app(cfg, server.ServerSettings(local_port=8750)), base_url="http://127.0.0.1:8750")
    body = b"[" * 30000 + b"]" * 30000
    r = c.post("/api/login", content=body, headers={"origin": "http://127.0.0.1:8750", "content-type": "application/json"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_json"


# ---------------------------------------------------------------- service files


@pytest.mark.parametrize("user", ["root", "nobody", "daemon", "www-data"])
def test_units_refuse_administrator_and_shared_accounts(user):
    with pytest.raises(UsageError) as e:
        units.check_service_user(user)
    assert e.value.code == "account_refused"


def test_units_refuse_shared_socket_owner_and_world_writable_program():
    site = {"origins": ["https://box.example.ts.net"]}
    with pytest.raises(UsageError):
        units.render_units("demo", socket_owner="nobody", **site)
    for exe in ("/tmp/evil/easy-ui", "/var/tmp/easy-ui", "/dev/shm/easy-ui"):
        with pytest.raises(UsageError) as e:
            units.render_units("demo", exec_path=exe, **site)
        assert e.value.code == "path_refused"
    assert "SocketUser=root" in units.render_units("demo", **site)["mes-demo-web.socket"]


def test_units_carry_the_allowed_host_names():
    svc = units.render_units("demo", allow_hosts=["Box.Example.ts.net"])["mes-demo-web.service"]
    assert " --origin https://box.example.ts.net" in svc and "EASYUI_ALLOW_HOSTS" not in svc
    with pytest.raises(UsageError):
        units.render_units("demo", allow_hosts=["bad host\nExecStart=/bin/sh"])


# ---------------------------------------------------------------- preview


@pytest.mark.parametrize("name", ["sample.db", "sample.sqlite3", "sample.sqlite"])
def test_preview_finds_sample_data_with_common_names(tmp_path, name):
    """Finding: the fitness fixture is named .db (because .gitignore refuses *.sqlite) and was not found."""
    app = make_demo_app(tmp_path, dashboards=False)
    fx = app.app_dir / "ui" / "fixtures" / name
    fx.parent.mkdir(parents=True)
    fx.write_bytes(app.db_path.read_bytes())
    cfg = appconfig.load(app.app_dir)
    path, kind = preview.choose_data(cfg, fixture=True)
    assert path == fx and kind == "fixture"


def test_no_sample_data_message_says_what_was_looked_for(tmp_path):
    app = make_demo_app(tmp_path, dashboards=False)
    with pytest.raises(NotFound) as e:
        preview.choose_data(appconfig.load(app.app_dir), fixture=True)
    assert ".sqlite, .sqlite3 or .db" in e.value.message and "ui/fixtures/sample.sqlite" in e.value.hint


# ---------------------------------------------------------------- serve


def test_serve_stops_by_itself_only_under_systemd():
    assert server.resolve_idle_minutes(None, {}) == 0
    assert server.resolve_idle_minutes(None, {"LISTEN_FDS": "1"}) == server.DEFAULT_IDLE_MINUTES
    assert server.resolve_idle_minutes(3, {"LISTEN_FDS": "1"}) == 3


@pytest.mark.skipif(not EASY_UI.is_file(), reason="the easy-ui script is not installed in this environment")
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_serve_prints_its_json_result_when_stopped_by_a_signal(built_app, sig):
    """Finding: with --json, serve printed nothing when stopped with Ctrl-C or SIGTERM."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {**os.environ, **built_app.env()}
    p = subprocess.Popen([str(EASY_UI), "--json", "serve", "--port", str(port)], cwd=built_app.app_dir, env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        p.send_signal(sig)
        out, err = p.communicate(timeout=20)
    finally:
        p.kill()
    assert p.returncode == 0, err
    assert json.loads(out) == {"stopped": "stopped", "app": "demo", "listened_on": f"http://127.0.0.1:{port}"}
    assert "runs until" not in err and "only when asked to (Ctrl-C)" in err


def test_temporary_failures_say_when_to_try_again(demo_app):
    from starlette.testclient import TestClient

    cfg = demo_app.config()
    st = cfg.state.ensure()
    auth.set_password(st.auth, PW)
    c = TestClient(server.create_app(cfg, server.ServerSettings(local_port=8750)), base_url="http://127.0.0.1:8750")
    origin = {"origin": "http://127.0.0.1:8750"}
    assert c.post("/api/login", json={"password": PW}, headers=origin).status_code == 200
    r = c.post("/api/query", json={"dashboard": "overview", "queries": ["totals"]}, headers=origin)
    assert r.status_code in (404, 503)  # nothing published / no data yet
    if r.status_code == 503:
        assert r.headers["retry-after"] == "5"


# ---------------------------------------------------------------- order and start page


def test_dashboards_follow_the_app_order(demo_app):
    cfg = demo_app.config()
    assert cfg.dashboards == ("overview", "activities")
    items = [{"slug": s} for s in ("activities", "extra", "overview")]
    assert [d["slug"] for d in server.order_dashboards(items, cfg.dashboards)] == ["overview", "activities", "extra"]


@pytest.mark.parametrize(
    "value, needle",
    [('"overview"', "must be a list"), ('["Overview"]', "not a dashboard name"), ('["a", "a"]', "more than once")],
)
def test_bad_dashboard_order(tmp_path, value, needle):
    (tmp_path / "project.toml").write_text(f'[project]\nname = "demo"\n[ui]\ndashboards = {value}\n')
    with pytest.raises(ConfigError) as e:
        appconfig.load(tmp_path)
    assert needle in e.value.message


def test_validate_notes_an_order_name_without_a_file(built_app):
    toml = built_app.app_dir / "project.toml"
    toml.write_text(toml.read_text().replace('dashboards = ["overview", "activities"]', 'dashboards = ["overveiw"]'))
    code, data = run_cli_json("validate")
    assert code == 0 and any("overveiw" in n for n in data["notes"])


# ---------------------------------------------------------------- rollback


def test_rollback_says_the_files_still_hold_the_newer_version_and_can_restore_them(built_app):
    """Finding: after a rollback the next publish quietly brought the change back."""
    cfg = built_app.config()
    publish.publish(cfg)
    original = (cfg.dashboards_dir / "overview.yaml").read_text()
    (cfg.dashboards_dir / "overview.yaml").write_text(original.replace("title: Overview", "title: Overview two"))
    publish.publish(cfg)
    code, out, _ = run_cli("rollback")
    assert code == 0 and "running easy-ui publish would show them again" in out and "--restore-files" in out
    res = publish.publish(cfg)
    assert any("same as version 2, which you rolled back from" in n for n in res["notes"])
    code, data = run_cli_json("rollback", "1", "--restore-files")
    assert code == 0 and data["files"]["restored"] == ["overview.yaml"]
    assert (cfg.dashboards_dir / "overview.yaml").read_text() == original
    backup = built_app.app_dir / data["files"]["backup"]
    assert (backup / "overview.yaml").read_text().count("Overview two") == 1
    assert publish.working_matches(cfg, 1)
    # the folder kept aside is ignored by validate and publish (its name starts with a dot)
    assert run_cli_json("publish")[1]["unchanged"] is True


def test_the_state_folder_can_be_deleted_to_start_over(built_app):
    import shutil

    publish.publish(built_app.config())
    shutil.rmtree(built_app.state_home)  # raises if a read-only folder is in the way
    assert not built_app.state_home.exists()


# ---------------------------------------------------------------- plain language


def test_usage_errors_are_plain():
    code, out, err = run_cli("nosuch")
    assert code == 2 and "There is no command called 'nosuch'. The commands are: components" in err
    assert "invalid choice" not in err and "argument <command>" not in err
    code, out, err = run_cli("tables", "--samples", "x")
    assert code == 2 and "--samples needs a whole number, but it was given 'x'" in err


YAML_CASES = [
    ("spec: easy-ui/v1\ntitle: T\nlayout:\n  - - type: stat\n    title: x\n", "not lined up"),
    ("spec: easy-ui/v1\ntitle: Sleep: last nights\nlayout: []\n", "': '"),
    ("spec: easy-ui/v1\ntitle: \"open\nlayout: []\n", "quote"),
]


@pytest.mark.parametrize("text, needle", YAML_CASES)
def test_yaml_mistakes_are_explained(text, needle):
    _dash, errors, _ = spec.check(text, slug="t", file="t.yaml")
    assert needle in errors[0].message and "<block" not in errors[0].message and errors[0].line


BASE = """\
spec: easy-ui/v1
title: T
queries:
  nights:
    sql: |
      SELECT day,
             resting_hr AS "Resting heart rate, 7-day average",
             stress_average AS stress
      FROM days
layout:
  - - type: line
      title: Rest
      query: nights
      x: day
      y: rest
"""


def test_one_round_reports_a_typo_and_a_wrong_column_together(bundle_path, tmp_path):
    """Finding: a setting typo hid the data check of other components, so fixing took two rounds."""
    text = """\
spec: easy-ui/v1
title: T
queries:
  a:
    sql: SELECT day, steps FROM days
layout:
  - - type: stat
      title: Steps
      query: a
      valeu: steps
    - type: line
      title: Steps
      query: a
      x: day
      y: stpes
"""
    p = tmp_path / "t.yaml"
    p.write_text(text)
    entry = spec.validate_file(p, bundle=bundle_path)
    problems = " | ".join(e["problem"] for e in entry["errors"])
    assert "valeu" in problems and 'does not return a column called "stpes"' in problems


def test_column_mistakes_point_at_the_line_and_suggest_a_name(bundle_path):
    dash, errors, _ = spec.check(BASE.replace("stress_average AS stress", "resting_hrr AS stress"), slug="t", file="t.yaml")
    assert not errors
    errs, _ = spec.check_with_data(dash, bundle_path)
    e = errs[0]
    assert 'uses a column called "resting_hrr", which does not exist. Did you mean "resting_hr"?' in e.message
    assert "single quotes" not in (e.hint or "") and e.line == 8


def test_allowed_names_with_commas_are_quoted(bundle_path):
    dash, errors, _ = spec.check(BASE.replace("stress_average", "steps"), slug="t", file="t.yaml")
    errs, _ = spec.check_with_data(dash, bundle_path)
    text = errs[0].text()
    assert 'Allowed: "day", "Resting heart rate, 7-day average", "stress".' in text
    assert "row 1, first component, setting y" in text


def test_a_unit_given_as_a_format_is_explained():
    text = BASE.replace("      y: rest\n", "      y: rest\n      format: bpm\n")
    _dash, errors, _ = spec.check(text, slug="t", file="t.yaml")
    assert "looks like a unit, not a format: write unit: bpm" in errors[0].hint


def test_better_setting():
    text = """\
spec: easy-ui/v1
title: T
queries:
  a: {sql: "SELECT 1 AS v, 2 AS d"}
layout:
  - - {type: stat, title: V, query: a, value: v, delta: d, better: lower}
"""
    dash, errors, _ = spec.check(text, slug="t", file="t.yaml")
    assert not errors and dash.layout[0][0]["better"] == "lower"
    _dash, errors, _ = spec.check(text.replace("better: lower", "better: up"), slug="t", file="t.yaml")
    assert errors and errors[0].allowed == ["higher", "lower"]


# ---------------------------------------------------------------- demo


def test_demo_command_writes_a_working_app(tmp_path, monkeypatch):
    monkeypatch.delenv("MES_DATA_HOME", raising=False)
    code, data = run_cli_json("demo", str(tmp_path / "d"))
    assert code == 0
    app = Path(data["app_dir"])
    assert (app / "project.toml").is_file() and (app / "ui" / "fixtures" / "demo.sqlite").is_file()
    monkeypatch.setenv("MES_DATA_HOME", data["env"]["MES_DATA_HOME"])
    monkeypatch.setenv("EASYUI_STATE_HOME", data["env"]["EASYUI_STATE_HOME"])
    cfg = appconfig.load(app)
    bundle.build(cfg)
    assert spec.validate_app(cfg)["ok"]
    code, data = run_cli_json("demo", str(tmp_path / "d"))
    assert code == 5 and data["error"]["code"] == "not_empty"

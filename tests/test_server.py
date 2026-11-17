"""The HTTP server: sessions on every route, cross-site and rebinding protection, headers, static files,
query limits, listening rules, idle stop, preview sign-in, and the check command."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import stat
import tempfile
import threading
import time
import warnings
from pathlib import Path

import httpx
import pytest
from argon2 import PasswordHasher, Type

from conftest import run_cli, run_cli_json, site_settings
from easyui import auth, bundle, sandbox, security, server, spec
from easyui.errors import NotFound, Refused, UsageError

warnings.filterwarnings("ignore", message=".*httpx.*", category=DeprecationWarning)
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from starlette.testclient import TestClient

PW = "correct horse battery staple"
API_ROUTES = [
    ("GET", "/api/dashboards"),
    ("GET", "/api/dashboards/overview"),
    ("GET", "/api/rev"),
    ("POST", "/api/query"),
    ("GET", "/api/signin"),
    ("POST", "/api/signin/password"),
    ("POST", "/api/passkeys/register"),
    ("POST", "/api/passkeys/remove"),
    ("POST", "/api/recovery-codes"),
    ("GET", "/api/nope"),
    ("POST", "/api/nope"),
]
SLOW_YAML = """\
spec: easy-ui/v1
title: Slow
queries:
  fine:
    sql: SELECT count(*) AS n FROM days
  slow:
    sql: |
      WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c)
      SELECT count(*) AS n FROM c
layout:
  - - type: stat
      title: Days
      query: fine
      value: n
    - type: stat
      title: Forever
      query: slow
      value: n
"""


class Clock:
    def __init__(self, t: float = 1_800_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


class DirSource:
    """A stand-in for the published source: dashboards straight from the app's folder (synthetic data)."""

    def __init__(self, cfg, only=None, fail=None):
        self.cfg, self.only, self.fail = cfg, only, fail

    def _files(self):
        return {
            p.stem: p
            for p in sorted(self.cfg.dashboards_dir.glob("*.yaml"))
            if self.only is None or p.stem in self.only
        }

    def list(self):
        if self.fail:
            raise self.fail
        return [{"slug": d.slug, "title": d.title, "description": d.description} for d in map(self.dashboard, self._files())]

    def dashboard(self, slug):
        f = self._files().get(slug)
        if f is None:
            raise NotFound(f'There is no published dashboard called "{slug}".', hint="Publish it with: easy-ui publish")
        return spec.load_file(f)

    def compiled(self, slug, *, today=None):
        return spec.compile_dashboard(self.dashboard(slug), today=today)

    def bundle_path(self):
        return bundle.require_current(self.cfg)

    def rev(self):
        h = hashlib.sha256(bundle.rev(self.cfg).encode())
        for p in self._files().values():
            h.update(f"{p.name}{p.stat().st_mtime_ns}".encode())
        return h.hexdigest()[:16]


@pytest.fixture(autouse=True)
def fast_hash(monkeypatch):
    monkeypatch.setattr(auth, "_hasher", PasswordHasher(time_cost=1, memory_cost=8, parallelism=1, hash_len=16, salt_len=16, type=Type.ID))


@pytest.fixture
def web_dir(tmp_path):
    w = tmp_path / "web"
    (w / "assets" / "vendor").mkdir(parents=True)
    (w / "index.html").write_text('<!doctype html><title>easy-ui</title><script type="module" src="assets/app.js"></script>')
    (w / "assets" / "app.js").write_text("export const x = 1;\n")
    (w / "assets" / "chart-3f9a1c2e.js").write_text("export const y = 2;\n")
    (w / "assets" / "style.css").write_text("body{margin:0}\n")
    (w / "assets" / "vendor" / "lib.min.js").write_text("/* synthetic */\n")
    (w / "assets" / "notes.php").write_text("<?php ?>")
    (w / "assets" / ".hidden.js").write_text("secret")
    (w / "favicon.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    (tmp_path / "outside.js").write_text("outside")
    (w / "assets" / "escape.js").symlink_to(tmp_path / "outside.js")
    return w


class Env:
    def __init__(self, app, cfg, clock, mono, web_dir):
        self.app, self.cfg, self.clock, self.mono, self.web_dir = app, cfg, clock, mono, web_dir

    def make(self, *, source=None, base="http://localhost", client=("testclient", 50000), raise_errors=True, **settings):
        settings.setdefault("web_dir", self.web_dir)
        site_settings(base, settings)
        asgi = server.create_app(
            self.cfg,
            server.ServerSettings(**settings),
            source=source or DirSource(self.cfg),
            clock=self.clock,
            monotonic=self.mono,
        )
        c = TestClient(asgi, base_url=base, client=client, raise_server_exceptions=raise_errors)
        c.asgi = asgi
        return c


@pytest.fixture
def env(built_app, web_dir):
    cfg = built_app.config()
    auth.set_password(cfg.state.auth, PW)
    return Env(built_app, cfg, Clock(), Clock(1000.0), web_dir)


@pytest.fixture
def client(env):
    return env.make()


def origin_of(c) -> str:
    return str(c.base_url).rstrip("/")


def login(c, pw=PW, **headers):
    h = {"origin": origin_of(c), **headers}
    return c.post("/api/login", json={"password": pw}, headers=h)


def post(c, path, body, **headers):
    return c.post(path, json=body, headers={"origin": origin_of(c), **headers})


def query(c, body):
    return post(c, "/api/query", body)


@pytest.fixture
def signed_in(client):
    r = login(client)
    assert r.status_code == 200, r.text
    return client


def assert_error(r, status, code=None):
    assert r.status_code == status, r.text
    err = r.json()["error"]
    assert set(err) == {"code", "message", "hint"}
    assert err["message"] and isinstance(err["message"], str)
    if code:
        assert err["code"] == code
    return err


# ---------------------------------------------------------------- sessions everywhere


def test_session_endpoint_before_sign_in(client):
    r = client.get("/api/session")
    assert r.status_code == 200
    assert r.json() == {
        "signed_in": False,
        "app": {"name": "demo", "title": "Demo project", "accent": "#2f6fdf"},
        "sign_in": {"password": True, "passkeys": False, "passkeys_here": True, "recovery": False},
    }


@pytest.mark.parametrize(("method", "path"), API_ROUTES)
def test_no_api_route_works_without_a_session(client, method, path):
    if method == "GET":
        r = client.get(path)
    else:
        r = post(client, path, {"dashboard": "overview", "queries": ["totals"]})
    err = assert_error(r, 401, "sign_in_required")
    assert "sign in" in (err["message"] + err["hint"]).lower()


@pytest.mark.parametrize(("method", "path"), API_ROUTES)
def test_a_forged_or_stale_cookie_does_not_work(client, method, path):
    client.cookies.set("easyui-demo", "x" * 43)
    r = client.get(path) if method == "GET" else post(client, path, {"dashboard": "overview", "queries": ["totals"]})
    assert r.status_code == 401


def test_sign_in_and_use_the_api(signed_in):
    assert signed_in.get("/api/session").json()["signed_in"] is True
    items = signed_in.get("/api/dashboards").json()
    assert {d["slug"] for d in items} == {"overview", "activities"}
    assert all(set(d) == {"slug", "title", "description"} for d in items)
    r = query(signed_in, {"dashboard": "overview", "queries": ["totals", "daily"], "params": {"since": "2026-04-01"}})
    assert r.status_code == 200
    res = r.json()["results"]
    assert res["totals"]["columns"][0] == "distance" and res["totals"]["truncated"] is False
    assert res["daily"]["rows"][0][0] == "2026-04-01"


def test_wrong_password(client):
    r = login(client, "not the password")
    err = assert_error(r, 401, "wrong_password")
    assert "case-sensitive" in err["hint"]
    assert "set-cookie" not in r.headers
    assert client.get("/api/session").json()["signed_in"] is False


def test_login_needs_a_password_field(client):
    assert_error(post(client, "/api/login", {}), 400)
    assert_error(post(client, "/api/login", {"password": 5}), 400)
    assert_error(post(client, "/api/login", {"password": PW, "admin": True}), 400, "bad_request")
    r = client.post("/api/login", content=b"{not json", headers={"origin": origin_of(client), "content-type": "application/json"})
    assert_error(r, 400, "bad_json")
    r = client.post("/api/login", content=b"[1, 2]", headers={"origin": origin_of(client), "content-type": "application/json"})
    assert_error(r, 400, "bad_json")


def test_no_password_set_needs_the_human(env):
    (env.cfg.state.auth / auth.PASSWORD_FILE).unlink()
    c = env.make()
    err = assert_error(login(c), 409, "needs_human")
    assert "set-password" in err["hint"]


def _cookies(r):
    return {h.split("=", 1)[0]: h for h in r.headers.get_list("set-cookie")}


def test_cookie_on_plain_local_http(client):
    r = login(client)
    cookies = _cookies(r)
    assert set(cookies) == {"easyui-demo", "easyui-demo-device"}
    for h in cookies.values():
        low = h.lower()
        assert "httponly" in low and "samesite=strict" in low and "path=/" in low
        assert "secure" not in low
    assert "max-age=2592000" in cookies["easyui-demo"].lower()


def test_cookie_over_https_uses_host_prefix_and_secure(env):
    c = env.make(base="https://localhost")
    r = login(c)
    cookies = _cookies(r)
    assert set(cookies) == {"__Host-easyui-demo", "__Host-easyui-demo-device"}
    for h in cookies.values():
        low = h.lower()
        assert "secure" in low and "httponly" in low and "samesite=strict" in low and "path=/" in low
        assert "domain=" not in low
    assert r.headers["strict-transport-security"].startswith("max-age=")
    assert c.get("/api/dashboards").status_code == 200


def test_https_follows_the_configured_origin_not_forwarded_proto(env):
    # The proxy talks plain http to the server; the configured origin is the browser's https address.
    for peer in (("127.0.0.1", 40000), ("203.0.113.7", 40000)):
        for headers in ({}, {"x-forwarded-proto": "https"}, {"x-forwarded-proto": "http"}):
            c = env.make(client=peer, origins=("https://localhost",))
            r = login(c, origin="https://localhost", **headers)
            assert r.status_code == 200
            assert "__Host-easyui-demo" in _cookies(r), (peer, headers)
            assert "strict-transport-security" in r.headers


def test_trusted_peer_rules():
    assert security.is_trusted_peer(None)  # local (unix) socket
    assert security.is_trusted_peer(("127.0.0.1", 1)) and security.is_trusted_peer(("127.8.9.10", 1))
    assert security.is_trusted_peer(("::1", 1)) and security.is_trusted_peer(("::ffff:127.0.0.1", 1))
    for peer in (("10.0.0.2", 1), ("100.64.0.1", 1), ("203.0.113.7", 1), ("testclient", 1), ("::", 1)):
        assert not security.is_trusted_peer(peer)


def test_session_is_rotated_at_sign_in(signed_in):
    old = signed_in.cookies.get("easyui-demo")
    assert login(signed_in).status_code == 200
    new = signed_in.cookies.get("easyui-demo")
    assert new and new != old
    assert signed_in.asgi.easyui.sessions.get(old) is None
    assert signed_in.asgi.easyui.sessions.count() == 1


def test_sign_out_removes_the_session(signed_in):
    secret = signed_in.cookies.get("easyui-demo")
    r = post(signed_in, "/api/logout", {})
    assert r.status_code == 200 and r.json() == {"signed_in": False}
    assert 'easyui-demo=""' in r.headers["set-cookie"] or "max-age=0" in r.headers["set-cookie"].lower()
    assert signed_in.asgi.easyui.sessions.get(secret) is None
    signed_in.cookies.set("easyui-demo", secret)
    assert signed_in.get("/api/dashboards").status_code == 401


def test_logout_without_a_session_is_harmless_but_still_same_origin_only(client):
    assert post(client, "/api/logout", {}).status_code == 200
    assert client.post("/api/logout", json={}, headers={"origin": "https://evil.example"}).status_code == 403


def test_sessions_are_stored_hashed(signed_in, env):
    secret = signed_in.cookies.get("easyui-demo")
    raw = (env.cfg.state.auth / "sessions.json").read_text()
    assert secret not in raw and hashlib.sha256(secret.encode()).hexdigest() in raw


def test_idle_and_absolute_session_lifetimes(env):
    c = env.make(session_idle=3600, session_absolute=4 * 3600)
    login(c)
    env.clock.advance(3000)
    assert c.get("/api/rev").status_code == 200
    env.clock.advance(3000)
    assert c.get("/api/rev").status_code == 200  # still in use
    env.clock.advance(3700)
    assert c.get("/api/rev").status_code == 401  # idle too long
    login(c)
    codes = []
    for _ in range(5):  # used every 3500 s (less than the idle time) until past 4 hours
        env.clock.advance(3500)
        codes.append(c.get("/api/rev").status_code)
    assert codes == [200, 200, 200, 200, 401]  # absolute lifetime


def test_expired_cookie_is_cleared_by_the_session_endpoint(env):
    c = env.make(session_idle=60)
    login(c)
    env.clock.advance(120)
    r = c.get("/api/session")
    assert r.json()["signed_in"] is False and "easyui-demo=" in r.headers.get("set-cookie", "")


def test_throttled_sign_in_answers_429_with_retry_after(client):
    for _ in range(3):
        login(client, "wrong password!!")
    r = login(client)
    err = assert_error(r, 429, "slow_down")
    assert int(r.headers["retry-after"]) >= 1 and "second" in err["message"]


def test_known_device_is_not_slowed_down_by_failures_elsewhere(env):
    owner = env.make()
    assert login(owner).status_code == 200
    attacker = env.make()
    for _ in range(6):
        env.clock.advance(200)
        login(attacker, "wrong password!!")
    assert login(attacker).status_code == 429
    assert login(owner).status_code == 200  # the owner's browser has its device token


# ---------------------------------------------------------------- cross-site requests and rebinding


def test_state_changing_requests_need_same_origin(client):
    body = {"password": PW}
    assert_error(client.post("/api/login", json=body), 403, "cross_site")  # no Origin, no Sec-Fetch-Site
    assert_error(client.post("/api/login", json=body, headers={"origin": "https://evil.example"}), 403, "cross_site")
    assert_error(client.post("/api/login", json=body, headers={"origin": "null"}), 403)
    assert_error(client.post("/api/login", json=body, headers={"origin": "http://localhost:9999"}), 403)
    assert_error(
        client.post("/api/login", json=body, headers={"origin": "http://localhost", "sec-fetch-site": "cross-site"}), 403
    )
    assert_error(
        client.post("/api/login", json=body, headers={"origin": "http://localhost", "sec-fetch-site": "same-site"}), 403
    )
    assert client.post("/api/login", json=body, headers={"sec-fetch-site": "same-origin"}).status_code == 200
    assert client.post("/api/login", json=body, headers={"origin": "http://localhost"}).status_code == 200


def test_the_origin_must_be_exactly_the_configured_one(env):
    # Local use: only http://localhost:PORT, so an https page (another program on this computer) is refused.
    plain = env.make()
    assert login(plain, origin="https://localhost").status_code == 403
    # Behind a TLS proxy that talks plain http to the server: the configured https origin, never the http one.
    proxied = env.make(origins=("https://localhost",))
    assert login(proxied, origin="https://localhost").status_code == 200
    assert login(proxied, origin="http://localhost").status_code == 403
    tls = env.make(base="https://localhost")
    assert login(tls, origin="http://localhost").status_code == 403


def test_query_from_another_site_is_refused_even_with_a_session(signed_in):
    r = signed_in.post(
        "/api/query", json={"dashboard": "overview", "queries": ["totals"]}, headers={"origin": "https://evil.example"}
    )
    assert_error(r, 403, "cross_site")


@pytest.mark.parametrize(
    "ctype",
    ["text/plain", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x", None, "application/json; charset=latin-1"],
)
def test_state_changing_requests_need_json(signed_in, ctype):
    headers = {"origin": "http://localhost"}
    if ctype:
        headers["content-type"] = ctype
    r = signed_in.post("/api/query", content=b'{"dashboard":"overview","queries":["totals"]}', headers=headers)
    assert_error(r, 415, "json_required")


def test_json_with_charset_is_fine(signed_in):
    r = signed_in.post(
        "/api/query",
        content=b'{"dashboard":"overview","queries":["totals"]}',
        headers={"origin": "http://localhost", "content-type": "application/json; charset=utf-8"},
    )
    assert r.status_code == 200


@pytest.mark.parametrize("host", ["evil.example", "localhost.evil.example", "127.0.0.2.nip.io", "a b", "", "localhost:99999"])
def test_unknown_host_names_are_refused(signed_in, host):
    r = signed_in.get("/api/dashboards", headers={"host": host})
    err = assert_error(r, 400, "unknown_host")
    assert "--allow-host" in err["hint"]
    assert signed_in.get("/", headers={"host": host}).status_code == 400


@pytest.mark.parametrize("host", ["localhost", "localhost:80", "127.0.0.1", "127.0.0.1:80", "[::1]", "[::1]:80", "LOCALHOST"])
def test_local_host_names_are_accepted(signed_in, host):
    origin = "http://" + host.lower().removesuffix(":80")  # browsers leave out the default port
    r = signed_in.post("/api/query", json={"dashboard": "overview", "queries": ["totals"]}, headers={"host": host, "origin": origin})
    assert r.status_code == 200


@pytest.mark.parametrize("host", ["localhost:8765", "127.0.0.1:9999", "[::1]:81"])
def test_local_names_on_another_port_are_refused(signed_in, host):
    """The server listens on port 80 here: a page on another port of this computer is another site."""
    r = signed_in.get("/api/dashboards", headers={"host": host})
    assert_error(r, 400, "unknown_host")


def test_configured_host_names(env):
    c = env.make(base="https://dash.example.ts.net", origins=("https://dash.example.ts.net",))
    assert login(c).status_code == 200
    assert c.get("/api/dashboards").status_code == 200
    assert c.get("/api/dashboards", headers={"host": "other.example.ts.net"}).status_code == 400


def test_host_normalising():
    assert security.normalize_host("Example.ORG:443") == "example.org"
    assert security.normalize_host("[::1]:80") == "[::1]"
    for bad in ("", None, "a b", "exa_mple.org", "[::1", "host:port", "host:0", "x" * 400, "-bad.example"):
        assert security.normalize_host(bad) is None


@pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH", "OPTIONS", "TRACE"])
def test_only_get_head_post(client, method):
    r = client.request(method, "/api/login", headers={"origin": "http://localhost", "content-type": "application/json"})
    assert_error(r, 405, "method_not_allowed")
    assert r.headers["allow"] == "GET, HEAD, POST"


def test_wrong_method_on_a_known_route(signed_in):
    assert signed_in.get("/api/login").status_code == 405
    assert signed_in.get("/api/query").status_code == 405


# ---------------------------------------------------------------- limits


def test_request_body_size_limit(signed_in):
    big = {"dashboard": "overview", "queries": ["totals"], "params": {"since": "x" * 70_000}}
    assert_error(query(signed_in, big), 413, "too_large")


def test_body_limit_holds_without_content_length(env):
    c = env.make(max_body=1024)
    login(c)

    def chunks():
        for _ in range(10):
            yield b" " * 512

    r = c.post("/api/query", content=chunks(), headers={"origin": "http://localhost", "content-type": "application/json"})
    assert_error(r, 413, "too_large")


def test_query_count_limit(signed_in):
    r = query(signed_in, {"dashboard": "overview", "queries": ["totals"] * (server.MAX_QUERIES_PER_REQUEST + 1)})
    err = assert_error(r, 400, "bad_param")
    assert str(server.MAX_QUERIES_PER_REQUEST) in err["hint"]
    assert query(signed_in, {"dashboard": "overview", "queries": ["totals"] * server.MAX_QUERIES_PER_REQUEST}).status_code == 200


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"dashboard": "overview"}, 400),
        ({"dashboard": "overview", "queries": []}, 400),
        ({"dashboard": "overview", "queries": "totals"}, 400),
        ({"dashboard": "overview", "queries": [1]}, 400),
        ({"dashboard": "overview", "queries": ["totals"], "params": [1]}, 400),
        ({"dashboard": "overview", "queries": ["totals"], "sql": "SELECT 1"}, 400),
        ({"dashboard": "overview", "queries": ["nope"]}, 404),
        ({"dashboard": "nope", "queries": ["totals"]}, 404),
        ({"dashboard": "../etc", "queries": ["totals"]}, 404),
        ({"dashboard": 5, "queries": ["totals"]}, 404),
        ({"dashboard": "overview", "queries": ["totals"], "params": {"since": "yesterday"}}, 400),
        ({"dashboard": "overview", "queries": ["totals"], "params": {"since": "2026-01-01'; DROP TABLE days; --"}}, 400),
        ({"dashboard": "overview", "queries": ["totals"], "params": {"evil": 1}}, 400),
        ({"dashboard": "activities", "queries": ["recent"], "params": {"min_km": 9999}}, 400),
    ],
)
def test_bad_queries_are_refused_plainly(signed_in, body, status):
    assert_error(query(signed_in, body), status)


def test_only_published_dashboards_can_be_queried_or_shown(env):
    c = env.make(source=DirSource(env.cfg, only={"overview"}))
    login(c)
    assert [d["slug"] for d in c.get("/api/dashboards").json()] == ["overview"]
    assert_error(c.get("/api/dashboards/activities"), 404)
    assert_error(query(c, {"dashboard": "activities", "queries": ["recent"]}), 404)


def test_compiled_dashboard_has_no_sql(signed_in):
    r = signed_in.get("/api/dashboards/overview")
    assert r.status_code == 200
    data = r.json()
    assert data["slug"] == "overview" and data["layout"]
    text = json.dumps(data)
    assert '"sql"' not in text and "SELECT" not in text.upper().replace("SELECTED", "")
    assert_error(signed_in.get("/api/dashboards/Not_A_Slug"), 404)


def test_a_failing_query_does_not_blank_the_others(env):
    env.app.write_dashboard("slow", SLOW_YAML)
    c = env.make(limits=sandbox.Limits(timeout_s=0.2))
    login(c)
    r = query(c, {"dashboard": "slow", "queries": ["fine", "slow"]})
    assert r.status_code == 200
    res = r.json()["results"]
    assert res["fine"]["rows"] == [[120]]
    assert res["slow"]["error"]["code"] == "query_timeout" and res["slow"]["error"]["hint"]


def test_no_data_yet(demo_app, web_dir):
    cfg = demo_app.config()
    auth.set_password(cfg.state.auth, PW)
    c = TestClient(server.create_app(cfg, server.ServerSettings(web_dir=web_dir, local_port=80), source=DirSource(cfg)), base_url="http://localhost")
    login(c)
    err = assert_error(query(c, {"dashboard": "overview", "queries": ["totals"]}), 503, "no_data")
    assert "data build" in err["hint"]


def test_rev_changes_with_data_or_dashboards(signed_in, env):
    r1 = signed_in.get("/api/rev").json()["rev"]
    assert signed_in.get("/api/rev").json()["rev"] == r1
    time.sleep(0.01)
    env.app.write_dashboard("overview", (env.app.app_dir / "ui/dashboards/overview.yaml").read_text() + "\n")
    assert signed_in.get("/api/rev").json()["rev"] != r1


# ---------------------------------------------------------------- errors never leak internals


def test_unexpected_errors_have_no_trace_or_path(env):
    c = env.make(source=DirSource(env.cfg, fail=RuntimeError("boom in /srv/secret/app/thing.py")), raise_errors=False)
    login(c)
    r = c.get("/api/dashboards")
    err = assert_error(r, 500, "internal")
    assert "Traceback" not in r.text and "/srv" not in r.text and "boom" not in r.text
    assert r.headers["content-security-policy"] == security.CSP


def test_paths_are_scrubbed_from_error_messages(env):
    c = env.make(source=DirSource(env.cfg, fail=NotFound("Missing /home/someone/.local/state/x.yaml", hint="See ~/notes.txt")))
    login(c)
    err = assert_error(c.get("/api/dashboards"), 404)
    assert "/home" not in err["message"] and "~/" not in err["hint"]
    assert "(a file on the server)" in err["message"]


def test_scrub_keeps_urls_and_words():
    assert security.scrub("See https://example.org/a/b for km/h and and/or.") == "See https://example.org/a/b for km/h and and/or."
    assert security.scrub("Run: easy-ui data build") == "Run: easy-ui data build"
    assert security.scrub("file /var/lib/x.sqlite is gone") == "file (a file on the server) is gone"


# ---------------------------------------------------------------- headers on everything


@pytest.mark.parametrize("path", ["/api/session", "/api/dashboards", "/", "/assets/app.js", "/nope.php", "/api/nope"])
def test_security_headers_on_every_response(client, path):
    r = client.get(path)
    h = r.headers
    assert h["content-security-policy"] == (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
        "font-src 'self'; manifest-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )
    assert h["x-content-type-options"] == "nosniff"
    assert h["referrer-policy"] == "no-referrer"
    assert h["cross-origin-opener-policy"] == "same-origin"
    assert h["cross-origin-resource-policy"] == "same-origin"
    assert h["x-frame-options"] == "DENY"
    pp = [p.strip() for p in h["permissions-policy"].split(",")]
    assert "camera=()" in pp and "geolocation=()" in pp
    # Every feature is denied except passkeys, which only this site itself may use.
    allowed = {p for p in pp if not p.endswith("=()")}
    assert allowed == {"publickey-credentials-create=(self)", "publickey-credentials-get=(self)"}
    # Chromium warns about features it does not know on every page load.
    assert not any(p.startswith(("ambient-light-sensor", "battery")) for p in pp)
    assert "server" not in h
    assert "strict-transport-security" not in h  # plain http
    if path.startswith("/api/"):
        assert h["cache-control"] == "no-store"
    else:
        assert h.get("cache-control") != "no-store"


def test_headers_also_on_refusals(client):
    for r in (
        client.get("/", headers={"host": "evil.example"}),
        client.request("DELETE", "/api/session"),
        client.post("/api/login", json={}),
    ):
        assert r.headers["content-security-policy"] == security.CSP and r.headers["x-frame-options"] == "DENY"


def test_hsts_on_every_https_response(env):
    c = env.make(base="https://localhost")
    for path in ("/", "/api/session", "/assets/app.js"):
        assert c.get(path).headers["strict-transport-security"] == "max-age=31536000"


# ---------------------------------------------------------------- static files


def test_index_and_assets_with_types_and_caching(client):
    r = client.get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert r.headers["cache-control"] == "no-cache" and r.headers["etag"]
    r = client.get("/assets/app.js")
    assert r.headers["content-type"] == "text/javascript; charset=utf-8" and r.headers["cache-control"] == "no-cache"
    r = client.get("/assets/chart-3f9a1c2e.js")
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert client.get("/assets/style.css").headers["content-type"] == "text/css; charset=utf-8"
    assert client.get("/assets/vendor/lib.min.js").status_code == 200
    assert client.get("/favicon.svg").headers["content-type"] == "image/svg+xml"
    assert client.head("/assets/app.js").status_code == 200


def test_etag_revalidation(client):
    etag = client.get("/assets/app.js").headers["etag"]
    r = client.get("/assets/app.js", headers={"if-none-match": etag})
    assert r.status_code == 304 and r.content == b""


@pytest.mark.parametrize(
    "path",
    [
        "/assets/",
        "/assets/vendor",
        "/assets/vendor/",
        "/assets/%2e%2e/index.html",
        "/assets/..%2f..%2fserver.py",
        "/assets/.hidden.js",
        "/assets/notes.php",
        "/assets/escape.js",
        "/assets/missing.js",
        "/assets/a\\b.js",
        "/assets/%00.js",
        "/server.py",
        "/web/index.html",
        "/.env",
    ],
)
def test_no_listing_no_escape_no_unknown_types(client, path):
    r = client.get(path)
    assert r.status_code == 404, (path, r.status_code)
    assert "outside" not in r.text and "secret" not in r.text and "<?php" not in r.text


def _raw_get(asgi, path: str) -> tuple[int, bytes]:
    """Send a path exactly as given (an HTTP client would clean up "..")."""
    import asyncio

    scope = {
        "type": "http", "method": "GET", "path": path, "raw_path": path.encode("latin-1"), "query_string": b"",
        "headers": [(b"host", b"localhost")], "client": ("testclient", 1), "server": ("localhost", 80),
        "scheme": "http", "http_version": "1.1", "root_path": "",
    }  # fmt: skip
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(asgi(scope, receive, send))
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return sent[0]["status"], body


@pytest.mark.parametrize(
    "path",
    [
        "/assets/../index.html",
        "/assets/../../outside.js",
        "/assets/vendor/../../outside.js",
        "/assets/./app.js",
        "/assets//app.js",
        "/assets/../../../../etc/passwd",
        "/../outside.js",
        "/./favicon.svg",
        "/assets/escape.js",
    ],
)
def test_raw_dot_paths_never_escape(client, path):
    status, body = _raw_get(client.asgi, path)
    assert status == 404, (path, status)
    assert b"outside" not in body


def test_page_addresses_get_the_page(client):
    for path in ("/overview", "/index.html"):
        r = client.get(path)
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/html"), path
    assert client.get("/Weird_Path!").status_code == 404


def test_a_deeper_unknown_address_gets_a_not_found_page_not_a_broken_page(client):
    """Finding: /nope/x got the dashboards page, whose relative links (assets/app.js) then pointed at /nope/assets/
    and failed."""
    for path, up in (("/nope/x", "../"), ("/d/overview", "../"), ("/a/b/c/", "../../../")):
        r = client.get(path)
        assert r.status_code == 404 and r.headers["content-type"].startswith("text/html"), path
        assert "There is no page at this address" in r.text
        assert f'href="{up}assets/style.css"' in r.text and f'href="{up}"' in r.text
        assert "app.js" not in r.text
        assert "default-src 'none'" in r.headers["content-security-policy"]
    r = client.get("/overview")
    assert r.status_code == 200 and 'src="assets/app.js"' in r.text


def test_static_files_do_not_need_a_session_and_do_not_count_as_use(client, env):
    before = client.asgi.easyui.activity.last
    env.mono.advance(5)
    assert client.get("/").status_code == 200
    assert client.asgi.easyui.activity.last == before


def test_missing_page_explains_itself(env, tmp_path):
    c = env.make(web_dir=tmp_path / "nothing")
    r = c.get("/")
    assert r.status_code == 503 and "not installed" in r.text


def test_the_real_page_is_served(env):
    if not (server.WEB_DIR / "index.html").is_file():
        pytest.skip("the renderer files are not in this checkout yet")
    c = env.make(web_dir=server.WEB_DIR)
    assert c.get("/").status_code == 200


# ---------------------------------------------------------------- idle stop


def test_only_signed_in_requests_other_than_rev_count_as_use(env):
    c = env.make()
    act = c.asgi.easyui.activity
    env.mono.advance(100)
    c.get("/api/session")
    c.get("/api/dashboards")  # 401
    assert act.idle_for() == pytest.approx(100)
    login(c)
    assert act.idle_for() == 0
    env.mono.advance(50)
    c.get("/api/rev")
    assert act.idle_for() == pytest.approx(50)
    c.get("/api/dashboards")
    assert act.idle_for() == 0


def test_idle_watcher_stops_the_server(built_app, web_dir):
    cfg = built_app.config()
    stopped = threading.Event()
    asgi = server.create_app(
        cfg, server.ServerSettings(web_dir=web_dir, idle_seconds=0.3, local_port=80), source=DirSource(cfg), on_idle=stopped.set
    )
    with TestClient(asgi, base_url="http://localhost") as c:
        t0 = time.monotonic()
        while not stopped.is_set() and time.monotonic() - t0 < 5:
            c.get("/api/rev")  # polling does not keep it alive
            time.sleep(0.05)
    assert stopped.is_set()


def test_activity_keeps_the_server_running(built_app, web_dir):
    cfg = built_app.config()
    auth.set_password(cfg.state.auth, PW)
    stopped = threading.Event()
    asgi = server.create_app(
        cfg, server.ServerSettings(web_dir=web_dir, idle_seconds=0.4, local_port=80), source=DirSource(cfg), on_idle=stopped.set
    )
    with TestClient(asgi, base_url="http://localhost") as c:
        login(c)
        t0 = time.monotonic()
        while time.monotonic() - t0 < 1.0:
            c.get("/api/dashboards")
            time.sleep(0.05)
        assert not stopped.is_set()
        assert stopped.wait(3)


# ---------------------------------------------------------------- preview sign-in


def test_preview_token_signs_in_and_leaves_the_address(env):
    asgi = server.create_app(
        env.cfg, server.ServerSettings(web_dir=env.web_dir, local_port=8750), source=DirSource(env.cfg), preview_token="tok-" + "a" * 30
    )
    c = TestClient(asgi, base_url="http://127.0.0.1:8750", follow_redirects=False)
    r = c.get("/?token=wrong")
    assert r.status_code == 200 and "set-cookie" not in r.headers
    assert c.get("/api/dashboards").status_code == 401
    r = c.get("/?token=tok-" + "a" * 30)
    assert r.status_code == 303 and r.headers["location"] == "/"
    # its own cookie name: cookies do not keep ports apart, so it must not replace a real server's cookie
    assert "easyui-demo-preview=" in r.headers["set-cookie"]
    assert c.get("/api/dashboards").status_code == 200
    assert c.get("/api/session").json()["preview"] is True


def test_preview_link_works_once_and_a_new_one_is_offered(env):
    used = []
    asgi = server.create_app(
        env.cfg, server.ServerSettings(web_dir=env.web_dir, local_port=8750), source=DirSource(env.cfg),
        preview_token="tok-" + "b" * 30, on_preview_link_used=used.append,
    )
    first = TestClient(asgi, base_url="http://127.0.0.1:8750", follow_redirects=False)
    assert first.get("/?token=tok-" + "b" * 30).status_code == 303
    assert len(used) == 1 and used[0] != "tok-" + "b" * 30
    second = TestClient(asgi, base_url="http://127.0.0.1:8750", follow_redirects=False)
    r = second.get("/?token=tok-" + "b" * 30)  # the same link again: no longer signs in
    assert r.status_code == 200 and "set-cookie" not in r.headers
    assert second.get("/api/dashboards").status_code == 401
    assert second.get("/?token=" + used[0]).status_code == 303  # the new link does
    assert second.get("/api/dashboards").status_code == 200


def test_preview_hook_signature_matches_what_preview_expects():
    import inspect

    params = inspect.signature(server.create_app).parameters
    assert "source" in params and "preview_token" in params


def test_no_token_sign_in_without_preview(client):
    r = client.get("/?token=anything", follow_redirects=False)
    assert r.status_code == 200 and "set-cookie" not in r.headers


# ---------------------------------------------------------------- listening


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "[::1]", "127.0.0.53"])
def test_loopback_addresses_are_accepted(host):
    assert server.check_bind_address(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "100.101.102.103", "example.org", "", "10.0.0.1"])
def test_other_addresses_are_refused_with_an_explanation(host):
    with pytest.raises(Refused) as e:
        server.check_bind_address(host)
    assert e.value.code == "not_local" and e.value.exit_code == 5
    assert "other computers" in e.value.message and "--port" in e.value.hint


def test_tcp_listener_is_loopback():
    sock, url, kind = server.open_listener(port=0, env={})
    try:
        assert kind == "tcp" and sock.getsockname()[0] == "127.0.0.1" and url.startswith("http://127.0.0.1:")
    finally:
        sock.close()


@pytest.fixture
def short_dir():
    d = Path(tempfile.mkdtemp(prefix="eui-", dir="/tmp"))
    yield d
    for p in sorted(d.rglob("*"), reverse=True):
        p.unlink() if not p.is_dir() else p.rmdir()
    d.rmdir()


def test_unix_socket_listener(short_dir):
    path = short_dir / "app.sock"
    sock, where, kind = server.open_listener(socket_path=path, env={})
    try:
        assert kind == "unix" and stat.S_ISSOCK(path.stat().st_mode)
        assert stat.S_IMODE(path.stat().st_mode) == 0o660
        with pytest.raises(Exception) as e:  # a second server on the same socket
            server.open_listener(socket_path=path, env={})
        assert getattr(e.value, "code", "") == "temporary"
    finally:
        sock.close()
    sock2, _, _ = server.open_listener(socket_path=path, env={})  # a stale socket is replaced
    sock2.close()
    other = short_dir / "file"
    other.write_text("x")
    with pytest.raises(Refused):
        server.open_listener(socket_path=other, env={})


def test_socket_and_port_together_is_a_usage_error(short_dir):
    with pytest.raises(UsageError):
        server.open_listener(socket_path=short_dir / "a.sock", port=8000, env={})


def test_systemd_socket_activation():
    lst = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    lst.bind(("127.0.0.1", 0))
    lst.listen(1)
    fd = os.dup(lst.fileno())
    try:
        env = {"LISTEN_FDS": "1", "LISTEN_PID": str(os.getpid())}
        sock = server._systemd_socket(env, first_fd=fd)
        assert sock is not None and sock.getsockname() == lst.getsockname()
        sock.detach()
        assert server._systemd_socket({"LISTEN_FDS": "1", "LISTEN_PID": "1"}, first_fd=fd) is None  # not for us
        assert server._systemd_socket({}, first_fd=fd) is None
        with pytest.raises(UsageError):
            server._systemd_socket({"LISTEN_FDS": "2", "LISTEN_PID": str(os.getpid())}, first_fd=fd)
    finally:
        os.close(fd)
        lst.close()


def test_systemd_socket_activation_with_a_unix_socket(short_dir):
    lst = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    lst.bind(str(short_dir / "sd.sock"))
    lst.listen(1)
    fd = os.dup(lst.fileno())
    try:
        sock = server._systemd_socket({"LISTEN_FDS": "1"}, first_fd=fd)
        assert sock.family == socket.AF_UNIX
        sock.detach()
    finally:
        os.close(fd)
        lst.close()


def test_serve_refuses_a_non_local_address(demo_app):
    code, data = run_cli_json("serve", "--bind", "0.0.0.0", "--port", "8765")
    assert code == 5 and data["error"]["code"] == "not_local"


def test_serve_refuses_to_run_as_root(demo_app, monkeypatch):
    monkeypatch.setattr(server.os, "geteuid", lambda: 0)
    code, data = run_cli_json("serve", "--port", "0")
    assert code == 5 and data["error"]["code"] == "root_refused"


def test_serve_end_to_end_on_a_unix_socket_and_stops_when_idle(built_app, short_dir):
    cfg = built_app.config()
    auth.set_password(cfg.state.auth, PW)
    path = short_dir / "s.sock"
    out: dict = {}
    previous_heap = sandbox.set_heap_limit(0)

    def run():
        out["result"] = run_cli("--json", "serve", "--socket", str(path), "--idle-minutes", "0.02", "--origin", "http://localhost")

    t = threading.Thread(target=run, daemon=True)
    t.start()
    try:
        t0 = time.monotonic()
        while not path.exists() and time.monotonic() - t0 < 10:
            time.sleep(0.05)
        transport = httpx.HTTPTransport(uds=str(path))
        with httpx.Client(transport=transport, base_url="http://localhost") as h:
            r = h.get("/api/session")
            assert r.status_code == 200 and r.json()["signed_in"] is False
            assert r.headers["content-security-policy"] == security.CSP and "server" not in r.headers
            r = h.post("/api/login", json={"password": PW}, headers={"origin": "http://localhost"})
            assert r.status_code == 200
            assert h.get("/api/dashboards").status_code == 200
            assert h.get("/", headers={"host": "evil.example"}).status_code == 400
        t.join(20)
        assert not t.is_alive(), "the server did not stop by itself"
        code, stdout, _err = out["result"]
        assert code == 0
        data = json.loads(stdout)  # with --json, stdout is only JSON
        assert data["stopped"] == "idle"
        assert not path.exists()
    finally:
        sandbox.set_heap_limit(previous_heap)


# ---------------------------------------------------------------- check


def _items(data):
    return {i["id"]: i for i in data["items"]}


def test_check_before_and_after_setup(demo_app):
    code, data = run_cli_json("check")
    assert code == 1 and data["ok"] is False
    items = _items(data)
    assert {"not_root", "state_private", "password_set"} <= set(items)
    for i in data["items"]:
        assert set(i) == {"id", "title", "status", "why", "fix"} and i["status"] in ("ok", "fail", "warn")
        assert i["why"]
        if i["status"] != "ok":
            assert i["fix"]
    assert items["not_root"]["status"] == "ok"
    assert items["password_set"]["status"] == "fail" and "set-password" in items["password_set"]["fix"]

    cfg = demo_app.config()
    with pytest.MonkeyPatch.context() as m:
        m.setattr(auth, "_hasher", PasswordHasher(type=Type.ID, **auth.ARGON2))
        auth.set_password(cfg.state.auth, PW)
    code, data = run_cli_json("check")
    items = _items(data)
    assert items["password_set"]["status"] == "ok" and items["state_private"]["status"] == "ok"
    assert items["data_ready"]["status"] == "warn" and "data build" in items["data_ready"]["fix"]
    assert code == 0 and data["ok"] is True


def test_check_finds_a_weak_hash_and_open_permissions(demo_app):
    cfg = demo_app.config()
    auth.set_password(cfg.state.auth, PW)  # the cheap test hasher: below OWASP
    code, data = run_cli_json("check")
    assert code == 1 and _items(data)["password_set"]["status"] == "fail"
    os.chmod(cfg.state.root, 0o755)
    code, data = run_cli_json("check")
    item = _items(data)["state_private"]
    assert item["status"] == "fail" and "chmod" in item["fix"]


def test_check_as_root_fails(demo_app, monkeypatch):
    monkeypatch.setattr(server.os, "geteuid", lambda: 0)
    code, data = run_cli_json("check")
    assert code == 1 and _items(data)["not_root"]["status"] == "fail"


def test_check_text_output(demo_app):
    code, out, _ = run_cli("check")
    assert code == 1 and "[FAIL]" in out and "What to do:" in out and "Why it matters:" in out


# ---------------------------------------------------------------- with the real publish part


def test_default_source_serves_published_dashboards(built_app, web_dir):
    publish = pytest.importorskip("easyui.publish")
    cfg = built_app.config()
    auth.set_password(cfg.state.auth, PW)
    c = TestClient(server.create_app(cfg, server.ServerSettings(web_dir=web_dir, local_port=80)), base_url="http://localhost")
    login(c)
    assert c.get("/api/dashboards").json() == []
    publish.publish(cfg)
    assert {d["slug"] for d in c.get("/api/dashboards").json()} == {"overview", "activities"}
    assert c.get("/api/dashboards/overview").json()["slug"] == "overview"
    r = query(c, {"dashboard": "activities", "queries": ["recent", "by_kind"], "params": {"kind": "run"}})
    assert r.status_code == 200 and set(r.json()["results"]) == {"recent", "by_kind"}


def test_preview_through_the_preview_part(built_app):
    preview = pytest.importorskip("easyui.preview")
    cfg = built_app.config()
    p = preview.prepare(cfg)
    try:
        asgi = preview._server_app(p, port=8750)
        c = TestClient(asgi, base_url="http://127.0.0.1:8750", follow_redirects=False)
        assert c.get(f"/?token={p.token}").status_code == 303
        assert {d["slug"] for d in c.get("/api/dashboards").json()} >= {"overview", "activities"}
        r = query(c, {"dashboard": "overview", "queries": ["totals"]})
        assert r.status_code == 200
        assert not (cfg.state.auth / "sessions.json").exists()  # the preview never touches the real state
    finally:
        p.close()

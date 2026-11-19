"""The request's host behind a proxy that forwards to a local (unix) socket, the socket's own safety, folder
problems as plain errors, and the sign-in page before a password is set.

``tailscale serve --https=443 unix:/run/mes/<project>-web.sock`` sends ``Host: localhost`` (the socket's own
address) and puts the name the browser used in ``X-Forwarded-Host`` (replacing any value the browser sent).
On a TCP backend it keeps the browser's ``Host``. These tests send exactly that through real sockets.
"""

from __future__ import annotations

import contextlib
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
from conftest import run_cli, run_cli_json
from softauthn import SoftAuthenticator

from easyui import auth, cli, paths, sandbox, security, server
from easyui.errors import Refused

warnings.filterwarnings("ignore", message=".*httpx.*", category=DeprecationWarning)

PW = "correct horse battery staple"
NAME = "box.example.ts.net"
SITE = f"https://{NAME}"
TAILSCALE = {"host": "localhost", "x-forwarded-host": NAME}  # what tailscale serve sends to a unix socket


@pytest.fixture(autouse=True)
def fast_hash(monkeypatch):
    monkeypatch.setattr(auth, "_hasher", PasswordHasher(time_cost=1, memory_cost=8, parallelism=1, hash_len=16, salt_len=16, type=Type.ID))


@pytest.fixture
def short_dir():
    d = Path(tempfile.mkdtemp(prefix="eui-", dir="/tmp"))  # a unix socket path must be short
    yield d
    for p in sorted(d.rglob("*"), reverse=True):
        with contextlib.suppress(OSError):
            os.chmod(p, 0o700) if p.is_dir() and not p.is_symlink() else None
            p.rmdir() if p.is_dir() and not p.is_symlink() else p.unlink()
    d.rmdir()


@pytest.fixture
def cfg(built_app):
    c = built_app.config()
    auth.set_password(c.state.auth, PW)
    return c


@contextlib.contextmanager
def serving(cfg, sock, kind, origins=(SITE,)):
    """Run the real server (uvicorn, easy-ui's settings) on ``sock``, set up the way ``serve`` sets it up."""
    import uvicorn

    settings = server.settings_for_listener(sock, kind, origins, idle_seconds=0)
    app = server.create_app(cfg, settings)
    srv = uvicorn.Server(server.uvicorn_config(app, log_config=None))
    t = threading.Thread(target=srv.run, kwargs={"sockets": [sock]}, daemon=True)
    t.start()
    t0 = time.monotonic()
    while not srv.started and time.monotonic() - t0 < 10:
        time.sleep(0.02)
    try:
        yield app
    finally:
        srv.should_exit = True
        t.join(10)
        sock.close()


class Browser:
    """An httpx client that keeps cookies itself: the __Host- cookies are Secure, and the socket is plain http."""

    def __init__(self, client: httpx.Client, headers: dict) -> None:
        self.c, self.headers, self.cookies = client, dict(headers), {}

    def _keep(self, r: httpx.Response) -> httpx.Response:
        for raw in r.headers.get_list("set-cookie"):
            name, _, rest = raw.partition("=")
            value = rest.split(";", 1)[0]
            if "max-age=0" in raw.lower() or not value:
                self.cookies.pop(name, None)
            else:
                self.cookies[name] = value
        return r

    def _h(self, extra):
        h = {**self.headers, **(extra or {})}
        if self.cookies:
            h["cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        return h

    def get(self, path, headers=None):
        return self._keep(self.c.get(path, headers=self._h(headers)))

    def post(self, path, body=None, headers=None, origin=SITE):
        h = {"origin": origin, **(headers or {})} if origin else dict(headers or {})
        return self._keep(self.c.post(path, json=body if body is not None else {}, headers=self._h(h)))


def unix_client(path):
    return httpx.Client(transport=httpx.HTTPTransport(uds=str(path)), base_url="http://localhost")


def err(r):
    return r.json()["error"]


# ---------------------------------------------------------------- through a real unix socket, as tailscale serve


def test_tailscale_on_a_unix_socket_signs_in_with_password_and_passkey(cfg, short_dir):
    path = short_dir / "web.sock"
    sock, _, kind = server.open_listener(socket_path=path, env={})
    with serving(cfg, sock, kind), unix_client(path) as h:
        b = Browser(h, TAILSCALE)
        r = b.get("/api/session")
        assert r.status_code == 200, r.text
        assert r.json()["signed_in"] is False and r.json()["sign_in"]["passkeys_here"] is True
        assert "strict-transport-security" in r.headers  # https comes from the configured origin

        # Sign in with the password; the cookies are the https ones.
        r = b.post("/api/login", {"password": PW}, headers={"x-forwarded-proto": "http"})  # the proto is ignored
        assert r.status_code == 200, r.text
        cookie = next(c for c in r.headers.get_list("set-cookie") if c.startswith("__Host-easyui-demo="))
        for flag in ("Secure", "HttpOnly", "SameSite=Strict", "Path=/"):
            assert flag.lower() in cookie.lower()
        assert b.get("/api/dashboards").status_code == 200

        # POSTs must come from the forwarded site itself.
        assert err(b.post("/api/logout", origin="http://localhost"))["code"] == "cross_site"
        assert err(b.post("/api/logout", origin=f"https://{NAME}:8443"))["code"] == "cross_site"
        assert err(b.post("/api/logout", origin=None, headers={"sec-fetch-site": "cross-site"}))["code"] == "cross_site"

        # A passkey is made for the forwarded name and its https origin, and signs in again.
        a = SoftAuthenticator()
        opts = b.post("/api/passkeys/options", {"purpose": "register"})
        assert opts.status_code == 200, opts.text
        assert opts.json()["publicKey"]["rp"]["id"] == NAME
        r = b.post("/api/passkeys/register", {"credential": a.register(opts.json(), SITE), "name": "phone"})
        assert r.status_code == 200, r.text
        assert b.post("/api/logout").status_code == 200
        assert b.get("/api/dashboards").status_code == 401
        opts = b.post("/api/passkeys/options", {"purpose": "login"})
        assert opts.status_code == 200 and opts.json()["publicKey"]["rpId"] == NAME
        r = b.post("/api/passkeys/login", {"credential": a.login(opts.json(), SITE)})
        assert r.status_code == 200, r.text
        assert b.get("/api/dashboards").status_code == 200


@pytest.mark.parametrize(
    ("headers", "code"),
    [
        ({"host": "localhost", "x-forwarded-host": "evil.example"}, "unknown_host"),  # another site's name
        ({"host": NAME, "x-forwarded-host": "evil.example"}, "unknown_host"),  # the forwarded name decides
        ({"host": "localhost", "x-forwarded-host": f"{NAME}:8443"}, "unknown_host"),  # another port
        ({"host": "localhost"}, "unknown_host"),  # a proxy that does not say the name
        ({"host": "localhost", "x-forwarded-host": f"{NAME}, evil.example"}, "bad_request"),  # a list
        ({"host": "localhost", "x-forwarded-host": f"{NAME}/x"}, "bad_request"),
        ({"host": "localhost", "x-forwarded-host": ""}, "bad_request"),
    ],
)
def test_unix_socket_refuses_other_forwarded_names(cfg, short_dir, headers, code):
    path = short_dir / "web.sock"
    sock, _, kind = server.open_listener(socket_path=path, env={})
    with serving(cfg, sock, kind), unix_client(path) as h:
        r = h.get("/api/session", headers=headers)
        assert r.status_code == 400 and err(r)["code"] == code
        r = h.post("/api/login", json={"password": PW}, headers={**headers, "origin": SITE})
        assert r.status_code == 400 and "set-cookie" not in r.headers


def test_unix_socket_refuses_a_repeated_forwarded_host(cfg, short_dir):
    path = short_dir / "web.sock"
    sock, _, kind = server.open_listener(socket_path=path, env={})
    with serving(cfg, sock, kind), unix_client(path) as h:
        r = h.get("/api/session", headers=[("host", "localhost"), ("x-forwarded-host", NAME), ("x-forwarded-host", NAME)])
        assert r.status_code == 400 and err(r)["code"] == "bad_request" and "X-Forwarded-Host" in err(r)["message"]
        assert h.get("/api/session", headers=TAILSCALE).status_code == 200


def test_unix_socket_without_forwarded_host_uses_the_host_header(cfg, short_dir):
    path = short_dir / "web.sock"
    sock, _, kind = server.open_listener(socket_path=path, env={})
    with serving(cfg, sock, kind), unix_client(path) as h:
        assert h.get("/api/session", headers={"host": NAME}).status_code == 200
        r = h.get("/api/session", headers={"host": "localhost"})
        e = err(r)
        assert e["code"] == "unknown_host" and "(localhost)" in e["message"]
        assert "X-Forwarded-Host" in e["hint"] and "--origin" in e["hint"] and "tailscale serve" in e["hint"]
        assert NAME not in json.dumps(e)  # the configured name is not told to whoever asked under another name


def test_systemd_unix_socket_reads_the_forwarded_host(cfg, short_dir, monkeypatch):
    """Socket activation: the socket systemd passes in is a unix socket, so the same rule holds; the whole
    ``serve`` command runs, as under systemd."""
    lst = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    path = short_dir / "sd.sock"
    lst.bind(str(path))
    lst.listen(8)
    fd = os.dup(lst.fileno())
    lst.close()
    real = server._systemd_socket
    monkeypatch.setattr(server, "_systemd_socket", lambda env, first_fd=3: real({"LISTEN_FDS": "1"}, first_fd=fd))
    previous_heap = sandbox.set_heap_limit(0)
    out = {}
    t = threading.Thread(
        target=lambda: out.setdefault("r", run_cli("--json", "serve", "--origin", SITE, "--idle-minutes", "0.03")),
        daemon=True,
    )
    t.start()
    try:
        with unix_client(path) as h:
            t0 = time.monotonic()
            while True:
                try:
                    r = h.get("/api/session", headers=TAILSCALE)
                    break
                except httpx.TransportError:
                    assert time.monotonic() - t0 < 10
                    time.sleep(0.05)
            assert r.status_code == 200, r.text
            r = h.post("/api/login", json={"password": PW}, headers={**TAILSCALE, "origin": SITE})
            assert r.status_code == 200 and r.headers["set-cookie"].startswith("__Host-easyui-demo=")
            assert h.get("/api/session", headers={"host": "localhost", "x-forwarded-host": "evil.example"}).status_code == 400
        t.join(20)
        assert not t.is_alive()
        assert out["r"][0] == 0
    finally:
        sandbox.set_heap_limit(previous_heap)
        with contextlib.suppress(OSError):
            path.unlink()


def test_serve_socket_command_reads_the_forwarded_host(cfg, short_dir):
    path = short_dir / "s.sock"
    previous_heap = sandbox.set_heap_limit(0)
    out = {}
    t = threading.Thread(
        target=lambda: out.setdefault(
            "r", run_cli("--json", "serve", "--socket", str(path), "--origin", SITE, "--idle-minutes", "0.03")
        ),
        daemon=True,
    )
    t.start()
    try:
        t0 = time.monotonic()
        while not path.exists() and time.monotonic() - t0 < 10:
            time.sleep(0.05)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        with unix_client(path) as h:
            assert h.get("/api/session", headers=TAILSCALE).status_code == 200
            assert h.get("/api/session", headers={"host": "localhost"}).status_code == 400
        t.join(20)
        assert not t.is_alive() and out["r"][0] == 0
    finally:
        sandbox.set_heap_limit(previous_heap)


# ---------------------------------------------------------------- over TCP the Host header alone decides


def test_tcp_ignores_forwarded_host(cfg):
    sock, url, kind = server.open_listener(port=0, env={})
    port = sock.getsockname()[1]
    with serving(cfg, sock, kind), httpx.Client(base_url=url) as h:
        # The same headers tailscale sends to a unix socket: over TCP the forwarded name means nothing.
        r = h.get("/api/session", headers={"host": "localhost", "x-forwarded-host": NAME})
        assert r.status_code == 400 and err(r)["code"] == "unknown_host"
        r = h.get("/api/session", headers={"host": f"127.0.0.1:{port}", "x-forwarded-host": NAME})
        assert r.status_code == 400 and err(r)["code"] == "unknown_host"
        # Host decides; another site's forwarded name, or a repeated one, is not even looked at.
        assert h.get("/api/session", headers={"host": NAME, "x-forwarded-host": "evil.example"}).status_code == 200
        r = h.get("/api/session", headers=[("host", NAME), ("x-forwarded-host", "a.example"), ("x-forwarded-host", "b")])
        assert r.status_code == 200
        r = h.post("/api/login", json={"password": PW}, headers={"host": NAME, "x-forwarded-host": "evil.example", "origin": SITE})
        assert r.status_code == 200 and r.headers["set-cookie"].startswith("__Host-easyui-demo=")


def test_the_choice_comes_from_the_listening_socket(short_dir):
    tcp, _, kind = server.open_listener(port=0, env={})
    try:
        s = server.settings_for_listener(tcp, kind, (SITE,))
        assert s.unix_socket is False and s.local_port == tcp.getsockname()[1]
    finally:
        tcp.close()
    unix, _, kind = server.open_listener(socket_path=short_dir / "a.sock", env={})
    try:
        s = server.settings_for_listener(unix, kind, (SITE,))
        assert s.unix_socket is True and s.local_port is None
    finally:
        unix.close()
    lst = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)  # the socket systemd passes in
    lst.bind(str(short_dir / "sd.sock"))
    lst.listen(1)
    fd = os.dup(lst.fileno())
    try:
        sd = server._systemd_socket({"LISTEN_FDS": "1"}, first_fd=fd)
        assert server.settings_for_listener(sd, "systemd", (SITE,)).unix_socket is True
        sd.detach()
    finally:
        os.close(fd)
        lst.close()


def test_effective_host_rule():
    h = [(b"host", b"localhost"), (b"x-forwarded-host", NAME.encode())]
    assert security.effective_host(h, True) == NAME
    assert security.effective_host(h, False) == "localhost"
    assert security.effective_host([(b"host", b"localhost")], True) == "localhost"
    for bad in ([*h, (b"x-forwarded-host", b"x.example")], [(b"x-forwarded-host", b"a, b")], [(b"x-forwarded-host", b"")]):
        with pytest.raises(ValueError):
            security.effective_host(bad, True)
        security.effective_host(bad, False)  # never read over TCP


# ---------------------------------------------------------------- the socket itself


def test_socket_in_a_folder_others_can_write_is_refused(short_dir):
    for mode in (0o777, 0o770, 0o1777):
        d = short_dir / f"d{mode:o}"
        d.mkdir()
        os.chmod(d, mode)
        with pytest.raises(Refused) as e:
            server.open_listener(socket_path=d / "web.sock", env={})
        assert e.value.code == "socket_dir_unsafe" and "trusted" in e.value.message
        assert not (d / "web.sock").exists()
    with pytest.raises(Refused):  # /tmp itself: everyone can write there
        server.open_listener(socket_path=Path("/tmp") / f"eui-{os.getpid()}.sock", env={})


def test_socket_folder_is_made_owner_only(short_dir):
    path = short_dir / "new" / "web.sock"
    sock, _, _ = server.open_listener(socket_path=path, env={})
    try:
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    finally:
        sock.close()


def test_serve_socket_refuses_an_unsafe_folder_as_a_plain_error(demo_app, short_dir):
    d = short_dir / "open"
    d.mkdir()
    os.chmod(d, 0o777)
    code, data = run_cli_json("serve", "--socket", str(d / "web.sock"), "--origin", SITE, "--idle-minutes", "0.01")
    assert code == 5 and data["error"]["code"] == "socket_dir_unsafe"


# ---------------------------------------------------------------- folder problems are plain errors


needs_permissions = pytest.mark.skipif(os.geteuid() == 0, reason="root may create folders anywhere")


@needs_permissions
@pytest.mark.parametrize(
    "argv",
    [
        ["set-password"],
        ["serve", "--port", "0"],
        ["passkeys", "list"],
        ["passkeys", "remove", "x"],
        ["sign-in"],
        ["sign-in", "password", "on"],
        ["recovery-codes"],
        ["rollback"],
        ["check"],
    ],
)
def test_a_ui_folder_that_cannot_be_made_is_a_plain_error(tmp_path, monkeypatch, argv):
    ro = tmp_path / "ro"
    ro.mkdir()
    os.chmod(ro, 0o500)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(auth, "read_hidden", lambda prompt: PW)
    try:
        for extra in (["--ui-dir", str(ro / "fit" / "ui")], ["--ui-dir", str(tmp_path / "fit" / "ui"), "--auth-dir", str(ro / "a" / "auth")]):
            code, out, errtext = run_cli("--json", *argv, *extra)
            assert "Traceback" not in out + errtext
            data = json.loads(out)
            if argv == ["check"]:
                assert code == 1 and data["ok"] is False
                continue
            assert code in (3, 5), data
            if argv != ["rollback"]:  # rollback does not use the sign-in folder
                assert data["error"]["code"] in ("folder_missing", "auth_dir_unsafe"), data
            assert data["error"]["hint"] and "Errno" not in data["error"]["message"]
    finally:
        os.chmod(ro, 0o700)


@needs_permissions
def test_set_password_with_an_impossible_ui_folder_says_what_to_do(tmp_path, monkeypatch):
    ro = tmp_path / "ro"
    ro.mkdir()
    os.chmod(ro, 0o500)
    monkeypatch.chdir(tmp_path)
    try:
        code, out, errtext = run_cli("set-password", "--ui-dir", str(ro / "fit" / "ui"))
        assert code == 5 and "Traceback" not in out + errtext
        assert "does not exist" in out + errtext and "--auth-dir" in out + errtext
        code, data = run_cli_json("rollback", "--ui-dir", str(ro / "fit" / "ui"))
        assert code == 3 and data["error"]["code"] == "folder_missing" and "--ui-dir" in data["error"]["hint"]
    finally:
        os.chmod(ro, 0o700)


def test_a_file_in_place_of_a_folder_is_a_plain_error(tmp_path):
    (tmp_path / "ui").mkdir()
    (tmp_path / "ui" / "data").write_text("x")
    with pytest.raises(Refused) as caught:
        paths.StatePaths(tmp_path / "ui").ensure()
    assert caught.value.code == "not_a_folder" and "a file is where a folder should be" in caught.value.message


def test_an_unexpected_os_error_is_a_plain_error(demo_app, monkeypatch):
    def broken(args):
        raise PermissionError(13, "Permission denied", "/somewhere/secret")

    monkeypatch.setattr(server, "_cmd_check", broken)
    code, out, errtext = run_cli("--json", "check")
    assert code == 5 and "Traceback" not in out + errtext
    data = json.loads(out)
    assert data["error"]["code"] == "file_problem" and data["error"]["hint"]
    assert cli.file_problem(FileNotFoundError(2, "No such file or directory", "x")).exit_code == 3
    assert cli.file_problem(OSError(5, "Input/output error")).exit_code == 1


# ---------------------------------------------------------------- the page before a password is set


def test_session_says_when_no_password_is_set(built_app, short_dir):
    cfg = built_app.config()
    path = short_dir / "web.sock"
    sock, _, kind = server.open_listener(socket_path=path, env={})
    with serving(cfg, sock, kind), unix_client(path) as h:
        ways = h.get("/api/session", headers=TAILSCALE).json()["sign_in"]
        assert ways["password_set"] is False and ways["password"] is False and ways["passkeys"] is False
        auth.set_password(cfg.state.auth, PW)
        assert h.get("/api/session", headers=TAILSCALE).json()["sign_in"]["password_set"] is True


def test_sign_in_page_tells_who_sets_the_first_password():
    js = (server.WEB_DIR / "assets" / "signin.js").read_text(encoding="utf-8")
    assert "w.password_set === false" in js
    assert "Nobody can sign in yet: no sign-in password has been set" in js
    assert "The owner sets one by typing " in js and 'text: "easy-ui set-password"' in js
    assert "!notSetUp && !showPassword" in js  # not the "password is turned off" note in that state

"""Regression tests for the second security review. Each test first plays the attack that worked before.

1. The web address (origin) and the passkey site (RP ID) came from the request, so a page on another port of the
   same host name could relay a passkey answer and get a fresh session. Now they come from configuration only.
2. Ed25519 keys of small order (and non-canonical encodings) were accepted, so a planted key verified one fixed
   signature for every message. RSA keys with an even modulus or an exponent other than 65537 were accepted.
3. Sessions survived changes to how signing in works.
4. A passkey removed while its answer was being checked still signed in.
5. A session made with the password or a recovery code could change the sign-in settings without a passkey;
   the backup-eligible flag was not compared; clientDataJSON could name a field twice.
6. The sign-in folder lived inside ui/, which the project's account writes; files under ui/ were read through links.
7. Adding a passkey needed no passkey confirmation, so a password or recovery-code session could add its own
   passkey, sign in with it and then change everything without confirming.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import warnings

import pytest
from argon2 import PasswordHasher, Type
from conftest import run_cli_json, site_settings
from softauthn import SoftAuthenticator, cbor

from easyui import (
    appconfig,
    auth,
    bundle,
    passkeys,
    paths,
    publish,
    sandbox,
    security,
    server,
    webauthn,
)
from easyui.errors import NoData, NotFound, Refused, UsageError
from easyui.webauthn import EDDSA, ES256, RS256, WebAuthnError, b64url_encode

warnings.filterwarnings("ignore", message=".*httpx.*", category=DeprecationWarning)
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from starlette.testclient import TestClient

PW = "correct horse battery staple"
LOCAL = "http://localhost:8765"


@pytest.fixture(autouse=True)
def fast_hash(monkeypatch):
    monkeypatch.setattr(auth, "_hasher", PasswordHasher(time_cost=1, memory_cost=8, parallelism=1, hash_len=16, salt_len=16, type=Type.ID))


class Clock:
    def __init__(self, t: float = 1_800_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class _Source:
    def list(self):
        return []

    def rev(self):
        return "r"


class Env:
    def __init__(self, cfg, web_dir):
        self.cfg, self.web_dir, self.clock = cfg, web_dir, Clock()
        self.apps: dict = {}

    def app(self, **settings):
        key = json.dumps(settings, sort_keys=True, default=str)
        if key not in self.apps:  # one server process per configuration (challenges live in the process)
            settings.setdefault("web_dir", self.web_dir)
            self.apps[key] = server.create_app(self.cfg, server.ServerSettings(**settings), source=_Source(), clock=self.clock)
        return self.apps[key]

    def client(self, base=LOCAL, **settings):
        if not settings:
            settings = site_settings(base, {})
        return TestClient(self.app(**settings), base_url=base, client=("127.0.0.1", 50000))


@pytest.fixture
def env(built_app, tmp_path):
    cfg = built_app.config()
    auth.set_password(cfg.state.auth, PW)
    w = tmp_path / "web"
    w.mkdir()
    (w / "index.html").write_text("<!doctype html><title>x</title>")
    return Env(cfg, w)


def origin_of(c):
    return str(c.base_url).rstrip("/")


def post(c, path, body=None, *, origin=None, **headers):
    return c.post(path, json=body if body is not None else {}, headers={"origin": origin or origin_of(c), **headers})


def login_pw(c, **kw):
    return post(c, "/api/login", {"password": PW}, **kw)


def add_passkey(c, a, *, origin=None, **kw):
    opts = post(c, "/api/passkeys/options", {"purpose": "register"}, origin=origin)
    assert opts.status_code == 200, opts.text
    return post(c, "/api/passkeys/register", {"credential": a.register(opts.json(), origin or origin_of(c), **kw)}, origin=origin)


def passkey_login(c, a, *, as_origin=None, origin=None, **kw):
    opts = post(c, "/api/passkeys/options", {"purpose": "login"}, origin=origin)
    assert opts.status_code == 200, opts.text
    cred = a.login(opts.json(), as_origin or origin or origin_of(c), **kw)
    return post(c, "/api/passkeys/login", {"credential": cred}, origin=origin)


def confirm_answer(c, a, **kw):
    opts = post(c, "/api/passkeys/options", {"purpose": "confirm"})
    assert opts.status_code == 200, opts.text
    return a.login(opts.json(), origin_of(c), **kw)


@pytest.fixture
def owner(env):
    """The owner's passkey, added at http://localhost:8765; then signed out."""
    c = env.client()
    assert login_pw(c).status_code == 200
    a = SoftAuthenticator(ES256)
    assert add_passkey(c, a).status_code == 200
    post(c, "/api/logout")
    return a


def session_cookie(c):
    return c.cookies.get("easyui-demo")


# ---------------------------------------------------------------- 1. origins and RP ID come from configuration


def test_relay_from_another_local_port_gets_no_session(env, owner):
    """The attack: a page on http://localhost:9999 asks the browser for a passkey with rpId=localhost (allowed:
    same host name), and its server relays the answer to easy-ui under its own Host and Origin."""
    attacker = env.client("http://localhost:9999", local_port=8765)
    r = post(attacker, "/api/passkeys/options", {"purpose": "login"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "unknown_host"
    # Relayed under the real Host and Origin, the answer still says where it was made: refused.
    real = env.client()
    r = passkey_login(real, owner, as_origin="http://localhost:9999")
    assert r.status_code == 400 and "another web address" in r.json()["error"]["message"]
    assert session_cookie(real) is None
    assert passkey_login(env.client(), owner).status_code == 200  # the real page still works


def test_relay_from_another_port_of_the_https_name_gets_no_session(env):
    site = {"origins": ("https://dash.example.net",)}
    c = env.client("https://dash.example.net", **site)
    assert login_pw(c).status_code == 200
    a = SoftAuthenticator(EDDSA)
    assert add_passkey(c, a).status_code == 200
    attacker = env.client("https://dash.example.net:8443", **site)
    assert attacker.get("/api/session").status_code == 400
    r = passkey_login(env.client("https://dash.example.net", **site), a, as_origin="https://dash.example.net:8443")
    assert r.status_code == 400
    assert passkey_login(env.client("https://dash.example.net", **site), a).status_code == 200


def test_loopback_names_are_refused_once_a_public_origin_is_configured(env):
    site = {"origins": ("https://dash.example.net",), "local_port": 8765}
    for base in (LOCAL, "http://127.0.0.1:8765", "http://[::1]:8765"):
        assert env.client(base, **site).get("/api/session").json()["error"]["code"] == "unknown_host"
    assert env.client("https://dash.example.net", **site).get("/api/session").status_code == 200


def test_without_configuration_only_the_listening_port_is_accepted(env):
    assert env.client(LOCAL, local_port=8765).get("/api/session").status_code == 200
    assert env.client("http://127.0.0.1:8765", local_port=8765).get("/api/session").status_code == 200
    assert env.client("http://localhost:8766", local_port=8765).get("/api/session").status_code == 400
    assert env.client(LOCAL).get("/api/session").status_code == 200
    nothing = TestClient(env.app(local_port=None), base_url=LOCAL)
    assert nothing.get("/api/session").status_code == 400  # a socket without --origin answers nothing


def test_rp_id_is_the_configured_one_and_each_passkey_keeps_its_own(env, owner):
    opts = post(env.client(), "/api/passkeys/options", {"purpose": "login"}).json()
    assert opts["publicKey"]["rpId"] == "localhost"
    saved = next(iter(passkeys.PasskeyStore(env.cfg.state.auth).credentials().values()))
    assert saved["rp_id"] == "localhost"
    # The same passkey store served for another configured address: that passkey is for another site.
    other = env.client("https://dash.example.net", origins=("https://dash.example.net",))
    r = passkey_login(other, owner, rp_id="localhost")
    assert r.status_code == 403 and r.json()["error"]["code"] == "other_address"


def test_more_than_one_origin_header_is_refused(env):
    c = env.client()
    r = c.post("/api/login", json={"password": PW}, headers=[("origin", "https://evil.example"), ("origin", LOCAL)])
    assert r.status_code == 400 and "more than one Origin" in r.json()["error"]["message"]
    r = c.post("/api/login", json={"password": PW}, headers=[("origin", LOCAL), ("origin", "https://evil.example")])
    assert r.status_code == 400


def test_origin_parsing_and_configuration(monkeypatch):
    p = security.parse_origin
    assert str(p("HTTPS://Box.Example.ts.net/")) == "https://box.example.ts.net"
    assert str(p("https://box.example.ts.net:443")) == "https://box.example.ts.net"
    assert str(p("https://box.example.ts.net:8443")) == "https://box.example.ts.net:8443"
    assert p("http://localhost:8765").rp_id == "localhost" and p("http://127.0.0.1:8765").rp_id is None
    assert p("http://box.example.net").rp_id is None and p("https://box.example.net").rp_id == "box.example.net"
    for bad in ("box.example.net", "ftp://box", "https://box/x", "https://u@box", "https://box:0", "https://box:70000",
                "https://", "https://bo x", "https://box?q", "https://box#f", "https://b%6fx", "", None, "javascript:x"):
        with pytest.raises(ValueError):
            p(bad)
    monkeypatch.setenv("EASYUI_ORIGINS", "https://a.example.net:8443, https://a.example.net:8443")
    monkeypatch.setenv("EASYUI_ALLOW_HOSTS", "b.example.net")
    assert server.configured_origins(["https://c.example.net"], ["d.example.net"]) == (
        "https://c.example.net", "https://a.example.net:8443", "https://d.example.net", "https://b.example.net")
    with pytest.raises(UsageError):
        server.configured_origins(["box.example.net"], [])
    with pytest.raises(UsageError):
        server.configured_origins([], ["box.example.net:8443"])


def test_host_matching_pins_the_port():
    o = security.Origins(["https://box.example.net", "http://box.example.net:8080"])
    assert str(o.match("box.example.net")) == "https://box.example.net"
    assert str(o.match("box.example.net:443")) == "https://box.example.net"
    assert str(o.match("box.example.net:8080")) == "http://box.example.net:8080"
    for h in ("box.example.net:8443", "box.example.net:80", "localhost", "other.example.net", "", None):
        assert o.match(h) is None
    assert o.passkey_origins("box.example.net") == {"https://box.example.net"}


def test_serve_on_a_socket_needs_an_origin(demo_app):
    import tempfile
    from pathlib import Path

    auth.set_password(demo_app.config().state.auth, PW)
    d = Path(tempfile.mkdtemp(prefix="eui-", dir="/tmp"))  # unix socket paths must be short
    try:
        code, data = run_cli_json("serve", "--socket", str(d / "x.sock"))
        assert code == 2 and data["error"]["code"] == "origin_needed"
        assert not (d / "x.sock").exists()
    finally:
        for p in d.iterdir():
            p.unlink()
        d.rmdir()


def test_preview_answers_only_on_its_own_port(built_app):
    from easyui import preview

    p = preview.prepare(built_app.config())
    try:
        asgi = preview._server_app(p, port=8750)
        assert TestClient(asgi, base_url="http://127.0.0.1:8750").get("/api/session").status_code == 200
        assert TestClient(asgi, base_url="http://127.0.0.1:8751").get("/api/session").status_code == 400
    finally:
        p.close()


# ---------------------------------------------------------------- 2. Ed25519 small order, RSA


_P = 2**255 - 19
# libsodium's blocklist (ge25519_has_small_order): the 7 encodings, each also with the sign bit set.
_SMALL = [bytes(32), (1).to_bytes(32, "little"),
          bytes.fromhex("26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc05"),
          bytes.fromhex("c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a"),
          (_P - 1).to_bytes(32, "little"), _P.to_bytes(32, "little"), (_P + 1).to_bytes(32, "little")]
SMALL_ORDER = _SMALL + [x[:31] + bytes([x[31] | 0x80]) for x in _SMALL]


@pytest.mark.parametrize("key", SMALL_ORDER, ids=lambda k: k.hex()[:8] + ".." + k.hex()[-2:])
def test_small_order_and_non_canonical_ed25519_keys_are_refused(key):
    cose = cbor({1: 1, 3: EDDSA, -1: 6, -2: key})
    # The attack: with such a key, the signature R=identity, S=0 verified for every message.
    with pytest.raises(WebAuthnError):
        webauthn.parse_cose_key(cose)
    with pytest.raises(WebAuthnError):
        webauthn.verify_signature(EDDSA, cose, (1).to_bytes(32, "little") + bytes(32), os.urandom(40))


def test_non_canonical_y_is_refused_and_real_keys_still_work():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    assert not webauthn.ed25519_key_is_safe((_P + 5).to_bytes(32, "little"))  # y >= p
    assert not webauthn.ed25519_key_is_safe(((1 << 255) - 1).to_bytes(32, "little"))
    for _ in range(40):
        raw = ed25519.Ed25519PrivateKey.generate().public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        assert webauthn.ed25519_key_is_safe(raw)


def test_a_planted_small_order_key_cannot_be_registered_over_http(env):
    c = env.client()
    assert login_pw(c).status_code == 200
    a = SoftAuthenticator(EDDSA)
    opts = post(c, "/api/passkeys/options", {"purpose": "register"}).json()
    cred = a.register(opts, LOCAL, cose={1: 1, 3: EDDSA, -1: 6, -2: (1).to_bytes(32, "little")})
    r = post(c, "/api/passkeys/register", {"credential": cred})
    assert r.status_code == 400 and "not a safe key" in r.json()["error"]["message"]
    assert passkeys.PasskeyStore(env.cfg.state.auth).count() == 0


def test_rsa_keys_need_an_odd_modulus_and_e_65537():
    a = SoftAuthenticator(RS256)
    good = a.cose_key()
    webauthn.parse_cose_key(cbor(good))
    n = int.from_bytes(good[-1], "big")
    for bad in ({**good, -2: (3).to_bytes(1, "big")}, {**good, -2: (65539).to_bytes(3, "big")},
                {**good, -1: (n + 1).to_bytes(len(good[-1]), "big")}):
        with pytest.raises(WebAuthnError):
            webauthn.parse_cose_key(cbor(bad))


# ---------------------------------------------------------------- 3. other sessions end on changes


@pytest.mark.parametrize("change", ["remove", "password_off", "codes"])
def test_changes_to_signing_in_end_every_other_session(env, owner, change):
    other = env.client()
    assert login_pw(other).status_code == 200  # e.g. a stolen session, or a browser left signed in
    me = env.client()
    assert passkey_login(me, owner).status_code == 200
    if change == "remove":
        extra = SoftAuthenticator(ES256)
        assert add_passkey(me, extra).status_code == 200
        pid = b64url_encode(extra.credential_id)
        r = post(me, "/api/passkeys/remove", {"id": pid})
    elif change == "password_off":
        r = post(me, "/api/signin/password", {"on": False})
    else:
        r = post(me, "/api/recovery-codes")
    assert r.status_code == 200, r.text
    assert r.json()["signed_out_others"] == 1
    assert other.get("/api/signin").status_code == 401
    assert me.get("/api/signin").status_code == 200


def test_commands_on_the_server_sign_every_browser_out(env, owner):
    c = env.client()
    assert login_pw(c).status_code == 200
    code, data = run_cli_json("sign-in", "password", "off")
    assert code == 0 and data["signed_out"] == 1
    assert c.get("/api/signin").status_code == 401
    c = env.client()
    assert passkey_login(c, owner).status_code == 200
    code, data = run_cli_json("passkeys", "remove", b64url_encode(owner.credential_id))
    assert code == 0 and data["signed_out"] == 1
    assert c.get("/api/signin").status_code == 401


# ---------------------------------------------------------------- 4. removed while being checked


def test_a_passkey_removed_while_it_is_checked_does_not_sign_in(env, owner, monkeypatch):
    real = webauthn.verify_assertion

    def remove_meanwhile(*a, **kw):
        out = real(*a, **kw)
        passkeys.PasskeyStore(env.cfg.state.auth).remove(b64url_encode(owner.credential_id))
        return out

    monkeypatch.setattr(webauthn, "verify_assertion", remove_meanwhile)
    c = env.client()
    r = passkey_login(c, owner)
    assert r.status_code == 400 and r.json()["error"]["code"] == "unknown_passkey"
    assert session_cookie(c) is None


def test_touch_refuses_a_removed_passkey(tmp_path):
    store = passkeys.PasskeyStore(tmp_path / "auth", Clock())
    a = SoftAuthenticator(EDDSA)
    opts = {"publicKey": {"challenge": b64url_encode(b"c" * 40), "rp": {"id": "localhost"}, "user": {"id": b64url_encode(b"u" * 16)}}}
    reg = webauthn.verify_registration(a.register(opts, LOCAL), challenge=b"c" * 40, origins={LOCAL}, rp_id="localhost")
    cid = store.add(reg, rp_id="localhost", name=None)["id"]
    store.remove(cid)
    with pytest.raises(WebAuthnError):
        store.touch(cid, 1, False)


# ---------------------------------------------------------------- 5. passkey confirmation, BE flag, duplicate keys


@pytest.mark.parametrize("how", ["password", "recovery"])
@pytest.mark.parametrize("change", ["remove", "password_off", "codes"])
def test_password_or_recovery_sessions_must_confirm_with_a_passkey(env, owner, how, change):
    setup = env.client()
    assert passkey_login(setup, owner).status_code == 200
    codes = post(setup, "/api/recovery-codes").json()["codes"]
    c = env.client()
    r = login_pw(c) if how == "password" else post(c, "/api/recovery", {"code": codes[0]})
    assert r.status_code == 200
    assert c.get("/api/signin").json()["confirm_needed"] is True
    path, body = {"remove": ("/api/passkeys/remove", {"id": b64url_encode(owner.credential_id)}),
                  "password_off": ("/api/signin/password", {"on": False}),
                  "codes": ("/api/recovery-codes", {})}[change]
    # The attack: a recent password (or recovery code) session changes the sign-in settings on its own.
    r = post(c, path, body)
    assert r.status_code == 403 and r.json()["error"]["code"] == "confirm_with_passkey"
    # An answer to a login challenge does not count; a fresh confirmation does.
    login_opts = post(c, "/api/passkeys/options", {"purpose": "login"}).json()
    r = post(c, path, {**body, "confirm": owner.login(login_opts, LOCAL)})
    assert r.status_code == 400
    r = post(c, path, {**body, "confirm": confirm_answer(c, owner)})
    assert r.status_code == 200, r.text


def test_a_passkey_session_needs_no_confirmation_and_confirmations_are_bound_to_the_session(env, owner):
    a_ = env.client()
    assert passkey_login(a_, owner).status_code == 200
    assert a_.get("/api/signin").json()["confirm_needed"] is False
    b = env.client()
    assert login_pw(b).status_code == 200
    stolen = confirm_answer(a_, owner)  # made for session a_
    r = post(b, "/api/signin/password", {"on": False, "confirm": stolen})
    assert r.status_code == 400 and r.json()["error"]["code"] == "passkey_stale"
    assert post(a_, "/api/signin/password", {"on": False}).status_code == 200


def test_backup_eligible_flag_must_match_registration(env, owner):
    c = env.client()
    r = passkey_login(c, owner, flags=owner.flags(be=False, bs=False))
    assert r.status_code == 400 and "synced" in r.json()["error"]["message"]
    assert passkey_login(env.client(), owner).status_code == 200


def test_client_data_naming_a_field_twice_is_refused(env, owner):
    c = env.client()
    opts = post(c, "/api/passkeys/options", {"purpose": "login"}).json()
    cred = owner.login(opts, LOCAL)
    ad = webauthn.b64url_decode(cred["response"]["authenticatorData"], what="x")
    ch = opts["publicKey"]["challenge"]
    cd = (f'{{"type":"webauthn.get","challenge":"{ch}","origin":"http://localhost:9999","origin":"{LOCAL}",'
          '"crossOrigin":false}').encode()
    cred["response"]["clientDataJSON"] = b64url_encode(cd)
    cred["response"]["signature"] = b64url_encode(owner.sign(ad + hashlib.sha256(cd).digest()))
    r = post(c, "/api/passkeys/login", {"credential": cred})
    assert r.status_code == 400 and "twice" in r.json()["error"]["message"]


# ---------------------------------------------------------------- 6. the sign-in folder, and links under ui/


def test_sign_in_folder_is_never_inside_the_ui_folder(tmp_path, monkeypatch):
    monkeypatch.delenv("EASYUI_STATE_HOME", raising=False)
    monkeypatch.setenv("MES_ROOT", str(tmp_path / "srv"))
    assert paths.auth_dir("demo") == tmp_path / "srv" / "demo" / "auth"
    assert paths.state_paths("demo").auth == tmp_path / "srv" / "demo" / "auth"
    monkeypatch.delenv("MES_ROOT")
    monkeypatch.setenv("MES_STATE_HOME", str(tmp_path / "state"))
    assert paths.state_paths("demo").root == tmp_path / "state" / "demo" / "ui"
    assert paths.state_paths("demo").auth == tmp_path / "state" / "demo" / "auth"


def test_auth_dir_option(built_app, tmp_path):
    elsewhere = tmp_path / "signin"
    elsewhere.mkdir(mode=0o700)
    auth.set_password(elsewhere, PW)
    code, data = run_cli_json("--auth-dir", str(elsewhere), "sign-in")
    assert code == 0 and data["password_set"] is True
    code, data = run_cli_json("sign-in")  # the usual folder has no password
    assert code == 0 and data["password_set"] is False


def test_unsafe_sign_in_folders_are_refused(built_app, tmp_path, monkeypatch):
    target = tmp_path / "planted"
    target.mkdir(mode=0o700)
    auth.set_password(target, "a password the attacker knows")
    link = tmp_path / "linked-auth"
    link.symlink_to(target)
    code, data = run_cli_json("--auth-dir", str(link), "serve", "--port", "0")
    assert code == 5 and data["error"]["code"] == "auth_dir_unsafe" and "link" in data["error"]["message"]
    open_dir = tmp_path / "open-auth"
    open_dir.mkdir()
    os.chmod(open_dir, 0o755)
    code, data = run_cli_json("--auth-dir", str(open_dir), "sign-in", "password", "on")
    assert code == 5 and "0755" in data["error"]["message"]
    shared = tmp_path / "shared"
    shared.mkdir()
    os.chmod(shared, 0o777)
    (shared / "auth").mkdir(mode=0o700)
    with pytest.raises(Refused) as e:
        paths.check_auth_dir(shared / "auth")
    assert "another account can change" in e.value.message
    real_uid = os.geteuid()
    monkeypatch.setattr(paths.os, "geteuid", lambda: real_uid + 1)
    with pytest.raises(Refused) as e:
        paths.check_auth_dir(target)
    assert "belongs to another account" in e.value.message
    monkeypatch.undo()
    assert paths.check_auth_dir(target) == target
    with pytest.raises(Refused):
        paths.check_auth_dir(tmp_path / "missing")


def test_the_checklist_reports_an_unsafe_sign_in_folder(built_app, tmp_path):
    cfg = built_app.config()
    os.chmod(cfg.state.auth, 0o750)
    item = next(i for i in server.run_checks(cfg)["items"] if i["id"] == "auth_dir_safe")
    assert item["status"] == "fail" and "chmod 700" in item["fix"]


def _swap_for_link(path, target):
    path.rename(path.with_name(path.name + ".real"))
    path.symlink_to(target)


def test_bundle_reached_through_a_link_is_not_used(built_app, tmp_path):
    cfg = built_app.config()
    st = cfg.state
    cur = bundle.current_bundle(cfg)
    assert cur is not None
    with sandbox.Sandbox(cur) as sb:
        assert sb.tables
    # The attack: the project's account replaces the bundle with a link to a file only the web account can read.
    secret = tmp_path / "only-the-web-account"
    secret.write_bytes(cur.read_bytes())
    _swap_for_link(cur, secret)
    assert bundle.current_bundle(cfg) is None
    with pytest.raises(NoData):
        sandbox.Sandbox(cur)
    # The data folder itself replaced by a link.
    cur.unlink()
    cur.with_name(cur.name + ".real").rename(cur)
    assert bundle.current_bundle(cfg) is not None
    elsewhere = tmp_path / "elsewhere"
    st.data.rename(elsewhere)
    st.data.symlink_to(elsewhere)
    assert bundle.current_bundle(cfg) is None
    with pytest.raises(NoData):
        sandbox.Sandbox(st.data / cur.name)


def test_published_files_reached_through_a_link_are_not_used(built_app, tmp_path):
    cfg = built_app.config()
    publish.publish(cfg)
    st = cfg.state
    v = publish.current_version(cfg)
    vdir = st.specs / "versions" / str(v)
    assert v is not None
    manifest = vdir / "manifest.json"
    copy = tmp_path / "manifest.json"
    copy.write_bytes(manifest.read_bytes())
    os.chmod(vdir, 0o755)
    _swap_for_link(manifest, copy)
    with pytest.raises(Exception) as e:
        publish._read_manifest(vdir)
    assert "manifest" in str(e.value.message if hasattr(e.value, "message") else e.value)
    manifest.unlink()
    manifest.with_name("manifest.json.real").rename(manifest)
    other = tmp_path / "versions"
    (st.specs / "versions").rename(other)
    (st.specs / "versions").symlink_to(other)
    assert publish.current_version(cfg) is None


def test_project_json_reached_through_a_link_is_not_read(built_app, tmp_path):
    cfg = built_app.config()
    ui = cfg.state.root
    assert appconfig.load_snapshot(ui).name == "demo"
    fake = tmp_path / "project.json"
    fake.write_text(json.dumps({"v": 1, "name": "other"}))
    pj = ui / "project.json"
    pj.unlink()
    pj.symlink_to(fake)
    with pytest.raises(NotFound):
        appconfig.load_snapshot(ui)


def test_open_nofollow_rules(tmp_path):
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "b" / "f").write_text("x")
    (tmp_path / "link").symlink_to(tmp_path / "a")
    assert paths.read_nofollow(tmp_path / "a" / "b" / "f", 2, 10) == b"x"
    assert paths.read_nofollow(tmp_path / "link" / "b" / "f", 2, 10) == b"x"  # above the checked part: trusted
    with pytest.raises(OSError):
        paths.read_nofollow(tmp_path / "link" / "b" / "f", 3, 10)
    assert stat.S_ISLNK(paths.lstat_nofollow(tmp_path / "link", 1).st_mode)


# ---------------------------------------------------------------- 7. adding a passkey needs the same confirmation
# The attack: someone with only the password (or one recovery code) signed in, added their own passkey with no
# confirmation, signed in with it, and then removed the owner's passkeys, turned the password off and made new
# recovery codes, because a passkey session needs no confirmation.


def _add(c, a, confirm=None, name="Mine"):
    opts = post(c, "/api/passkeys/options", {"purpose": "register"})
    assert opts.status_code == 200, opts.text
    body = {"credential": a.register(opts.json(), origin_of(c)), "name": name}
    if confirm is not None:
        body["confirm"] = confirm
    return post(c, "/api/passkeys/register", body)


def _signed_in(env, owner, how):
    """A fresh browser signed in with the password or a recovery code, while the owner's passkey exists."""
    if how == "recovery":
        setup = env.client()
        assert passkey_login(setup, owner).status_code == 200
        codes = post(setup, "/api/recovery-codes").json()["codes"]
        c = env.client()
        assert post(c, "/api/recovery", {"code": codes[0]}).status_code == 200
        return c
    c = env.client()
    assert login_pw(c).status_code == 200
    return c


@pytest.mark.parametrize("how", ["password", "recovery"])
def test_adding_a_passkey_to_password_or_recovery_session_needs_a_passkey_confirmation(env, owner, how):
    c = _signed_in(env, owner, how)
    store = passkeys.PasskeyStore(env.cfg.state.auth, env.clock)
    intruder = SoftAuthenticator(ES256)
    r = _add(c, intruder)
    assert r.status_code == 403 and r.json()["error"]["code"] == "confirm_with_passkey"
    assert "easy-ui passkeys remove" in r.json()["error"]["hint"]
    assert "add a new passkey first" not in json.dumps(r.json())
    assert store.count() == 1
    # The intruder's own passkey cannot confirm (it is not saved), nor can an answer to a login challenge.
    opts = post(c, "/api/passkeys/options", {"purpose": "confirm"}).json()
    r = _add(c, intruder, confirm=intruder.login(opts, LOCAL))
    assert r.status_code == 403 and r.json()["error"]["code"] == "unknown_passkey"
    login_opts = post(c, "/api/passkeys/options", {"purpose": "login"}).json()
    r = _add(c, intruder, confirm=owner.login(login_opts, LOCAL))
    assert r.status_code == 400 and store.count() == 1
    # A fresh confirmation by the owner's existing passkey: added.
    r = _add(c, SoftAuthenticator(ES256), confirm=confirm_answer(c, owner))
    assert r.status_code == 200, r.text
    assert r.json()["added"]["name"] == "Mine" and store.count() == 2


def test_the_first_passkey_needs_only_a_recent_sign_in(env):
    c = env.client()
    assert login_pw(c).status_code == 200
    assert c.get("/api/signin").json()["confirm_needed"] is False
    r = _add(c, SoftAuthenticator(ES256))
    assert r.status_code == 200, r.text
    env.clock.t += passkeys.RECENT_SIGN_IN + 1
    r = post(c, "/api/passkeys/options", {"purpose": "register"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "sign_in_again"


def test_a_passkey_session_adds_a_passkey_without_confirmation(env, owner):
    c = env.client()
    assert passkey_login(c, owner).status_code == 200
    assert _add(c, SoftAuthenticator(ES256)).status_code == 200


def test_a_passkey_added_in_this_session_cannot_confirm_this_session(env):
    """The first passkey, added after a password sign-in, cannot then confirm changes for that same session."""
    c = env.client()
    assert login_pw(c).status_code == 200
    first = SoftAuthenticator(ES256)
    assert _add(c, first).status_code == 200
    s = c.get("/api/signin").json()
    assert s["confirm_needed"] is True and s["confirm_possible"] is False
    r = post(c, "/api/passkeys/options", {"purpose": "confirm"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "sign_in_with_new_passkey"
    assert _add(c, SoftAuthenticator(ES256)).json()["error"]["code"] == "confirm_with_passkey"
    # With an older passkey saved too, a confirm challenge is issued (listing only the older one); an answer
    # by the passkey added in this session is still refused.
    older = SoftAuthenticator(ES256)
    other = env.client()
    assert passkey_login(other, first).status_code == 200 and _add(other, older).status_code == 200
    opts = post(c, "/api/passkeys/options", {"purpose": "confirm"}).json()
    assert [x["id"] for x in opts["publicKey"]["allowCredentials"]] == [b64url_encode(older.credential_id)]
    for path, body in (("/api/signin/password", {"on": False}), ("/api/recovery-codes", {}),
                       ("/api/passkeys/remove", {"id": b64url_encode(older.credential_id)}), ("add", None)):
        answer = confirm_answer(c, first)
        r = _add(c, SoftAuthenticator(ES256), confirm=answer) if path == "add" else post(c, path, {**body, "confirm": answer})
        assert r.status_code == 403 and r.json()["error"]["code"] == "sign_in_with_new_passkey", path
    assert passkeys.PasskeyStore(env.cfg.state.auth, env.clock).count() == 2
    assert post(c, "/api/recovery-codes", {"confirm": confirm_answer(c, older)}).status_code == 200
    # Signed in with it, it works as any passkey does; in another password session it can confirm.
    c2 = env.client()
    assert passkey_login(c2, first).status_code == 200
    assert post(c2, "/api/signin/password", {"on": False}).status_code == 200
    assert post(c2, "/api/signin/password", {"on": True}).status_code == 200
    c3 = env.client()
    assert login_pw(c3).status_code == 200
    assert c3.get("/api/signin").json()["confirm_possible"] is True
    assert _add(c3, SoftAuthenticator(ES256), confirm=confirm_answer(c3, first)).status_code == 200


def test_the_first_passkey_check_is_repeated_when_it_is_saved(tmp_path):
    """Two first passkeys added at once: the one saved second must have been confirmed."""
    store = passkeys.PasskeyStore(tmp_path / "auth", Clock())
    a, b = SoftAuthenticator(ES256), SoftAuthenticator(ES256)

    def reg(x):
        opts = {"publicKey": {"challenge": b64url_encode(b"c" * 40), "rp": {"id": "localhost"}, "user": {"id": b64url_encode(b"u" * 16)}}}
        return webauthn.verify_registration(x.register(opts, LOCAL), challenge=b"c" * 40, origins=frozenset({LOCAL}), rp_id="localhost")

    store.add(reg(a), rp_id="localhost", name=None, only_if_first=True)
    with pytest.raises(Refused) as e:
        store.add(reg(b), rp_id="localhost", name=None, only_if_first=True)
    assert e.value.code == "confirm_with_passkey" and store.count() == 1
    assert "added_in" not in json.dumps(store.listing())


def test_the_settings_page_confirms_before_adding_a_passkey():
    """The page asks for the confirmation when the server needs one, and sends it with the new passkey."""
    from pathlib import Path

    assets = Path(server.__file__).parent / "web" / "assets"
    js = (assets / "settings.js").read_text(encoding="utf-8")
    add = js[js.index("  addPasskey(name) {"):js.index("  removePasskey(p) {")]
    assert add.index("await this.confirmation()") < add.index('passkeyOptions("register")')
    assert "api.passkeyRegister(credential, name, confirm)" in add
    assert "confirm_possible === false" in js[js.index("async confirmation()"):js.index("  addPasskey(name) {")]
    api_js = (assets / "api.js").read_text(encoding="utf-8")
    assert '"passkeys/register", confirm ? { credential, name, confirm } : { credential, name }' in api_js

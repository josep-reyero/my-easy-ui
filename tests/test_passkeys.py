"""Passkeys (WebAuthn): the verification core, challenges, storage, recovery codes, the password switch,
the HTTP ceremonies (including replay, origin confusion and the new-device lockout) and the commands."""

from __future__ import annotations

import base64
import hashlib
import json
import stat
import struct
import warnings

import pytest
from argon2 import PasswordHasher, Type
from conftest import run_cli, run_cli_json, site_settings
from softauthn import SoftAuthenticator, cbor

from easyui import auth, passkeys, server, webauthn
from easyui.errors import NotFound, Refused, UsageError
from easyui.webauthn import EDDSA, ES256, RS256, WebAuthnError, b64url_encode

warnings.filterwarnings("ignore", message=".*httpx.*", category=DeprecationWarning)
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from starlette.testclient import TestClient

PW = "correct horse battery staple"
ORIGIN = "http://localhost:8765"
RP = "localhost"


@pytest.fixture(autouse=True)
def fast_hash(monkeypatch):
    monkeypatch.setattr(auth, "_hasher", PasswordHasher(time_cost=1, memory_cost=8, parallelism=1, hash_len=16, salt_len=16, type=Type.ID))


class Clock:
    def __init__(self, t: float = 1_800_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


def reg_options(challenge: bytes = b"c" * 40, rp_id: str = RP) -> dict:
    return {"publicKey": {"challenge": b64url_encode(challenge), "rp": {"id": rp_id}, "user": {"id": b64url_encode(b"u" * 16)}}}


def login_options(challenge: bytes = b"c" * 40, rp_id: str = RP) -> dict:
    return {"publicKey": {"challenge": b64url_encode(challenge), "rpId": rp_id}}


def verify_reg(cred, challenge=b"c" * 40, origins=frozenset({ORIGIN}), rp_id=RP):
    return webauthn.verify_registration(cred, challenge=challenge, origins=origins, rp_id=rp_id)


# ---------------------------------------------------------------- encodings and CBOR


def test_b64url_is_strict():
    assert webauthn.b64url_decode("AQID", what="x") == b"\x01\x02\x03"
    for bad in ("AQID=", "AQ+D", "A", " AQID", "AQIE\n", 5, None):
        with pytest.raises(WebAuthnError):
            webauthn.b64url_decode(bad, what="x")
    with pytest.raises(WebAuthnError):  # a non-canonical spelling of the same bytes
        webauthn.b64url_decode("AQJ", what="x")


def test_cbor_round_trip_and_strictness():
    value = {1: 2, 3: -7, -1: 1, -2: b"x" * 32, "fmt": "none", "list": [True, False, None, 1000, 70000, 2**33]}
    assert webauthn.cbor_decode(cbor(value)) == value
    with pytest.raises(WebAuthnError):
        webauthn.cbor_decode(cbor(1) + b"\x00")  # trailing bytes
    with pytest.raises(WebAuthnError):
        webauthn.cbor_decode(b"\x18\x05")  # 5 written in two bytes (not the shortest form)
    with pytest.raises(WebAuthnError):
        webauthn.cbor_decode(b"\xa2\x01\x01\x01\x02")  # a key twice
    with pytest.raises(WebAuthnError):
        webauthn.cbor_decode(b"\xc0\x01")  # a tag
    with pytest.raises(WebAuthnError):
        webauthn.cbor_decode(b"\x5f\x41\x00\xff")  # indefinite length
    with pytest.raises(WebAuthnError):
        webauthn.cbor_decode(b"\x81" * 20 + b"\x01")  # too deep
    with pytest.raises(WebAuthnError):
        webauthn.cbor_decode(b"\x45ab")  # cut short
    with pytest.raises(WebAuthnError):
        webauthn.cbor_decode(b"\x62\xff\xfe")  # not UTF-8
    with pytest.raises(WebAuthnError):
        webauthn.cbor_decode(b"\xa1\x41\x00\x01")  # a byte-string key


# ---------------------------------------------------------------- registration


@pytest.mark.parametrize("alg", [ES256, EDDSA, RS256])
def test_registration_and_assertion_for_every_algorithm(alg):
    a = SoftAuthenticator(alg)
    reg = verify_reg(a.register(reg_options(), ORIGIN))
    assert (reg.credential_id, reg.alg, reg.transports) == (a.credential_id, alg, ("internal",))
    assert reg.backup_eligible and reg.backed_up
    res = webauthn.verify_assertion(
        a.login(login_options(), ORIGIN), challenge=b"c" * 40, origins={ORIGIN}, rp_id=RP,
        public_key=reg.public_key, alg=reg.alg, stored_sign_count=reg.sign_count, user_id=b"u" * 16,
    )
    assert res.sign_count == 1 and res.credential_id == a.credential_id


@pytest.mark.parametrize(
    "change, words",
    [
        ({"origin": "https://evil.example"}, "another web address"),
        ({"origin": "http://localhost:9999"}, "another web address"),
        ({"challenge": b64url_encode(b"d" * 40)}, "old or a different request"),
        ({"kind": "webauthn.get"}, "different kind of request"),
        ({"rp_id": "evil.example"}, "another web address"),
        ({"client_extra": {"crossOrigin": True}}, "another site"),
        ({"fmt": "packed"}, "attestation"),
        ({"att_stmt": {"alg": -7}}, "attestation"),
        ({"auth_data_tail": b"\x00"}, "extra bytes"),
        ({"credential_id": b"other-id"}, "does not match"),
        ({"flags": 0x01 | 0x40}, "check that it is you"),
        ({"flags": 0x04 | 0x40}, "someone was there"),
        ({"flags": 0x05}, "extra bytes"),  # attested data without the flag that announces it
        ({"flags": 0x05 | 0x10 | 0x40}, "contradict"),
        ({"cose": {1: 2, 3: -7, -1: 1, -2: b"\x01" * 32, -3: b"\x02" * 32}}, "not a point on the curve"),
        ({"cose": {1: 2, 3: -7, -1: 2, -2: b"\x01" * 32, -3: b"\x02" * 32}}, "not well formed"),
        ({"cose": {1: 1, 3: -8, -1: 6, -2: b"\x01" * 31}}, "not well formed"),
        ({"cose": {1: 2, 3: -36, -1: 1}}, "algorithm"),
    ],
)
def test_registration_refusals(change, words):
    a = SoftAuthenticator(ES256)
    opts = reg_options()
    if "challenge" in change:
        opts["publicKey"]["challenge"] = change.pop("challenge")
    origin = change.pop("origin", ORIGIN)
    kind = change.pop("kind", None)
    cred = a.register(opts, origin, **change)
    if kind:
        cd = json.loads(base64.urlsafe_b64decode(cred["response"]["clientDataJSON"] + "=="))
        cd["type"] = kind
        cred["response"]["clientDataJSON"] = b64url_encode(json.dumps(cd).encode())
    with pytest.raises(WebAuthnError) as e:
        verify_reg(cred)
    assert words in e.value.message


def test_registration_refuses_inconsistent_ids_and_shapes():
    a = SoftAuthenticator()
    cred = a.register(reg_options(), ORIGIN)
    for broken in (
        {**cred, "type": "password"},
        {**cred, "id": b64url_encode(b"x")},
        {**cred, "rawId": "not base64!"},
        {**cred, "response": "nope"},
        {**cred, "response": {"clientDataJSON": cred["response"]["clientDataJSON"]}},
        "a string",
    ):
        with pytest.raises(WebAuthnError):
            verify_reg(broken)
    att = webauthn.cbor_decode(webauthn.b64url_decode(cred["response"]["attestationObject"], what="a", limit=9999))
    cred2 = json.loads(json.dumps(cred))
    cred2["response"]["attestationObject"] = b64url_encode(cbor({**att, "extra": 1}))
    with pytest.raises(WebAuthnError):
        verify_reg(cred2)
    with pytest.raises(WebAuthnError):  # an algorithm the server did not ask for
        webauthn.verify_registration(cred, challenge=b"c" * 40, origins={ORIGIN}, rp_id=RP, algorithms=(EDDSA,))


def test_rsa_keys_below_2048_bits_are_refused():
    weak = {1: 3, 3: RS256, -1: (2**1023 + 1).to_bytes(128, "big"), -2: (65537).to_bytes(3, "big")}
    with pytest.raises(WebAuthnError, match="too weak"):
        webauthn.parse_cose_key(cbor(weak))


def test_extension_data_must_be_one_map():
    a = SoftAuthenticator()
    ok = verify_reg(a.register(reg_options(), ORIGIN, flags=a.flags(at=True) | 0x80, auth_data_tail=cbor({"credProtect": 2})))
    assert ok.credential_id == a.credential_id
    with pytest.raises(WebAuthnError):
        verify_reg(a.register(reg_options(), ORIGIN, flags=a.flags(at=True) | 0x80))
    with pytest.raises(WebAuthnError):
        verify_reg(a.register(reg_options(), ORIGIN, flags=a.flags(at=True) | 0x80, auth_data_tail=cbor([1])))


# ---------------------------------------------------------------- assertions


@pytest.fixture
def registered():
    a = SoftAuthenticator(ES256)
    return a, verify_reg(a.register(reg_options(), ORIGIN))


def assertion(a, reg, *, stored=None, challenge=b"c" * 40, origins=frozenset({ORIGIN}), rp_id=RP, user_id=b"u" * 16, **kw):
    cred = a.login(login_options(), kw.pop("origin", ORIGIN), **kw)
    return webauthn.verify_assertion(
        cred, challenge=challenge, origins=origins, rp_id=rp_id, public_key=reg.public_key, alg=reg.alg,
        stored_sign_count=reg.sign_count if stored is None else stored, user_id=user_id,
    )


@pytest.mark.parametrize(
    "kw, words",
    [
        ({"bad_signature": True}, "signature does not match"),
        ({"origin": "https://evil.example"}, "another web address"),
        ({"rp_id": "evil.example"}, "another web address"),
        ({"kind": "webauthn.create"}, "different kind"),
        ({"flags": 0x01}, "check that it is you"),
        ({"flags": 0x45}, "cut short"),
        ({"user_handle": b"someone-else-123"}, "belongs to someone else"),
        ({"client_extra": {"crossOrigin": True}}, "another site"),
    ],
)
def test_assertion_refusals(registered, kw, words):
    a, reg = registered
    with pytest.raises(WebAuthnError) as e:
        assertion(a, reg, **kw)
    assert words in e.value.message


def test_assertion_with_another_challenge_is_refused(registered):
    a, reg = registered
    with pytest.raises(WebAuthnError, match="old or a different"):
        assertion(a, reg, challenge=b"d" * 40)


def test_signature_counter(registered):
    a, reg = registered
    assert assertion(a, reg, stored=0).sign_count == 1
    with pytest.raises(WebAuthnError, match="went backwards"):
        assertion(a, reg, stored=5, count=5)
    with pytest.raises(WebAuthnError, match="went backwards"):
        assertion(a, reg, stored=5, count=0)
    # A passkey that keeps no counter reports 0 every time, which is fine.
    assert assertion(a, reg, stored=0, count=0).sign_count == 0


def test_user_handle_may_be_left_out(registered):
    a, reg = registered
    assert assertion(a, reg, user_handle=None).user_handle is None


def test_signature_of_another_key_is_refused(registered):
    a, reg = registered
    other = SoftAuthenticator(ES256)
    other.credential_id = a.credential_id
    other.user_handle = a.user_handle
    cred = other.login(login_options(), ORIGIN)
    with pytest.raises(WebAuthnError, match="signature does not match"):
        webauthn.verify_assertion(cred, challenge=b"c" * 40, origins={ORIGIN}, rp_id=RP, public_key=reg.public_key,
                                  alg=reg.alg, stored_sign_count=0, user_id=b"u" * 16)


def test_algorithm_must_match_the_saved_key(registered):
    _a, reg = registered
    with pytest.raises(WebAuthnError, match="algorithm"):
        webauthn.verify_signature(EDDSA, reg.public_key, b"sig", b"data")


# A registration and a sign-in recorded from Chromium's own virtual authenticator (EdDSA, "none"
# attestation), through the real dashboards page on http://localhost. Fixed test vectors: they
# prove the parser and verifier accept what a real browser produces, byte for byte.
def test_recorded_chromium_vector():
    from pathlib import Path

    f = Path(__file__).parent / "vectors" / "chromium_passkey.json"
    v = json.loads(f.read_text())
    reg = webauthn.verify_registration(
        v["registration"], challenge=webauthn.b64url_decode(v["register_challenge"], what="c"),
        origins={v["origin"]}, rp_id=v["rp_id"],
    )
    assert reg.alg == EDDSA and b64url_encode(reg.credential_id) == v["registration"]["id"]
    assert reg.sign_count == 1 and not reg.backup_eligible
    res = webauthn.verify_assertion(
        v["assertion"], challenge=webauthn.b64url_decode(v["login_challenge"], what="c"), origins={v["origin"]},
        rp_id=v["rp_id"], public_key=reg.public_key, alg=reg.alg, stored_sign_count=reg.sign_count,
        user_id=webauthn.b64url_decode(v["user_id"], what="u"),
    )
    assert res.credential_id == reg.credential_id
    with pytest.raises(WebAuthnError):  # the same bytes on another address
        webauthn.verify_assertion(
            v["assertion"], challenge=webauthn.b64url_decode(v["login_challenge"], what="c"),
            origins={"https://evil.example"}, rp_id=v["rp_id"], public_key=reg.public_key, alg=reg.alg,
            stored_sign_count=reg.sign_count, user_id=webauthn.b64url_decode(v["user_id"], what="u"),
        )


# ---------------------------------------------------------------- challenges


def test_challenges_expire_and_are_bound():
    clock = Clock()
    ch = passkeys.Challenges(clock)
    c = ch.issue("login", "browser-a")
    ch.check(c, "login", "browser-a")
    for purpose, binding in (("register", "browser-a"), ("login", "browser-b")):
        with pytest.raises(WebAuthnError, match="too old or was not made"):
            ch.check(c, purpose, binding)
    tampered = c[:16] + struct.pack(">Q", int(clock()) + 10**6) + c[24:]
    with pytest.raises(WebAuthnError):
        ch.check(tampered, "login", "browser-a")
    clock.advance(passkeys.CHALLENGE_SECONDS + 1)
    with pytest.raises(WebAuthnError):
        ch.check(c, "login", "browser-a")
    with pytest.raises(WebAuthnError):  # a new server process does not know the old key
        passkeys.Challenges(clock).check(ch.issue("login", "x"), "login", "x")
    with pytest.raises(WebAuthnError):
        ch.check(b"short", "login", "x")


def test_an_accepted_challenge_cannot_be_used_twice():
    clock = Clock()
    ch = passkeys.Challenges(clock)
    c = ch.issue("login", "b")
    ch.accept(c)
    with pytest.raises(WebAuthnError, match="already used"):
        ch.accept(c)
    clock.advance(passkeys.CHALLENGE_SECONDS + 5)
    ch.accept(ch.issue("login", "b"))  # old entries are forgotten after they expire
    assert c not in ch._used


# ---------------------------------------------------------------- storage


def test_store_add_list_remove_and_password_switch(tmp_path):
    store = passkeys.PasskeyStore(tmp_path / "auth", Clock())
    assert store.password_sign_in() is True and store.listing() == []
    with pytest.raises(Refused, match="no passkey"):
        store.set_password_sign_in(False, password_set=True)
    a, b = SoftAuthenticator(), SoftAuthenticator(EDDSA)
    pa = store.add(verify_reg(a.register(reg_options(), ORIGIN)), rp_id=RP, name="  Phone\x1b[2J ")
    store.clock.advance(60)
    pb = store.add(verify_reg(b.register(reg_options(), ORIGIN)), rp_id=RP, name=None)
    assert pa["name"] == "Phone [2J" and pb["name"].startswith("Passkey added ")
    with pytest.raises(Refused, match="already saved"):
        store.add(verify_reg(a.register(reg_options(), ORIGIN)), rp_id=RP, name="again")
    assert [p["name"] for p in store.listing()] == [pa["name"], pb["name"]]
    assert "public_key" not in json.dumps(store.listing())
    store.set_password_sign_in(False, password_set=True)
    assert store.password_sign_in() is False
    assert store.remove("PHONE [2j")["password_sign_in_turned_on"] is False  # names match without case
    with pytest.raises(NotFound):
        store.remove("phone [2j")
    # Removing the last passkey turns the password back on, so nobody is left without a way in.
    assert store.remove(pb["id"])["password_sign_in_turned_on"] is True
    assert store.password_sign_in() is True
    f = tmp_path / "auth" / "passkeys.json"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    store.add(verify_reg(a.register(reg_options(), ORIGIN)), rp_id=RP, name="x")
    with pytest.raises(Refused, match="no password"):
        store.set_password_sign_in(True, password_set=False)


def test_store_has_a_limit_and_a_stable_user_id(tmp_path):
    store = passkeys.PasskeyStore(tmp_path / "auth", Clock())
    uid = store.user_id()
    assert len(uid) == 16 and store.user_id() == uid
    for _ in range(passkeys.MAX_PASSKEYS):
        a = SoftAuthenticator(EDDSA)
        store.add(verify_reg(a.register(reg_options(), ORIGIN)), rp_id=RP, name=None)
    a = SoftAuthenticator(EDDSA)
    with pytest.raises(Refused, match="the most"):
        store.add(verify_reg(a.register(reg_options(), ORIGIN)), rp_id=RP, name=None)
    with pytest.raises(UsageError):
        passkeys.clean_name("x" * 61)
    assert len({p["name"] for p in store.listing()}) == passkeys.MAX_PASSKEYS  # default names never repeat


# ---------------------------------------------------------------- recovery codes


def test_recovery_codes_are_shown_once_stored_hashed_and_work_once(tmp_path):
    rc = passkeys.RecoveryCodes(tmp_path / "auth", Clock())
    assert rc.left() == 0 and rc.use("ABCD-EFGH-JKMN-PQRS") is False
    codes = rc.make()
    assert len(codes) == 10 and len(set(codes)) == 10
    assert all(len(c) == 19 and c.count("-") == 3 for c in codes)
    stored = (tmp_path / "auth" / "recovery.json").read_text()
    for c in codes:
        assert c not in stored and c.replace("-", "") not in stored
    assert stat.S_IMODE((tmp_path / "auth" / "recovery.json").stat().st_mode) == 0o600
    sloppy = codes[0].lower().replace("-", " ").replace("0", "o").replace("1", "l")
    assert rc.use(sloppy) is True
    assert rc.use(codes[0]) is False and rc.left() == 9
    assert rc.use(codes[1][:-1] + ("0" if codes[1][-1] != "0" else "1")) is False
    assert rc.use("x" * 500) is False and rc.use(None) is False
    old = codes[2]
    rc.make()
    assert rc.use(old) is False and rc.left() == 10  # new codes replace the old ones


def test_code_normalisation():
    assert passkeys.normalize_code("abcd efgh-jkmn_pqrs") == "ABCDEFGHJKMNPQRS"
    assert passkeys.normalize_code("ABCD-EFGH-JKMN-PQR") is None
    assert passkeys.normalize_code("ABCD-EFGH-JKMN-PQRU") is None  # U is not in the alphabet


# ---------------------------------------------------------------- HTTP ceremonies


class Env:
    def __init__(self, cfg, clock, web_dir):
        self.cfg, self.clock, self.web_dir = cfg, clock, web_dir

    def make(self, base=ORIGIN, client=("127.0.0.1", 50000), **settings):
        settings.setdefault("web_dir", self.web_dir)
        site_settings(base, settings)
        asgi = server.create_app(self.cfg, server.ServerSettings(**settings), source=_Source(self.cfg), clock=self.clock)
        c = TestClient(asgi, base_url=base, client=client)
        c.asgi = asgi
        return c


class _Source:
    def __init__(self, cfg):
        self.cfg = cfg

    def list(self):
        return []

    def rev(self):
        return "r"


@pytest.fixture
def env(built_app, tmp_path):
    cfg = built_app.config()
    auth.set_password(cfg.state.auth, PW)
    w = tmp_path / "web"
    w.mkdir()
    (w / "index.html").write_text("<!doctype html><title>x</title>")
    return Env(cfg, Clock(), w)


def origin(c):
    return str(c.base_url).rstrip("/")


def post(c, path, body=None, **headers):
    return c.post(path, json=body if body is not None else {}, headers={"origin": origin(c), **headers})


def password_login(c, pw=PW):
    return post(c, "/api/login", {"password": pw})


def add_passkey(c, a, name="Phone", rp=None):
    opts = post(c, "/api/passkeys/options", {"purpose": "register"})
    assert opts.status_code == 200, opts.text
    pk = opts.json()["publicKey"]
    assert pk["attestation"] == "none" and pk["authenticatorSelection"]["userVerification"] == "required"
    assert [p["alg"] for p in pk["pubKeyCredParams"]] == [EDDSA, ES256, RS256]
    r = post(c, "/api/passkeys/register", {"credential": a.register(opts.json(), origin(c), rp_id=rp), "name": name})
    return r


def passkey_login(c, a, **kw):
    opts = post(c, "/api/passkeys/options", {"purpose": "login"})
    assert opts.status_code == 200, opts.text
    cred = a.login(opts.json(), kw.pop("as_origin", origin(c)), **kw)
    return post(c, "/api/passkeys/login", {"credential": cred}), cred


@pytest.fixture
def with_passkey(env):
    c = env.make()
    assert password_login(c).status_code == 200
    a = SoftAuthenticator(ES256)
    r = add_passkey(c, a)
    assert r.status_code == 200, r.text
    assert r.json()["added"]["name"] == "Phone"
    post(c, "/api/logout")
    c.cookies.clear()
    return env, a


def test_full_passkey_journey(with_passkey):
    env, a = with_passkey
    c = env.make()
    assert c.get("/api/session").json()["sign_in"] == {"password": True, "passkeys": True, "passkeys_here": True, "recovery": False}
    r, _cred = passkey_login(c, a)
    assert r.status_code == 200, r.text
    assert r.json()["signed_in"] is True
    assert c.get("/api/session").json()["signed_in"] is True
    s = c.get("/api/signin").json()
    assert [p["name"] for p in s["passkeys"]] == ["Phone"] and s["passkeys"][0]["last_used"] is not None
    assert s["passkeys"][0]["here"] is True and "public_key" not in json.dumps(s)
    assert c.get("/api/dashboards").status_code == 200


def test_replayed_passkey_answer_is_refused(env):
    # A passkey that keeps no counter (most synced passkeys): only the challenge stops a replay.
    c = env.make()
    assert password_login(c).status_code == 200
    a = SoftAuthenticator(ES256, sign_count=None)
    assert add_passkey(c, a).status_code == 200
    post(c, "/api/logout")
    c = env.make()
    opts = post(c, "/api/passkeys/options", {"purpose": "login"})
    binding = c.cookies.get("easyui-demo-passkey")
    cred = a.login(opts.json(), ORIGIN)
    assert post(c, "/api/passkeys/login", {"credential": cred}).status_code == 200
    # The same answer sent again, even from the same browser with the same challenge cookie:
    c.cookies.set("easyui-demo-passkey", binding)
    again = post(c, "/api/passkeys/login", {"credential": cred})
    assert again.status_code == 400 and again.json()["error"]["code"] == "passkey_replayed"
    # Another browser (no challenge cookie) cannot use it either.
    other = env.make()
    r2 = post(other, "/api/passkeys/login", {"credential": cred})
    assert r2.status_code == 400 and r2.json()["error"]["code"] == "passkey_stale"


def test_passkey_answer_from_another_origin_is_refused(with_passkey):
    env, a = with_passkey
    c = env.make()
    r, _ = passkey_login(c, a, as_origin="https://evil.example")
    assert r.status_code == 400 and "another web address" in r.json()["error"]["message"]
    r, _ = passkey_login(c, a, rp_id="evil.example")
    assert r.status_code == 400
    # A request sent from another site's page is stopped before it reaches the passkey check.
    opts = post(c, "/api/passkeys/options", {"purpose": "login"})
    cred = a.login(opts.json(), ORIGIN)
    r = post(c, "/api/passkeys/login", {"credential": cred}, origin="https://evil.example")
    assert r.status_code == 403 and r.json()["error"]["code"] == "cross_site"


def test_unknown_passkey_and_counter_rollback(with_passkey):
    env, a = with_passkey
    c = env.make()
    r, _ = passkey_login(c, SoftAuthenticator(ES256))
    assert r.status_code == 403 and r.json()["error"]["code"] == "unknown_passkey"
    assert passkey_login(c, a)[0].status_code == 200
    r, _ = passkey_login(c, a, count=1)  # the counter is already 1
    assert r.status_code == 400 and "went backwards" in r.json()["error"]["message"]


def test_passkeys_need_https_or_localhost(env):
    for base in ("http://127.0.0.1:8765", "http://[::1]:8765"):
        c = env.make(base=base)
        r = post(c, "/api/passkeys/options", {"purpose": "login"})
        assert r.status_code == 403 and r.json()["error"]["code"] == "passkeys_unavailable"
        assert "localhost" in r.json()["error"]["hint"]
    c = env.make(base="http://dash.example.net", origins=("http://dash.example.net",))
    r = post(c, "/api/passkeys/options", {"purpose": "login"})
    assert r.status_code == 403 and "https" in r.json()["error"]["message"]
    assert c.get("/api/session").json()["sign_in"]["passkeys_here"] is False


@pytest.mark.parametrize("base", ["https://dash.example.net", "http://dash.example.net"])
def test_passkeys_over_https_behind_the_private_network_proxy(env, base):
    # https: the connection says so. http: a proxy on this computer that does not say the browser used
    # https; the browser's own Origin (https) is what counts then, never the other way round.
    c = env.make(base=base, origins=("https://dash.example.net",))
    https = "https://dash.example.net"
    h = {"origin": https}
    r = c.post("/api/login", json={"password": PW}, headers=h)
    assert r.status_code == 200
    a = SoftAuthenticator(EDDSA)
    opts = c.post("/api/passkeys/options", json={"purpose": "register"}, headers=h)
    assert opts.status_code == 200 and opts.json()["publicKey"]["rp"]["id"] == "dash.example.net"
    r = c.post("/api/passkeys/register", json={"credential": a.register(opts.json(), https)}, headers=h)
    assert r.status_code == 200, r.text
    opts = c.post("/api/passkeys/options", json={"purpose": "login"}, headers=h)
    r = c.post("/api/passkeys/login", json={"credential": a.login(opts.json(), https)}, headers=h)
    assert r.status_code == 200, r.text
    # An answer made for plain http on that name is refused (the page can never be plain http there).
    opts = c.post("/api/passkeys/options", json={"purpose": "login"}, headers=h)
    r = c.post("/api/passkeys/login", json={"credential": a.login(opts.json(), "http://dash.example.net")}, headers=h)
    assert r.status_code == 400
    # The same passkey does not work on another address of the same server.
    local = env.make()
    r, _ = passkey_login(local, a, rp_id="localhost")
    assert r.status_code == 403 and r.json()["error"]["code"] == "other_address"


def test_registration_needs_a_recent_sign_in(env):
    c = env.make()
    r = post(c, "/api/passkeys/options", {"purpose": "register"})
    assert r.status_code == 401
    assert password_login(c).status_code == 200
    env.clock.advance(passkeys.RECENT_SIGN_IN + 1)
    r = post(c, "/api/passkeys/options", {"purpose": "register"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "sign_in_again"
    for path, body in (("/api/passkeys/remove", {"id": "x"}), ("/api/signin/password", {"on": False}), ("/api/recovery-codes", {})):
        r = post(c, path, body)
        assert r.status_code == 403 and r.json()["error"]["code"] == "sign_in_again", path


def test_registration_challenge_is_bound_to_the_session(env):
    c1, c2 = env.make(), env.make()
    assert password_login(c1).status_code == 200 and password_login(c2).status_code == 200
    opts = post(c1, "/api/passkeys/options", {"purpose": "register"}).json()
    a = SoftAuthenticator()
    r = post(c2, "/api/passkeys/register", {"credential": a.register(opts, ORIGIN)})
    assert r.status_code == 400 and r.json()["error"]["code"] == "passkey_stale"


def test_passkey_sign_in_works_while_password_guessing_is_paused(with_passkey):
    """The v0.1 known limit: someone guessing passwords kept new browsers out. Passkeys are not throttled."""
    env, a = with_passkey
    attacker = env.make()
    codes = [password_login(attacker, "wrong password!!").status_code for _ in range(8)]
    assert 429 in codes
    new_browser = env.make()
    assert password_login(new_browser).status_code == 429  # the password is paused for new browsers...
    r, _ = passkey_login(new_browser, a)
    assert r.status_code == 200  # ...but the passkey signs in at once


def test_password_sign_in_can_be_turned_off_and_on(with_passkey):
    env, a = with_passkey
    c = env.make()
    assert passkey_login(c, a)[0].status_code == 200
    r = post(c, "/api/signin/password", {"on": False})
    assert r.status_code == 200 and r.json()["password_sign_in"] is False
    other = env.make()
    r = password_login(other)
    assert r.status_code == 403 and r.json()["error"]["code"] == "password_off"
    assert other.get("/api/session").json()["sign_in"]["password"] is False
    r = post(c, "/api/signin/password", {"on": "yes"})
    assert r.status_code == 400
    assert post(c, "/api/signin/password", {"on": True}).json()["password_sign_in"] is True
    assert password_login(other).status_code == 200


def test_removing_the_last_passkey_turns_the_password_back_on(with_passkey):
    env, a = with_passkey
    c = env.make()
    assert passkey_login(c, a)[0].status_code == 200
    post(c, "/api/signin/password", {"on": False})
    pid = c.get("/api/signin").json()["passkeys"][0]["id"]
    r = post(c, "/api/passkeys/remove", {"id": pid})
    assert r.status_code == 200 and r.json()["removed"]["password_sign_in_turned_on"] is True
    assert r.json()["password_sign_in"] is True and r.json()["passkeys"] == []
    assert post(c, "/api/passkeys/remove", {"id": pid}).status_code == 404


def test_recovery_codes_over_http(with_passkey):
    env, a = with_passkey
    c = env.make()
    assert passkey_login(c, a)[0].status_code == 200
    r = post(c, "/api/recovery-codes")
    assert r.status_code == 200
    codes = r.json()["codes"]
    assert len(codes) == 10 and r.json()["recovery"]["left"] == 10
    assert r.headers["cache-control"] == "no-store"
    other = env.make()
    r = post(other, "/api/recovery", {"code": codes[3]})
    assert r.status_code == 200 and r.json()["signed_in"] is True
    third = env.make()
    r = post(third, "/api/recovery", {"code": codes[3]})
    assert r.status_code == 401 and r.json()["error"]["code"] == "wrong_code"


def test_recovery_code_guessing_is_throttled_but_passkeys_are_not(with_passkey):
    env, a = with_passkey
    c = env.make()
    assert passkey_login(c, a)[0].status_code == 200
    codes = post(c, "/api/recovery-codes").json()["codes"]
    guesser = env.make()
    results = [post(guesser, "/api/recovery", {"code": "ABCD-EFGH-JKMN-PQRS"}).status_code for _ in range(10)]
    assert results[:3] == [401, 401, 401] and 429 in results
    r = post(env.make(), "/api/recovery", {"code": codes[0]})
    assert r.status_code == 429 and "passkey" in r.json()["error"]["message"]
    assert passkey_login(env.make(), a)[0].status_code == 200
    assert password_login(env.make()).status_code == 200  # the password has its own buckets
    env.clock.advance(auth.UNTRUSTED_CAP + 1)
    assert post(env.make(), "/api/recovery", {"code": codes[0]}).status_code == 200


def test_preview_has_no_passkeys(env):
    asgi = server.create_app(env.cfg, server.ServerSettings(web_dir=env.web_dir, local_port=8765), source=_Source(env.cfg), preview_token="t" * 32)
    c = TestClient(asgi, base_url=ORIGIN)
    r = post(c, "/api/passkeys/options", {"purpose": "login"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "preview"
    assert "sign_in" not in c.get("/api/session").json()


def test_options_need_a_known_purpose(env):
    c = env.make()
    assert post(c, "/api/passkeys/options", {"purpose": "steal"}).status_code == 400
    assert post(c, "/api/passkeys/options", {"purpose": "login", "x": 1}).status_code == 400
    r = post(c, "/api/passkeys/login", {"credential": {"type": "public-key"}})
    assert r.status_code == 400
    r = post(c, "/api/passkeys/login", {"credential": "garbage"})
    assert r.status_code == 400


def test_set_password_keeps_passkeys(with_passkey):
    env, a = with_passkey
    auth.set_password(env.cfg.state.auth, "another long password")
    assert passkey_login(env.make(), a)[0].status_code == 200


# ---------------------------------------------------------------- commands


def test_commands_list_remove_and_switch(with_passkey):
    env, _a = with_passkey
    code, data = run_cli_json("passkeys")
    assert code == 0 and [p["name"] for p in data["passkeys"]] == ["Phone"]
    assert data["password_set"] is True and data["password_sign_in"] is True
    code, out, _ = run_cli("passkeys", "list")
    assert code == 0 and "Phone" in out and "Password: set" in out
    code, data = run_cli_json("sign-in", "password", "off")
    assert code == 0 and data["password_sign_in"] is False
    code, data = run_cli_json("passkeys", "remove", "phone")
    assert code == 0 and data["removed"]["password_sign_in_turned_on"] is True
    code, data = run_cli_json("sign-in", "password", "off")
    assert code == 5 and data["error"]["code"] == "needs_passkey"
    code, data = run_cli_json("passkeys", "remove", "nothing")
    assert code == 3
    code, out, _ = run_cli("sign-in")
    assert code == 0 and "Passkeys: none yet" in out


def test_recovery_codes_command_needs_the_person(env, monkeypatch):
    monkeypatch.setattr(auth, "open_terminal", lambda: None)
    code, data = run_cli_json("recovery-codes")
    assert code == 6 and data["error"]["code"] == "needs_human"
    assert passkeys.RecoveryCodes(env.cfg.state.auth).left() == 0


def test_recovery_codes_command_writes_only_to_the_terminal(env, monkeypatch, tmp_path):
    tty = tmp_path / "tty.txt"
    monkeypatch.setattr(auth, "open_terminal", lambda: open(tty, "w"))
    code, out, _ = run_cli("--json", "recovery-codes")
    assert code == 0 and json.loads(out)["made"] == 10
    shown = tty.read_text()
    codes = [line.strip() for line in shown.splitlines() if line.startswith("  ")]
    assert len(codes) == 10
    assert all(c not in out for c in codes)
    assert passkeys.RecoveryCodes(env.cfg.state.auth).use(codes[0]) is True


def test_hashes_of_sessions_and_passkeys_are_not_secrets_in_the_listing(with_passkey):
    env, _ = with_passkey
    text = (env.cfg.state.auth / "passkeys.json").read_text()
    data = json.loads(text)
    assert set(data) == {"v", "user_id", "credentials", "password_sign_in"}
    assert hashlib.sha256(PW.encode()).hexdigest() not in text


def test_counter_is_checked_again_when_saved(tmp_path):
    """Two answers verified at once against the same stored counter (a copied passkey): only one gets in."""
    store = passkeys.PasskeyStore(tmp_path / "auth", Clock())
    a = SoftAuthenticator(EDDSA)
    added = store.add(verify_reg(a.register(reg_options(), ORIGIN)), rp_id=RP, name=None)
    cid = added["id"]
    store.touch(cid, 5, False)
    with pytest.raises(webauthn.WebAuthnError):
        store.touch(cid, 5, False)
    with pytest.raises(webauthn.WebAuthnError):
        store.touch(cid, 3, False)  # never moves back
    assert store.get(cid)["sign_count"] == 5
    store.touch(cid, 6, False)
    # Authenticators without a counter (always 0) keep working.
    b = SoftAuthenticator(EDDSA)
    bid = store.add(verify_reg(b.register(reg_options(), ORIGIN)), rp_id=RP, name=None)["id"]
    store.touch(bid, 0, True)
    store.touch(bid, 0, True)

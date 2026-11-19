"""Sign-in: password storage (Argon2id), set-password, throttling, device tokens and sessions."""

from __future__ import annotations

import getpass
import json
import stat
import threading
import time
import warnings

import pytest
from argon2 import PasswordHasher, Type, extract_parameters

from conftest import run_cli, run_cli_json
from easyui import auth, sessions
from easyui.errors import ExitCode, NeedsHuman, UsageError

PW = "correct horse battery staple"


class Clock:
    def __init__(self, t: float = 1_800_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


@pytest.fixture
def fast_hash(monkeypatch):
    """A cheap Argon2id for tests that sign in many times (the real parameters are tested separately)."""
    monkeypatch.setattr(auth, "_hasher", PasswordHasher(time_cost=1, memory_cost=8, parallelism=1, hash_len=16, salt_len=16, type=Type.ID))


class SpyHasher:
    """Wraps the hasher in use so a test can watch or replace verify()."""

    def __init__(self, base, verify):
        self.base, self._verify = base, verify

    def hash(self, p):
        return self.base.hash(p)

    def check_needs_rehash(self, h):
        return self.base.check_needs_rehash(h)

    def verify(self, h, p):
        return self._verify(self.base, h, p)


def spy(monkeypatch, fn):
    monkeypatch.setattr(auth, "_hasher", SpyHasher(auth._hasher, fn))


@pytest.fixture
def auth_dir(tmp_path):
    d = tmp_path / "state" / "demo" / "auth"
    d.mkdir(parents=True, mode=0o700)
    return d


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def authn(auth_dir, clock, fast_hash):
    auth.set_password(auth_dir, PW)
    store = sessions.SessionStore(auth_dir, clock=clock)
    return auth.Authenticator(auth_dir, store, clock)


# ---------------------------------------------------------------- password storage


def test_real_argon2id_parameters_meet_owasp(auth_dir):
    auth.set_password(auth_dir, PW)
    h = auth.load_hash(auth_dir)
    assert h.startswith("$argon2id$v=19$m=65536,t=3,p=4$")
    p = extract_parameters(h)
    assert p.type is Type.ID
    assert p.memory_cost >= auth.OWASP_MINIMUM["memory_cost"] and p.time_cost >= auth.OWASP_MINIMUM["time_cost"]
    assert p.salt_len >= 16 and p.hash_len >= 32
    status = auth.password_status(auth_dir)
    assert status == {**status, "set": True, "file_private": True, "algorithm_ok": True}


def test_password_file_is_owner_only_and_has_no_plain_text(auth_dir, fast_hash):
    auth.set_password(auth_dir, PW)
    f = auth.password_file(auth_dir)
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert PW not in f.read_text()
    assert stat.S_IMODE(auth_dir.stat().st_mode) & 0o077 == 0


def test_weak_stored_hash_is_reported(auth_dir):
    weak = PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1, type=Type.ID).hash(PW)
    sessions.write_private_json(auth.password_file(auth_dir), {"v": 1, "hash": weak})
    assert auth.password_status(auth_dir)["algorithm_ok"] is False
    argon2i = PasswordHasher(type=Type.I).hash(PW)
    sessions.write_private_json(auth.password_file(auth_dir), {"v": 1, "hash": argon2i})
    assert auth.password_status(auth_dir)["algorithm_ok"] is False


@pytest.mark.parametrize("pw", ["a" * 11, "short", " " * 20, "", "　" * 12])
def test_too_short_or_blank_passwords_are_refused(pw):
    with pytest.raises(UsageError) as e:
        auth.check_new_password(pw)
    assert e.value.code == "password_too_short"
    assert "12" in e.value.message and e.value.hint


def test_minimum_and_maximum_length():
    assert auth.check_new_password("a" * 12) == "a" * 12
    with pytest.raises(UsageError) as e:
        auth.check_new_password("a" * (auth.MAX_LENGTH + 1))
    assert e.value.code == "password_too_long"


def test_nfkc_normalisation_means_same_password_on_another_keyboard(authn, auth_dir):
    auth.set_password(auth_dir, "ｆｕｌｌｗｉｄｔｈ ｐａｓｓ")  # full-width letters
    assert authn.sign_in("fullwidth pass").session_secret


def test_set_password_signs_everyone_out_and_clears_throttle_and_devices(authn, auth_dir):
    ok = authn.sign_in(PW)
    for _ in range(5):
        with pytest.raises((auth.WrongPassword, auth.TooManyAttempts)):
            authn.sign_in("wrong password!!")
    assert authn.sessions.count() == 1 and authn.devices.count() == 1
    res = auth.set_password(auth_dir, "another long password")
    assert res == {"password_set": True, "signed_out": 1}
    assert authn.sessions.get(ok.session_secret) is None
    assert authn.throttle.failures(auth.UNTRUSTED) == 0
    assert authn.devices.count() == 0


# ---------------------------------------------------------------- sign-in


def test_right_and_wrong_password(authn):
    with pytest.raises(auth.WrongPassword) as e:
        authn.sign_in("not the password")
    assert e.value.http_status == 401 and e.value.code == "wrong_password"
    assert "not right" in e.value.message and "case-sensitive" in e.value.hint
    res = authn.sign_in(PW)
    assert authn.sessions.get(res.session_secret) is not None
    assert res.device_secret  # first successful sign-in on this browser gets a device token


def test_no_password_set_needs_the_human(auth_dir, clock, fast_hash):
    a = auth.Authenticator(auth_dir, sessions.SessionStore(auth_dir, clock=clock), clock)
    with pytest.raises(NeedsHuman) as e:
        a.sign_in(PW)
    assert e.value.http_status == 409 and "set-password" in e.value.hint


@pytest.mark.parametrize("pw", [None, "", 123, ["x"], {"a": 1}])
def test_missing_or_non_text_password(authn, pw):
    with pytest.raises(UsageError) as e:
        authn.sign_in(pw)
    assert e.value.http_status == 400


def test_absurdly_long_password_is_refused_without_hashing(authn, monkeypatch):
    calls = []
    spy(monkeypatch, lambda *a: calls.append(1))
    with pytest.raises(UsageError):
        authn.sign_in("x" * 10_000)
    assert calls == []


def test_right_and_wrong_both_run_exactly_one_full_check(authn, monkeypatch):
    calls = []

    def counting(base, h, p):
        calls.append(p)
        return base.verify(h, p)

    spy(monkeypatch, counting)
    authn.sign_in(PW)
    with pytest.raises(auth.WrongPassword):
        authn.sign_in("wrong password!!")
    assert len(calls) == 2


def test_hash_is_upgraded_when_parameters_were_lower(auth_dir, clock, fast_hash):
    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1, hash_len=16, salt_len=8, type=Type.ID).hash(PW)
    sessions.write_private_json(auth.password_file(auth_dir), {"v": 1, "hash": weak})
    a = auth.Authenticator(auth_dir, sessions.SessionStore(auth_dir, clock=clock), clock)
    a.sign_in(PW)
    new = auth.load_hash(auth_dir)
    assert new != weak and not auth._hasher.check_needs_rehash(new)
    a.sign_in(PW)


def test_old_session_is_removed_at_sign_in(authn):
    first = authn.sign_in(PW)
    second = authn.sign_in(PW, old_session=first.session_secret)
    assert authn.sessions.get(first.session_secret) is None
    assert authn.sessions.get(second.session_secret) is not None


def test_damaged_password_file_needs_the_human(auth_dir, clock, fast_hash):
    sessions.write_private_json(auth.password_file(auth_dir), {"v": 1, "hash": "$argon2id$v=19$garbage"})
    a = auth.Authenticator(auth_dir, sessions.SessionStore(auth_dir, clock=clock), clock)
    with pytest.raises(NeedsHuman):
        a.sign_in(PW)


# ---------------------------------------------------------------- throttling


def _fail(a, n=1, device=None):
    """n wrong passwords in a row, each made once the current pause (if any) is over."""
    for _ in range(n):
        key = a.devices.check(device)
        bucket = f"device:{key[:32]}" if key else auth.UNTRUSTED
        a.clock.advance(a.throttle.wait_left(bucket) + 0.01)
        with pytest.raises(auth.WrongPassword):
            a.sign_in("wrong password!!", device_secret=device)


def test_delay_schedule():
    assert [auth.delay_for(n, 120) for n in range(1, 12)] == [0, 0, 1, 2, 4, 8, 16, 32, 64, 120, 120]
    assert auth.delay_for(10_000, 30) == 30


def test_first_failures_are_free_then_waits_grow(authn, clock):
    for _ in range(auth.FREE_ATTEMPTS - 1):
        with pytest.raises(auth.WrongPassword):
            authn.sign_in("wrong password!!")  # no pause between these
    _fail(authn)  # the 3rd failure starts a 1 s wait
    with pytest.raises(auth.TooManyAttempts) as e:
        authn.sign_in(PW)
    assert e.value.http_status == 429 and e.value.retry_after == 1
    assert "second" in e.value.message and e.value.hint
    clock.advance(1.01)
    _fail(authn)  # 4th: 2 s
    with pytest.raises(auth.TooManyAttempts) as e:
        authn.sign_in(PW)
    assert e.value.retry_after == 2
    clock.advance(2.01)
    assert authn.sign_in(PW).session_secret  # success resets the bucket
    assert authn.throttle.failures(auth.UNTRUSTED) == 0


def test_attempts_while_waiting_are_not_counted_and_do_not_extend_the_wait(authn, clock, monkeypatch):
    _fail(authn, 5)  # wait of 4 s
    before = authn.throttle.failures(auth.UNTRUSTED)
    calls = []
    spy(monkeypatch, lambda *a: calls.append(1))
    for _ in range(50):
        with pytest.raises(auth.TooManyAttempts):
            authn.sign_in("guess guess guess")
    assert calls == []  # no password check was done for refused attempts
    assert authn.throttle.failures(auth.UNTRUSTED) == before
    assert authn.throttle.wait_left(auth.UNTRUSTED) <= 4


def test_untrusted_wait_is_capped(authn, clock):
    for _ in range(20):
        clock.advance(auth.UNTRUSTED_CAP + 1)
        _fail(authn)
    assert authn.throttle.wait_left(auth.UNTRUSTED) == pytest.approx(auth.UNTRUSTED_CAP)


def test_throttle_survives_a_restart(authn, auth_dir, clock):
    _fail(authn, 4)
    restarted = auth.Authenticator(auth_dir, sessions.SessionStore(auth_dir, clock=clock), clock)
    with pytest.raises(auth.TooManyAttempts):
        restarted.sign_in(PW)
    data = json.loads((auth_dir / auth.THROTTLE_FILE).read_text())
    assert data["buckets"][auth.UNTRUSTED]["failures"] == 4
    assert stat.S_IMODE((auth_dir / auth.THROTTLE_FILE).stat().st_mode) == 0o600


def test_failures_are_forgotten_after_a_quiet_hour(authn, clock):
    _fail(authn, 6)
    clock.advance(auth.FORGET_AFTER + 1)
    assert authn.throttle.failures(auth.UNTRUSTED) == 0
    _fail(authn, 2)  # free again


def test_failure_is_recorded_before_the_check(authn, auth_dir, monkeypatch):
    seen = []

    def watch(base, h, p):
        seen.append(json.loads((auth_dir / auth.THROTTLE_FILE).read_text())["buckets"][auth.UNTRUSTED]["failures"])
        return base.verify(h, p)

    spy(monkeypatch, watch)
    _fail(authn)
    assert seen == [1]


def test_parallel_attempts_cannot_slip_past_the_throttle(authn, monkeypatch):
    _fail(authn, 2)
    results = []
    gate = threading.Barrier(6)

    def attempt():
        gate.wait()
        try:
            authn.sign_in("wrong password!!")
        except Exception as e:  # noqa: BLE001
            results.append(type(e).__name__)

    threads = [threading.Thread(target=attempt) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # Only one more attempt is counted before the wait starts; the rest are refused.
    assert results.count("WrongPassword") == 1
    assert results.count("TooManyAttempts") == 5


def test_owner_device_is_not_locked_out_by_an_attacker(authn, clock):
    owner = authn.sign_in(PW)
    device = owner.device_secret
    _fail(authn, 12)  # attacker, no device token
    assert authn.throttle.wait_left(auth.UNTRUSTED) > 60
    with pytest.raises(auth.TooManyAttempts):
        authn.sign_in(PW)  # a new browser has to wait
    again = authn.sign_in(PW, device_secret=device)  # the owner's browser does not
    assert again.session_secret and again.device_secret is None  # the device token is kept


def test_device_bucket_has_a_short_cap_and_is_forgotten_after_many_failures(authn, clock):
    device = authn.sign_in(PW).device_secret
    key = authn.devices.check(device)
    for _ in range(auth.DEVICE_MAX_FAILURES - 1):
        clock.advance(auth.DEVICE_CAP + 1)
        _fail(authn, device=device)
    assert authn.throttle.wait_left(f"device:{key[:32]}") <= auth.DEVICE_CAP
    assert authn.throttle.failures(auth.UNTRUSTED) == 0  # the device's failures do not touch the shared bucket
    clock.advance(auth.DEVICE_CAP + 1)
    _fail(authn, device=device)
    assert authn.devices.check(device) is None  # the token no longer buys its own bucket


def test_unknown_or_expired_device_token_counts_as_untrusted(authn, clock):
    device = authn.sign_in(PW).device_secret
    _fail(authn, device="made-up-token")
    assert authn.throttle.failures(auth.UNTRUSTED) == 1
    clock.advance(auth.DEVICE_LIFETIME + 1)
    assert authn.devices.check(device) is None


# ---------------------------------------------------------------- memory: the semaphore


def test_busy_verifier_answers_try_again(authn, monkeypatch):
    sem = threading.BoundedSemaphore(1)
    sem.acquire()
    monkeypatch.setattr(auth, "_verify_slots", sem)
    monkeypatch.setattr(auth, "VERIFY_WAIT", 0.05)
    with pytest.raises(auth.VerifierBusy) as e:
        authn.sign_in(PW)
    assert e.value.http_status == 503 and e.value.hint
    assert authn.throttle.failures(auth.UNTRUSTED) == 0  # not counted against the person


def test_at_most_verify_slots_checks_run_at_once(authn, monkeypatch, clock):
    lock = threading.Lock()
    state = {"now": 0, "max": 0}

    def slow(base, h, p):
        with lock:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
        time.sleep(0.05)
        with lock:
            state["now"] -= 1
        return base.verify(h, p)

    spy(monkeypatch, slow)
    threads = [threading.Thread(target=lambda: authn.sign_in(PW)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert 1 <= state["max"] <= auth.VERIFY_SLOTS


# ---------------------------------------------------------------- hidden prompt and the command


def test_no_terminal_needs_the_human(monkeypatch):
    monkeypatch.setattr(auth, "_terminal_available", lambda: False)
    with pytest.raises(NeedsHuman) as e:
        auth.read_hidden("Password: ")
    assert e.value.exit_code == ExitCode.NEEDS_HUMAN and "set-password" in e.value.hint


def test_echoing_fallback_is_refused(monkeypatch):
    monkeypatch.setattr(auth, "_terminal_available", lambda: True)

    def falls_back(prompt):
        warnings.warn("Can not control echo on the terminal.", getpass.GetPassWarning, stacklevel=1)
        return "typed in clear text"

    monkeypatch.setattr(auth.getpass, "getpass", falls_back)
    with pytest.raises(NeedsHuman):
        auth.read_hidden("Password: ")


@pytest.fixture
def typed(monkeypatch):
    answers: list[str] = []
    monkeypatch.setattr(auth, "read_hidden", lambda prompt: answers.pop(0))
    return answers


def test_set_password_command(demo_app, typed, fast_hash):
    typed += [PW, PW]
    code, data = run_cli_json("set-password")
    assert code == 0 and data["ok"] is True and data["password_set"] is True
    cfg = demo_app.config()
    assert auth.load_hash(cfg.state.auth)
    assert stat.S_IMODE(cfg.state.root.stat().st_mode) & 0o077 == 0


def test_set_password_text_output(demo_app, typed, fast_hash):
    typed += [PW, PW]
    code, out, err = run_cli("set-password")
    assert code == 0 and "password is set" in out and PW not in out + err


def test_set_password_mismatch_and_too_short(demo_app, typed, fast_hash):
    typed += [PW, PW + "x"] * auth.PROMPT_TRIES
    code, data = run_cli_json("set-password")
    assert code == 2 and data["error"]["code"] == "passwords_differ"
    typed += ["short"] * auth.PROMPT_TRIES
    code, data = run_cli_json("set-password")
    assert code == 2 and data["error"]["code"] == "password_too_short"
    assert "12" in data["error"]["message"] and "set-password" in data["error"]["hint"]
    assert auth.load_hash(demo_app.config().state.auth) is None
    assert not typed


@pytest.fixture
def prompts(monkeypatch):
    """Answers to the hidden prompt, and the prompts that were shown."""
    answers: list[str] = []
    shown: list[str] = []

    def read(prompt):
        shown.append(prompt)
        return answers.pop(0)

    monkeypatch.setattr(auth, "read_hidden", read)
    return answers, shown


def test_set_password_prompt_states_the_rule_and_asks_again(demo_app, prompts, fast_hash):
    answers, shown = prompts
    answers += ["short", PW, PW + "x", PW, PW]  # too short, then a mismatch, then right
    code, out, err = run_cli("--json", "set-password")
    assert code == 0 and json.loads(out)["password_set"] is True  # stdout is only the JSON result
    assert f"at least {auth.MIN_LENGTH} characters" in shown[0]
    assert "has 5 characters; it needs at least 12" in shown[1] and "2 tries left" in shown[1]
    assert "not the same" in shown[3] and "1 try left" in shown[3]
    assert all(f"at least {auth.MIN_LENGTH} characters" in p for p in shown if p.startswith(("New", "That", "The two")))
    assert PW not in out + err and "short" not in out + err
    assert auth.load_hash(demo_app.config().state.auth)


def test_set_password_gives_up_after_three_tries(demo_app, prompts, fast_hash):
    answers, shown = prompts
    answers += ["short", "tiny", "x" * 11]
    code, out, err = run_cli("set-password")
    assert code == 2 and "too short" in out + err and "asked 3 times" in out + err
    assert len(shown) == 3 and not answers
    assert auth.load_hash(demo_app.config().state.auth) is None


def test_set_password_never_takes_an_argument_or_environment(demo_app, monkeypatch):
    code, data = run_cli_json("set-password", PW)
    assert code == 2 and data["error"]["code"] == "usage"
    code, _ = run_cli_json("set-password", "--password", PW)
    assert code == 2
    monkeypatch.setenv("EASYUI_PASSWORD", PW)
    monkeypatch.setattr(auth, "_terminal_available", lambda: False)
    code, data = run_cli_json("set-password")
    assert code == 6 and data["error"]["code"] == "needs_human"
    assert auth.load_hash(demo_app.config().state.auth) is None


# ---------------------------------------------------------------- sessions


@pytest.fixture
def store(auth_dir, clock):
    return sessions.SessionStore(auth_dir, idle=3600, absolute=10 * 3600, clock=clock)


def test_session_ids_are_256_bit_and_stored_hashed(store, auth_dir):
    secret, s = store.create()
    assert len(secret) >= 43  # 32 random bytes, URL-safe base64
    raw = (auth_dir / sessions.SESSIONS_FILE).read_text()
    assert secret not in raw
    assert s.key == sessions.digest(secret) and s.key in raw
    assert stat.S_IMODE((auth_dir / sessions.SESSIONS_FILE).stat().st_mode) == 0o600
    assert store.get(secret).key == s.key
    assert store.get(s.key) is None  # the stored hash itself is not a valid id


def test_idle_lifetime(store, clock):
    secret, _ = store.create()
    clock.advance(3599)
    assert store.get(secret) is not None  # use refreshes "last used"
    clock.advance(3599)
    assert store.get(secret) is not None
    clock.advance(3601)
    assert store.get(secret) is None
    assert store.count() == 0


def test_absolute_lifetime_even_when_used(store, clock):
    secret, _ = store.create()
    for _ in range(9):
        clock.advance(3500)
        assert store.get(secret) is not None
    clock.advance(4000)
    assert store.get(secret) is None


def test_last_used_is_written_at_most_once_a_minute(store, auth_dir, clock):
    secret, _ = store.create()
    f = auth_dir / sessions.SESSIONS_FILE
    before = f.stat().st_mtime_ns
    clock.advance(10)
    store.get(secret)
    assert f.stat().st_mtime_ns == before
    clock.advance(sessions.TOUCH_EVERY)
    store.get(secret)
    assert json.loads(f.read_text())["sessions"][sessions.digest(secret)]["seen"] == clock.t


def test_delete_clear_and_bad_input(store):
    a, _ = store.create()
    b, _ = store.create()
    assert store.delete(a) is True and store.delete(a) is False
    assert store.get(a) is None and store.get(b) is not None
    for bad in (None, "", "x" * 500, 12):
        assert store.get(bad) is None
    assert store.clear() == 1 and store.get(b) is None


def test_changes_by_another_process_are_seen(store, auth_dir, clock):
    secret, _ = store.create()
    assert store.get(secret) is not None
    sessions.SessionStore(auth_dir, clock=clock).clear()  # e.g. easy-ui set-password while the server runs
    assert store.get(secret) is None


def test_number_of_sessions_is_bounded(store, clock):
    first, _ = store.create()
    for _ in range(sessions.MAX_SESSIONS + 5):
        clock.advance(1)
        store.create()
    assert store.count() == sessions.MAX_SESSIONS
    assert store.get(first) is None


def test_broken_sessions_file_means_signed_out_not_a_crash(store, auth_dir):
    secret, _ = store.create()
    (auth_dir / sessions.SESSIONS_FILE).write_text("{not json")
    assert store.get(secret) is None
    store.create()

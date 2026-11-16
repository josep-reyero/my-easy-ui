"""Sign-in for the one owner of an app: password, attempt throttling, device tokens.

Password
--------
The owner sets the password with ``easy-ui set-password``. It is typed at a
hidden prompt on the terminal (``/dev/tty``); it is never taken from a command
argument, an environment variable or a pipe, so an agent cannot set it. It must
have at least 12 characters (after Unicode NFKC normalisation, which is also
applied at sign-in, so the same password typed on another keyboard still
works) and at most 1024.

It is stored as an Argon2id hash (``argon2-cffi``) with time cost 3, 64 MiB of
memory, parallelism 4, a 16-byte salt and a 32-byte hash: RFC 9106's
"second recommended" setting, above OWASP's current minimum (19 MiB, time cost
2, parallelism 1). The hash lives in ``<state>/auth/password.json`` with mode
0600 inside a 0700 folder. When the parameters are raised in a later version,
the hash is upgraded at the next successful sign-in. Setting a password signs
out every browser and clears the throttle and device lists.

Checking a password costs 64 MiB of memory, so at most ``VERIFY_SLOTS`` (2)
checks run at the same time (a semaphore); a request that cannot get a slot
within ``VERIFY_WAIT`` seconds is told to try again (HTTP 503), so a flood of
parallel sign-in attempts cannot exhaust memory.

Constant-time behaviour: a right and a wrong password both run one full
Argon2id check (whose comparison is constant time), and the answer is the same
apart from the result. Requests refused before any check (throttled, no
password set) give away nothing secret: the waiting time is announced anyway.

Throttling, and why it cannot lock the owner out for long
---------------------------------------------------------
Failed attempts are counted per *bucket* and kept on disk
(``<state>/auth/throttle.json``), so restarting the server (it stops by itself
when idle) does not reset them. After ``FREE_ATTEMPTS`` (3) failures in a row
each further failure makes the bucket wait 1, 2, 4, 8 ... seconds before the
next attempt, up to a cap. A failure is recorded *before* the password is
checked, so parallel attempts cannot slip through together. An attempt made
while the bucket is waiting is refused (HTTP 429 with ``Retry-After``) without
being counted and without checking anything, so hammering does not extend the
wait. A bucket forgets its failures after ``FORGET_AFTER`` (1 hour) without
any, and on a successful sign-in.

The buckets follow OWASP's "device cookie" pattern. A browser that has signed
in successfully before receives a random 256-bit *device token* (an HttpOnly
cookie, stored hashed in ``<state>/auth/devices.json``, valid 400 days). Its
attempts are counted in its own bucket (cap 30 seconds). Every other attempt
(new browsers, scripts, attackers) shares the *untrusted* bucket (cap 120
seconds). So:

- An attacker without a device token can slow down only the untrusted bucket.
  The owner's usual browsers keep their own bucket and are not affected at all.
- The known limit: while someone keeps sending wrong passwords (every few
  milliseconds, all the time), a browser that has never signed in shares their
  bucket and is refused almost every time, for as long as that goes on. A new
  device and an attacker look the same to the server, so it cannot let one in
  and not the other without also letting the attacker guess faster. The way
  back in is a browser that has signed in before, or ``easy-ui set-password``
  on the server (which clears every wait; an attacker who keeps going starts a
  new one). The message on the sign-in page says so, and every failed sign-in
  and every burst of refused attempts is written to the server log (and
  counted by ``easy-ui check``), so an attack can be seen.
- An attacker gets at most about 720 guesses a day (one per 2 minutes), which
  is hopeless against a 12-character password, and the server is only
  reachable on the owner's private network in the first place.
- A device bucket that reaches ``DEVICE_MAX_FAILURES`` (10) failures in a row
  loses its device token, so a stolen device cookie does not buy extra
  guesses; that browser falls back to the untrusted bucket.

The trade-off: the caps are short on purpose. Long lockouts would let anyone on
the network keep the owner out; short caps keep online guessing far too slow
to matter, given the minimum length and Argon2id.

Public API
----------
``MIN_LENGTH``, ``MAX_LENGTH``, ``ARGON2``, ``OWASP_MINIMUM``, ``VERIFY_SLOTS``,
``FREE_ATTEMPTS``, ``UNTRUSTED_CAP``, ``DEVICE_CAP``, ``FORGET_AFTER``,
``DEVICE_MAX_FAILURES``, ``DEVICE_LIFETIME``
``WrongPassword`` (401), ``TooManyAttempts`` (429, ``retry_after``), ``VerifierBusy`` (503).
``normalize(password) -> str``
``check_new_password(password)``          raises ``UsageError`` for a password that is too short or long.
``hash_password(password) -> str``
``password_file(auth_dir) -> Path``
``load_hash(auth_dir) -> str | None``
``password_status(auth_dir) -> dict``     ``{"set", "file_private", "algorithm_ok", "parameters"}``.
``set_password(auth_dir, password) -> dict`` saves it; signs everyone out; ``{"signed_out", ...}``.
``read_hidden(prompt) -> str``            hidden prompt on the terminal; ``NeedsHuman`` without one.
``prompt_new_password() -> str``          asks twice.
``delay_for(failures, cap) -> float``
``Throttle(auth_dir, clock=time.time)``   ``wait_left(bucket)``, ``reserve(bucket, cap)``,
                                          ``succeed(bucket)``, ``failures(bucket)``, ``clear()``.
``DeviceStore(auth_dir, clock=time.time)`` ``check(secret)``, ``issue()``, ``forget(key)``, ``clear()``.
``Authenticator(auth_dir, sessions, clock=time.time)``
    ``sign_in(password, *, device_secret=None, old_session=None) -> SignIn`` (blocking; run it in a
    worker thread). ``SignIn`` has ``session_secret``, ``session``, ``device_secret`` (a new device
    token to set, or None to keep the current one).
"""

from __future__ import annotations

import contextlib
import getpass
import logging
import math
import os
import threading
import time
import unicodedata
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from argon2 import PasswordHasher, Type, extract_parameters
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from easyui.errors import NeedsHuman, SignInRequired, TemporaryFailure, UsageError
from easyui.sessions import (
    Session,
    SessionStore,
    digest,
    ensure_private_dir,
    locked,
    new_secret,
    read_json,
    write_private_json,
)

MIN_LENGTH = 12
MAX_LENGTH = 1024
ARGON2 = {"time_cost": 3, "memory_cost": 65536, "parallelism": 4, "hash_len": 32, "salt_len": 16}
OWASP_MINIMUM = {"time_cost": 2, "memory_cost": 19456, "parallelism": 1}
VERIFY_SLOTS = 2
VERIFY_WAIT = 10.0
FREE_ATTEMPTS = 3
UNTRUSTED_CAP = 120.0
DEVICE_CAP = 30.0
FORGET_AFTER = 3600.0
DEVICE_MAX_FAILURES = 10
DEVICE_LIFETIME = 400 * 24 * 3600
MAX_DEVICES = 20
UNTRUSTED = "untrusted"

PASSWORD_FILE = "password.json"
THROTTLE_FILE = "throttle.json"
DEVICES_FILE = "devices.json"
LOCK_FILE = ".lock"

log = logging.getLogger("easyui.auth")
_hasher = PasswordHasher(type=Type.ID, **ARGON2)
_verify_slots = threading.BoundedSemaphore(VERIFY_SLOTS)

FORGOT_HINT = (
    "If the password is forgotten, set a new one by typing easy-ui set-password in a terminal on the computer that "
    "runs the dashboards."
)
NEW_BROWSER_HINT = (
    "If this keeps happening although you typed the right password, someone may be guessing it: a browser you have "
    "signed in with before still works, and typing easy-ui set-password on the computer that runs the dashboards "
    "clears the pause."
)


class WrongPassword(SignInRequired):
    default_code = "wrong_password"


class TooManyAttempts(TemporaryFailure):
    http_status = 429
    default_code = "slow_down"

    def __init__(self, retry_after: float, *, new_browser: bool = False) -> None:
        self.retry_after = max(1, math.ceil(retry_after))
        super().__init__(
            f"There were several wrong passwords in a row, so sign-in is paused for {self.retry_after} "
            "more second(s). This makes guessing the password far too slow to work.",
            hint=f"Wait {self.retry_after} second(s), then try again. "
            + (NEW_BROWSER_HINT if new_browser else FORGOT_HINT),
        )


class VerifierBusy(TemporaryFailure):
    default_code = "busy"


# ---------------------------------------------------------------- password


def normalize(password: str) -> str:
    return unicodedata.normalize("NFKC", password)


def check_new_password(password: Any) -> str:
    if not isinstance(password, str):
        raise UsageError("The password must be text.", hint="Type the password again.")
    pw = normalize(password)
    if len(pw) < MIN_LENGTH or not pw.strip():
        raise UsageError(
            f"That password is too short, so it was not saved. A password needs at least {MIN_LENGTH} "
            "characters to be hard to guess.",
            hint=f"Choose a password of {MIN_LENGTH} or more characters; a few unrelated words work well. "
            "Then run easy-ui set-password again.",
            code="password_too_short",
        )
    if len(pw) > MAX_LENGTH:
        raise UsageError(
            f"That password is longer than {MAX_LENGTH} characters, so it was not saved.",
            hint="Choose a shorter password and run easy-ui set-password again.",
            code="password_too_long",
        )
    return pw


def hash_password(password: str) -> str:
    return _hasher.hash(normalize(password))


def password_file(auth_dir: Path) -> Path:
    return Path(auth_dir) / PASSWORD_FILE


def load_hash(auth_dir: Path) -> str | None:
    data = read_json(password_file(auth_dir), None)
    h = data.get("hash") if isinstance(data, dict) else None
    return h if isinstance(h, str) and h.startswith("$argon2") else None


def _params_ok(h: str) -> tuple[bool, dict[str, Any]]:
    try:
        p = extract_parameters(h)
    except (InvalidHashError, ValueError):
        return False, {}
    info = {"type": p.type.name.lower(), "time_cost": p.time_cost, "memory_kib": p.memory_cost, "parallelism": p.parallelism}
    ok = (
        p.type is Type.ID
        and p.time_cost >= OWASP_MINIMUM["time_cost"]
        and p.memory_cost >= OWASP_MINIMUM["memory_cost"]
        and p.parallelism >= OWASP_MINIMUM["parallelism"]
    )
    return ok, info


def password_status(auth_dir: Path) -> dict[str, Any]:
    f = password_file(auth_dir)
    h = load_hash(auth_dir)
    private = None
    if f.exists():
        st = f.stat()
        private = (st.st_mode & 0o077) == 0 and st.st_uid == os.geteuid()
    ok, info = _params_ok(h) if h else (False, {})
    return {"set": h is not None, "file_private": private, "algorithm_ok": ok, "parameters": info}


def set_password(auth_dir: Path, password: str, *, clock: Callable[[], float] = time.time) -> dict[str, Any]:
    pw = check_new_password(password)
    h = _hasher.hash(pw)
    auth_dir = ensure_private_dir(Path(auth_dir))
    with locked(auth_dir / LOCK_FILE):
        write_private_json(password_file(auth_dir), {"v": 1, "hash": h, "set_at": clock()})
    signed_out = SessionStore(auth_dir).clear()
    Throttle(auth_dir).clear()
    DeviceStore(auth_dir).clear()
    return {"password_set": True, "signed_out": signed_out}


# ---------------------------------------------------------------- prompting


def _no_terminal() -> NeedsHuman:
    return NeedsHuman(
        "Setting the sign-in password needs the person to type it on this computer's terminal, so it cannot be "
        "done from here (there is no terminal to type into). This keeps the password known only to them.",
        hint="Ask the person to open a terminal on the server, go to the app's folder and run: easy-ui set-password",
    )


def _terminal_available() -> bool:
    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    except OSError:
        return False
    os.close(fd)
    return True


def read_hidden(prompt: str) -> str:
    if not _terminal_available():
        raise _no_terminal()
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt)
        except getpass.GetPassWarning:
            raise _no_terminal() from None
        except EOFError:
            raise UsageError("No password was typed, so nothing was changed.", hint="Run easy-ui set-password again.") from None


def prompt_new_password() -> str:
    pw = read_hidden(f"New sign-in password (at least {MIN_LENGTH} characters; it does not show while you type): ")
    check_new_password(pw)
    again = read_hidden("Type the same password again: ")
    if normalize(again) != normalize(pw):
        raise UsageError(
            "The two passwords were not the same, so nothing was changed.",
            hint="Run easy-ui set-password again and type the same password twice.",
            code="passwords_differ",
        )
    return pw


# ---------------------------------------------------------------- throttle


def delay_for(failures: int, cap: float) -> float:
    if failures < FREE_ATTEMPTS:
        return 0.0
    return float(min(cap, 2 ** min(failures - FREE_ATTEMPTS, 30)))


class Throttle:
    def __init__(self, auth_dir: Path, clock: Callable[[], float] = time.time) -> None:
        self.dir = Path(auth_dir)
        self.path = self.dir / THROTTLE_FILE
        self.lock_path = self.dir / LOCK_FILE
        self.clock = clock

    def _read(self) -> dict[str, dict[str, Any]]:
        data = read_json(self.path, {})
        b = data.get("buckets") if isinstance(data, dict) else None
        return {k: v for k, v in (b or {}).items() if isinstance(v, dict)}

    def _write(self, buckets: dict[str, dict[str, Any]]) -> None:
        write_private_json(self.path, {"v": 1, "buckets": buckets})

    def _live(self, rec: dict[str, Any] | None, now: float) -> dict[str, Any] | None:
        if not rec:
            return None
        try:
            last = float(rec.get("last", 0))
        except (TypeError, ValueError):
            return None
        if now - last > FORGET_AFTER:
            return None
        return rec

    def failures(self, bucket: str) -> int:
        rec = self._live(self._read().get(bucket), self.clock())
        return int(rec.get("failures", 0)) if rec else 0

    def wait_left(self, bucket: str) -> float:
        now = self.clock()
        rec = self._live(self._read().get(bucket), now)
        if not rec:
            return 0.0
        try:
            until = float(rec.get("until", 0))
        except (TypeError, ValueError):
            return 0.0
        # A clock that went backwards must not make the wait endless.
        return max(0.0, min(until - now, UNTRUSTED_CAP))

    def reserve(self, bucket: str, cap: float) -> int:
        """Refuse when the bucket is waiting; otherwise count one failure up front and return the count."""
        with locked(self.lock_path):
            now = self.clock()
            buckets = self._read()
            rec = self._live(buckets.get(bucket), now) or {"failures": 0, "last": 0.0, "until": 0.0}
            left = max(0.0, min(float(rec.get("until", 0)) - now, cap))
            if left > 0:
                raise TooManyAttempts(left, new_browser=bucket == UNTRUSTED)
            n = int(rec.get("failures", 0)) + 1
            buckets[bucket] = {"failures": n, "last": now, "until": now + delay_for(n, cap)}
            buckets = {k: v for k, v in buckets.items() if self._live(v, now)}
            self._write(buckets)
            return n

    def succeed(self, bucket: str) -> None:
        with locked(self.lock_path):
            buckets = self._read()
            if buckets.pop(bucket, None) is not None:
                self._write(buckets)

    def clear(self) -> None:
        with locked(self.lock_path):
            self._write({})


# ---------------------------------------------------------------- device tokens


class DeviceStore:
    def __init__(self, auth_dir: Path, clock: Callable[[], float] = time.time) -> None:
        self.dir = Path(auth_dir)
        self.path = self.dir / DEVICES_FILE
        self.lock_path = self.dir / LOCK_FILE
        self.clock = clock

    def _read(self) -> dict[str, dict[str, Any]]:
        data = read_json(self.path, {})
        d = data.get("devices") if isinstance(data, dict) else None
        return {k: v for k, v in (d or {}).items() if isinstance(k, str) and len(k) == 64 and isinstance(v, dict)}

    def _write(self, devices: dict[str, dict[str, Any]]) -> None:
        write_private_json(self.path, {"v": 1, "devices": devices})

    def _alive(self, rec: dict[str, Any], now: float) -> bool:
        try:
            return now - float(rec["created"]) < DEVICE_LIFETIME
        except (KeyError, TypeError, ValueError):
            return False

    def check(self, secret: str | None) -> str | None:
        """The stored key of a known, unexpired device token, else None."""
        if not secret or not isinstance(secret, str) or len(secret) > 200:
            return None
        key = digest(secret)
        rec = self._read().get(key)
        return key if rec is not None and self._alive(rec, self.clock()) else None

    def issue(self) -> str:
        secret = new_secret()
        now = self.clock()
        with locked(self.lock_path):
            devices = {k: v for k, v in self._read().items() if self._alive(v, now)}
            devices[digest(secret)] = {"created": now, "used": now}
            if len(devices) > MAX_DEVICES:
                devices = dict(sorted(devices.items(), key=lambda kv: float(kv[1].get("used", 0)), reverse=True)[:MAX_DEVICES])
            self._write(devices)
        return secret

    def touch(self, key: str) -> None:
        with locked(self.lock_path):
            devices = self._read()
            if key in devices:
                devices[key] = {**devices[key], "used": self.clock()}
                self._write(devices)

    def forget(self, key: str) -> None:
        with locked(self.lock_path):
            devices = self._read()
            if devices.pop(key, None) is not None:
                self._write(devices)

    def clear(self) -> None:
        with locked(self.lock_path):
            self._write({})

    def count(self) -> int:
        now = self.clock()
        return sum(1 for v in self._read().values() if self._alive(v, now))


# ---------------------------------------------------------------- sign-in


@dataclass(frozen=True)
class SignIn:
    session_secret: str
    session: Session
    device_secret: str | None


class Authenticator:
    def __init__(self, auth_dir: Path, sessions: SessionStore, clock: Callable[[], float] = time.time) -> None:
        self.dir = Path(auth_dir)
        self.sessions = sessions
        self.throttle = Throttle(self.dir, clock)
        self.devices = DeviceStore(self.dir, clock)
        self.clock = clock
        self._refused = 0
        self._refused_logged = -math.inf

    def _note_refused(self, bucket: str) -> None:
        """Log refused attempts at most once a minute, so a flood is visible without flooding the log."""
        self._refused += 1
        now = self.clock()
        if now - self._refused_logged >= 60:
            log.warning(
                "%d sign-in attempt(s) refused while sign-in is paused after wrong passwords (%s); if this was not "
                "you, someone may be guessing the password",
                self._refused,
                "new browsers" if bucket == UNTRUSTED else "one known browser",
            )
            self._refused = 0
            self._refused_logged = now

    def password_is_set(self) -> bool:
        return load_hash(self.dir) is not None

    def sign_in(self, password: Any, *, device_secret: str | None = None, old_session: str | None = None) -> SignIn:
        if not isinstance(password, str) or not password:
            raise UsageError("Type the password to sign in.", hint="Enter the app's password and try again.", code="password_missing")
        if len(password) > MAX_LENGTH * 4:
            raise UsageError("That password is far too long to be right.", hint="Check it and try again.", code="password_too_long")
        stored = load_hash(self.dir)
        if stored is None:
            raise NeedsHuman(
                "No sign-in password has been set for this app yet, so nobody can sign in.",
                hint="Ask the owner to run easy-ui set-password on the server (they type the password themselves).",
            )
        device_key = self.devices.check(device_secret)
        bucket = f"device:{device_key[:32]}" if device_key else UNTRUSTED
        cap = DEVICE_CAP if device_key else UNTRUSTED_CAP
        left = min(self.throttle.wait_left(bucket), cap)
        if left > 0:  # cheap early answer; reserve() below decides under the lock
            self._note_refused(bucket)
            raise TooManyAttempts(left, new_browser=not device_key)

        if not _verify_slots.acquire(timeout=VERIFY_WAIT):
            raise VerifierBusy(
                "Too many sign-in attempts are being checked at the same time, so this one was not checked.",
                hint="Wait a few seconds and try again.",
            )
        try:
            try:
                failures = self.throttle.reserve(bucket, cap)
            except TooManyAttempts:
                self._note_refused(bucket)
                raise
            try:
                ok = _hasher.verify(stored, normalize(password))
            except VerifyMismatchError:
                ok = False
            except (VerificationError, InvalidHashError):
                raise NeedsHuman(
                    "The saved sign-in password could not be read, so nobody can sign in.",
                    hint="Ask the owner to set it again on the server with: easy-ui set-password",
                ) from None
        finally:
            _verify_slots.release()

        if not ok:
            log.warning(
                "failed sign-in from %s (%d wrong password(s) in a row)",
                "a browser that has signed in before" if device_key else "a browser that has not signed in before",
                failures,
            )
            if device_key and failures >= DEVICE_MAX_FAILURES:
                self.devices.forget(device_key)
                self.throttle.succeed(bucket)
            wait = delay_for(failures, cap)
            later = f" Sign-in pauses for {math.ceil(wait)} second(s) before the next try, to stop guessing." if wait else ""
            raise WrongPassword(
                "That password is not right, so you are not signed in." + later,
                hint="Check the password and try again. Passwords are case-sensitive.",
            )

        self.throttle.succeed(bucket)
        log.info("signed in from %s", "a browser that has signed in before" if device_key else "a new browser")
        if _hasher.check_needs_rehash(stored):
            with contextlib.suppress(OSError), locked(self.dir / LOCK_FILE):
                if load_hash(self.dir) == stored:
                    data = read_json(password_file(self.dir), {})
                    write_private_json(password_file(self.dir), {**data, "hash": _hasher.hash(normalize(password))})
        if old_session:
            self.sessions.delete(old_session)
        if device_key:
            self.devices.touch(device_key)
            new_device = None
        else:
            new_device = self.devices.issue()
            device_key = digest(new_device)
        secret, session = self.sessions.create(device=device_key[:16])
        return SignIn(secret, session, new_device)

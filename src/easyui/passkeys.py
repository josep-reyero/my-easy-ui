"""Passkeys, recovery codes and the password switch for the one owner of a project.

Passkeys (WebAuthn) are the main way to sign in. The password stays as the way
to sign in for the first time (a passkey can only be added by someone who is
already signed in) and as a fallback, until the owner turns password sign-in
off. One-time recovery codes are the way back in when every passkey is lost.

Files (all in ``<ui>/auth/``, mode 0600, written atomically under the auth lock):

- ``passkeys.json``: ``{"v": 1, "user_id", "credentials": {id: {...}}, "password_sign_in"}``.
  Each credential: ``public_key`` (the COSE key, base64url), ``alg``, ``sign_count``,
  ``rp_id`` (the host name it was made for), ``name``, ``created``, ``last_used``,
  ``backup_eligible``, ``backed_up``, ``aaguid``, ``transports``. Public keys are not
  secret; nothing in this file lets anyone sign in.
- ``recovery.json``: ``{"v": 1, "salt", "created", "codes": [{"hash", "used"}]}``. Only
  salted SHA-256 hashes are stored; the codes are shown once, when they are made.

Rules
-----
- At most ``MAX_PASSKEYS`` (20) passkeys. Several per owner; each can be named,
  listed and removed (from the command line or the sign-in settings page).
- Password sign-in can be turned off only while at least one passkey exists.
  Removing the last passkey turns it back on (when a password is set), so the
  owner is never left without a way in by accident.
- Recovery codes: ``RECOVERY_CODES`` (10) codes of 16 characters from the
  Crockford base-32 alphabet (80 random bits each), shown as ``ABCD-EFGH-JKMN-PQRS``.
  Typing is forgiving (case, spaces and dashes do not matter; O reads as 0, I and
  L as 1). Each works once. Making new codes replaces all old ones. Guessing is
  hopeless (10 codes of 80 bits) and slowed down further by the same throttle the
  password uses, in its own buckets, so it never slows down passkeys or the
  password.
- Challenges are 40 bytes: 16 random bytes, an 8-byte expiry time and a 16-byte
  HMAC-SHA256 tag over the purpose (``register`` / ``login``), what the challenge is
  bound to (the session for ``register``, a random browser cookie for ``login``),
  the random part and the expiry, with a key made fresh in each server process.
  So the server keeps no list of open challenges (nobody can fill it up to
  push out the owner's), a challenge expires after ``CHALLENGE_SECONDS`` (5
  minutes), cannot be used for the other purpose or from another browser, and
  does not survive a restart. A challenge whose answer was accepted is
  remembered until it expires and refused when it comes back (replay); only
  accepted answers are remembered, so this list cannot be flooded either.
  Checking and remembering happen under one lock, so two copies of the same
  answer sent at once cannot both sign in.

Public API
----------
``MAX_PASSKEYS``, ``RECOVERY_CODES``, ``CHALLENGE_SECONDS``, ``RECENT_SIGN_IN``
``PasskeyStore(auth_dir, clock=time.time)``
    ``user_id()``, ``credentials() -> dict``, ``listing() -> list[dict]`` (no keys), ``count()``,
    ``get(credential_id_b64) -> dict | None``, ``add(registration, *, rp_id, name) -> dict``,
    ``touch(credential_id_b64, sign_count, backed_up)``, ``remove(id_or_name) -> dict`` (the removed one),
    ``password_sign_in() -> bool``, ``set_password_sign_in(on, *, password_set) -> bool``.
``RecoveryCodes(auth_dir, clock=time.time)``
    ``make() -> list[str]`` (shown once), ``use(code) -> bool``, ``left() -> int``, ``status() -> dict``.
``normalize_code(text) -> str | None``
``Challenges(clock=time.time)``
    ``issue(purpose, binding) -> bytes``, ``check(challenge, purpose, binding)`` (raises),
    ``accept(challenge)`` (raises when it was already used; call after the answer verified).
``default_name(listing, now) -> str``
``clean_name(text) -> str``
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import secrets
import struct
import threading
import time
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import Any

from easyui.errors import NotFound, Refused, UsageError
from easyui.sessions import ensure_private_dir, locked, read_json, write_private_json
from easyui.webauthn import Registration, WebAuthnError, b64url_decode, b64url_encode

MAX_PASSKEYS = 20
RECOVERY_CODES = 10
CHALLENGE_SECONDS = 300
RECENT_SIGN_IN = 600  # sign-in settings can be changed for this long after signing in
PASSKEYS_FILE = "passkeys.json"
RECOVERY_FILE = "recovery.json"
LOCK_FILE = ".lock"
NAME_MAX = 60
CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base 32: no I, L, O, U
CODE_LENGTH = 16
_CODE_MAP = str.maketrans({"O": "0", "I": "1", "L": "1"})


def clean_name(text: Any) -> str:
    if not isinstance(text, str):
        raise UsageError("A passkey name must be text.", hint="For example: Phone or Laptop.")
    s = unicodedata.normalize("NFKC", text)
    s = "".join(c if c.isprintable() else " " for c in s)
    s = " ".join(s.split())
    if len(s) > NAME_MAX:
        raise UsageError(f"That passkey name is longer than {NAME_MAX} characters.", hint="Choose a shorter name.")
    return s


def default_name(listing: list[dict[str, Any]], now: float) -> str:
    day = _dt.datetime.fromtimestamp(now, _dt.UTC).date()
    base = f"Passkey added {day.day} {day.strftime('%b')} {day.year}"
    names = {p["name"] for p in listing}
    if base not in names:
        return base
    n = 2
    while f"{base} ({n})" in names:
        n += 1
    return f"{base} ({n})"


# ---------------------------------------------------------------- passkeys


class PasskeyStore:
    def __init__(self, auth_dir: Path, clock: Callable[[], float] = time.time) -> None:
        self.dir = Path(auth_dir)
        self.path = self.dir / PASSKEYS_FILE
        self.lock_path = self.dir / LOCK_FILE
        self.clock = clock

    def _read(self) -> dict[str, Any]:
        data = read_json(self.path, {})
        if not isinstance(data, dict):
            data = {}
        creds = data.get("credentials")
        data["credentials"] = {
            k: v for k, v in (creds or {}).items() if isinstance(k, str) and isinstance(v, dict) and isinstance(v.get("public_key"), str)
        } if isinstance(creds, dict) else {}
        return data

    def _write(self, data: dict[str, Any]) -> None:
        ensure_private_dir(self.dir)
        write_private_json(self.path, {**data, "v": 1})

    def user_id(self) -> bytes:
        """The owner's random WebAuthn user handle (made on first use)."""
        data = self._read()
        uid = data.get("user_id")
        if isinstance(uid, str):
            try:
                raw = b64url_decode(uid, what="the user id", limit=64)
                if len(raw) == 16:
                    return raw
            except WebAuthnError:
                pass
        with locked(self.lock_path):
            data = self._read()
            uid = data.get("user_id")
            if not isinstance(uid, str) or len(uid) != 22:
                data["user_id"] = b64url_encode(secrets.token_bytes(16))
                self._write(data)
            return b64url_decode(data["user_id"], what="the user id", limit=64)

    def credentials(self) -> dict[str, dict[str, Any]]:
        return self._read()["credentials"]

    def count(self) -> int:
        return len(self.credentials())

    def get(self, credential_id: str) -> dict[str, Any] | None:
        return self.credentials().get(credential_id)

    def listing(self) -> list[dict[str, Any]]:
        items = []
        for cid, c in self.credentials().items():
            items.append({
                "id": cid,
                "name": c.get("name") or "Passkey",
                "created": c.get("created"),
                "last_used": c.get("last_used"),
                "rp_id": c.get("rp_id"),
                "backed_up": bool(c.get("backed_up")),
                "synced": bool(c.get("backup_eligible")),
            })
        items.sort(key=lambda p: (p["created"] or 0, p["id"]))
        return items

    def add(self, reg: Registration, *, rp_id: str, name: str | None) -> dict[str, Any]:
        cid = b64url_encode(reg.credential_id)
        now = self.clock()
        with locked(self.lock_path):
            data = self._read()
            creds = data["credentials"]
            if cid in creds:
                raise Refused(
                    "This passkey is already saved, so it was not added again.",
                    hint="It is in the list of passkeys already.",
                    code="passkey_exists",
                )
            if len(creds) >= MAX_PASSKEYS:
                raise Refused(
                    f"There are already {MAX_PASSKEYS} passkeys, the most easy-ui keeps, so this one was not added.",
                    hint="Remove one you no longer use, then add this one.",
                    code="too_many_passkeys",
                )
            label = clean_name(name) if name else ""
            if not label:
                label = default_name(self.listing(), now)
            creds[cid] = {
                "public_key": b64url_encode(reg.public_key),
                "alg": reg.alg,
                "sign_count": reg.sign_count,
                "rp_id": rp_id,
                "name": label,
                "created": now,
                "last_used": None,
                "backup_eligible": reg.backup_eligible,
                "backed_up": reg.backed_up,
                "aaguid": reg.aaguid,
                "transports": list(reg.transports),
            }
            data.setdefault("password_sign_in", True)
            self._write(data)
        return next(p for p in self.listing() if p["id"] == cid)

    def touch(self, credential_id: str, sign_count: int, backed_up: bool) -> None:
        with locked(self.lock_path):
            data = self._read()
            c = data["credentials"].get(credential_id)
            if c is None:
                return
            # Checked again under the lock: two answers verified at the same time against the same stored
            # counter (a copied authenticator) must not both get in, and the counter never moves back.
            stored = int(c.get("sign_count") or 0)
            if (sign_count or stored) and sign_count <= stored:
                raise WebAuthnError(
                    "This passkey's counter did not go up, which happens when a passkey has been copied, so you "
                    "are not signed in with it.",
                    hint="Sign in another way and remove this passkey on the Sign-in settings page.",
                    code="passkey_refused",
                )
            c["sign_count"] = int(sign_count)
            c["backed_up"] = bool(backed_up)
            c["last_used"] = self.clock()
            self._write(data)

    def _find(self, creds: dict[str, dict[str, Any]], key: str) -> str:
        if key in creds:
            return key
        matches = [cid for cid, c in creds.items() if (c.get("name") or "").casefold() == str(key).casefold()]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise UsageError(
                f"More than one passkey is called {str(key)[:60]!r}, so easy-ui does not know which one to remove.",
                hint="Use its id from: easy-ui passkeys list",
            )
        raise NotFound(
            f"There is no passkey called {str(key)[:60]!r}.",
            hint="See the passkeys and their ids with: easy-ui passkeys list",
            code="no_such_passkey",
        )

    def remove(self, key: str, *, password_set: bool = True) -> dict[str, Any]:
        with locked(self.lock_path):
            data = self._read()
            creds = data["credentials"]
            cid = self._find(creds, key)
            removed = {"id": cid, "name": creds[cid].get("name") or "Passkey"}
            del creds[cid]
            turned_on = False
            if not creds and data.get("password_sign_in") is False and password_set:
                data["password_sign_in"] = True
                turned_on = True
            self._write(data)
        return {**removed, "password_sign_in_turned_on": turned_on}

    def password_sign_in(self) -> bool:
        data = self._read()
        if not data["credentials"]:
            return True
        return data.get("password_sign_in") is not False

    def set_password_sign_in(self, on: bool, *, password_set: bool) -> bool:
        with locked(self.lock_path):
            data = self._read()
            if not on and not data["credentials"]:
                raise Refused(
                    "Password sign-in cannot be turned off while there is no passkey, because then nobody could sign "
                    "in at all.",
                    hint="Add a passkey first (sign in, then open Sign-in settings), then turn password sign-in off.",
                    code="needs_passkey",
                )
            if on and not password_set:
                raise Refused(
                    "Password sign-in cannot be turned on because no password is set.",
                    hint="Set one first with: easy-ui set-password",
                    code="no_password",
                )
            data["password_sign_in"] = bool(on)
            self._write(data)
        return bool(on)


# ---------------------------------------------------------------- recovery codes


def normalize_code(text: Any) -> str | None:
    if not isinstance(text, str) or len(text) > 100:
        return None
    s = "".join(c for c in text.upper() if c not in " -_\t").translate(_CODE_MAP)
    if len(s) != CODE_LENGTH or any(c not in CODE_ALPHABET for c in s):
        return None
    return s


def _show(code: str) -> str:
    return "-".join(code[i : i + 4] for i in range(0, CODE_LENGTH, 4))


class RecoveryCodes:
    def __init__(self, auth_dir: Path, clock: Callable[[], float] = time.time) -> None:
        self.dir = Path(auth_dir)
        self.path = self.dir / RECOVERY_FILE
        self.lock_path = self.dir / LOCK_FILE
        self.clock = clock

    def _read(self) -> dict[str, Any]:
        data = read_json(self.path, {})
        if not isinstance(data, dict) or not isinstance(data.get("salt"), str) or not isinstance(data.get("codes"), list):
            return {"codes": []}
        data["codes"] = [c for c in data["codes"] if isinstance(c, dict) and isinstance(c.get("hash"), str)]
        return data

    @staticmethod
    def _hash(salt: str, code: str) -> str:
        return hashlib.sha256(f"easy-ui recovery v1\0{salt}\0{code}".encode()).hexdigest()

    def make(self) -> list[str]:
        codes = ["".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH)) for _ in range(RECOVERY_CODES)]
        salt = secrets.token_hex(16)
        ensure_private_dir(self.dir)
        with locked(self.lock_path):
            write_private_json(self.path, {
                "v": 1,
                "salt": salt,
                "created": self.clock(),
                "codes": [{"hash": self._hash(salt, c), "used": None} for c in codes],
            })
        return [_show(c) for c in codes]

    def use(self, text: Any) -> bool:
        code = normalize_code(text)
        if code is None:
            return False
        with locked(self.lock_path):
            data = self._read()
            if not data["codes"]:
                return False
            h = self._hash(data["salt"], code)
            hit = None
            for c in data["codes"]:
                # Compare every stored hash, so the time taken does not depend on which one matches.
                if hmac.compare_digest(c["hash"], h) and c.get("used") is None:
                    hit = c
            if hit is None:
                return False
            hit["used"] = self.clock()
            write_private_json(self.path, data)
            return True

    def left(self) -> int:
        return sum(1 for c in self._read()["codes"] if c.get("used") is None)

    def status(self) -> dict[str, Any]:
        data = self._read()
        return {"made": data.get("created"), "left": self.left(), "total": len(data["codes"])}


# ---------------------------------------------------------------- challenges


class Challenges:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self._key = secrets.token_bytes(32)
        self._used: dict[bytes, float] = {}
        self._lock = threading.Lock()

    def _tag(self, purpose: str, binding: str, nonce: bytes, expires: bytes) -> bytes:
        msg = b"\0".join([b"easy-ui webauthn v1", purpose.encode(), binding.encode("utf-8", "surrogatepass"), nonce, expires])
        return hmac.new(self._key, msg, hashlib.sha256).digest()[:16]

    def issue(self, purpose: str, binding: str) -> bytes:
        nonce = secrets.token_bytes(16)
        expires = struct.pack(">Q", int(self.clock()) + CHALLENGE_SECONDS)
        return nonce + expires + self._tag(purpose, binding, nonce, expires)

    def check(self, challenge: bytes, purpose: str, binding: str) -> None:
        stale = WebAuthnError(
            "That passkey request is too old or was not made for this page, so it was not accepted.",
            hint="Try again: the page asks for a fresh one.",
            code="passkey_stale",
        )
        if not isinstance(challenge, bytes) or len(challenge) != 40:
            raise stale
        nonce, expires, tag = challenge[:16], challenge[16:24], challenge[24:]
        if not hmac.compare_digest(tag, self._tag(purpose, binding, nonce, expires)):
            raise stale
        if struct.unpack(">Q", expires)[0] < self.clock():
            raise stale

    def accept(self, challenge: bytes) -> None:
        """Remember an answered challenge; refuse one that was answered before."""
        now = self.clock()
        with self._lock:
            for k in [k for k, exp in self._used.items() if exp < now]:
                del self._used[k]
            if challenge in self._used:
                raise WebAuthnError(
                    "That passkey answer was already used once, so it was not accepted again.",
                    hint="Sign in again with the passkey.",
                    code="passkey_replayed",
                )
            self._used[challenge] = struct.unpack(">Q", challenge[16:24])[0] + 1

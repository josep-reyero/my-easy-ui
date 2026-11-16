"""The small WebAuthn (passkey) verification core: registration and sign-in ceremonies.

This module only checks; it keeps no state. ``easyui.passkeys`` stores the
credentials and issues the challenges. Signatures are checked with the
``cryptography`` package; everything else (CBOR, authenticator data, COSE keys,
client data) is parsed here, strictly: unknown or malformed input is refused,
never guessed at.

What is checked (WebAuthn Level 2, sections 7.1 and 7.2)
--------------------------------------------------------
Both ceremonies:

- ``type`` is ``public-key``; ``id`` and ``rawId`` are the same credential id
  (base64url without padding, at most 1023 bytes).
- ``clientDataJSON`` is a UTF-8 JSON object whose ``type`` is exactly
  ``webauthn.create`` / ``webauthn.get``, whose ``challenge`` decodes to exactly
  the challenge the server issued (compared in constant time), whose ``origin``
  is exactly one of the expected origins, and whose ``crossOrigin`` is absent or
  false (the page must not be framed by another site).
- the authenticator data starts with SHA-256 of the RP ID (constant-time
  comparison), has the user-present (UP) and user-verified (UV) flags, a
  backup-state flag only together with backup-eligible, and no trailing bytes;
  extension data, when flagged, must be one well-formed CBOR map.

Registration also: the attestation object is a CBOR map of exactly ``fmt``,
``attStmt`` and ``authData``; ``fmt`` is ``none`` with an empty statement (no
other attestation is accepted, so nothing about the device is trusted beyond
the key itself); attested credential data is present and its credential id is
the one the browser named; the public key is a COSE key of an allowed
algorithm with matching key type and curve: ES256 (-7, P-256, point checked on
the curve), EdDSA (-8, Ed25519) or RS256 (-257, at least 2048 bits, e = 65537
or another odd number of at least 3).

Sign-in also: the credential is one of the stored ones (the caller passes it),
no attested credential data is present, the signature over
``authenticatorData || SHA-256(clientDataJSON)`` verifies with the stored key
and algorithm, the user handle (when the authenticator returns one) is the
owner's, and the signature counter goes up whenever either side is not zero
(a counter that does not go up can mean a copied authenticator, so it is
refused). Passkeys that keep no counter report 0 every time; that is fine.

Public API
----------
``ES256``, ``EDDSA``, ``RS256``, ``ALGORITHMS``, ``MAX_CREDENTIAL_ID``
``WebAuthnError`` (``Refused``, exit 5, HTTP 400, code ``passkey_refused``).
``b64url_encode(data) -> str``, ``b64url_decode(text, *, what) -> bytes``
``cbor_decode(data) -> Any``                 one complete CBOR item (strict subset).
``AuthData`` (dataclass) and ``parse_auth_data(data) -> AuthData``
``parse_cose_key(data) -> (alg, public_key)``
``verify_signature(alg, public_key_cose, signature, data)``
``Registration`` (dataclass): ``credential_id`` (bytes), ``public_key`` (COSE bytes), ``alg``,
    ``sign_count``, ``aaguid`` (hex), ``backup_eligible``, ``backed_up``, ``transports``.
``verify_registration(credential, *, challenge, origins, rp_id, algorithms=ALGORITHMS) -> Registration``
``Assertion`` (dataclass): ``credential_id``, ``sign_count``, ``backed_up``, ``user_handle``.
``credential_id_of(credential) -> bytes``    the id a sign-in response names (checked like the rest).
``verify_assertion(credential, *, challenge, origins, rp_id, public_key, alg, stored_sign_count, user_id) -> Assertion``
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import struct
from dataclasses import dataclass, field
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa

from easyui.errors import Refused

ES256 = -7
EDDSA = -8
RS256 = -257
ALGORITHMS = (EDDSA, ES256, RS256)
MAX_CREDENTIAL_ID = 1023
MAX_CLIENT_DATA = 4096
MAX_AUTH_DATA = 4096
MAX_ATTESTATION = 8192
MAX_SIGNATURE = 1024
_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]*\Z")
_TRANSPORTS = frozenset({"usb", "nfc", "ble", "internal", "hybrid", "smart-card"})

FLAG_UP = 0x01
FLAG_UV = 0x04
FLAG_BE = 0x08
FLAG_BS = 0x10
FLAG_AT = 0x40
FLAG_ED = 0x80


class WebAuthnError(Refused):
    http_status = 400
    default_code = "passkey_refused"


def _refuse(why: str, hint: str | None = None) -> WebAuthnError:
    return WebAuthnError(
        f"The passkey answer was not accepted ({why}), so you are not signed in with it.",
        hint=hint or "Try again. If it keeps happening, sign in another way and add the passkey again.",
    )


# ---------------------------------------------------------------- encodings


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: Any, *, what: str, limit: int = 65536) -> bytes:
    if not isinstance(text, str) or len(text) > limit * 2 or not _B64URL_RE.match(text) or len(text) % 4 == 1:
        raise _refuse(f"{what} is not in the expected form")
    try:
        out = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        raise _refuse(f"{what} is not in the expected form") from None
    if b64url_encode(out) != text:  # only the one canonical spelling
        raise _refuse(f"{what} is not in the expected form")
    if len(out) > limit:
        raise _refuse(f"{what} is too long")
    return out


# ---------------------------------------------------------------- CBOR (strict subset)


class _Cbor:
    """Definite lengths only; integers, byte and text strings, arrays, maps, false/true/null."""

    MAX_DEPTH = 8
    MAX_ITEMS = 256

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def _take(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.data):
            raise _refuse("the data ends too early")
        out = self.data[self.pos : self.pos + n]
        self.pos += n
        return out

    def _arg(self, info: int) -> int:
        if info < 24:
            return info
        if info == 24:
            v = self._take(1)[0]
            if v < 24:
                raise _refuse("a number is not written in its shortest form")
            return v
        if info == 25:
            v = struct.unpack(">H", self._take(2))[0]
            if v < 256:
                raise _refuse("a number is not written in its shortest form")
            return v
        if info == 26:
            v = struct.unpack(">I", self._take(4))[0]
            if v < 65536:
                raise _refuse("a number is not written in its shortest form")
            return v
        if info == 27:
            v = struct.unpack(">Q", self._take(8))[0]
            if v < 2**32:
                raise _refuse("a number is not written in its shortest form")
            return v
        raise _refuse("it uses a CBOR form easy-ui does not accept")

    def item(self, depth: int = 0) -> Any:
        if depth > self.MAX_DEPTH:
            raise _refuse("the data is nested too deeply")
        first = self._take(1)[0]
        major, info = first >> 5, first & 0x1F
        if major == 7:
            if info == 20:
                return False
            if info == 21:
                return True
            if info == 22:
                return None
            raise _refuse("it uses a CBOR value easy-ui does not accept")
        n = self._arg(info)
        if major == 0:
            return n
        if major == 1:
            return -1 - n
        if major == 2:
            return self._take(n)
        if major == 3:
            try:
                return self._take(n).decode("utf-8")
            except UnicodeDecodeError:
                raise _refuse("a text value is not UTF-8") from None
        if major == 4:
            if n > self.MAX_ITEMS:
                raise _refuse("a list is too long")
            return [self.item(depth + 1) for _ in range(n)]
        if major == 5:
            if n > self.MAX_ITEMS:
                raise _refuse("a map is too long")
            out: dict[Any, Any] = {}
            for _ in range(n):
                k = self.item(depth + 1)
                if not isinstance(k, (int, str)) or isinstance(k, bool):
                    raise _refuse("a map key is not a number or text")
                if k in out:
                    raise _refuse("a map key appears twice")
                out[k] = self.item(depth + 1)
            return out
        raise _refuse("it uses a CBOR form easy-ui does not accept")  # tags (6)


def cbor_decode(data: bytes, *, allow_rest: bool = False) -> Any:
    d = _Cbor(bytes(data))
    value = d.item()
    if d.pos != len(d.data) and not allow_rest:
        raise _refuse("there are extra bytes after the data")
    return (value, d.pos) if allow_rest else value


# ---------------------------------------------------------------- authenticator data


@dataclass(frozen=True)
class AuthData:
    rp_id_hash: bytes
    flags: int
    sign_count: int
    aaguid: bytes | None = None
    credential_id: bytes | None = None
    public_key: bytes | None = None
    extensions: dict[Any, Any] | None = None

    @property
    def user_present(self) -> bool:
        return bool(self.flags & FLAG_UP)

    @property
    def user_verified(self) -> bool:
        return bool(self.flags & FLAG_UV)

    @property
    def backup_eligible(self) -> bool:
        return bool(self.flags & FLAG_BE)

    @property
    def backed_up(self) -> bool:
        return bool(self.flags & FLAG_BS)


def parse_auth_data(data: bytes) -> AuthData:
    data = bytes(data)
    if len(data) < 37:
        raise _refuse("the authenticator data is too short")
    if len(data) > MAX_AUTH_DATA:
        raise _refuse("the authenticator data is too long")
    rp_hash, flags, count = data[:32], data[32], struct.unpack(">I", data[33:37])[0]
    pos = 37
    aaguid = cred_id = pub = None
    if flags & FLAG_AT:
        if len(data) < pos + 18:
            raise _refuse("the new credential's data is cut short")
        aaguid = data[pos : pos + 16]
        n = struct.unpack(">H", data[pos + 16 : pos + 18])[0]
        pos += 18
        if n == 0 or n > MAX_CREDENTIAL_ID or len(data) < pos + n:
            raise _refuse("the credential id is missing or too long")
        cred_id = data[pos : pos + n]
        pos += n
        key, used = cbor_decode(data[pos:], allow_rest=True)
        if not isinstance(key, dict):
            raise _refuse("the public key is not a COSE key")
        pub = data[pos : pos + used]
        pos += used
    ext = None
    if flags & FLAG_ED:
        if pos >= len(data):
            raise _refuse("the extension data is missing")
        ext, used = cbor_decode(data[pos:], allow_rest=True)
        if not isinstance(ext, dict):
            raise _refuse("the extension data is not a map")
        pos += used
    if pos != len(data):
        raise _refuse("there are extra bytes after the authenticator data")
    return AuthData(rp_hash, flags, count, aaguid, cred_id, pub, ext)


def _check_flags(ad: AuthData, rp_id: str) -> None:
    if not hmac.compare_digest(ad.rp_id_hash, hashlib.sha256(rp_id.encode("idna")).digest()):
        raise _refuse(
            "it was made for another web address",
            "Open the dashboards with the address you added the passkey on, then try again.",
        )
    if not ad.user_present:
        raise _refuse("the authenticator did not confirm that someone was there")
    if not ad.user_verified:
        raise _refuse(
            "the device did not check that it is you (fingerprint, face or PIN)",
            "Use a passkey that asks for your fingerprint, face or device PIN.",
        )
    if ad.backed_up and not ad.backup_eligible:
        raise _refuse("its backup flags contradict each other")


# ---------------------------------------------------------------- COSE keys and signatures


def parse_cose_key(data: bytes) -> tuple[int, Any]:
    key = cbor_decode(data)
    if not isinstance(key, dict):
        raise _refuse("the public key is not a COSE key")
    kty, alg = key.get(1), key.get(3)
    if alg == ES256:
        x, y = key.get(-2), key.get(-3)
        if kty != 2 or key.get(-1) != 1 or not isinstance(x, bytes) or not isinstance(y, bytes) or len(x) != 32 or len(y) != 32:
            raise _refuse("the P-256 key is not well formed")
        try:
            pub = ec.EllipticCurvePublicNumbers(int.from_bytes(x, "big"), int.from_bytes(y, "big"), ec.SECP256R1()).public_key()
        except ValueError:
            raise _refuse("the P-256 key is not a point on the curve") from None
        return alg, pub
    if alg == EDDSA:
        x = key.get(-2)
        if kty != 1 or key.get(-1) != 6 or not isinstance(x, bytes) or len(x) != 32:
            raise _refuse("the Ed25519 key is not well formed")
        try:
            return alg, ed25519.Ed25519PublicKey.from_public_bytes(x)
        except ValueError:
            raise _refuse("the Ed25519 key is not valid") from None
    if alg == RS256:
        n, e = key.get(-1), key.get(-2)
        if kty != 3 or not isinstance(n, bytes) or not isinstance(e, bytes) or not n or not e or len(e) > 8:
            raise _refuse("the RSA key is not well formed")
        ni, ei = int.from_bytes(n, "big"), int.from_bytes(e, "big")
        if ni.bit_length() < 2048 or ni.bit_length() > 8192 or ei < 3 or ei % 2 == 0:
            raise _refuse("the RSA key is too weak or not valid")
        try:
            return alg, rsa.RSAPublicNumbers(ei, ni).public_key()
        except ValueError:
            raise _refuse("the RSA key is not valid") from None
    raise _refuse("it uses a signature algorithm easy-ui does not accept")


def verify_signature(alg: int, public_key_cose: bytes, signature: bytes, data: bytes) -> None:
    key_alg, pub = parse_cose_key(public_key_cose)
    if key_alg != alg:
        raise _refuse("its algorithm does not match the saved passkey")
    if not signature or len(signature) > MAX_SIGNATURE:
        raise _refuse("the signature is missing or too long")
    try:
        if alg == ES256:
            pub.verify(signature, data, ec.ECDSA(hashes.SHA256()))
        elif alg == EDDSA:
            pub.verify(signature, data)
        elif alg == RS256:
            pub.verify(signature, data, padding.PKCS1v15(), hashes.SHA256())
        else:  # pragma: no cover - parse_cose_key refuses other algorithms
            raise _refuse("it uses a signature algorithm easy-ui does not accept")
    except InvalidSignature:
        raise _refuse("its signature does not match the saved passkey") from None
    except ValueError:  # e.g. an ECDSA signature that is not DER
        raise _refuse("its signature is not well formed") from None


# ---------------------------------------------------------------- client data


def _client_data(raw: bytes, *, kind: str, challenge: bytes, origins: frozenset[str] | set[str]) -> None:
    if len(raw) > MAX_CLIENT_DATA:
        raise _refuse("the browser's part is too long")
    try:
        cd = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise _refuse("the browser's part is not readable") from None
    if not isinstance(cd, dict):
        raise _refuse("the browser's part is not readable")
    if cd.get("type") != kind:
        raise _refuse("it is the answer to a different kind of request")
    got = b64url_decode(cd.get("challenge"), what="the challenge", limit=256)
    if not hmac.compare_digest(got, challenge):
        raise _refuse("it answers an old or a different request", "Try again.")
    origin = cd.get("origin")
    if not isinstance(origin, str) or origin not in origins:
        raise _refuse(
            "it was made on another web address",
            "Open the dashboards with their own address (not through another site), then try again.",
        )
    if cd.get("crossOrigin", False) is not False:
        raise _refuse("it was made inside another site's page")


def _credential_parts(credential: Any, response_keys: tuple[str, ...]) -> tuple[bytes, dict[str, Any]]:
    if not isinstance(credential, dict) or credential.get("type") != "public-key":
        raise _refuse("it is not a passkey answer")
    raw_id = b64url_decode(credential.get("rawId"), what="the credential id", limit=MAX_CREDENTIAL_ID)
    if not raw_id or credential.get("id") != b64url_encode(raw_id):
        raise _refuse("its credential id is inconsistent")
    resp = credential.get("response")
    if not isinstance(resp, dict):
        raise _refuse("it has no response")
    for k in response_keys:
        if k not in resp:
            raise _refuse(f"it has no {k}")
    return raw_id, resp


# ---------------------------------------------------------------- registration


@dataclass(frozen=True)
class Registration:
    credential_id: bytes
    public_key: bytes
    alg: int
    sign_count: int
    aaguid: str
    backup_eligible: bool
    backed_up: bool
    transports: tuple[str, ...] = field(default_factory=tuple)


def verify_registration(
    credential: Any,
    *,
    challenge: bytes,
    origins: frozenset[str] | set[str],
    rp_id: str,
    algorithms: tuple[int, ...] = ALGORITHMS,
) -> Registration:
    raw_id, resp = _credential_parts(credential, ("clientDataJSON", "attestationObject"))
    cdj = b64url_decode(resp["clientDataJSON"], what="the browser's part", limit=MAX_CLIENT_DATA)
    _client_data(cdj, kind="webauthn.create", challenge=challenge, origins=origins)
    att = cbor_decode(b64url_decode(resp["attestationObject"], what="the attestation", limit=MAX_ATTESTATION))
    if not isinstance(att, dict) or set(att) != {"fmt", "attStmt", "authData"}:
        raise _refuse("the attestation is not in the expected form")
    if att["fmt"] != "none" or att["attStmt"] != {}:
        raise _refuse(
            "the device sent attestation information easy-ui does not use",
            "Try again; if your browser asks whether to share information about the device, say no.",
        )
    if not isinstance(att["authData"], bytes):
        raise _refuse("the authenticator data is missing")
    ad = parse_auth_data(att["authData"])
    _check_flags(ad, rp_id)
    if not ad.flags & FLAG_AT or ad.credential_id is None or ad.public_key is None:
        raise _refuse("it does not contain a new credential")
    if not hmac.compare_digest(ad.credential_id, raw_id):
        raise _refuse("its credential id does not match the one the browser named")
    alg, _pub = parse_cose_key(ad.public_key)
    if alg not in algorithms:
        raise _refuse("it uses a signature algorithm easy-ui did not ask for")
    raw_transports = resp.get("transports", [])
    transports = tuple(
        t for t in (raw_transports if isinstance(raw_transports, list) else []) if isinstance(t, str) and t in _TRANSPORTS
    )[:6]
    return Registration(
        credential_id=raw_id,
        public_key=ad.public_key,
        alg=alg,
        sign_count=ad.sign_count,
        aaguid=(ad.aaguid or b"").hex(),
        backup_eligible=ad.backup_eligible,
        backed_up=ad.backed_up,
        transports=transports,
    )


# ---------------------------------------------------------------- sign-in


@dataclass(frozen=True)
class Assertion:
    credential_id: bytes
    sign_count: int
    backed_up: bool
    user_handle: bytes | None


def credential_id_of(credential: Any) -> bytes:
    raw_id, _ = _credential_parts(credential, ("clientDataJSON", "authenticatorData", "signature"))
    return raw_id


def verify_assertion(
    credential: Any,
    *,
    challenge: bytes,
    origins: frozenset[str] | set[str],
    rp_id: str,
    public_key: bytes,
    alg: int,
    stored_sign_count: int,
    user_id: bytes,
) -> Assertion:
    raw_id, resp = _credential_parts(credential, ("clientDataJSON", "authenticatorData", "signature"))
    cdj = b64url_decode(resp["clientDataJSON"], what="the browser's part", limit=MAX_CLIENT_DATA)
    _client_data(cdj, kind="webauthn.get", challenge=challenge, origins=origins)
    raw_ad = b64url_decode(resp["authenticatorData"], what="the authenticator data", limit=MAX_AUTH_DATA)
    ad = parse_auth_data(raw_ad)
    _check_flags(ad, rp_id)
    if ad.flags & FLAG_AT:
        raise _refuse("it unexpectedly contains a new credential")
    handle = resp.get("userHandle")
    user_handle = None
    if handle not in (None, ""):
        user_handle = b64url_decode(handle, what="the user handle", limit=64)
        if not hmac.compare_digest(user_handle, user_id):
            raise _refuse("the passkey belongs to someone else")
    sig = b64url_decode(resp["signature"], what="the signature", limit=MAX_SIGNATURE)
    verify_signature(alg, public_key, sig, raw_ad + hashlib.sha256(cdj).digest())
    if (ad.sign_count or stored_sign_count) and ad.sign_count <= stored_sign_count:
        raise _refuse(
            "its use counter went backwards, which can mean the passkey was copied",
            "Sign in another way, remove this passkey on the sign-in settings page and add it again.",
        )
    return Assertion(raw_id, ad.sign_count, ad.backed_up, user_handle)

"""A software passkey authenticator for the tests, and a small CBOR encoder.

It produces exactly what a browser hands the page after ``navigator.credentials.create`` /
``.get`` (in the JSON form ``signin.js`` sends: base64url strings), so the server's
verification can be tested end to end with every algorithm, and every field can be
tampered with. Keys are generated fresh in each test; nothing here is a real credential.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa

from easyui.webauthn import EDDSA, ES256, RS256, b64url_decode, b64url_encode


def _head(major: int, n: int) -> bytes:
    if n < 24:
        return bytes([major << 5 | n])
    if n < 256:
        return bytes([major << 5 | 24, n])
    if n < 65536:
        return bytes([major << 5 | 25]) + struct.pack(">H", n)
    if n < 2**32:
        return bytes([major << 5 | 26]) + struct.pack(">I", n)
    return bytes([major << 5 | 27]) + struct.pack(">Q", n)


def cbor(v: Any) -> bytes:
    if v is False:
        return b"\xf4"
    if v is True:
        return b"\xf5"
    if v is None:
        return b"\xf6"
    if isinstance(v, int):
        return _head(0, v) if v >= 0 else _head(1, -1 - v)
    if isinstance(v, bytes):
        return _head(2, len(v)) + v
    if isinstance(v, str):
        b = v.encode()
        return _head(3, len(b)) + b
    if isinstance(v, list):
        return _head(4, len(v)) + b"".join(cbor(x) for x in v)
    if isinstance(v, dict):
        return _head(5, len(v)) + b"".join(cbor(k) + cbor(x) for k, x in v.items())
    raise TypeError(type(v))


class SoftAuthenticator:
    def __init__(self, alg: int = ES256, *, sign_count: int | None = 0, uv: bool = True, up: bool = True) -> None:
        self.alg = alg
        self.credential_id = os.urandom(32)
        self.count = sign_count  # None: a passkey that keeps no counter (always 0)
        self.uv, self.up = uv, up
        self.user_handle: bytes | None = None
        if alg == ES256:
            self.key = ec.generate_private_key(ec.SECP256R1())
        elif alg == EDDSA:
            self.key = ed25519.Ed25519PrivateKey.generate()
        elif alg == RS256:
            self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        else:
            raise ValueError(alg)

    def cose_key(self) -> dict[int, Any]:
        pub = self.key.public_key()
        if self.alg == ES256:
            n = pub.public_numbers()
            return {1: 2, 3: ES256, -1: 1, -2: n.x.to_bytes(32, "big"), -3: n.y.to_bytes(32, "big")}
        if self.alg == EDDSA:
            raw = pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
            return {1: 1, 3: EDDSA, -1: 6, -2: raw}
        n = pub.public_numbers()
        return {1: 3, 3: RS256, -1: n.n.to_bytes((n.n.bit_length() + 7) // 8, "big"), -2: n.e.to_bytes(3, "big")}

    def flags(self, *, at: bool = False, be: bool = True, bs: bool = True) -> int:
        return (0x01 if self.up else 0) | (0x04 if self.uv else 0) | (0x08 if be else 0) | (0x10 if bs else 0) | (0x40 if at else 0)

    def _count(self) -> int:
        if self.count is None:
            return 0
        self.count += 1
        return self.count

    @staticmethod
    def client_data(kind: str, challenge: str, origin: str, **extra: Any) -> bytes:
        return json.dumps({"type": kind, "challenge": challenge, "origin": origin, "crossOrigin": False, **extra}).encode()

    def register(
        self,
        options: dict[str, Any],
        origin: str,
        *,
        rp_id: str | None = None,
        fmt: str = "none",
        att_stmt: dict | None = None,
        cose: dict | None = None,
        client_extra: dict | None = None,
        auth_data_tail: bytes = b"",
        credential_id: bytes | None = None,
        flags: int | None = None,
    ) -> dict[str, Any]:
        pk = options["publicKey"]
        rp = rp_id or pk["rp"]["id"]
        self.user_handle = b64url_decode(pk["user"]["id"], what="user")
        cid = credential_id if credential_id is not None else self.credential_id
        attested = os.urandom(16) + struct.pack(">H", len(cid)) + cid + cbor(cose or self.cose_key())
        ad = (
            hashlib.sha256(rp.encode()).digest()
            + bytes([flags if flags is not None else self.flags(at=True)])
            + struct.pack(">I", 0 if self.count is None else self.count)
            + attested
            + auth_data_tail
        )
        att = cbor({"fmt": fmt, "attStmt": att_stmt or {}, "authData": ad})
        cd = self.client_data("webauthn.create", pk["challenge"], origin, **(client_extra or {}))
        return {
            "id": b64url_encode(self.credential_id),
            "rawId": b64url_encode(self.credential_id),
            "type": "public-key",
            "response": {"clientDataJSON": b64url_encode(cd), "attestationObject": b64url_encode(att), "transports": ["internal"]},
        }

    def sign(self, data: bytes) -> bytes:
        if self.alg == ES256:
            return self.key.sign(data, ec.ECDSA(hashes.SHA256()))
        if self.alg == EDDSA:
            return self.key.sign(data)
        return self.key.sign(data, padding.PKCS1v15(), hashes.SHA256())

    def login(
        self,
        options: dict[str, Any],
        origin: str,
        *,
        rp_id: str | None = None,
        client_extra: dict | None = None,
        kind: str = "webauthn.get",
        flags: int | None = None,
        count: int | None = None,
        user_handle: bytes | None | str = "same",
        bad_signature: bool = False,
    ) -> dict[str, Any]:
        pk = options["publicKey"]
        rp = rp_id or pk["rpId"]
        n = self._count() if count is None else count
        ad = hashlib.sha256(rp.encode()).digest() + bytes([flags if flags is not None else self.flags()]) + struct.pack(">I", n)
        cd = self.client_data(kind, pk["challenge"], origin, **(client_extra or {}))
        sig = self.sign(ad + hashlib.sha256(cd).digest())
        if bad_signature:
            sig = self.sign(ad + hashlib.sha256(cd + b" ").digest())
        handle = self.user_handle if user_handle == "same" else user_handle
        return {
            "id": b64url_encode(self.credential_id),
            "rawId": b64url_encode(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": b64url_encode(cd),
                "authenticatorData": b64url_encode(ad),
                "signature": b64url_encode(sig),
                "userHandle": b64url_encode(handle) if isinstance(handle, bytes) else handle,
            },
        }

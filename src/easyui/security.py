"""HTTP safety rules for the easy-ui server, applied to every request.

``SecurityMiddleware`` is a plain ASGI middleware that sits in front of the
whole app. In order, for each HTTP request it:

1. Checks the ``Host`` header against an allowlist (the configured host names
   plus ``localhost``, ``127.0.0.1`` and ``[::1]``, any port). This stops DNS
   rebinding: a web page on another site cannot point its own name at this
   computer and read the dashboards.
2. Works out whether the request arrived over HTTPS: the connection itself is
   TLS, or ``X-Forwarded-Proto: https`` was sent by a *trusted* peer (the local
   socket or a loopback address, which is where the private-network proxy
   connects from). From any other peer the header is ignored.
3. Allows only ``GET``, ``HEAD`` and ``POST``.
4. For every state-changing request (``POST``) requires a same-origin request:
   an ``Origin`` header equal to this server's own origin, or, when there is no
   ``Origin``, ``Sec-Fetch-Site: same-origin``. A ``Sec-Fetch-Site`` other than
   ``same-origin`` is always refused. It also requires a JSON content type, so
   a plain HTML form on another site can never post here. (The session check
   is done by the routes; the sign-in endpoints are the only ones without it.)
5. Limits the request body (default 64 KiB); a bigger body gets 413, and a body
   that has not fully arrived within ``BODY_TIMEOUT`` (10 s) gets 408.
6. Adds the security headers to every response (``security_headers``):
   a strict Content-Security-Policy, ``X-Content-Type-Options: nosniff``,
   ``Referrer-Policy: no-referrer``, ``Cross-Origin-Opener-Policy`` and
   ``Cross-Origin-Resource-Policy: same-origin``, a ``Permissions-Policy``
   denying every feature except passkeys for this site itself, ``X-Frame-Options: DENY``,
   ``Cache-Control: no-store`` on ``/api/`` responses and HSTS over HTTPS.
7. Turns any unexpected exception into a plain JSON 500 without a stack trace
   or file path (the details go to the server log only).

The result of steps 1-2 is stored in ``scope["state"]["easyui"]`` as a
``RequestInfo`` (``https``, ``host``, ``origin``), read back with
``request_info(request)``.

Public API
----------
``CSP``, ``PERMISSIONS_POLICY``, ``LOCAL_HOSTS``, ``DEFAULT_MAX_BODY``, ``BODY_TIMEOUT``
``RequestInfo`` (frozen dataclass): ``https``, ``host``, ``origin``, ``trusted_peer``.
``normalize_host(value) -> str | None``  ``"Example.org:443"`` -> ``"example.org"``; None when malformed.
``host_allowed(host_header, allowed) -> bool``
``is_trusted_peer(client) -> bool``      unix socket (no peer address) or loopback.
``is_https(scope) -> bool``
``same_origin(headers, info) -> bool``
``is_json_content_type(value) -> bool``
``security_headers(path, https) -> list[tuple[bytes, bytes]]``
``scrub(text) -> str``                   removes file paths from text sent to the browser.
``error_body(code, message, hint) -> bytes``
``SecurityMiddleware(app, *, allowed_hosts=(), max_body=DEFAULT_MAX_BODY, body_timeout=BODY_TIMEOUT)``
``request_info(request) -> RequestInfo``
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("easyui.server")

# Exactly the policy in web/index.html's meta tag, plus frame-ancestors (which a meta tag cannot set).
# tests/test_web_static.py checks that the two stay the same.
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
    "font-src 'self'; manifest-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
PERMISSIONS_POLICY = ", ".join(
    f"{feature}=()"
    for feature in (
        "accelerometer", "autoplay", "bluetooth", "camera",
        "display-capture", "encrypted-media", "fullscreen", "gamepad", "geolocation", "gyroscope", "hid",
        "idle-detection", "local-fonts", "magnetometer", "microphone", "midi", "payment",
        "picture-in-picture", "screen-wake-lock", "serial", "storage-access", "usb", "web-share", "window-management",
        "xr-spatial-tracking",
    )
) + ", publickey-credentials-create=(self), publickey-credentials-get=(self)"  # fmt: skip
# Passkeys (WebAuthn) are the one browser feature the page uses, and only for this site itself.
HSTS = "max-age=31536000"
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]"})
DEFAULT_MAX_BODY = 64 * 1024
# A request body must arrive within this many seconds, so a slow sender cannot hold a connection open.
BODY_TIMEOUT = 10.0
ALLOWED_METHODS = ("GET", "HEAD", "POST")
STATE_CHANGING = frozenset({"POST", "PUT", "PATCH", "DELETE"})

_HOSTNAME_RE = re.compile(r"^(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9-]{0,62})(?:\.[a-z0-9](?:[a-z0-9-]{0,62}))*\.?\Z")
_PORT_RE = re.compile(r"^\d{1,5}\Z")
# A Unix path or a ~/ path, not part of a URL ("https://...") or a word ("km/h").
_PATH_RE = re.compile(r"(?<![\w:/.])(?:~/|/)(?:[\w.@+-]+/)*[\w.@+-]*[\w@+-](?:/)?")


@dataclass(frozen=True)
class RequestInfo:
    https: bool
    host: str
    origin: str
    trusted_peer: bool


# ---------------------------------------------------------------- pure helpers


def normalize_host(value: str | None) -> str | None:
    """Lower-case host name from a Host-style value, without the port; None when malformed."""
    if not value or len(value) > 300:
        return None
    v = value.strip().lower()
    if v.startswith("["):
        end = v.find("]")
        if end < 0:
            return None
        host, rest = v[: end + 1], v[end + 1 :]
        try:
            ipaddress.IPv6Address(host[1:-1])
        except ValueError:
            return None
    else:
        host, sep, port = v.partition(":")
        rest = sep + port
        if not _HOSTNAME_RE.match(host):
            return None
        host = host.rstrip(".")
    if rest:
        if not rest.startswith(":") or not _PORT_RE.match(rest[1:]) or not 0 < int(rest[1:]) < 65536:
            return None
    return host


def host_allowed(host_header: str | None, allowed: Iterable[str]) -> bool:
    host = normalize_host(host_header)
    if host is None:
        return False
    return host in LOCAL_HOSTS or host in {h.lower().rstrip(".") for h in allowed}


def is_trusted_peer(client: Any) -> bool:
    """True for a local-socket peer (no address) or a loopback address."""
    if not client:
        return True
    try:
        addr = ipaddress.ip_address(str(client[0]).strip("[]"))
    except (ValueError, IndexError, TypeError):
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return addr.is_loopback


def _header(scope: dict[str, Any], name: bytes) -> str | None:
    for k, v in scope.get("headers") or ():
        if k == name:
            return v.decode("latin-1")
    return None


def is_https(scope: dict[str, Any]) -> bool:
    if scope.get("scheme") in ("https", "wss"):
        return True
    if is_trusted_peer(scope.get("client")):
        proto = _header(scope, b"x-forwarded-proto")
        if proto and proto.split(",")[0].strip().lower() == "https":
            return True
    return False


def same_origin(headers: dict[str, str], info: RequestInfo) -> bool:
    """The rule for state-changing requests (see the module docstring, step 4)."""
    site = headers.get("sec-fetch-site")
    if site is not None and site.strip().lower() != "same-origin":
        return False
    origin = headers.get("origin")
    if origin is None:
        return site is not None
    o = origin.strip().lower()
    if o == info.origin:
        return True
    # Behind a TLS proxy that does not say so, the browser's origin is https while the
    # request looks like plain http. Accept that (same host and port only), never the reverse.
    return not info.https and o == "https://" + info.origin.removeprefix("http://")


def is_json_content_type(value: str | None) -> bool:
    if not value:
        return False
    media, _, params = value.partition(";")
    if media.strip().lower() != "application/json":
        return False
    for p in params.split(";"):
        k, _, v = p.partition("=")
        if k.strip().lower() == "charset" and v.strip().strip('"').lower() not in ("utf-8", "utf8"):
            return False
    return True


def security_headers(path: str, https: bool) -> list[tuple[bytes, bytes]]:
    h = [
        (b"content-security-policy", CSP.encode()),
        (b"x-content-type-options", b"nosniff"),
        (b"referrer-policy", b"no-referrer"),
        (b"cross-origin-opener-policy", b"same-origin"),
        (b"cross-origin-resource-policy", b"same-origin"),
        (b"permissions-policy", PERMISSIONS_POLICY.encode()),
        (b"x-frame-options", b"DENY"),
    ]
    if path == "/api" or path.startswith("/api/"):
        h.append((b"cache-control", b"no-store"))
    if https:
        h.append((b"strict-transport-security", HSTS.encode()))
    return h


def scrub(text: Any) -> Any:
    """Remove file paths from a message before it is sent to the browser."""
    if not isinstance(text, str):
        return text
    return _PATH_RE.sub("(a file on the server)", text)


def error_dict(code: str, message: str, hint: str | None) -> dict[str, Any]:
    return {"error": {"code": code, "message": scrub(message), "hint": scrub(hint)}}


def error_body(code: str, message: str, hint: str | None) -> bytes:
    return json.dumps(error_dict(code, message, hint), ensure_ascii=False).encode()


def request_info(request: Any) -> RequestInfo:
    info = request.scope.get("state", {}).get("easyui")
    if info is None:  # only when the middleware is not installed (never in the real app)
        https = is_https(request.scope)
        host = normalize_host(request.headers.get("host")) or "localhost"
        info = RequestInfo(https, host, ("https://" if https else "http://") + host, is_trusted_peer(request.scope.get("client")))
    return info


# ---------------------------------------------------------------- middleware


class SecurityMiddleware:
    def __init__(
        self, app: Any, *, allowed_hosts: Iterable[str] = (), max_body: int = DEFAULT_MAX_BODY, body_timeout: float = BODY_TIMEOUT
    ) -> None:
        self.app = app
        self.body_timeout = float(body_timeout)
        self.allowed_hosts = frozenset(h.lower().rstrip(".") for h in allowed_hosts)
        self.max_body = int(max_body)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "/")
        https = is_https(scope)
        sent_start = False

        async def send_with_headers(message: dict[str, Any]) -> None:
            nonlocal sent_start
            if message["type"] == "http.response.start":
                sent_start = True
                drop = {b"server", b"x-powered-by"}
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() not in drop]
                names = {k.lower() for k, _ in headers}
                for k, v in security_headers(path, https):
                    if k == b"cache-control" and k in names:
                        headers = [(hk, hv) for hk, hv in headers if hk.lower() != b"cache-control"]
                    headers.append((k, v))
                message = {**message, "headers": headers}
            await send(message)

        async def refuse(status: int, code: str, message: str, hint: str | None, extra: list | None = None) -> None:
            body = error_body(code, message, hint)
            await send_with_headers(
                {
                    "type": "http.response.start",
                    "status": status,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                        *(extra or []),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body if scope.get("method") != "HEAD" else b""})

        host_header = _header(scope, b"host")
        if not host_allowed(host_header, self.allowed_hosts):
            await refuse(
                400,
                "unknown_host",
                "This server was reached under a name it does not answer to, so it refused the request. "
                "This protects your dashboards from other web sites that try to reach them.",
                "Open the dashboards with the address easy-ui gave you (for example http://localhost:PORT). "
                "If you use a new name for this server, add it with easy-ui serve --allow-host NAME.",
            )
            return
        host = normalize_host(host_header) or "localhost"
        port = ""
        raw = (host_header or "").strip().lower()
        if raw.rsplit("]", 1)[-1].count(":") == 1:
            port = ":" + raw.rsplit(":", 1)[1]
        info = RequestInfo(https, host + port, ("https://" if https else "http://") + host + port, is_trusted_peer(scope.get("client")))
        scope.setdefault("state", {})["easyui"] = info

        method = scope.get("method", "GET").upper()
        if method not in ALLOWED_METHODS:
            await refuse(
                405,
                "method_not_allowed",
                f"easy-ui does not accept {method[:16]} requests.",
                "Only reading pages and sending dashboard requests from the easy-ui page are supported.",
                [(b"allow", ", ".join(ALLOWED_METHODS).encode())],
            )
            return

        if method in STATE_CHANGING:
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or ()}
            if not same_origin(headers, info):
                await refuse(
                    403,
                    "cross_site",
                    "This request did not come from the easy-ui page itself, so it was refused. This stops other "
                    "web sites from acting on your dashboards in your name.",
                    "Use the dashboards page directly. If you saw this on the dashboards page, reload it and try again.",
                )
                return
            if not is_json_content_type(headers.get("content-type")):
                await refuse(
                    415,
                    "json_required",
                    "This request was not sent as JSON, so it was refused.",
                    "Send the request from the easy-ui page, or with the header Content-Type: application/json.",
                )
                return
            length = headers.get("content-length")
            if length is not None:
                if not length.strip().isdigit():
                    await refuse(400, "bad_request", "The request size was not understood.", "Send the request again.")
                    return
                if int(length) > self.max_body:
                    await refuse(
                        413,
                        "too_large",
                        "The request was too big, so easy-ui did not read it.",
                        f"Requests may be at most {self.max_body // 1024} KiB. Ask for fewer things at once.",
                    )
                    return
            chunks: list[bytes] = []
            size = 0
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.body_timeout
            while True:
                try:
                    message = await asyncio.wait_for(receive(), timeout=max(0.0, deadline - loop.time()))
                except TimeoutError:
                    await refuse(
                        408,
                        "too_slow",
                        "The request arrived too slowly, so easy-ui stopped waiting for it.",
                        "Check the network connection, then try again.",
                        [(b"connection", b"close")],
                    )
                    return
                if message["type"] == "http.disconnect":
                    return
                if message["type"] != "http.request":
                    continue
                chunk = message.get("body", b"")
                size += len(chunk)
                if size > self.max_body:
                    await refuse(
                        413,
                        "too_large",
                        "The request was too big, so easy-ui did not read it.",
                        f"Requests may be at most {self.max_body // 1024} KiB. Ask for fewer things at once.",
                    )
                    return
                chunks.append(chunk)
                if not message.get("more_body", False):
                    break
            body = b"".join(chunks)
            replayed = False

            async def app_receive() -> dict[str, Any]:
                nonlocal replayed
                if not replayed:
                    replayed = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()
        else:
            app_receive = receive

        try:
            await self.app(scope, app_receive, send_with_headers)
        except Exception:
            if sent_start:
                return  # the app already answered (and logged) this error
            log.exception("unexpected error while answering %s %s", method, path[:200])
            await refuse(
                500,
                "internal",
                "Something went wrong inside easy-ui while answering, so this part could not be shown.",
                "Try again in a moment. If it keeps happening, run easy-ui check on the server and look at its log.",
            )

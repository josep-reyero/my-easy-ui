"""HTTP safety rules for the easy-ui server, applied to every request.

The web addresses the server answers to are *configured*, never taken from the
request (``Origins``):

- With one or more public origins (``easy-ui serve --origin https://NAME[:PORT]``,
  ``EASYUI_ORIGINS``, or the shorthand ``--allow-host NAME`` / ``EASYUI_ALLOW_HOSTS``
  for ``https://NAME``), exactly those origins (scheme, host and port) are accepted,
  and nothing else: not ``localhost``, not ``127.0.0.1``, not another port.
- Without any, and only when the server listens on ``127.0.0.1:PORT``, the local
  origins ``http://localhost:PORT``, ``http://127.0.0.1:PORT`` and ``http://[::1]:PORT``,
  with exactly the port it listens on. Passkeys only work on the first one (an IP
  address cannot be a passkey's site).

``SecurityMiddleware`` is a plain ASGI middleware that sits in front of the
whole app. In order, for each HTTP request it:

1. Refuses a request that has more than one ``Host``, ``Origin``,
   ``Sec-Fetch-Site`` or ``Content-Type`` header (each part of easy-ui must read
   the same value), and, on a local (unix) socket, more than one
   ``X-Forwarded-Host``.
2. Works out the request's *host*: the ``Host`` header, except when the server
   listens on a local (unix) socket (``unix_socket=True``, decided from the
   listening socket's family, never from the request) and the request has an
   ``X-Forwarded-Host`` header: then that header's single value. A proxy that
   forwards to a socket names the socket's own address in ``Host``
   (``tailscale serve`` sends ``Host: localhost``) and the name the browser
   used in ``X-Forwarded-Host``, replacing any value the browser sent.
   Whoever can connect to the socket is trusted to state the host, so the
   socket must be reachable only by the proxy (``serve --socket`` makes it
   owner-only; the systemd unit makes it root's, mode 0600). Over TCP
   ``X-Forwarded-Host`` is never read: there ``Host`` alone decides. A
   malformed ``X-Forwarded-Host`` (a list, a path, a bad name) is refused. The
   host (name *and* port) is matched against the accepted origins; a request
   under any other name or port is refused. This stops DNS rebinding, and pages
   on other ports of the same computer. When it came from ``X-Forwarded-Host``,
   the ``Host`` header the app sees is replaced by it, so every later part
   reads the same host.
3. Works out whether the page is on HTTPS (for the cookie flags and HSTS) from
   the configured origin the request matched: ``https://NAME`` means HTTPS,
   ``http://...`` means plain http. No request header (``X-Forwarded-Proto``
   included) changes it, so a proxy that forgets the header, or a client that
   sends it, cannot make the cookies lose ``Secure`` or change their names.
   Only when both ``http://NAME`` and ``https://NAME`` are configured does the
   connection itself (TLS or not) pick one.
4. Allows only ``GET``, ``HEAD`` and ``POST``.
5. For every state-changing request (``POST``) requires a same-origin request:
   an ``Origin`` header exactly equal to the matched configured origin, or, when
   there is no ``Origin``, ``Sec-Fetch-Site: same-origin``. A ``Sec-Fetch-Site``
   other than ``same-origin`` is always refused. It also requires a JSON content
   type, so a plain HTML form on another site can never post here. (The session
   check is done by the routes; the sign-in endpoints are the only ones without it.)
6. Limits the request body (default 64 KiB); a bigger body gets 413, and a body
   that has not fully arrived within ``BODY_TIMEOUT`` (10 s) gets 408.
7. Adds the security headers to every response (``security_headers``):
   a strict Content-Security-Policy, ``X-Content-Type-Options: nosniff``,
   ``Referrer-Policy: no-referrer``, ``Cross-Origin-Opener-Policy`` and
   ``Cross-Origin-Resource-Policy: same-origin``, a ``Permissions-Policy``
   denying every feature except passkeys for this site itself, ``X-Frame-Options: DENY``,
   ``Cache-Control: no-store`` on ``/api/`` responses and HSTS over HTTPS.
8. Turns any unexpected exception into a plain JSON 500 without a stack trace
   or file path (the details go to the server log only).

The result of steps 2-3 is stored in ``scope["state"]["easyui"]`` as a
``RequestInfo`` (``https``, ``host``, ``origin``, ``site``), read back with
``request_info(request)``.

Public API
----------
``CSP``, ``PERMISSIONS_POLICY``, ``LOCAL_HOSTS``, ``DEFAULT_MAX_BODY``, ``BODY_TIMEOUT``
``Origin`` (frozen dataclass): ``scheme``, ``host``, ``port``; ``str(o)`` is the browser's
    serialisation; ``rp_id`` is the passkey site for it (None when passkeys cannot work there).
``parse_origin(text) -> Origin``          raises ``ValueError`` with a plain message.
``origin_for_host(name) -> Origin``       the ``--allow-host`` shorthand: ``https://NAME``.
``Origins(public=(), *, local_port=None)``  ``accepted``, ``public``, ``match(host_header, https)``,
    ``rp_ids()``, ``passkey_origins(rp_id)``.
``RequestInfo`` (frozen dataclass): ``https``, ``host``, ``origin``, ``trusted_peer``, ``site``.
``split_host(value) -> (host, port | None) | None``
``effective_host(headers, unix_socket) -> str | None``  the host a request names (step 2); raises ``ValueError``
    for a malformed or repeated ``X-Forwarded-Host`` on a unix socket.
``normalize_host(value) -> str | None``  ``"Example.org:443"`` -> ``"example.org"``; None when malformed.
``is_trusted_peer(client) -> bool``      unix socket (no peer address) or loopback.
``is_https(scope) -> bool``               the connection itself is TLS (headers are never read).
``same_origin(headers, info) -> bool``
``is_json_content_type(value) -> bool``
``security_headers(path, https) -> list[tuple[bytes, bytes]]``
``scrub(text) -> str``                   removes file paths from text sent to the browser.
``error_body(code, message, hint) -> bytes``
``SecurityMiddleware(app, *, origins=Origins(), max_body=DEFAULT_MAX_BODY, body_timeout=BODY_TIMEOUT, unix_socket=False)``
    ``unix_socket``: the server listens on a local (unix) socket, so ``X-Forwarded-Host`` names the host.
``request_info(request) -> RequestInfo``
``verify_vendored(web_dir) -> None``     checks the third-party page files (ECharts) against the hashes in
    ``assets/vendor/MANIFEST.json``; raises ``Refused`` (code ``vendor_changed``) when one differs or is missing.
    ``serve``, ``preview`` and ``export`` call it before they start.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
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
# Headers that must appear at most once: every part of easy-ui has to read the same value.
_SINGLE_HEADERS = (b"host", b"origin", b"sec-fetch-site", b"content-type")
FORWARDED_HOST = b"x-forwarded-host"  # read only on a local (unix) socket; see effective_host
_MAX_LOGGED_HOSTS = 20  # unknown names noted in the log, so a scanner cannot fill it
# A Unix path or a ~/ path, not part of a URL ("https://...") or a word ("km/h").
_PATH_RE = re.compile(r"(?<![\w:/.])(?:~/|/)(?:[\w.@+-]+/)*[\w.@+-]*[\w@+-](?:/)?")


@dataclass(frozen=True)
class Origin:
    """One exact web address: scheme, host name (lower case, IPv6 in brackets) and port."""

    scheme: str
    host: str
    port: int

    @property
    def default_port(self) -> bool:
        return self.port == (443 if self.scheme == "https" else 80)

    def __str__(self) -> str:
        return f"{self.scheme}://{self.host}" + ("" if self.default_port else f":{self.port}")

    @property
    def host_port(self) -> str:
        return self.host + ("" if self.default_port else f":{self.port}")

    @property
    def is_ip(self) -> bool:
        try:
            ipaddress.ip_address(self.host.strip("[]"))
        except ValueError:
            return False
        return True

    @property
    def rp_id(self) -> str | None:
        """The passkey site (RP ID) for this address: its host name, over https or on http://localhost only."""
        if self.is_ip:
            return None
        if self.scheme == "https" or self.host == "localhost":
            return self.host
        return None


@dataclass(frozen=True)
class RequestInfo:
    https: bool
    host: str
    origin: str
    trusted_peer: bool
    site: Origin | None = None


# ---------------------------------------------------------------- pure helpers


def split_host(value: str | None) -> tuple[str, int | None] | None:
    """``(host, port)`` from a Host-style value (port None when not given); None when malformed."""
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
    if not rest:
        return host, None
    if not rest.startswith(":") or not _PORT_RE.match(rest[1:]) or not 0 < int(rest[1:]) < 65536:
        return None
    return host, int(rest[1:])


def normalize_host(value: str | None) -> str | None:
    """Lower-case host name from a Host-style value, without the port; None when malformed."""
    parts = split_host(value)
    return parts[0] if parts else None


def _bad_origin(text: Any, why: str) -> ValueError:
    return ValueError(f"{str(text)[:80]!r} is not a web address easy-ui can use ({why}).")


def parse_origin(text: Any) -> Origin:
    """``https://name[:port]`` or ``http://name[:port]`` (a trailing ``/`` is allowed); nothing else."""
    if not isinstance(text, str) or not text or len(text) > 300:
        raise _bad_origin(text, "it is empty or too long")
    t = text.strip()
    scheme, sep, rest = t.partition("://")
    scheme = scheme.lower()
    if not sep or scheme not in ("http", "https"):
        raise _bad_origin(text, "it must start with https:// or http://")
    rest = rest.removesuffix("/")
    if not rest or any(c in rest for c in "/?#@\\ \t\r\n%"):
        raise _bad_origin(text, "give only the scheme, the name and maybe a port, without a path")
    parts = split_host(rest)
    if parts is None or not parts[0]:
        raise _bad_origin(text, "the name or the port is not valid")
    host, port = parts
    return Origin(scheme, host, port if port is not None else (443 if scheme == "https" else 80))


def origin_for_host(name: Any) -> Origin:
    """The ``--allow-host NAME`` shorthand: ``https://NAME`` on the default port."""
    if not isinstance(name, str):
        raise _bad_origin(name, "it is not text")
    parts = split_host(name)
    if parts is None or parts[1] is not None or not parts[0]:
        raise _bad_origin(name, "give a host name without a port, or use --origin https://NAME:PORT")
    return Origin("https", parts[0], 443)


class Origins:
    """The exact web addresses this server answers to (see the module docstring)."""

    def __init__(self, public: Iterable[Origin | str] = (), *, local_port: int | None = None) -> None:
        self.public: tuple[Origin, ...] = tuple(
            dict.fromkeys(o if isinstance(o, Origin) else parse_origin(o) for o in public)
        )
        self.local_port = int(local_port) if local_port else None
        if self.public:
            self.accepted = self.public
        elif self.local_port:
            self.accepted = tuple(Origin("http", h, self.local_port) for h in ("localhost", "127.0.0.1", "[::1]"))
        else:
            self.accepted = ()

    def match(self, host_header: str | None, https: bool = False) -> Origin | None:
        """The accepted origin a request's Host header names (name and port), or None."""
        parts = split_host(host_header)
        if parts is None:
            return None
        host, port = parts
        found = [o for o in self.accepted if o.host == host and (o.port == port if port is not None else o.default_port)]
        if len(found) > 1:  # http://NAME and https://NAME both configured: the connection decides
            found = [o for o in found if (o.scheme == "https") == https] or found
        return found[0] if found else None

    def rp_ids(self) -> frozenset[str]:
        return frozenset(o.rp_id for o in self.accepted if o.rp_id)

    def passkey_origins(self, rp_id: str) -> frozenset[str]:
        """The configured origins a passkey answer for ``rp_id`` may come from."""
        return frozenset(str(o) for o in self.accepted if o.rp_id == rp_id)

    def describe(self) -> str:
        return ", ".join(str(o) for o in self.accepted) or "no address"


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


def effective_host(headers: Iterable[tuple[bytes, bytes]], unix_socket: bool) -> str | None:
    """The host a request names: its ``X-Forwarded-Host`` when the server listens on a local (unix) socket and
    the header is there, else its ``Host`` header (None when it has none). On a unix socket an
    ``X-Forwarded-Host`` that is repeated or is not one ``name[:port]`` raises ``ValueError``. Over TCP
    ``X-Forwarded-Host`` is never read."""
    headers = list(headers)
    host = next((v.decode("latin-1") for k, v in headers if k == b"host"), None)
    if not unix_socket:
        return host
    forwarded = [v.decode("latin-1") for k, v in headers if k == FORWARDED_HOST]
    if not forwarded:
        return host
    if len(forwarded) > 1:
        raise ValueError("more than one X-Forwarded-Host header")
    if split_host(forwarded[0]) is None:
        raise ValueError("an X-Forwarded-Host header that is not one name and port")
    return forwarded[0].strip()


def is_https(scope: dict[str, Any]) -> bool:
    return scope.get("scheme") in ("https", "wss")


def same_origin(headers: dict[str, str], info: RequestInfo) -> bool:
    """The rule for state-changing requests (see the module docstring, step 5)."""
    site = headers.get("sec-fetch-site")
    if site is not None and site.strip().lower() != "same-origin":
        return False
    origin = headers.get("origin")
    if origin is None:
        return site is not None
    # Exactly the configured origin this request was matched to: scheme, name and port.
    return bool(info.origin) and origin.strip().lower() == info.origin


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
    if info is None:  # only when the middleware is not installed (never in the real app): match nothing
        info = RequestInfo(is_https(request.scope), "", "", is_trusted_peer(request.scope.get("client")), None)
    return info


# ---------------------------------------------------------------- middleware


class SecurityMiddleware:
    def __init__(
        self,
        app: Any,
        *,
        origins: Origins | None = None,
        max_body: int = DEFAULT_MAX_BODY,
        body_timeout: float = BODY_TIMEOUT,
        unix_socket: bool = False,
    ) -> None:
        self.app = app
        self.body_timeout = float(body_timeout)
        self.origins = origins if origins is not None else Origins()
        self.max_body = int(max_body)
        self.unix_socket = bool(unix_socket)
        self._logged_hosts: set[str] = set()

    def _note_unknown(self, host: str | None, forwarded: bool) -> None:
        name = host if host is not None and split_host(host) is not None else "(no valid name)"
        if name in self._logged_hosts or len(self._logged_hosts) >= _MAX_LOGGED_HOSTS:
            return
        self._logged_hosts.add(name)
        log.warning(
            "refused a request for %s (from %s, on a %s); this server answers only to %s",
            name[:300], "X-Forwarded-Host" if forwarded else "the Host header",
            "local socket" if self.unix_socket else "TCP port", self.origins.describe(),
        )

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "/")
        https = is_https(scope)  # until the request has matched a configured origin, which then decides
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

        raw_headers = list(scope.get("headers") or ())
        single = _SINGLE_HEADERS + ((FORWARDED_HOST,) if self.unix_socket else ())
        for name in single:
            if sum(1 for k, _ in raw_headers if k == name) > 1:
                await refuse(
                    400,
                    "bad_request",
                    f"The request had more than one {name.decode().title()} header, so it was refused.",
                    "Send the request again from the dashboards page.",
                )
                return
        try:
            host_header = effective_host(raw_headers, self.unix_socket)
        except ValueError:
            await refuse(
                400,
                "bad_request",
                "The request's X-Forwarded-Host header was not one web address name, so it was refused.",
                "A proxy in front of easy-ui sends the single name the browser used, for example "
                "X-Forwarded-Host: myserver.example.ts.net.",
            )
            return
        forwarded = self.unix_socket and any(k == FORWARDED_HOST for k, _ in raw_headers)
        site = self.origins.match(host_header, https)
        if site is None:
            self._note_unknown(host_header, forwarded)
            parts = split_host(host_header)
            seen = (parts[0] + (f":{parts[1]}" if parts[1] is not None else "")) if parts else None
            await refuse(
                400,
                "unknown_host",
                "This server was reached under a name or port it does not answer to"
                + (f" ({seen})" if seen else "")
                + ", so it refused the request. This protects your dashboards from other web sites that try to "
                "reach them.",
                "Open the dashboards with the address easy-ui gave you. If this server has a new address, start it "
                "with easy-ui serve --origin https://NAME (or --allow-host NAME). Behind a proxy, the proxy must pass "
                "on the name the browser used: in the Host header over a TCP port, or in Host or X-Forwarded-Host on "
                "a local socket (tailscale serve does this). The server's log names the address it answers to.",
            )
            return
        if forwarded:  # every later part reads the host from the Host header: give it the one that was matched
            scope["headers"] = [(k, v) for k, v in raw_headers if k not in (b"host", FORWARDED_HOST)] + [
                (b"host", host_header.encode("latin-1"))
            ]
        https = site.scheme == "https"
        info = RequestInfo(https, site.host_port, str(site), is_trusted_peer(scope.get("client")), site)
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


# ---------------------------------------------------------------- the third-party page files


def verify_vendored(web_dir: Path) -> None:
    from easyui.errors import Refused  # noqa: PLC0415

    vendor = Path(web_dir) / "assets" / "vendor"
    try:
        manifest = json.loads((vendor / "MANIFEST.json").read_text(encoding="utf-8"))
        files = manifest["files"]
    except (OSError, ValueError, KeyError, TypeError):
        files = None
    bad = []
    for name, info in (files or {}).items():
        try:
            digest = hashlib.sha256((vendor / name).read_bytes()).hexdigest()
        except OSError:
            digest = None
        if not isinstance(info, dict) or digest != info.get("sha256"):
            bad.append(name)
    if files is None or bad:
        raise Refused(
            "A third-party file of the dashboard page (" + (", ".join(bad) or "its list, MANIFEST.json")
            + ") is not the one easy-ui was shipped with, so it was not used.",
            hint="Reinstall my-easy-ui (for example: uv sync). Do not edit the files in easyui/web/assets/vendor/.",
            code="vendor_changed",
        )

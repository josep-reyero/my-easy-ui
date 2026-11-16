"""The easy-ui dashboard server (one process serves one app) and its commands.

Commands registered here (see ``easyui.cli`` for the contract):

- ``easy-ui serve``: run the server. It listens on a local socket
  (``--socket PATH``), on ``127.0.0.1:PORT`` for local use (``--port``, default
  8765; ``--bind`` may name another loopback address), or on the socket systemd
  passes in (``LISTEN_FDS``, socket activation). Any address that is not on
  this computer is refused with a plain explanation: the dashboards hold
  private data and are reached from other devices only through the private
  network's proxy. Started by systemd, it stops by itself after
  ``--idle-minutes`` (default 10) without use; only signed-in requests other
  than ``/api/rev`` count as use, so an open tab that only polls for changes
  does not keep it running (systemd starts it again on the next visit).
  Started by hand it runs until stopped (default ``--idle-minutes 0``). Ctrl-C
  and SIGTERM stop it gracefully and it still prints its result (with
  ``--json``: ``{"stopped", "app", "listened_on"}``). Connections must send a
  request's headers within ``HEADER_TIMEOUT`` (10 s) and its body within
  ``security.BODY_TIMEOUT`` (10 s), so slow clients cannot hold the
  connection limit. It refuses to run as root.
- ``easy-ui set-password``: the person types the sign-in password at a hidden
  prompt (see ``easyui.auth``).
- ``easy-ui check``: a checklist, from inside this configuration, that the
  process is not root, the state folder is owner-only, a password is set, plus
  advice on data, published dashboards and the browser page:
  ``{"ok", "items": [{"id", "title", "status", "why", "fix"}]}`` with status
  ``ok`` | ``fail`` | ``warn``; ``ok`` is false when any item fails (exit 1).

HTTP API (JSON; every route except session, login, logout, recovery, passkeys/options with purpose
"login" and passkeys/login needs a session)
-----------------------------------------------------------------------------------------------------
``GET /api/session``          ``{signed_in, app: {name, title, accent}, sign_in: {password, passkeys,
                              passkeys_here, recovery}}`` (``sign_in`` is left out in a preview, which
                              has ``preview: true``).
``POST /api/login``           ``{password}`` -> ``{signed_in: true, app}`` and the session cookie. 403
                              ``password_off`` when password sign-in is switched off; 429 when throttled.
``POST /api/recovery``        ``{code}`` -> signed in; the code is used up. Throttled in its own buckets.
``POST /api/passkeys/options`` ``{purpose: "login" | "register"}`` -> WebAuthn options (base64url).
                              Login also sets the short-lived passkey cookie its challenge is bound to;
                              register needs a recent sign-in (``sign_in_again`` otherwise).
``POST /api/passkeys/login``  ``{credential}`` -> signed in. Never throttled. Refused: ``unknown_passkey``,
                              ``other_address``, ``passkey_stale``, ``passkey_replayed``, ``passkey_refused``.
``POST /api/passkeys/register`` ``{credential, name}`` -> the new passkey (recent sign-in).
``POST /api/passkeys/remove`` ``{id}`` (recent sign-in); removing the last one turns the password back on.
``GET /api/signin``           the sign-in settings: passkeys (id, name, created, last used), password
                              on/off, recovery codes left.
``POST /api/signin/password`` ``{on}`` (recent sign-in; off only while a passkey exists).
``POST /api/recovery-codes``  ``{}`` -> ten new one-time codes, shown once (recent sign-in).
Passkeys work on ``http://localhost`` and over HTTPS; the RP ID is the host the browser used (an IP
address cannot be one), and the origin must match the request's own host.
``POST /api/logout``          ``{signed_in: false}``; the session is removed.
``GET /api/dashboards``       ``[{slug, title, description}]`` (published dashboards only), in the order of
                              ``[ui] dashboards`` in project.toml, then the others by name.
``GET /api/dashboards/{slug}`` the compiled dashboard (no SQL).
``POST /api/query``           ``{dashboard, queries: [names], params}`` ->
                              ``{results: {name: {columns, rows, truncated}}}``. A query that fails
                              gives ``{error: {code, message, hint}}`` in its own slot, so one broken
                              chart does not blank the page. All the queries of one request share
                              ``request_time_s`` (4 s); each one also has the sandbox's own limit (2 s).
``GET /api/rev``              ``{rev}``; changes when the data or the published dashboards change.
Errors are ``{"error": {"code", "message", "hint"}}`` with a fitting HTTP status (429 adds
``Retry-After``). Messages never contain stack traces or file paths.

The static page is served from ``easyui/web/``: ``/`` is ``index.html``
(``Cache-Control: no-cache``), ``/assets/...`` are the files under
``web/assets/`` (a file name with a content hash, such as ``app-3f9a1c2e.js``,
is cached for a year as immutable; others revalidate with an ETag). Only known
file types are served, never a folder listing, never anything outside those
folders. Other simple page paths (``/d/overview``) get ``index.html`` too, so
the page can use real addresses.

Cookies: the session cookie is ``easyui-<app>`` on plain local http and
``__Host-easyui-<app>`` over HTTPS (then also ``Secure``); always ``HttpOnly``,
``SameSite=Strict`` and ``Path=/``. The device token (see ``easyui.auth``)
uses the same rules with ``-device`` appended, and the passkey challenge cookie with ``-passkey``
(5 minutes).

Dashboards come from a *source* with the methods ``list()``, ``compiled(slug,
today=...)``, ``dashboard(slug)``, ``bundle_path()`` and ``rev()``. By default
it is ``easyui.publish.PublishedSource`` (the published version only); the
preview passes ``easyui.preview.WorkingSource`` together with a
``preview_token``. With a preview token, opening ``/?token=<token>`` (any page
path) signs that browser in and redirects to the same address without the
token, so the token does not stay in the address bar or the history. A token
works once: it is replaced at once and ``on_preview_link_used(new_token)`` is
called (the preview prints the new link). Preview cookies are named
``easyui-<app>-preview``, so they never replace a real server's cookie.

Public API
----------
``WEB_DIR``, ``DEFAULT_PORT``, ``DEFAULT_IDLE_MINUTES``, ``MAX_QUERIES_PER_REQUEST``
``ServerSettings`` (dataclass): ``allowed_hosts``, ``idle_seconds``, ``max_body``,
    ``max_queries``, ``query_slots``, ``limits``, ``web_dir``, ``session_idle``, ``session_absolute``,
    ``request_time_s``.
``create_app(config, settings=None, *, source=None, preview_token=None, on_preview_link_used=None, on_idle=None) -> ASGI app``
    The app has ``.easyui`` (the ``ServerState``: ``config``, ``sessions``, ``auth``, ``activity``,
    ``source``) for tests and tools.
``default_source(config)``                ``PublishedSource(config)``, or an empty source without
                                          ``easyui.publish``.
``cookie_names(app, https, preview=False) -> (session_cookie, device_cookie)``
``order_dashboards(items, order) -> list``  the ``[ui] dashboards`` order.
``guarded_protocol()``, ``uvicorn_config(app, **overrides)``, ``stop_signals()``  shared with the preview.
``check_bind_address(host) -> str``       the loopback address to bind; raises ``Refused``.
``open_listener(*, socket_path=None, port=None, bind=None, env=None) -> (socket, url_or_path, kind)``
``run_checks(config) -> dict``
``resolve_idle_minutes(value, env=None) -> float``  10 under systemd socket activation, else 0.
``register(subparsers)``
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as _dt
import errno
import hashlib
import hmac
import ipaddress
import json
import logging
import logging.config
import os
import re
import secrets
import signal
import socket
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import (
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import Mount, Route

from easyui import auth as authmod
from easyui import bundle, passkeys, paths, sandbox, security, spec, webauthn
from easyui.appconfig import AppConfig
from easyui.errors import (
    EasyUIError,
    NeedsHuman,
    NotAllowed,
    NotFound,
    ParamError,
    Refused,
    SignInRequired,
    TemporaryFailure,
    UsageError,
)
from easyui.sessions import ABSOLUTE_SECONDS, IDLE_SECONDS, Session, SessionStore

log = logging.getLogger("easyui.server")

WEB_DIR = Path(__file__).resolve().parent / "web"
DEFAULT_PORT = 8765
DEFAULT_IDLE_MINUTES = 10.0  # only when systemd started the server (it starts it again on the next visit)
HEADER_TIMEOUT = 10.0  # seconds a connection may take to send a request's headers
REQUEST_TIME_S = 4.0  # all the queries of one request share this much time
LIMIT_CONNECTIONS = 256
MAX_QUERIES_PER_REQUEST = spec.LIMITS["queries"]
HEAP_LIMIT = 256_000_000

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".map": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".txt": "text/plain; charset=utf-8",
    ".webmanifest": "application/manifest+json",
}
IMMUTABLE = "public, max-age=31536000, immutable"
REVALIDATE = "no-cache"
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z")
_HASHED_RE = re.compile(r"[.-](?=[A-Za-z0-9_]*\d)[A-Za-z0-9_]{8,64}\.[A-Za-z0-9]+\Z")
_PAGE_SEGMENT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}\Z")
_MAX_ASSET_BYTES = 8_000_000


# ---------------------------------------------------------------- settings and state


@dataclass
class ServerSettings:
    allowed_hosts: tuple[str, ...] = ()
    idle_seconds: float = DEFAULT_IDLE_MINUTES * 60
    max_body: int = security.DEFAULT_MAX_BODY
    max_queries: int = MAX_QUERIES_PER_REQUEST
    query_slots: int = 4
    limits: sandbox.Limits = sandbox.DEFAULT_LIMITS
    web_dir: Path = WEB_DIR
    session_idle: int = IDLE_SECONDS
    session_absolute: int = ABSOLUTE_SECONDS
    request_time_s: float = REQUEST_TIME_S


class Activity:
    """When the server was last used (signed-in requests other than /api/rev)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.last = clock()

    def touch(self) -> None:
        self.last = self.clock()

    def idle_for(self) -> float:
        return self.clock() - self.last


class NothingPublished:
    """The source used when the publish part of easy-ui is not installed: no dashboards."""

    preview = False

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def list(self) -> list[dict[str, Any]]:
        return []

    def dashboard(self, slug: str) -> spec.Dashboard:
        raise NotFound(
            "No dashboards have been published yet, so there is nothing to show.",
            hint="Publish them with: easy-ui publish",
            code="nothing_published",
        )

    def compiled(self, slug: str, *, today: _dt.date | None = None) -> dict[str, Any]:
        return spec.compile_dashboard(self.dashboard(slug), today=today)

    def bundle_path(self) -> Path:
        return bundle.require_current(self.config)

    def rev(self) -> str:
        return bundle.rev(self.config)


def default_source(config: AppConfig) -> Any:
    try:
        from easyui.publish import PublishedSource  # noqa: PLC0415 - optional part
    except ImportError:
        log.warning("the publish part of easy-ui is not installed, so no dashboards can be shown")
        return NothingPublished(config)
    return PublishedSource(config)


@dataclass
class ServerState:
    config: AppConfig
    settings: ServerSettings
    sessions: SessionStore
    auth: authmod.Authenticator
    activity: Activity
    source: Any
    preview_token: str | None = None
    preview: bool = False
    on_preview_link_used: Callable[[str], None] | None = None
    query_slots: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(4))
    challenges: passkeys.Challenges = field(default_factory=passkeys.Challenges)
    clock: Callable[[], float] = time.time
    _etags: dict[tuple[str, int, int], str] = field(default_factory=dict)


# ---------------------------------------------------------------- helpers


def cookie_names(app: str, https: bool, preview: bool = False) -> tuple[str, str]:
    # Cookies do not keep ports apart, so a preview on 127.0.0.1 uses its own name and never
    # replaces the session of a real server for the same project on the same computer.
    base = f"easyui-{app}" + ("-preview" if preview else "")
    prefix = "__Host-" if https else ""
    return prefix + base, prefix + base + "-device"


def passkey_cookie(app: str, https: bool) -> str:
    """The short-lived cookie that ties a passkey sign-in request to the browser that asked for it."""
    return ("__Host-" if https else "") + f"easyui-{app}-passkey"


def _state(request: Request) -> ServerState:
    return request.app.state.easyui


def _set_cookie(resp: Response, name: str, value: str, max_age: int, https: bool) -> None:
    resp.set_cookie(name, value, max_age=max_age, path="/", secure=https, httponly=True, samesite="strict")


def _clear_cookie(resp: Response, name: str, https: bool) -> None:
    resp.delete_cookie(name, path="/", secure=https, httponly=True, samesite="strict")


def current_session(request: Request) -> Session | None:
    st = _state(request)
    info = security.request_info(request)
    name, _ = cookie_names(st.config.name, info.https, st.preview)
    return st.sessions.get(request.cookies.get(name))


def require_session(request: Request) -> Session:
    s = current_session(request)
    if s is None:
        raise SignInRequired(
            "You are not signed in, so the dashboards stay hidden.",
            hint="Sign in with this project's password.",
        )
    if request.url.path != "/api/rev":
        _state(request).activity.touch()
    return s


async def json_body(request: Request, allowed: tuple[str, ...]) -> dict[str, Any]:
    raw = await request.body()
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise UsageError(
            "The request could not be read (it is not valid JSON), so nothing was done.",
            hint="Reload the page and try again.",
            code="bad_json",
        ) from None
    if not isinstance(data, dict):
        raise UsageError("The request must be a JSON object.", hint="Reload the page and try again.", code="bad_json")
    unknown = [k for k in data if k not in allowed]
    if unknown:
        raise UsageError(
            f"The request has a field easy-ui does not know: {str(unknown[0])[:40]!r}.",
            hint=f"The allowed fields are: {', '.join(allowed)}.",
            code="bad_request",
        )
    return data


def _error_response(e: EasyUIError) -> JSONResponse:
    resp = JSONResponse(security.error_dict(e.code, e.message, e.hint), status_code=e.http_status)
    if isinstance(e, authmod.TooManyAttempts):
        resp.headers["retry-after"] = str(e.retry_after)
    elif e.http_status == 503:
        resp.headers["retry-after"] = "5"
    return resp


async def _on_easyui_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, EasyUIError)
    return _error_response(exc)


async def _on_http_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, HTTPException)
    if exc.status_code == 405:
        resp: Response = JSONResponse(
            security.error_dict("method_not_allowed", "This address does not accept that kind of request.", "Use the easy-ui page."),
            status_code=405,
        )
    elif request.url.path.startswith("/api/") and exc.status_code == 404 and current_session(request) is None:
        # Unknown API addresses answer like every other API route: sign in first.
        resp = _error_response(SignInRequired("You are not signed in, so the dashboards stay hidden.", hint="Sign in with this project's password."))
    elif request.url.path.startswith("/api/"):
        resp = JSONResponse(
            security.error_dict("not_found", "There is nothing at this address.", "Check the address, or open the dashboards page."),
            status_code=exc.status_code,
        )
    else:
        resp = PlainTextResponse("There is no page at this address. Open the dashboards from the start page.", status_code=exc.status_code)
    for k, v in (exc.headers or {}).items():
        resp.headers[k] = v
    return resp


async def _on_unexpected(request: Request, exc: Exception) -> Response:
    log.error("unexpected error while answering %s %s", request.method, request.url.path[:200], exc_info=exc)
    return JSONResponse(
        security.error_dict(
            "internal",
            "Something went wrong inside easy-ui while answering, so this part could not be shown.",
            "Try again in a moment. If it keeps happening, run easy-ui check on the server and look at its log.",
        ),
        status_code=500,
    )


# ---------------------------------------------------------------- API routes


async def api_session(request: Request) -> Response:
    st = _state(request)
    info = security.request_info(request)
    name, _ = cookie_names(st.config.name, info.https, st.preview)
    s = current_session(request)
    body: dict[str, Any] = {"signed_in": s is not None, "app": st.config.public()}
    if st.preview:
        body["preview"] = True
    else:
        body["sign_in"] = await run_in_threadpool(_sign_in_ways, st, request)
    resp = JSONResponse(body)
    if s is None and name in request.cookies:
        _clear_cookie(resp, name, info.https)
    return resp


async def api_login(request: Request) -> Response:
    st = _state(request)
    info = security.request_info(request)
    body = await json_body(request, ("password",))
    session_cookie, device_cookie = cookie_names(st.config.name, info.https, st.preview)
    result = await run_in_threadpool(
        st.auth.sign_in,
        body.get("password"),
        device_secret=request.cookies.get(device_cookie),
        old_session=request.cookies.get(session_cookie),
    )
    return _signed_in_response(st, request, result)


async def api_logout(request: Request) -> Response:
    st = _state(request)
    info = security.request_info(request)
    await json_body(request, ())
    name, _ = cookie_names(st.config.name, info.https, st.preview)
    secret = request.cookies.get(name)
    if secret:
        await run_in_threadpool(st.sessions.delete, secret)
    resp = JSONResponse({"signed_in": False})
    _clear_cookie(resp, name, info.https)
    return resp


# ---------------------------------------------------------------- passkeys, recovery codes, password switch


def _sign_in_ways(st: ServerState, request: Request) -> dict[str, Any]:
    """What the sign-in page offers (no secrets): which ways in exist and work on this address."""
    store = passkeys.PasskeyStore(st.config.state.auth, st.clock)
    has_password = authmod.load_hash(st.config.state.auth) is not None
    n = store.count()
    return {
        "password": has_password and store.password_sign_in(),
        "passkeys": n > 0,
        "passkeys_here": passkeys_usable_here(request),
        "recovery": n > 0 and passkeys.RecoveryCodes(st.config.state.auth).left() > 0,
    }


def _hostname(host_with_port: str) -> str:
    return security.normalize_host(host_with_port) or ""


def _is_ip(hostname: str) -> bool:
    try:
        ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        return False
    return True


def passkeys_usable_here(request: Request) -> bool:
    """A guess for the page (a GET has no Origin): https, or http://localhost."""
    info = security.request_info(request)
    name = _hostname(info.host)
    return not _is_ip(name) and (info.https or name == "localhost" or request.headers.get("x-forwarded-proto", "") == "https")


def passkey_site(request: Request) -> tuple[str, frozenset[str]]:
    """The RP ID and the one origin a passkey answer must come from, for this request.

    The origin is the page's own: the request's Origin header (the middleware already made sure it is this
    server's own address, or its https form behind a proxy that does not say so). Passkeys need https, except
    on http://localhost for trying things out; an IP address cannot be an RP ID at all.
    """
    info = security.request_info(request)
    origin = (request.headers.get("origin") or info.origin).strip().lower()
    allowed = {info.origin} | ({"https://" + info.host} if not info.https else set())
    if origin not in allowed:
        raise NotAllowed(
            "This request did not come from the dashboards page itself, so passkeys were not used.",
            hint="Open the dashboards page directly and try again.",
            code="cross_site",
        )
    scheme, _, host = origin.partition("://")
    name = _hostname(host)
    if not name or _is_ip(name):
        raise NotAllowed(
            "Passkeys do not work when the dashboards are opened with a number address such as 127.0.0.1.",
            hint="Open them with a name instead, for example http://localhost:PORT on this computer, or the https "
            "address of your private network.",
            code="passkeys_unavailable",
        )
    if scheme != "https" and name != "localhost":
        raise NotAllowed(
            "Passkeys only work over a secure (https) connection, and this page was opened without one.",
            hint="Open the dashboards with their https address (for example the one your private network gives), "
            "or with http://localhost on this computer.",
            code="passkeys_unavailable",
        )
    return name, frozenset({origin})


def _no_passkeys_in_preview(st: ServerState) -> None:
    if st.preview:
        raise NotAllowed(
            "A preview has no sign-in of its own, so passkeys are not used in it.",
            hint="Try passkeys with easy-ui serve on this computer, opened at http://localhost:PORT.",
            code="preview",
        )


def _require_recent(st: ServerState, session: Session) -> None:
    if st.clock() - session.created > passkeys.RECENT_SIGN_IN:
        raise NotAllowed(
            "For your safety, how you sign in can only be changed shortly after signing in, and this page signed in "
            "a while ago.",
            hint="Sign out, sign in again, and then make the change within 10 minutes.",
            code="sign_in_again",
        )


def _settings(st: ServerState, request: Request, session: Session) -> dict[str, Any]:
    auth_dir = st.config.state.auth
    store = passkeys.PasskeyStore(auth_dir, st.clock)
    left = passkeys.RECENT_SIGN_IN - (st.clock() - session.created)
    return {
        "password_set": authmod.load_hash(auth_dir) is not None,
        "password_sign_in": store.password_sign_in(),
        "passkeys": [{k: v for k, v in p.items() if k != "rp_id"} | {"here": p["rp_id"] == _hostname(security.request_info(request).host)}
                     for p in store.listing()],
        "max_passkeys": passkeys.MAX_PASSKEYS,
        "recovery": passkeys.RecoveryCodes(auth_dir).status(),
        "passkeys_here": passkeys_usable_here(request),
        "can_change_for": max(0, int(left)),
    }


def _signed_in_response(st: ServerState, request: Request, result: authmod.SignIn) -> Response:
    info = security.request_info(request)
    session_cookie, device_cookie = cookie_names(st.config.name, info.https, st.preview)
    st.activity.touch()
    resp = JSONResponse({"signed_in": True, "app": st.config.public()})
    _set_cookie(resp, session_cookie, result.session_secret, st.sessions.max_age(), info.https)
    if result.device_secret:
        _set_cookie(resp, device_cookie, result.device_secret, authmod.DEVICE_LIFETIME, info.https)
    return resp


async def api_signin_settings(request: Request) -> Response:
    session = require_session(request)
    st = _state(request)
    _no_passkeys_in_preview(st)
    return JSONResponse(await run_in_threadpool(_settings, st, request, session))


async def api_passkey_options(request: Request) -> Response:
    st = _state(request)
    _no_passkeys_in_preview(st)
    body = await json_body(request, ("purpose",))
    purpose = body.get("purpose")
    rp_id, _origins = passkey_site(request)
    info = security.request_info(request)
    store = passkeys.PasskeyStore(st.config.state.auth, st.clock)
    if purpose == "login":
        token = secrets.token_urlsafe(24)
        challenge = st.challenges.issue("login", hashlib.sha256(token.encode()).hexdigest())
        resp = JSONResponse({"publicKey": {
            "challenge": webauthn.b64url_encode(challenge),
            "rpId": rp_id,
            "timeout": 120_000,
            "userVerification": "required",
            "allowCredentials": [],
        }})
        _set_cookie(resp, passkey_cookie(st.config.name, info.https), token, passkeys.CHALLENGE_SECONDS, info.https)
        return resp
    if purpose == "register":
        session = require_session(request)
        _require_recent(st, session)
        user_id = await run_in_threadpool(store.user_id)
        challenge = st.challenges.issue("register", session.key)
        existing = [cid for cid, c in (await run_in_threadpool(store.credentials)).items()]
        return JSONResponse({"publicKey": {
            "challenge": webauthn.b64url_encode(challenge),
            "rp": {"id": rp_id, "name": st.config.title},
            "user": {"id": webauthn.b64url_encode(user_id), "name": st.config.name, "displayName": f"{st.config.title} dashboards"},
            "pubKeyCredParams": [{"type": "public-key", "alg": a} for a in webauthn.ALGORITHMS],
            "timeout": 120_000,
            "attestation": "none",
            "authenticatorSelection": {"residentKey": "required", "requireResidentKey": True, "userVerification": "required"},
            "excludeCredentials": [{"type": "public-key", "id": cid} for cid in existing],
        }})
    raise UsageError("Say what the passkey is for: login or register.", hint="Reload the page and try again.")


def _challenge_of(credential: Any) -> bytes:
    """The challenge the browser's answer says it answers (checked for real by webauthn later)."""
    try:
        resp = credential["response"]
        raw = webauthn.b64url_decode(resp["clientDataJSON"], what="the browser's part", limit=webauthn.MAX_CLIENT_DATA)
        cd = json.loads(raw.decode("utf-8"))
        return webauthn.b64url_decode(cd["challenge"], what="the challenge", limit=256)
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, RecursionError):
        raise webauthn.WebAuthnError(
            "The passkey answer could not be read, so you are not signed in with it.", hint="Try again."
        ) from None


def _passkey_login(st: ServerState, request: Request, credential: Any) -> authmod.SignIn:
    rp_id, origins = passkey_site(request)
    info = security.request_info(request)
    token = request.cookies.get(passkey_cookie(st.config.name, info.https)) or ""
    challenge = _challenge_of(credential)
    st.challenges.check(challenge, "login", hashlib.sha256(token.encode()).hexdigest())
    store = passkeys.PasskeyStore(st.config.state.auth, st.clock)
    cid = webauthn.b64url_encode(webauthn.credential_id_of(credential))
    saved = store.get(cid)
    if saved is None:
        log.warning("refused a passkey that is not saved for these dashboards")
        raise NotAllowed(
            "This passkey is not one of the passkeys saved for these dashboards, so you are not signed in.",
            hint="Choose another passkey, or sign in another way and add this one on the Sign-in settings page.",
            code="unknown_passkey",
        )
    if saved.get("rp_id") != rp_id:
        raise NotAllowed(
            f"This passkey was added for the address {saved.get('rp_id')}, not for this one, so it cannot be used here.",
            hint=f"Open the dashboards at {saved.get('rp_id')}, or add a passkey for this address.",
            code="other_address",
        )
    try:
        public_key = webauthn.b64url_decode(saved["public_key"], what="the saved key", limit=4096)
        alg = int(saved["alg"])
        stored_count = int(saved.get("sign_count") or 0)
    except (KeyError, TypeError, ValueError):
        raise NotAllowed("The saved passkey could not be read.", hint="Remove it and add it again.", code="passkey_damaged") from None
    result = webauthn.verify_assertion(
        credential, challenge=challenge, origins=origins, rp_id=rp_id, public_key=public_key, alg=alg,
        stored_sign_count=stored_count, user_id=store.user_id(),
    )
    st.challenges.accept(challenge)  # after it verified; refuses a second copy of the same answer
    store.touch(cid, result.sign_count, result.backed_up)
    session_cookie, device_cookie = cookie_names(st.config.name, info.https, st.preview)
    return st.auth.sign_in_passkey(
        device_secret=request.cookies.get(device_cookie), old_session=request.cookies.get(session_cookie)
    )


async def api_passkey_login(request: Request) -> Response:
    st = _state(request)
    _no_passkeys_in_preview(st)
    body = await json_body(request, ("credential",))
    result = await run_in_threadpool(_passkey_login, st, request, body.get("credential"))
    resp = _signed_in_response(st, request, result)
    info = security.request_info(request)
    _clear_cookie(resp, passkey_cookie(st.config.name, info.https), info.https)
    return resp


def _passkey_register(st: ServerState, request: Request, session: Session, credential: Any, name: Any) -> dict[str, Any]:
    _require_recent(st, session)
    rp_id, origins = passkey_site(request)
    challenge = _challenge_of(credential)
    st.challenges.check(challenge, "register", session.key)
    reg = webauthn.verify_registration(credential, challenge=challenge, origins=origins, rp_id=rp_id)
    st.challenges.accept(challenge)
    store = passkeys.PasskeyStore(st.config.state.auth, st.clock)
    added = store.add(reg, rp_id=rp_id, name=name if isinstance(name, str) else None)
    log.warning("a passkey was added (%s)", added["name"][:60])
    return {"added": {k: v for k, v in added.items() if k != "rp_id"}, **_settings(st, request, session)}


async def api_passkey_register(request: Request) -> Response:
    session = require_session(request)
    st = _state(request)
    _no_passkeys_in_preview(st)
    body = await json_body(request, ("credential", "name"))
    return JSONResponse(await run_in_threadpool(_passkey_register, st, request, session, body.get("credential"), body.get("name")))


def _passkey_remove(st: ServerState, request: Request, session: Session, cid: Any) -> dict[str, Any]:
    _require_recent(st, session)
    if not isinstance(cid, str) or not cid or len(cid) > 2000:
        raise UsageError("Say which passkey to remove.", hint="Reload the page and try again.")
    store = passkeys.PasskeyStore(st.config.state.auth, st.clock)
    if cid not in store.credentials():
        raise NotFound("That passkey is not saved (any more).", hint="Reload the page.", code="no_such_passkey")
    removed = store.remove(cid, password_set=authmod.load_hash(st.config.state.auth) is not None)
    log.warning("a passkey was removed (%s)", removed["name"][:60])
    return {"removed": removed, **_settings(st, request, session)}


async def api_passkey_remove(request: Request) -> Response:
    session = require_session(request)
    st = _state(request)
    _no_passkeys_in_preview(st)
    body = await json_body(request, ("id",))
    return JSONResponse(await run_in_threadpool(_passkey_remove, st, request, session, body.get("id")))


def _password_switch(st: ServerState, request: Request, session: Session, on: Any) -> dict[str, Any]:
    _require_recent(st, session)
    if not isinstance(on, bool):
        raise UsageError("Say whether password sign-in should be on or off.", hint="Reload the page and try again.")
    store = passkeys.PasskeyStore(st.config.state.auth, st.clock)
    store.set_password_sign_in(on, password_set=authmod.load_hash(st.config.state.auth) is not None)
    log.warning("password sign-in was turned %s", "on" if on else "off")
    return _settings(st, request, session)


async def api_password_switch(request: Request) -> Response:
    session = require_session(request)
    st = _state(request)
    _no_passkeys_in_preview(st)
    body = await json_body(request, ("on",))
    return JSONResponse(await run_in_threadpool(_password_switch, st, request, session, body.get("on")))


def _new_codes(st: ServerState, request: Request, session: Session) -> dict[str, Any]:
    _require_recent(st, session)
    codes = passkeys.RecoveryCodes(st.config.state.auth, st.clock).make()
    log.warning("new recovery codes were made; the old ones no longer work")
    return {"codes": codes, **_settings(st, request, session)}


async def api_recovery_codes(request: Request) -> Response:
    session = require_session(request)
    st = _state(request)
    _no_passkeys_in_preview(st)
    await json_body(request, ())
    return JSONResponse(await run_in_threadpool(_new_codes, st, request, session))


async def api_recovery_login(request: Request) -> Response:
    st = _state(request)
    _no_passkeys_in_preview(st)
    info = security.request_info(request)
    body = await json_body(request, ("code",))
    session_cookie, device_cookie = cookie_names(st.config.name, info.https, st.preview)
    result = await run_in_threadpool(
        st.auth.sign_in_recovery,
        body.get("code"),
        device_secret=request.cookies.get(device_cookie),
        old_session=request.cookies.get(session_cookie),
    )
    return _signed_in_response(st, request, result)


def _check_slug(slug: Any) -> str:
    if not isinstance(slug, str) or not spec.SLUG_RE.match(slug):
        raise NotFound(
            f"There is no dashboard called {str(slug)[:41]!r}, so it cannot be shown.",
            hint="Pick a dashboard from the list. New or changed dashboards appear after easy-ui publish.",
        )
    return slug


async def api_dashboards(request: Request) -> Response:
    require_session(request)
    st = _state(request)
    items = order_dashboards(await run_in_threadpool(st.source.list), st.config.dashboards)
    return JSONResponse([{"slug": d["slug"], "title": d.get("title"), "description": d.get("description")} for d in items])


def order_dashboards(items: list[dict[str, Any]], order: tuple[str, ...] | list[str]) -> list[dict[str, Any]]:
    """The dashboards named in ``[ui] dashboards`` first, in that order, then the rest as they came."""
    rank = {slug: i for i, slug in enumerate(order or ())}
    indexed = list(enumerate(items))
    indexed.sort(key=lambda p: (rank.get(p[1].get("slug"), len(rank)), p[0]))
    return [d for _, d in indexed]


async def api_dashboard(request: Request) -> Response:
    require_session(request)
    st = _state(request)
    slug = _check_slug(request.path_params["slug"])
    compiled = await run_in_threadpool(lambda: st.source.compiled(slug, today=_dt.date.today()))
    return JSONResponse(compiled)


def _scrub_results(out: dict[str, Any]) -> dict[str, Any]:
    for res in out.get("results", {}).values():
        err = res.get("error") if isinstance(res, dict) else None
        if isinstance(err, dict):
            err["message"] = security.scrub(err.get("message"))
            err["hint"] = security.scrub(err.get("hint"))
    return out


async def api_query(request: Request) -> Response:
    require_session(request)
    st = _state(request)
    body = await json_body(request, ("dashboard", "queries", "params"))
    names = body.get("queries")
    if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
        raise ParamError("Say which queries to run, as a list of names.", hint="Reload the page and try again.")
    if len(names) > st.settings.max_queries:
        raise ParamError(
            f"Too many queries were asked for at once ({len(names)}).",
            hint=f"Ask for at most {st.settings.max_queries} queries in one request.",
        )
    params = body.get("params")
    if params is not None and not isinstance(params, dict):
        raise ParamError("The control values must be sent as named values.", hint="Reload the page and try again.")
    slug = _check_slug(body.get("dashboard"))
    dash = await run_in_threadpool(st.source.dashboard, slug)
    bundle_path = await run_in_threadpool(st.source.bundle_path)
    async with st.query_slots:
        out = await run_in_threadpool(
            spec.run_queries,
            dash,
            names,
            params,
            bundle_path=bundle_path,
            limits=st.settings.limits,
            today=_dt.date.today(),
            errors="inline",
            total_time_s=st.settings.request_time_s,
        )
    return JSONResponse(_scrub_results(out))


async def api_rev(request: Request) -> Response:
    require_session(request)
    st = _state(request)
    return JSONResponse({"rev": await run_in_threadpool(st.source.rev)})


# ---------------------------------------------------------------- static files


def _safe_parts(rel: str, *, max_depth: int = 4) -> list[str] | None:
    if not rel or "\\" in rel or "\x00" in rel or len(rel) > 512:
        return None
    parts = rel.split("/")
    if len(parts) > max_depth or not all(_SEGMENT_RE.match(p) and p not in (".", "..") for p in parts):
        return None
    return parts


def _file_response(request: Request, st: ServerState, base: Path, parts: list[str], cache: str | None = None) -> Response:
    ctype = CONTENT_TYPES.get(Path(parts[-1]).suffix.lower())
    if ctype is None:
        raise HTTPException(404)
    try:
        root = base.resolve(strict=True)
        target = root.joinpath(*parts).resolve(strict=True)
    except (OSError, RuntimeError):
        raise HTTPException(404) from None
    if not target.is_relative_to(root):
        raise HTTPException(404)
    try:
        s = target.stat()
    except OSError:
        raise HTTPException(404) from None
    if not stat.S_ISREG(s.st_mode) or s.st_size > _MAX_ASSET_BYTES:
        raise HTTPException(404)
    key = (str(target), s.st_mtime_ns, s.st_size)
    etag = st._etags.get(key)
    data: bytes | None = None
    if etag is None:
        data = target.read_bytes()
        etag = '"' + hashlib.sha256(data).hexdigest()[:32] + '"'
        if len(st._etags) > 512:
            st._etags.clear()
        st._etags[key] = etag
    cache = cache or (IMMUTABLE if _HASHED_RE.search(parts[-1]) else REVALIDATE)
    headers = {"etag": etag, "cache-control": cache}
    inm = request.headers.get("if-none-match")
    if inm and etag in [t.strip().removeprefix("W/") for t in inm.split(",")]:
        return Response(status_code=304, headers=headers)
    if data is None:
        data = target.read_bytes()
    return Response(data, media_type=ctype, headers=headers)


def _preview_sign_in(request: Request, st: ServerState) -> Response | None:
    token = request.query_params.get("token")
    if not st.preview_token or token is None:
        return None
    if not hmac.compare_digest(token.encode("utf-8", "replace"), st.preview_token.encode()):
        return None  # the page then shows its sign-in screen
    info = security.request_info(request)
    secret, _ = st.sessions.create()
    st.activity.touch()
    # The link works once: a copy of it (in a terminal, a log or the history) cannot sign anyone else in.
    st.preview_token = secrets.token_urlsafe(24)
    if st.on_preview_link_used is not None:
        st.on_preview_link_used(st.preview_token)
    resp = RedirectResponse(request.url.path, status_code=303)
    _set_cookie(resp, cookie_names(st.config.name, info.https, st.preview)[0], secret, st.sessions.max_age(), info.https)
    return resp


def _index(request: Request) -> Response:
    st = _state(request)
    signed_in = _preview_sign_in(request, st)
    if signed_in is not None:
        return signed_in
    if not (st.settings.web_dir / "index.html").is_file():
        return PlainTextResponse(
            "The dashboard page is not installed in this copy of easy-ui, so there is nothing to show. "
            "Reinstall my-easy-ui (for example: uv sync), then reload this page.",
            status_code=503,
        )
    return _file_response(request, st, st.settings.web_dir, ["index.html"], cache=REVALIDATE)


async def page_index(request: Request) -> Response:
    return _index(request)


async def page_asset(request: Request) -> Response:
    st = _state(request)
    parts = _safe_parts(request.path_params["path"])
    if parts is None:
        raise HTTPException(404)
    return _file_response(request, st, st.settings.web_dir / "assets", parts)


async def page_other(request: Request) -> Response:
    st = _state(request)
    rel = request.path_params["path"].rstrip("/")
    if rel.startswith("api/") or rel == "api":
        raise HTTPException(404)
    parts = _safe_parts(rel, max_depth=3)
    if parts is None:
        raise HTTPException(404)
    if len(parts) == 1 and "." in parts[0]:
        if parts[0] == "index.html":
            return _index(request)
        return _file_response(request, st, st.settings.web_dir, parts)
    if all(_PAGE_SEGMENT_RE.match(p) for p in parts):
        return _index(request)
    raise HTTPException(404)


# ---------------------------------------------------------------- the app


def create_app(
    config: AppConfig,
    settings: ServerSettings | None = None,
    *,
    source: Any = None,
    preview_token: str | None = None,
    on_preview_link_used: Callable[[str], None] | None = None,
    on_idle: Callable[[], None] | None = None,
    clock: Callable[[], float] = time.time,
    monotonic: Callable[[], float] = time.monotonic,
) -> security.SecurityMiddleware:
    settings = settings or ServerSettings()
    st_paths = config.state.ensure()  # the preview's config points this at a temporary folder
    sessions = SessionStore(st_paths.auth, idle=settings.session_idle, absolute=settings.session_absolute, clock=clock)
    state = ServerState(
        config=config,
        settings=settings,
        sessions=sessions,
        auth=authmod.Authenticator(st_paths.auth, sessions, clock),
        activity=Activity(monotonic),
        source=source if source is not None else default_source(config),
        preview_token=preview_token,
        preview=preview_token is not None,
        on_preview_link_used=on_preview_link_used,
        query_slots=asyncio.Semaphore(max(1, settings.query_slots)),
        challenges=passkeys.Challenges(clock),
        clock=clock,
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):  # type: ignore[no-untyped-def]
        task = None
        if on_idle is not None and settings.idle_seconds > 0:
            task = asyncio.create_task(_idle_watch(state, settings.idle_seconds, on_idle))
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    api = [
        Route("/session", api_session, methods=["GET"]),
        Route("/login", api_login, methods=["POST"]),
        Route("/logout", api_logout, methods=["POST"]),
        Route("/dashboards", api_dashboards, methods=["GET"]),
        Route("/dashboards/{slug}", api_dashboard, methods=["GET"]),
        Route("/query", api_query, methods=["POST"]),
        Route("/rev", api_rev, methods=["GET"]),
        Route("/signin", api_signin_settings, methods=["GET"]),
        Route("/signin/password", api_password_switch, methods=["POST"]),
        Route("/passkeys/options", api_passkey_options, methods=["POST"]),
        Route("/passkeys/login", api_passkey_login, methods=["POST"]),
        Route("/passkeys/register", api_passkey_register, methods=["POST"]),
        Route("/passkeys/remove", api_passkey_remove, methods=["POST"]),
        Route("/recovery-codes", api_recovery_codes, methods=["POST"]),
        Route("/recovery", api_recovery_login, methods=["POST"]),
    ]
    routes = [
        Mount("/api", routes=api),
        Route("/", page_index, methods=["GET"]),
        Route("/assets/{path:path}", page_asset, methods=["GET"]),
        Route("/{path:path}", page_other, methods=["GET"]),
    ]
    app = Starlette(
        debug=False,
        routes=routes,
        exception_handlers={EasyUIError: _on_easyui_error, HTTPException: _on_http_error, Exception: _on_unexpected},
        lifespan=lifespan,
    )
    app.state.easyui = state
    outer = security.SecurityMiddleware(app, allowed_hosts=settings.allowed_hosts, max_body=settings.max_body)
    outer.easyui = state  # type: ignore[attr-defined]
    return outer


async def _idle_watch(state: ServerState, idle_seconds: float, on_idle: Callable[[], None]) -> None:
    step = max(0.05, min(30.0, idle_seconds / 4))
    while True:
        await asyncio.sleep(step)
        if state.activity.idle_for() >= idle_seconds:
            log.info("no one has used the dashboards for %d seconds; stopping until the next visit", int(idle_seconds))
            on_idle()
            return


# ---------------------------------------------------------------- listening


def _not_local(what: str) -> Refused:
    return Refused(
        f"easy-ui will not listen on {what}, because that would let other computers reach the dashboards "
        "directly. Dashboards hold private data, so the server only listens on this computer.",
        hint="Use --port PORT (it listens on 127.0.0.1) or --socket PATH. To open the dashboards from your "
        "other devices, keep it local and let the private network (Tailscale) forward to it.",
        code="not_local",
    )


def check_bind_address(host: str) -> str:
    h = (host or "").strip().strip("[]")
    if h.lower() == "localhost":
        return "127.0.0.1"
    try:
        addr = ipaddress.ip_address(h)
    except ValueError:
        raise _not_local(f"{host!r} (not an address on this computer)") from None
    if not addr.is_loopback:
        raise _not_local(f"{host} (an address other computers can reach)")
    return str(addr)


def _systemd_socket(env: dict[str, str], *, first_fd: int = 3) -> socket.socket | None:
    fds = env.get("LISTEN_FDS")
    if not fds:
        return None
    pid = env.get("LISTEN_PID")
    if pid and pid.strip() != str(os.getpid()):
        return None
    if not fds.strip().isdigit() or int(fds) != 1:
        raise UsageError(
            f"systemd passed {fds} sockets, but easy-ui serves one app on exactly one socket.",
            hint="Give the project's .socket unit a single ListenStream= line.",
        )
    sock = socket.socket(fileno=first_fd)
    if sock.type != socket.SOCK_STREAM:
        sock.detach()
        raise UsageError("The socket from systemd is not a stream socket.", hint="Use ListenStream= in the .socket unit.")
    if sock.family in (socket.AF_INET, socket.AF_INET6):
        try:
            check_bind_address(sock.getsockname()[0])
        except Refused:
            sock.detach()
            raise
    elif sock.family != socket.AF_UNIX:
        sock.detach()
        raise UsageError("The socket from systemd is not a local or loopback socket.", hint="Use ListenStream=/path or 127.0.0.1:PORT.")
    for k in ("LISTEN_FDS", "LISTEN_PID", "LISTEN_FDNAMES"):
        os.environ.pop(k, None)
    return sock


def _unix_socket(path: Path) -> socket.socket:
    path = Path(path)
    if len(os.fsencode(path)) > 100:
        raise UsageError("The socket path is too long for this system.", hint="Use a shorter path, for example /run/easy-ui/app.sock.")
    if path.exists() or path.is_symlink():
        if not stat.S_ISSOCK(path.lstat().st_mode):
            raise Refused(
                f"{paths.display_path(path)} already exists and is not a socket, so easy-ui will not replace it.",
                hint="Choose another --socket path.",
            )
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(str(path))
        except OSError:
            path.unlink()  # a socket left behind by a server that stopped
        else:
            raise TemporaryFailure(
                "Another easy-ui server is already answering on that socket, so this one did not start.",
                hint="Stop the other server first, or use it instead.",
            )
        finally:
            probe.close()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o177)
    try:
        sock.bind(str(path))
    finally:
        os.umask(old)
    os.chmod(path, 0o660)
    return sock


def open_listener(
    *, socket_path: str | Path | None = None, port: int | None = None, bind: str | None = None, env: dict[str, str] | None = None
) -> tuple[socket.socket, str, str]:
    env = dict(os.environ if env is None else env)
    sd = _systemd_socket(env)
    if sd is not None:
        if socket_path or port or bind:
            sd.detach()
            raise UsageError(
                "systemd already gave easy-ui its socket, so --socket, --port and --bind cannot be used too.",
                hint="Remove those options from the service's command line.",
            )
        where = sd.getsockname()
        desc = where if isinstance(where, str) and where else f"http://{where[0]}:{where[1]}" if isinstance(where, tuple) else "socket"
        sd.setblocking(False)
        return sd, str(desc), "systemd"
    if socket_path and (port or bind):
        raise UsageError("Use either --socket or --port/--bind, not both.", hint="For example: easy-ui serve --port 8765")
    if socket_path:
        sock = _unix_socket(Path(socket_path))
        sock.listen(128)
        sock.setblocking(False)
        return sock, str(socket_path), "unix"
    host = check_bind_address(bind or "127.0.0.1")
    port = DEFAULT_PORT if port is None else int(port)
    if not 0 <= port <= 65535:
        raise UsageError("--port must be a number from 1 to 65535.", hint="For example: --port 8765")
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError as e:
        sock.close()
        if e.errno == errno.EADDRINUSE:
            raise TemporaryFailure(
                f"Port {port} is already in use on this computer, so the dashboard server could not start there.",
                hint="Stop the other program, or choose another port with --port.",
            ) from None
        raise
    sock.listen(128)
    sock.setblocking(False)
    h, p = sock.getsockname()[:2]
    shown = f"[{h}]" if ":" in h else h
    return sock, f"http://{shown}:{p}", "tcp"


# ---------------------------------------------------------------- uvicorn


def guarded_protocol() -> type:
    """uvicorn's h11 protocol, closing a connection that has not sent a request's headers in time.

    uvicorn has no time limit for reading headers, so a client that sends a few bytes every
    few seconds could keep a connection open for ever, and enough of them would fill the
    server's connection limit. Here every request (the first on a connection and each one
    after a keep-alive pause) must finish its headers within ``HEADER_TIMEOUT`` seconds.
    """
    import h11  # noqa: PLC0415 - comes with uvicorn
    from uvicorn.protocols.http.h11_impl import H11Protocol  # noqa: PLC0415

    class GuardedH11Protocol(H11Protocol):
        header_timeout = HEADER_TIMEOUT
        _header_timer: asyncio.TimerHandle | None = None

        def _arm_header_timer(self) -> None:
            self._cancel_header_timer()
            if self.conn.their_state is h11.IDLE and not self.transport.is_closing():
                self._header_timer = self.loop.call_later(self.header_timeout, self._header_too_slow)

        def _cancel_header_timer(self) -> None:
            if self._header_timer is not None:
                self._header_timer.cancel()
                self._header_timer = None

        def _header_too_slow(self) -> None:
            self._header_timer = None
            if self.conn.their_state is h11.IDLE and not self.transport.is_closing():
                self.transport.close()

        def connection_made(self, transport: Any) -> None:  # type: ignore[override]
            super().connection_made(transport)
            self._arm_header_timer()

        def handle_events(self) -> None:
            super().handle_events()
            if self.conn.their_state is not h11.IDLE:
                self._cancel_header_timer()

        def on_response_complete(self) -> None:
            super().on_response_complete()
            self._arm_header_timer()

        def connection_lost(self, exc: Exception | None) -> None:
            self._cancel_header_timer()
            super().connection_lost(exc)

    return GuardedH11Protocol


def uvicorn_config(app: Any, **overrides: Any) -> Any:
    """The uvicorn settings both ``serve`` and ``preview`` use."""
    import uvicorn  # noqa: PLC0415

    settings: dict[str, Any] = {
        "http": guarded_protocol(),
        "access_log": False,
        "proxy_headers": False,
        "server_header": False,
        "date_header": True,
        "lifespan": "on",
        "timeout_graceful_shutdown": 5,
        "timeout_keep_alive": 5,
        # uvicorn counts open connections against this limit, not only requests in progress.
        "limit_concurrency": LIMIT_CONNECTIONS,
        "h11_max_incomplete_event_size": 16 * 1024,
    }
    settings.update(overrides)
    return uvicorn.Config(app, **settings)


@contextlib.contextmanager
def stop_signals(on_signal: Callable[[], None] | None = None) -> Any:
    """Turn Ctrl-C and SIGTERM into a normal stop, so the command still prints its result.

    uvicorn stops gracefully on these signals and then raises them again with the handlers
    that were in place before it started; these handlers only take note. A signal that
    arrives before uvicorn has installed its own handlers calls ``on_signal`` (which asks
    the server to stop as soon as it has started).
    """
    caught: list[int] = []

    def note(signum: int, _frame: Any) -> None:
        caught.append(signum)
        if on_signal is not None:
            on_signal()

    old = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(ValueError, OSError):  # only possible in the main thread
            old[sig] = signal.signal(sig, note)
    try:
        yield caught
    finally:
        for sig, handler in old.items():
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, handler)


# ---------------------------------------------------------------- check


def _item(id_: str, title: str, status: str, why: str, fix: str | None) -> dict[str, Any]:
    return {"id": id_, "title": title, "status": status, "why": why, "fix": fix if status != "ok" else None}


def _private(p: Path) -> bool | None:
    try:
        s = p.lstat()
    except OSError:
        return None
    return (s.st_mode & 0o077) == 0 and s.st_uid == os.geteuid() and not stat.S_ISLNK(s.st_mode)


def run_checks(config: AppConfig) -> dict[str, Any]:
    items = []
    root = os.geteuid() == 0
    items.append(
        _item(
            "not_root",
            "The server runs as an ordinary account, not as the administrator (root)",
            "fail" if root else "ok",
            "A web server running as root could let one mistake in it take over the whole computer.",
            "Run easy-ui under the project's own ordinary account (the service files from easy-ui units do this).",
        )
    )

    st = config.state
    # On a server the ui folder belongs to the project's account and lets the web account's group read it;
    # then it only must not let others in, and auth/ must be private to this account.
    shared = paths.dir_mode(st.root) == 0o750
    folders = [st.auth] if shared else [st.root, st.auth]
    files = [st.auth / n for n in (authmod.PASSWORD_FILE, "sessions.json", authmod.THROTTLE_FILE, authmod.DEVICES_FILE,
                                   passkeys.PASSKEYS_FILE, passkeys.RECOVERY_FILE)]
    states = {p: _private(p) for p in folders + [f for f in files if f.exists()]}
    if shared:
        try:
            states[st.root] = (os.stat(st.root).st_mode & 0o027) == 0
        except OSError:
            states[st.root] = None
    if states.get(st.root) is None or (shared and states.get(st.auth) is None):
        status, fix = "fail", "Create it by setting the password: ask the person to run easy-ui set-password."
    elif all(states.values()):
        status, fix = "ok", None
    else:
        bad = [paths.display_path(p) for p, ok in states.items() if not ok]
        status = "fail"
        fix = "Make them private to this account: chmod 700 for folders and 600 for files: " + ", ".join(bad)
    items.append(
        _item(
            "state_private",
            "The project's private easy-ui folder can only be read by this account",
            status,
            "That folder holds the password hash, the passkeys, the recovery codes and the sign-in sessions; other "
            "accounts on the computer must not read them.",
            fix,
        )
    )

    pw = authmod.password_status(st.auth)
    if not pw["set"]:
        status, fix = "fail", "Ask the person to run easy-ui set-password on this computer (they type the password themselves)."
    elif not pw["algorithm_ok"]:
        status, fix = "fail", "Ask the person to set the password again with easy-ui set-password, so it is stored the current safe way."
    elif not pw["file_private"]:
        status, fix = "fail", "Make the password file private: chmod 600 " + paths.display_path(authmod.password_file(st.auth))
    else:
        status, fix = "ok", None
    items.append(
        _item(
            "password_set",
            "A sign-in password is set and stored safely (Argon2id)",
            status,
            "Without a password nobody can sign in to the dashboards, and a weakly stored one could be cracked if the file leaked.",
            fix,
        )
    )

    try:
        guesses = authmod.Throttle(st.auth).failures(authmod.UNTRUSTED) if st.auth.is_dir() else 0
    except OSError:
        guesses = 0
    items.append(
        _item(
            "no_guessing",
            "No run of wrong passwords from unknown browsers in the last hour",
            "warn" if guesses >= authmod.FREE_ATTEMPTS else "ok",
            f"{guesses} wrong password(s) in a row came from browsers that have never signed in. If that was not "
            "you, someone on your private network may be trying to guess the password."
            if guesses else "Wrong passwords from unknown browsers can mean someone is guessing the password.",
            "If it was not you: sign in from a browser you have used before (it is not slowed down), check which "
            "devices are on your private network, and consider a new password with easy-ui set-password. The server "
            "log lists every failed sign-in.",
        )
    )

    current = bundle.current_bundle(config)
    items.append(
        _item(
            "data_ready",
            "Dashboard data has been built",
            "ok" if current else "warn",
            "Dashboards show a copy of the allowed tables; until it is built they have nothing to show.",
            "Run: easy-ui data build",
        )
    )
    try:
        n = len(default_source(config).list())
    except (EasyUIError, OSError):
        n = 0
    items.append(
        _item(
            "dashboards_published",
            "At least one dashboard is published",
            "ok" if n else "warn",
            "The server only shows published dashboards, so people see a checked version, never a half-edited file.",
            "Check and publish them with: easy-ui validate, then easy-ui publish",
        )
    )
    items.append(
        _item(
            "page_installed",
            "The dashboard page (the part that runs in the browser) is installed",
            "ok" if (WEB_DIR / "index.html").is_file() else "warn",
            "Without it the browser has nothing to show.",
            "Reinstall my-easy-ui, for example: uv sync",
        )
    )
    ok = not any(i["status"] == "fail" for i in items)
    return {"ok": ok, "app": config.name, "items": items}


# ---------------------------------------------------------------- commands


def _log_config(level: str = "INFO") -> dict[str, Any]:
    handler = {"class": "logging.StreamHandler", "stream": "ext://sys.stderr", "formatter": "plain"}
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(message)s"}},
        "handlers": {"stderr": handler},
        "loggers": {
            "uvicorn": {"handlers": ["stderr"], "level": level, "propagate": False},
            "uvicorn.error": {"level": level},
            "uvicorn.access": {"handlers": ["stderr"], "level": "WARNING", "propagate": False},
            "easyui": {"handlers": ["stderr"], "level": level, "propagate": False},
        },
    }


def _allowed_hosts(args: argparse.Namespace) -> tuple[str, ...]:
    raw = list(args.allow_host or [])
    raw += re.split(r"[,\s]+", os.environ.get("EASYUI_ALLOW_HOSTS", ""))
    out = []
    for h in raw:
        h = h.strip()
        if not h:
            continue
        n = security.normalize_host(h)
        if n is None:
            raise UsageError(f"{h!r} is not a host name easy-ui can use.", hint="Give a name such as myserver.example.ts.net.")
        out.append(n)
    return tuple(dict.fromkeys(out))


def resolve_idle_minutes(value: float | None, env: dict[str, str] | None = None) -> float:
    """``--idle-minutes``, or its default: stopping when idle only makes sense when systemd starts the
    server again on the next visit (it passed the socket in ``LISTEN_FDS``); started by hand, never."""
    if value is not None:
        return float(value)
    env = os.environ if env is None else env
    return DEFAULT_IDLE_MINUTES if env.get("LISTEN_FDS") else 0.0


def _cmd_serve(args: argparse.Namespace) -> Any:
    from easyui.cli import Result, load_config  # noqa: PLC0415 - avoids an import cycle

    if os.geteuid() == 0:
        raise Refused(
            "easy-ui will not run the dashboard server as the administrator (root), because a mistake in a web "
            "server running as root could take over the whole computer.",
            hint="Run it under the project's own ordinary account (the service files from easy-ui units do this).",
            code="root_refused",
        )
    config = load_config(args)
    idle_minutes = resolve_idle_minutes(args.idle_minutes)
    if idle_minutes < 0:
        raise UsageError("--idle-minutes must be 0 (never stop) or more.", hint="For example: --idle-minutes 10")
    settings = ServerSettings(allowed_hosts=_allowed_hosts(args), idle_seconds=idle_minutes * 60)

    import uvicorn  # noqa: PLC0415 - only needed to serve

    stopped = {"reason": "stopped"}
    holder: dict[str, Any] = {}

    def stop_now() -> None:
        if "server" in holder:
            holder["server"].should_exit = True

    def on_idle() -> None:
        stopped["reason"] = "idle"
        stop_now()

    # From before the socket is opened: a stop signal at any moment ends in a normal stop with a result.
    with stop_signals(stop_now) as caught:
        sock, where, kind = open_listener(socket_path=args.socket, port=args.port, bind=args.bind)
        try:
            sandbox.set_heap_limit(HEAP_LIMIT)
            logging.config.dictConfig(_log_config())
            app = create_app(config, settings, on_idle=on_idle)
            if not authmod.load_hash(config.state.auth):
                log.warning(
                    "no sign-in password is set yet, so nobody can sign in; the person types one with: "
                    "easy-ui set-password"
                )
            server = uvicorn.Server(uvicorn_config(app, log_config=_log_config()))
            holder["server"] = server
            idle = f"after {idle_minutes:g} minutes without use" if idle_minutes else "only when asked to (Ctrl-C)"
            log.info("serving the dashboards of %s on %s; it stops %s", config.name, where, idle)
            if not caught:
                server.run(sockets=[sock])
        finally:
            sock.close()
            if kind == "unix" and args.socket:
                with contextlib.suppress(OSError):
                    Path(args.socket).unlink()
    reason = stopped["reason"]
    text = (
        "The dashboard server stopped because no one used it for a while. When systemd runs it, it starts "
        "again by itself on the next visit."
        if reason == "idle"
        else "The dashboard server stopped."
    )
    return Result({"stopped": reason, "app": config.name, "listened_on": where}, text)


def _cmd_set_password(args: argparse.Namespace) -> Any:
    from easyui.cli import Result, load_config  # noqa: PLC0415

    config = load_config(args)
    st = config.state.ensure()
    password = authmod.prompt_new_password()
    res = authmod.set_password(st.auth, password)
    n = res["signed_out"]
    text = "The sign-in password is set."
    if n:
        text += f" {n} browser(s) that were signed in have been signed out and must sign in with the new password."
    return Result({"ok": True, "app": config.name, **res}, text)


def _cmd_check(args: argparse.Namespace) -> Any:
    from easyui.cli import Result, load_config  # noqa: PLC0415

    config = load_config(args)
    report = run_checks(config)
    marks = {"ok": "[ok]  ", "fail": "[FAIL]", "warn": "[todo]"}
    lines = []
    for i in report["items"]:
        lines.append(f"{marks[i['status']]} {i['title']}")
        if i["status"] != "ok":
            lines.append(f"       Why it matters: {i['why']}")
            lines.append(f"       What to do: {i['fix']}")
    lines.append(
        "Everything needed to serve the dashboards safely is in place."
        if report["ok"]
        else "Something needed to serve the dashboards safely is missing; fix the items marked FAIL."
    )
    return Result(report, "\n".join(lines), 0 if report["ok"] else 1)


def _when(t: Any) -> str:
    if not isinstance(t, (int, float)):
        return "never"
    d = _dt.datetime.fromtimestamp(float(t))
    return f"{d.day} {d.strftime('%b')} {d.year} {d:%H:%M}"


def _signin_status(config: AppConfig) -> dict[str, Any]:
    auth_dir = config.state.auth
    store = passkeys.PasskeyStore(auth_dir)
    return {
        "app": config.name,
        "password_set": authmod.load_hash(auth_dir) is not None,
        "password_sign_in": store.password_sign_in(),
        "passkeys": store.listing(),
        "recovery": passkeys.RecoveryCodes(auth_dir).status(),
    }


def _status_lines(st: dict[str, Any]) -> list[str]:
    lines = []
    if not st["password_set"]:
        lines.append("Password: not set (set one with easy-ui set-password).")
    else:
        lines.append("Password: set, and " + ("can be used to sign in." if st["password_sign_in"] else "turned off for signing in."))
    if st["passkeys"]:
        lines.append(f"Passkeys ({len(st['passkeys'])}):")
        for p in st["passkeys"]:
            lines.append(f"  {p['name']}  (for {p['rp_id']}, added {_when(p['created'])}, last used {_when(p['last_used'])})  id {p['id']}")
    else:
        lines.append("Passkeys: none yet. Sign in on the dashboards page and open Sign-in settings to add one.")
    r = st["recovery"]
    lines.append(f"Recovery codes: {r['left']} of {r['total']} left." if r["total"] else "Recovery codes: none made yet (easy-ui recovery-codes).")
    return lines


def _cmd_passkeys_list(args: argparse.Namespace) -> Any:
    from easyui.cli import Result, load_config  # noqa: PLC0415

    st = _signin_status(load_config(args))
    return Result(st, "\n".join(_status_lines(st)))


def _cmd_passkeys_remove(args: argparse.Namespace) -> Any:
    from easyui.cli import Result, load_config  # noqa: PLC0415

    config = load_config(args)
    store = passkeys.PasskeyStore(config.state.auth)
    removed = store.remove(args.passkey, password_set=authmod.load_hash(config.state.auth) is not None)
    text = f"Removed the passkey {removed['name']!r}; it can no longer be used to sign in."
    if removed["password_sign_in_turned_on"]:
        text += " It was the last one, so signing in with the password is on again."
    return Result({"ok": True, "app": config.name, "removed": removed, **_signin_status(config)}, text)


def _cmd_signin(args: argparse.Namespace) -> Any:
    from easyui.cli import Result, load_config  # noqa: PLC0415

    config = load_config(args)
    if args.what is None:
        st = _signin_status(config)
        return Result(st, "\n".join(_status_lines(st)))
    if args.what != "password" or args.state not in ("on", "off"):
        raise UsageError("Say easy-ui sign-in password on, or easy-ui sign-in password off.", hint="Or easy-ui sign-in to see how sign-in is set up.")
    store = passkeys.PasskeyStore(config.state.auth)
    on = args.state == "on"
    store.set_password_sign_in(on, password_set=authmod.load_hash(config.state.auth) is not None)
    text = (
        "Signing in with the password is on." if on else
        "Signing in with the password is off: only passkeys and recovery codes sign in now. "
        "Turn it back on with: easy-ui sign-in password on"
    )
    return Result({"ok": True, **_signin_status(config)}, text)


def _cmd_recovery_codes(args: argparse.Namespace) -> Any:
    from easyui.cli import Result, load_config  # noqa: PLC0415

    config = load_config(args)
    st = config.state.ensure()
    tty = authmod.open_terminal()
    if tty is None:
        raise NeedsHuman(
            "Recovery codes are shown only on this computer's terminal, to the person, so they are not shown here "
            "(there is no terminal). Anyone who sees them could sign in.",
            hint="Ask the person to run easy-ui recovery-codes in a terminal on the server, or to make them on the "
            "Sign-in settings page of the dashboards.",
        )
    with tty:
        codes = passkeys.RecoveryCodes(st.auth).make()
        tty.write(
            "\nNew recovery codes for the dashboards of " + config.name + ". Each one signs in once, when you have no "
            "passkey at hand.\nWrite them down or keep them in your password manager. They are shown only now; the "
            "old codes no longer work.\n\n" + "\n".join("  " + c for c in codes) + "\n\n"
        )
        tty.flush()
    text = f"Made {len(codes)} new recovery codes and showed them on the terminal. The old codes no longer work."
    return Result({"ok": True, "app": config.name, "made": len(codes), "shown_on": "terminal"}, text)


def register(subparsers: Any) -> None:
    p = subparsers.add_parser(
        "serve",
        help="Run the dashboard server for this project (only reachable from this computer).",
        description="Run the dashboard server for this project. It listens on 127.0.0.1, on a local socket, or on the "
        "socket systemd passes in. Started by systemd, it stops by itself when no one uses it (systemd starts it "
        "again on the next visit); started by hand, it runs until you stop it with Ctrl-C.",
    )
    p.add_argument("--socket", metavar="PATH", help="listen on this local (unix) socket")
    p.add_argument("--port", type=int, metavar="PORT", help=f"listen on 127.0.0.1:PORT (default {DEFAULT_PORT})")
    p.add_argument("--bind", metavar="ADDRESS", help="a loopback address to listen on (default 127.0.0.1)")
    p.add_argument(
        "--allow-host",
        action="append",
        metavar="NAME",
        help="a host name the dashboards are opened under, e.g. via the private network (repeatable; "
        "also EASYUI_ALLOW_HOSTS)",
    )
    p.add_argument(
        "--idle-minutes",
        type=float,
        default=None,
        metavar="N",
        help=f"stop after N minutes without use (0 = never). Default: {DEFAULT_IDLE_MINUTES:g} when systemd started "
        "it (it starts it again on the next visit), otherwise never",
    )
    p.set_defaults(func=_cmd_serve)

    p = subparsers.add_parser(
        "set-password",
        help="Set the sign-in password (the person types it at a hidden prompt).",
        description="Set the password for signing in to this project's dashboards. The person types it on the "
        "terminal; it is never taken from an argument or an environment variable. Signs out every browser.",
    )
    p.set_defaults(func=_cmd_set_password)

    p = subparsers.add_parser(
        "check",
        help="Check that this project's dashboards can be served safely (a checklist).",
        description="Check, from inside this configuration, that the server would not run as root, that its "
        "private folder is owner-only and that a password is set.",
    )
    p.set_defaults(func=_cmd_check)

    p = subparsers.add_parser(
        "passkeys",
        help="List or remove the passkeys that can sign in.",
        description="List the passkeys that can sign in to this project's dashboards, or remove one. Passkeys are "
        "added on the dashboards page (Sign-in settings), by someone who is signed in.",
    )
    pk_sub = p.add_subparsers(dest="passkeys_command", metavar="<list|remove>")
    p.set_defaults(func=_cmd_passkeys_list)
    q = pk_sub.add_parser("list", help="List the passkeys (also: password and recovery codes).")
    q.set_defaults(func=_cmd_passkeys_list)
    q = pk_sub.add_parser("remove", help="Remove a passkey, by its id or name.")
    q.add_argument("passkey", help="the passkey's id or name (see easy-ui passkeys list)")
    q.set_defaults(func=_cmd_passkeys_remove)

    p = subparsers.add_parser(
        "sign-in",
        help="How signing in is set up; turn password sign-in on or off.",
        description="Show how signing in to the dashboards is set up, or turn signing in with the password on or "
        "off (off needs at least one passkey).",
    )
    p.add_argument("what", nargs="?", choices=["password"], help="password")
    p.add_argument("state", nargs="?", choices=["on", "off"], help="on or off")
    p.set_defaults(func=_cmd_signin)

    p = subparsers.add_parser(
        "recovery-codes",
        help="Make new one-time recovery codes (shown on the terminal only).",
        description="Make 10 new recovery codes, each of which signs in once when no passkey is at hand. They are "
        "shown on this computer's terminal only (never to an agent) and replace the old ones.",
    )
    p.set_defaults(func=_cmd_recovery_codes)

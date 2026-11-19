"""The easy-ui dashboard server (one process serves one app) and its commands.

Commands registered here (see ``easyui.cli`` for the contract):

- ``easy-ui serve``: run the server. It listens on a local socket
  (``--socket PATH``), on ``127.0.0.1:PORT`` for local use (``--port``, default
  8765; ``--bind`` may name another loopback address), or on the socket systemd
  passes in (``LISTEN_FDS``, socket activation). Any address that is not on
  this computer is refused with a plain explanation: the dashboards hold
  private data and are reached from other devices only through the private
  network's proxy. The web addresses it answers to come from ``--origin URL``
  (repeatable, also ``EASYUI_ORIGINS``; ``--allow-host NAME`` / ``EASYUI_ALLOW_HOSTS``
  is shorthand for ``https://NAME``); without any, it answers only to
  ``http://localhost:PORT`` and ``http://127.0.0.1:PORT`` with the port it listens
  on, and on a local socket it needs at least one (``origin_needed`` otherwise). Started by systemd, it stops by itself after
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
``GET /api/session``          ``{signed_in, app: {name, title, accent}, sign_in: {password, password_set,
                              passkeys, passkeys_here, recovery}}`` (``sign_in`` is left out in a preview,
                              which has ``preview: true``). With no password set and no passkey, the page
                              says that nobody can sign in yet and that the owner runs easy-ui set-password.
``POST /api/login``           ``{password}`` -> ``{signed_in: true, app}`` and the session cookie. 403
                              ``password_off`` when password sign-in is switched off; 429 when throttled.
``POST /api/recovery``        ``{code}`` -> signed in; the code is used up. Throttled in its own buckets.
``POST /api/passkeys/options`` ``{purpose: "login" | "register" | "confirm"}`` -> WebAuthn options
                              (base64url). Login also sets the short-lived passkey cookie its challenge
                              is bound to; register and confirm need a recent sign-in (``sign_in_again``
                              otherwise) and are bound to the session.
``POST /api/passkeys/login``  ``{credential}`` -> signed in. Never throttled. Refused: ``unknown_passkey``,
                              ``other_address``, ``passkey_stale``, ``passkey_replayed``, ``passkey_refused``.
``POST /api/passkeys/register`` ``{credential, name, confirm?}`` -> the new passkey (recent sign-in).
``POST /api/passkeys/remove`` ``{id, confirm?}`` (recent sign-in); removing the last one turns the password
                              back on.
``GET /api/signin``           the sign-in settings: passkeys (id, name, created, last used), password
                              on/off, recovery codes left, ``confirm_needed``, ``confirm_possible``.
``POST /api/signin/password`` ``{on, confirm?}`` (recent sign-in; off only while a passkey exists).
``POST /api/recovery-codes``  ``{confirm?}`` -> ten new one-time codes, shown once (recent sign-in).
Removing a passkey, switching password sign-in and making new recovery codes end every other
session (the current one stays). When the current session was made with the password or a
recovery code and a passkey exists, these and adding a passkey also need ``confirm``: a fresh
answer to a ``confirm`` challenge by a saved passkey (``confirm_with_passkey`` otherwise). A
passkey added in that same session cannot give it (``sign_in_with_new_passkey``;
``confirm_possible`` is false when no other passkey exists); the very first passkey needs only a
recent sign-in. So an owner who has lost every passkey cannot add one on the web page: they remove
the lost ones on the server (``easy-ui passkeys remove``, as the account that runs the dashboards),
which turns password sign-in back on with the last one, then sign in and add a new one.
Passkeys work on ``http://localhost:PORT`` and over HTTPS. The RP ID and the origins a passkey
answer may come from are the server's configured origins (``--origin``; see ``easyui.security``),
never taken from the request; each saved passkey keeps the RP ID it was made with.
``POST /api/logout``          ``{signed_in: false}``; the session is removed.
``GET /api/dashboards``       ``[{slug, title, description}]`` (published dashboards only), in the order of
                              ``[ui] dashboards`` in project.toml, then the others by name.
``GET /api/dashboards/{slug}`` the compiled dashboard (no SQL).
``POST /api/query``           ``{dashboard, queries: [names], params}`` ->
                              ``{results: {name: {columns, rows, truncated}}}``. A query that fails
                              gives ``{error: {code, message, hint}}`` in its own slot, so one broken
                              chart does not blank the page. All the queries of one request share
                              ``request_time_s`` (4 s); each one also has the sandbox's own limit (2 s).
                              They run in a worker process (``easyui.workers``) that is killed when a
                              query outlives its limit, so no query runs on past it. At most
                              ``query_slots`` (4) requests run queries at once; another one waits at most
                              ``slot_wait_s`` (4 s), then gets 503 ``busy`` ("try again in a few seconds").
``GET /api/rev``              ``{rev}``; changes when the data, the published dashboards or their look change.
``GET /api/styles``           ``{rev, theme_css, stylesheets: [names], app_stylesheets: [paths], assets: n}``: the look the dashboards use
                              (``problem`` when the published look is damaged; the page then keeps easy-ui's own).
``GET /ui/theme.css``, ``/ui/styles/<name>.css``, ``/ui/assets/<path>``  the project's generated theme stylesheet,
                              its stylesheets and its fonts and pictures (signed in only; content type from the
                              file's checked type; SVG with a sandboxing Content-Security-Policy and as a download
                              when opened on its own).
Errors are ``{"error": {"code", "message", "hint"}}`` with a fitting HTTP status (429 adds
``Retry-After``). Messages never contain stack traces or file paths.

The static page is served from ``easyui/web/``: ``/`` is ``index.html``
(``Cache-Control: no-cache``), ``/assets/...`` are the files under
``web/assets/`` (a file name with a content hash, such as ``app-3f9a1c2e.js``,
is cached for a year as immutable; others revalidate with an ETag). Only known
file types are served, never a folder listing, never anything outside those
folders. Other simple one-level page paths (``/overview``) get ``index.html``
too; deeper ones (``/d/overview``) get a short "no page here" page (404) with a
link back to the start, because the page's own links are relative to its folder.

Cookies: the session cookie is ``easyui-<app>`` on plain local http and
``__Host-easyui-<app>`` over HTTPS (then also ``Secure``); always ``HttpOnly``,
``SameSite=Strict`` and ``Path=/``. The device token (see ``easyui.auth``)
uses the same rules with ``-device`` appended, and the passkey challenge cookie with ``-passkey``
(5 minutes).

Dashboards come from a *source* with the methods ``list()``, ``compiled(slug,
today=...)``, ``dashboard(slug)``, ``bundle_path()`` and ``rev()``. By default
it is ``easyui.publish.PublishedSource`` (the published version only); the
preview passes ``easyui.preview.WorkingSource`` together with a
``preview_token``. With a preview token, opening (GET, never HEAD) ``/?token=<token>`` (any page
path) signs that browser in and redirects to the same address without the
token, so the token does not stay in the address bar or the history. A token
works once: it is replaced at once and ``on_preview_link_used(new_token)`` is
called (the preview prints the new link). Preview cookies are named
``easyui-<app>-preview``, so they never replace a real server's cookie.

Public API
----------
``WEB_DIR``, ``DEFAULT_PORT``, ``DEFAULT_IDLE_MINUTES``, ``MAX_QUERIES_PER_REQUEST``
``ServerSettings`` (dataclass): ``origins`` (the public web addresses), ``local_port`` (the
    127.0.0.1 port, for local use when there are no origins), ``unix_socket`` (it listens on a local socket,
    so ``X-Forwarded-Host`` names the host; see ``easyui.security``), ``idle_seconds``, ``max_body``,
    ``max_queries``, ``query_slots`` (also the number of query workers), ``slot_wait_s``, ``worker_idle_s``
    (an unused worker stops after this long), ``limits``, ``web_dir``, ``session_idle``, ``session_absolute``,
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
``configured_origins(origins=(), hosts=(), env=None) -> tuple[str, ...]``  ``--origin``, ``--allow-host``,
                                          ``EASYUI_ORIGINS`` and ``EASYUI_ALLOW_HOSTS`` together.
``open_listener(*, socket_path=None, port=None, bind=None, env=None) -> (socket, url_or_path, kind)``
    ``--socket PATH`` makes the socket owner-only (0600) and refuses (``socket_dir_unsafe``) a folder that
    another account can write to or change.
``settings_for_listener(sock, kind, origins=(), **settings) -> ServerSettings``  ``local_port`` and
    ``unix_socket`` from the socket itself.
``safe_auth_dir(config) -> Path``          the sign-in folder; ``Refused`` (``auth_dir_unsafe``) when it is a
                                          link, another account's, not 0700, or in a folder others can change.
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
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import Mount, Route

from easyui import auth as authmod
from easyui import bundle, passkeys, paths, sandbox, security, spec, webauthn, workers
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
SLOT_WAIT_S = 4.0  # how long a request waits for a free query slot before it gets "busy, try again"

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
    origins: tuple[str, ...] = ()  # the public web addresses, e.g. ("https://box.example.ts.net",)
    local_port: int | None = None  # the 127.0.0.1 port it listens on (local use when no origins are given)
    idle_seconds: float = DEFAULT_IDLE_MINUTES * 60
    max_body: int = security.DEFAULT_MAX_BODY
    max_queries: int = MAX_QUERIES_PER_REQUEST
    query_slots: int = 4
    slot_wait_s: float = SLOT_WAIT_S
    worker_idle_s: float = workers.IDLE_S
    limits: sandbox.Limits = sandbox.DEFAULT_LIMITS
    web_dir: Path = WEB_DIR
    session_idle: int = IDLE_SECONDS
    session_absolute: int = ABSOLUTE_SECONDS
    request_time_s: float = REQUEST_TIME_S
    # The server listens on a local (unix) socket: set from the listening socket's family (settings_for_listener),
    # never from a request. Only then does X-Forwarded-Host name the request's host (see easyui.security).
    unix_socket: bool = False


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
    pool: workers.WorkerPool = field(default_factory=lambda: workers.WorkerPool(4))
    challenges: passkeys.Challenges = field(default_factory=passkeys.Challenges)
    clock: Callable[[], float] = time.time
    origins: security.Origins = field(default_factory=security.Origins)
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
        "password_set": has_password,
        "passkeys": n > 0,
        "passkeys_here": passkeys_usable_here(request),
        "recovery": n > 0 and passkeys.RecoveryCodes(st.config.state.auth).left() > 0,
    }


def passkeys_usable_here(request: Request) -> bool:
    """Whether passkeys can work on the configured address this page was opened with."""
    site = security.request_info(request).site
    return site is not None and site.rp_id is not None


def passkey_site(request: Request) -> tuple[str, frozenset[str]]:
    """The RP ID and the origins a passkey answer must come from, for this request.

    Both come from the server's configuration (``Origins``), never from the request: the request only
    says which configured address it was made on (the middleware matched its Host, name and port). Passkeys
    need https, except on http://localhost for local use; an IP address cannot be an RP ID at all.
    """
    st = _state(request)
    info = security.request_info(request)
    site = info.site
    origin = request.headers.get("origin")
    if site is None or (origin is not None and origin.strip().lower() != str(site)):
        raise NotAllowed(
            "This request did not come from the dashboards page itself, so passkeys were not used.",
            hint="Open the dashboards page directly and try again.",
            code="cross_site",
        )
    if site.is_ip:
        raise NotAllowed(
            "Passkeys do not work when the dashboards are opened with a number address such as 127.0.0.1.",
            hint="Open them with a name instead, for example http://localhost:PORT on this computer, or the https "
            "address of your private network.",
            code="passkeys_unavailable",
        )
    if site.rp_id is None:
        raise NotAllowed(
            "Passkeys only work over a secure (https) connection, and this page was opened without one.",
            hint="Open the dashboards with their https address (for example the one your private network gives), "
            "or with http://localhost on this computer.",
            code="passkeys_unavailable",
        )
    return site.rp_id, st.origins.passkey_origins(site.rp_id)


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


def _confirm_needed(st: ServerState, session: Session) -> bool:
    """A session made with the password or a recovery code must confirm with a passkey, when one exists."""
    return session.how != "passkey" and passkeys.PasskeyStore(st.config.state.auth, st.clock).count() > 0


def _confirm_possible(st: ServerState, session: Session) -> bool:
    """Whether a passkey that was not added in this session exists (only such a passkey can confirm here)."""
    mark = passkeys.added_in(session.key)
    creds = passkeys.PasskeyStore(st.config.state.auth, st.clock).credentials()
    return any(c.get("added_in") != mark for c in creds.values())


# Said whenever the web page cannot make a sign-in change because no saved passkey can confirm it.
_LOST_PASSKEYS_HINT = (
    "If you no longer have any of your passkeys, this cannot be done on the web page: on the server, as the account "
    "that runs the dashboards, list them with easy-ui passkeys and remove each lost one with "
    "easy-ui passkeys remove NAME. When the last one is removed, password sign-in is on again, and you can sign "
    "in with the password and add a new passkey."
)


def _settings(st: ServerState, request: Request, session: Session) -> dict[str, Any]:
    auth_dir = st.config.state.auth
    store = passkeys.PasskeyStore(auth_dir, st.clock)
    left = passkeys.RECENT_SIGN_IN - (st.clock() - session.created)
    site = security.request_info(request).site
    here = site.rp_id if site is not None else None
    return {
        "password_set": authmod.load_hash(auth_dir) is not None,
        "password_sign_in": store.password_sign_in(),
        "passkeys": [{k: v for k, v in p.items() if k != "rp_id"} | {"here": here is not None and p["rp_id"] == here}
                     for p in store.listing()],
        "max_passkeys": passkeys.MAX_PASSKEYS,
        "recovery": passkeys.RecoveryCodes(auth_dir).status(),
        "passkeys_here": passkeys_usable_here(request),
        "can_change_for": max(0, int(left)),
        "confirm_needed": _confirm_needed(st, session),
        "confirm_possible": _confirm_possible(st, session),
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


def _sign_out_others(st: ServerState, session: Session, why: str) -> int:
    """End every session except this one, after a change to how signing in works."""
    n = st.sessions.clear_others(session.key)
    if n:
        log.warning("%s; %d other browser(s) were signed out", why, n)
    return n


async def api_signin_settings(request: Request) -> Response:
    session = require_session(request)
    st = _state(request)
    _no_passkeys_in_preview(st)
    return JSONResponse(await run_in_threadpool(_settings, st, request, session))


def _get_options(challenge: bytes, rp_id: str, allow: list[str] | None = None) -> dict[str, Any]:
    return {"publicKey": {
        "challenge": webauthn.b64url_encode(challenge),
        "rpId": rp_id,
        "timeout": 120_000,
        "userVerification": "required",
        "allowCredentials": [{"type": "public-key", "id": cid} for cid in (allow or [])],
    }}


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
        resp = JSONResponse(_get_options(challenge, rp_id))
        _set_cookie(resp, passkey_cookie(st.config.name, info.https), token, passkeys.CHALLENGE_SECONDS, info.https)
        return resp
    if purpose == "confirm":
        session = require_session(request)
        _require_recent(st, session)
        challenge = st.challenges.issue("confirm", session.key)
        here = [(cid, c) for cid, c in (await run_in_threadpool(store.credentials)).items() if c.get("rp_id") == rp_id]
        mark = passkeys.added_in(session.key)
        mine = [cid for cid, c in here if c.get("added_in") != mark]  # one added in this session cannot confirm it
        if here and not mine:
            raise NotAllowed(
                "The passkeys for this address were added after this page signed in with the password or a recovery "
                "code, so they cannot confirm changes for this same sign-in.",
                hint="Sign out, sign in with your passkey, and make the change then.",
                code="sign_in_with_new_passkey",
            )
        if not mine:
            raise NotAllowed(
                "None of the saved passkeys was made for this address, so it cannot confirm the change here.",
                hint="Open the dashboards at the address you added your passkey on, or make the change on the server "
                "with easy-ui passkeys, easy-ui sign-in or easy-ui recovery-codes.",
                code="other_address",
            )
        return JSONResponse(_get_options(challenge, rp_id, mine))
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
    raise UsageError("Say what the passkey is for: login, confirm or register.", hint="Reload the page and try again.")


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


def _verify_passkey(st: ServerState, request: Request, credential: Any, purpose: str, binding: str) -> str:
    """Check one passkey answer to a challenge issued for ``purpose`` and ``binding``; the credential's id."""
    rp_id, origins = passkey_site(request)
    challenge = _challenge_of(credential)
    st.challenges.check(challenge, purpose, binding)
    store = passkeys.PasskeyStore(st.config.state.auth, st.clock)
    cid = webauthn.b64url_encode(webauthn.credential_id_of(credential))
    saved = store.get(cid)
    if saved is None:
        log.warning("refused a passkey that is not saved for these dashboards")
        raise NotAllowed(
            "This passkey is not one of the passkeys saved for these dashboards, so it was not accepted.",
            hint="Choose another passkey, or sign in another way and add this one on the Sign-in settings page.",
            code="unknown_passkey",
        )
    saved_rp = saved.get("rp_id")
    if saved_rp != rp_id or saved_rp not in st.origins.rp_ids():
        raise NotAllowed(
            f"This passkey was added for the address {saved_rp}, not for this one, so it cannot be used here.",
            hint=f"Open the dashboards at {saved_rp}, or add a passkey for this address.",
            code="other_address",
        )
    try:
        public_key = webauthn.b64url_decode(saved["public_key"], what="the saved key", limit=4096)
        alg = int(saved["alg"])
        stored_count = int(saved.get("sign_count") or 0)
        be = saved.get("backup_eligible")
        backup_eligible = be if isinstance(be, bool) else None
    except (KeyError, TypeError, ValueError):
        raise NotAllowed("The saved passkey could not be read.", hint="Remove it and add it again.", code="passkey_damaged") from None
    result = webauthn.verify_assertion(
        credential, challenge=challenge, origins=origins, rp_id=saved_rp, public_key=public_key, alg=alg,
        stored_sign_count=stored_count, user_id=store.user_id(), backup_eligible=backup_eligible,
    )
    st.challenges.accept(challenge)  # after it verified; refuses a second copy of the same answer
    store.touch(cid, result.sign_count, result.backed_up)  # refuses a passkey removed in the meantime
    return cid


def _passkey_login(st: ServerState, request: Request, credential: Any) -> authmod.SignIn:
    info = security.request_info(request)
    token = request.cookies.get(passkey_cookie(st.config.name, info.https)) or ""
    _verify_passkey(st, request, credential, "login", hashlib.sha256(token.encode()).hexdigest())
    session_cookie, device_cookie = cookie_names(st.config.name, info.https, st.preview)
    return st.auth.sign_in_passkey(
        device_secret=request.cookies.get(device_cookie), old_session=request.cookies.get(session_cookie)
    )


def _require_confirmation(st: ServerState, request: Request, session: Session, confirm: Any) -> bool:
    """For a session made with the password or a recovery code: a fresh answer from one of the saved passkeys,
    one that was not added in this same session. Whether a confirmation was needed (and given)."""
    if not _confirm_needed(st, session):
        return False
    if confirm is None:
        raise NotAllowed(
            "This page signed in with the password or a recovery code, so this change must be confirmed with one of "
            "your saved passkeys first.",
            hint="Confirm with one of your passkeys when the page asks, or sign in with a passkey and make the "
            "change then. " + _LOST_PASSKEYS_HINT,
            code="confirm_with_passkey",
        )
    cid = _verify_passkey(st, request, confirm, "confirm", session.key)
    saved = passkeys.PasskeyStore(st.config.state.auth, st.clock).get(cid) or {}
    if saved.get("added_in") == passkeys.added_in(session.key):
        raise NotAllowed(
            "This passkey was added after this page signed in with the password or a recovery code, so it cannot "
            "confirm changes for this same sign-in.",
            hint="Sign out, sign in with this passkey, and make the change then. Or confirm with a passkey you "
            "added before.",
            code="sign_in_with_new_passkey",
        )
    return True


async def api_passkey_login(request: Request) -> Response:
    st = _state(request)
    _no_passkeys_in_preview(st)
    body = await json_body(request, ("credential",))
    result = await run_in_threadpool(_passkey_login, st, request, body.get("credential"))
    resp = _signed_in_response(st, request, result)
    info = security.request_info(request)
    _clear_cookie(resp, passkey_cookie(st.config.name, info.https), info.https)
    return resp


def _passkey_register(
    st: ServerState, request: Request, session: Session, credential: Any, name: Any, confirm: Any = None
) -> dict[str, Any]:
    _require_recent(st, session)
    rp_id, origins = passkey_site(request)
    challenge = _challenge_of(credential)
    st.challenges.check(challenge, "register", session.key)
    # Adding a passkey is a sign-in change like the others: once a passkey exists, a session made with the
    # password or a recovery code must confirm it with a saved passkey (else it could add its own passkey,
    # sign in with it, and then change everything without confirming). The very first passkey needs only a
    # recent sign-in; that it is still the first is checked again when it is saved.
    confirmed = _require_confirmation(st, request, session, confirm)
    reg = webauthn.verify_registration(credential, challenge=challenge, origins=origins, rp_id=rp_id)
    st.challenges.accept(challenge)
    store = passkeys.PasskeyStore(st.config.state.auth, st.clock)
    added = store.add(
        reg, rp_id=rp_id, name=name if isinstance(name, str) else None,
        added_in=passkeys.added_in(session.key) if session.how != "passkey" else None,
        only_if_first=session.how != "passkey" and not confirmed,
    )
    log.warning("a passkey was added (%s)", added["name"][:60])
    return {"added": {k: v for k, v in added.items() if k != "rp_id"}, **_settings(st, request, session)}


async def api_passkey_register(request: Request) -> Response:
    session = require_session(request)
    st = _state(request)
    _no_passkeys_in_preview(st)
    body = await json_body(request, ("credential", "name", "confirm"))
    return JSONResponse(await run_in_threadpool(
        _passkey_register, st, request, session, body.get("credential"), body.get("name"), body.get("confirm")
    ))


def _passkey_remove(st: ServerState, request: Request, session: Session, cid: Any, confirm: Any) -> dict[str, Any]:
    _require_recent(st, session)
    if not isinstance(cid, str) or not cid or len(cid) > 2000:
        raise UsageError("Say which passkey to remove.", hint="Reload the page and try again.")
    store = passkeys.PasskeyStore(st.config.state.auth, st.clock)
    if cid not in store.credentials():
        raise NotFound("That passkey is not saved (any more).", hint="Reload the page.", code="no_such_passkey")
    _require_confirmation(st, request, session, confirm)
    removed = store.remove(cid, password_set=authmod.load_hash(st.config.state.auth) is not None)
    log.warning("a passkey was removed (%s)", removed["name"][:60])
    n = _sign_out_others(st, session, "a passkey was removed")
    return {"removed": removed, "signed_out_others": n, **_settings(st, request, session)}


async def api_passkey_remove(request: Request) -> Response:
    session = require_session(request)
    st = _state(request)
    _no_passkeys_in_preview(st)
    body = await json_body(request, ("id", "confirm"))
    return JSONResponse(await run_in_threadpool(_passkey_remove, st, request, session, body.get("id"), body.get("confirm")))


def _password_switch(st: ServerState, request: Request, session: Session, on: Any, confirm: Any) -> dict[str, Any]:
    _require_recent(st, session)
    if not isinstance(on, bool):
        raise UsageError("Say whether password sign-in should be on or off.", hint="Reload the page and try again.")
    _require_confirmation(st, request, session, confirm)
    store = passkeys.PasskeyStore(st.config.state.auth, st.clock)
    store.set_password_sign_in(on, password_set=authmod.load_hash(st.config.state.auth) is not None)
    log.warning("password sign-in was turned %s", "on" if on else "off")
    n = _sign_out_others(st, session, f"password sign-in was turned {'on' if on else 'off'}")
    return {"signed_out_others": n, **_settings(st, request, session)}


async def api_password_switch(request: Request) -> Response:
    session = require_session(request)
    st = _state(request)
    _no_passkeys_in_preview(st)
    body = await json_body(request, ("on", "confirm"))
    return JSONResponse(await run_in_threadpool(_password_switch, st, request, session, body.get("on"), body.get("confirm")))


def _new_codes(st: ServerState, request: Request, session: Session, confirm: Any) -> dict[str, Any]:
    _require_recent(st, session)
    _require_confirmation(st, request, session, confirm)
    codes = passkeys.RecoveryCodes(st.config.state.auth, st.clock).make()
    log.warning("new recovery codes were made; the old ones no longer work")
    n = _sign_out_others(st, session, "new recovery codes were made")
    return {"codes": codes, "signed_out_others": n, **_settings(st, request, session)}


async def api_recovery_codes(request: Request) -> Response:
    session = require_session(request)
    st = _state(request)
    _no_passkeys_in_preview(st)
    body = await json_body(request, ("confirm",))
    return JSONResponse(await run_in_threadpool(_new_codes, st, request, session, body.get("confirm")))


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
    # A request waits a few seconds at most for a free slot, then hears "busy" (503) instead of queueing.
    try:
        async with asyncio.timeout(st.settings.slot_wait_s):
            await st.query_slots.acquire()
    except TimeoutError:
        raise TemporaryFailure(
            "The dashboards are busy with other requests right now, so this one was not run.",
            hint="Try again in a few seconds.",
            code="busy",
        ) from None
    try:
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
            pool=st.pool,
        )
    finally:
        st.query_slots.release()
    return JSONResponse(_scrub_results(out))


async def api_rev(request: Request) -> Response:
    require_session(request)
    st = _state(request)
    return JSONResponse({"rev": await run_in_threadpool(st.source.rev)})


def _source_styles(st: ServerState) -> Any:
    from easyui import styles  # noqa: PLC0415

    get = getattr(st.source, "styles", None)
    return get() if callable(get) else styles.EMPTY


async def api_styles(request: Request) -> Response:
    """Which theme stylesheet and project stylesheets the dashboards use (the files are under /ui/)."""
    require_session(request)
    st = _state(request)
    try:
        got = await run_in_threadpool(_source_styles, st)
    except EasyUIError as e:
        log.warning("the published look could not be used: %s", e.message)
        return JSONResponse({"rev": "none", "theme_css": False, "stylesheets": [], "app_stylesheets": [], "assets": 0,
                             "problem": {"code": e.code, "message": security.scrub(e.message),
                                         "hint": security.scrub(e.hint)}})
    return JSONResponse(got.public())


_UI_FILE_RE = re.compile(
    r"^(?:theme\.css|(?:apps/[a-z][a-z0-9-]{1,30}/)?(?:styles/[a-z0-9][a-z0-9._-]{0,60}\.css"
    r"|assets/[A-Za-z0-9][A-Za-z0-9._/-]{0,300}))\Z")


async def page_ui_file(request: Request) -> Response:
    """``/ui/theme.css``, ``/ui/styles/<name>.css``, ``/ui/assets/<path>`` and an app's
    ``/ui/apps/<app>/styles|assets/...``: the project's look, after sign-in."""
    st = _state(request)
    if current_session(request) is None:
        return _error_response(SignInRequired("You are not signed in, so the dashboards stay hidden.",
                                              hint="Sign in with this project's password."))
    rel = request.url.path[len("/ui/"):]
    if not _UI_FILE_RE.match(rel) or "//" in rel or "/." in rel or rel.endswith("/"):
        raise HTTPException(404)
    try:
        got = await run_in_threadpool(lambda: _source_styles(st).get(rel))
    except EasyUIError:
        got = None
    if got is None:
        raise HTTPException(404)
    data, ctype = got
    etag = '"' + hashlib.sha256(data).hexdigest()[:32] + '"'
    headers = {"etag": etag, "cache-control": REVALIDATE}
    if ctype.startswith("image/svg+xml"):
        # An SVG is only ever a picture here: opened on its own it would be a document, so it is a download in a
        # sandbox that can run nothing.
        headers["content-security-policy"] = "default-src 'none'; sandbox"
        headers["content-disposition"] = 'attachment; filename="image.svg"'
    inm = request.headers.get("if-none-match")
    if inm and etag in [t.strip().removeprefix("W/") for t in inm.split(",")]:
        return Response(status_code=304, headers=headers)
    return Response(data, media_type=ctype, headers=headers)


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
    # Only a real page visit (GET) uses up the one-time link; a HEAD (a link preview, a checker) never does.
    if not st.preview_token or token is None or request.method != "GET":
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
    if len(parts) == 1 and _PAGE_SEGMENT_RE.match(parts[0]):
        return _index(request)
    if all(_PAGE_SEGMENT_RE.match(p) for p in parts):
        # The page's own files and requests are relative to its folder (so it also works below a path on a
        # shared host), so it is served only one level deep; deeper, a short page leads back to the start.
        return _not_found_page(request)
    raise HTTPException(404)


def _not_found_page(request: Request) -> Response:
    up = "../" * max(0, request.url.path.count("/") - 1) or "./"
    return HTMLResponse(
        "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n<title>No page here</title>\n"
        f"<link rel=\"stylesheet\" href=\"{up}assets/style.css\">\n</head>\n<body>\n<main class=\"notfound\">\n"
        "<h1>There is no page at this address</h1>\n"
        f"<p><a href=\"{up}\">Open the dashboards</a></p>\n</main>\n</body>\n</html>\n",
        status_code=404,
        headers={"cache-control": REVALIDATE},
    )


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
        pool=workers.WorkerPool(max(1, settings.query_slots), idle_s=settings.worker_idle_s,
                                wait_s=settings.slot_wait_s),
        challenges=passkeys.Challenges(clock),
        clock=clock,
        origins=security.Origins(settings.origins, local_port=settings.local_port),
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):  # type: ignore[no-untyped-def]
        task = None
        if on_idle is not None and settings.idle_seconds > 0:
            task = asyncio.create_task(_idle_watch(state, settings.idle_seconds, on_idle))
        try:
            yield
        finally:
            state.pool.close()
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
        Route("/styles", api_styles, methods=["GET"]),
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
        Route("/ui/{path:path}", page_ui_file, methods=["GET"]),
        Route("/{path:path}", page_other, methods=["GET"]),
    ]
    app = Starlette(
        debug=False,
        routes=routes,
        exception_handlers={EasyUIError: _on_easyui_error, HTTPException: _on_http_error, Exception: _on_unexpected},
        lifespan=lifespan,
    )
    app.state.easyui = state
    outer = security.SecurityMiddleware(app, origins=state.origins, max_body=settings.max_body,
                                        unix_socket=settings.unix_socket)
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
    _socket_folder(path)
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
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o177)  # owner-only from the moment it exists: whoever can connect is trusted to state the host
    try:
        sock.bind(str(path))
    except OSError as e:
        sock.close()
        raise Refused(
            f"easy-ui could not make the socket {paths.display_path(path)} ({(e.strerror or 'refused').lower()}), so "
            "the server did not start.",
            hint="Choose a --socket path in a folder this account owns and may write to.",
            code="socket_unusable",
        ) from None
    finally:
        os.umask(old)
    os.chmod(path, 0o600)
    return sock


def _socket_folder(path: Path) -> None:
    """Make the socket's folder (0700) when missing, and refuse one another account can write to or change.

    A proxy that forwards to a local socket states the request's host (``X-Forwarded-Host``), and easy-ui
    believes it, so only the account running the server (and root, which runs the proxy) may reach the socket:
    in a folder others can write, another account could put its own socket in its place."""
    folder = path.parent
    try:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        st = os.stat(folder)
        link = os.lstat(folder)
    except OSError as e:
        raise Refused(
            f"The folder for the socket, {paths.display_path(folder)}, does not exist and cannot be created "
            f"({(e.strerror or 'refused').lower()}), so the server did not start.",
            hint="Choose a --socket path in a folder this account owns, for example one made with mkdir -m 700.",
            code="socket_dir_unsafe",
        ) from None
    unsafe = None
    if link.st_uid not in (0, os.geteuid()):
        unsafe = f"{paths.display_path(folder)} is a link that belongs to another account"
    elif st.st_uid not in (0, os.geteuid()):
        unsafe = f"{paths.display_path(folder)} belongs to another account"
    elif st.st_mode & 0o022:
        unsafe = f"other accounts can write to {paths.display_path(folder)}"
    else:
        real = Path(os.path.realpath(folder))
        for d in real.parents:
            with contextlib.suppress(OSError):
                if paths._changeable_by_others(os.stat(d)):
                    unsafe = f"another account can change {paths.display_path(d)}, which holds it"
                    break
    if unsafe:
        raise Refused(
            f"easy-ui will not listen on {paths.display_path(path)}, because {unsafe}. Whoever can connect to the "
            "socket is trusted to say which web address a request was made on, so the socket must stay reachable "
            "only by this account (and the local proxy, which runs as root).",
            hint="Choose a --socket path in a folder only this account can write to, for example one made with "
            "mkdir -m 700 (a socket in /tmp itself is refused).",
            code="socket_dir_unsafe",
        )


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


def _group_name(gid: int) -> str:
    try:
        import grp  # noqa: PLC0415

        return grp.getgrgid(gid).gr_name
    except (ImportError, KeyError, OSError):
        return str(gid)


def _ui_group_item(config: AppConfig) -> dict[str, Any]:
    """On a server: the ui folder, data/ and specs/ let in exactly one group, the web account's, and nobody else."""
    st = config.state
    expected = f"mes-{config.name}-web"
    mine = {os.getegid(), *os.getgroups()}
    problems = []
    gids = set()
    for d in (st.root, st.data, st.specs):
        try:
            s = os.lstat(d)
        except OSError:
            continue
        gids.add(s.st_gid)
        if stat.S_IMODE(s.st_mode) & 0o007:
            problems.append(f"{paths.display_path(d)} lets every account in")
        name = _group_name(s.st_gid)
        # Run by the web account, the group must be one of its own; run by the project's account, the web one's.
        if not (s.st_gid in mine and s.st_uid != os.geteuid()) and name != expected:
            problems.append(f"{paths.display_path(d)} is readable by the group {name}, not {expected}")
    if len(gids) > 1:
        problems.append("the folders do not all belong to the same group")
    return _item(
        "ui_folder_group",
        "The project's dashboard folder is readable only by the web account's group",
        "fail" if problems else "ok",
        "On a server the web account reads the dashboards and their data through one group; any other group or "
        "account that can read the folder can read the dashboard data too." + (f" Found: {'; '.join(problems)}." if problems else ""),
        f"Give the folders to that group and close them to others: chgrp -R {expected} {st.root} && chmod -R o-rwx "
        f"{st.root}" if problems else None,
    )


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

    if shared:
        items.append(_ui_group_item(config))

    try:
        paths.check_auth_dir(st.auth)
        status, why_not, fix = "ok", "", None
    except EasyUIError as e:
        status, why_not, fix = "fail", " " + e.message, e.hint
    items.append(
        _item(
            "auth_dir_safe",
            "The sign-in folder is a real folder of this account (0700), outside the folder the project's account writes",
            status,
            "Anyone who can replace the sign-in folder can plant a password, a passkey or a session." + why_not,
            fix,
        )
    )

    try:
        w = workers._Worker(workers.MEMORY_LIMIT, sandbox.HEAP_LIMIT)
        w.kill()
        status, fix, why_not = "ok", None, ""
    except EasyUIError as e:
        status, fix, why_not = "fail", e.hint, f" ({e.detail or e.message})"
    items.append(
        _item(
            "query_workers",
            "Dashboard queries can run in their own worker processes",
            status,
            "Every query runs in a separate process that is stopped when its time is up; without one, no dashboard "
            "can show its numbers." + why_not,
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


def configured_origins(origins: Any = (), hosts: Any = (), env: dict[str, str] | None = None) -> tuple[str, ...]:
    """The public origins from ``--origin`` / ``EASYUI_ORIGINS`` and the ``--allow-host`` / ``EASYUI_ALLOW_HOSTS``
    shorthand (``NAME`` means ``https://NAME``), normalised, in order, without repeats. Raises ``UsageError``."""
    env = os.environ if env is None else env
    out: list[security.Origin] = []
    raw_origins = list(origins or []) + re.split(r"[,\s]+", env.get("EASYUI_ORIGINS", ""))
    raw_hosts = list(hosts or []) + re.split(r"[,\s]+", env.get("EASYUI_ALLOW_HOSTS", ""))
    for text, parse in [(o, security.parse_origin) for o in raw_origins] + [(h, security.origin_for_host) for h in raw_hosts]:
        text = text.strip() if isinstance(text, str) else text
        if not text:
            continue
        try:
            out.append(parse(text))
        except ValueError as e:
            raise UsageError(
                str(e),
                hint="Give the address the dashboards are opened with, for example --origin "
                "https://myserver.example.ts.net (or --allow-host myserver.example.ts.net for the same thing).",
                code="bad_origin",
            ) from None
    return tuple(dict.fromkeys(str(o) for o in out))


def _listening_port(sock: socket.socket, kind: str) -> int | None:
    if kind == "unix" or sock.family not in (socket.AF_INET, socket.AF_INET6):
        return None
    return int(sock.getsockname()[1])


def settings_for_listener(sock: socket.socket, kind: str, origins: tuple[str, ...] = (), **kw: Any) -> ServerSettings:
    """The ``ServerSettings`` for the socket ``serve`` listens on (``--socket``, ``--port`` or systemd's): the local
    port for TCP, and ``unix_socket`` from the socket's own family, so a request can never claim it."""
    return ServerSettings(origins=tuple(origins), local_port=_listening_port(sock, kind),
                          unix_socket=sock.family == socket.AF_UNIX, **kw)


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
    safe_auth_dir(config)
    idle_minutes = resolve_idle_minutes(args.idle_minutes)
    if idle_minutes < 0:
        raise UsageError("--idle-minutes must be 0 (never stop) or more.", hint="For example: --idle-minutes 10")
    origins = configured_origins(args.origin, args.allow_host)

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
            settings = settings_for_listener(sock, kind, origins, idle_seconds=idle_minutes * 60)
            local_port = settings.local_port
            if not origins and local_port is None:
                raise UsageError(
                    "The server listens on a local socket but was not told the web address the dashboards are opened "
                    "with, so it would refuse every request. It did not start.",
                    hint="Give that address, for example: easy-ui serve --socket PATH --origin "
                    "https://myserver.example.ts.net (the service files from easy-ui units --origin do this).",
                    code="origin_needed",
                )
            security.verify_vendored(settings.web_dir)
            sandbox.ensure_heap_limit()  # queries run in workers (easyui.workers), each with its own cap
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
            log.info("serving the dashboards of %s on %s for %s; it stops %s", config.name, where,
                     security.Origins(origins, local_port=local_port).describe(), idle)
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


def safe_auth_dir(config: AppConfig) -> Path:
    """The sign-in folder, created when missing (where this account may) and refused when unsafe. Only the
    sign-in folder is made: the sign-in commands do not need the ui folder."""
    st = config.state.ensure_auth()
    return paths.check_auth_dir(st.auth)


def _cmd_set_password(args: argparse.Namespace) -> Any:
    from easyui.cli import Result, load_config  # noqa: PLC0415

    config = load_config(args)
    auth_dir = safe_auth_dir(config)
    password = authmod.prompt_new_password()
    res = authmod.set_password(auth_dir, password)
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

    config = load_config(args)
    safe_auth_dir(config)
    st = _signin_status(config)
    return Result(st, "\n".join(_status_lines(st)))


def _cmd_passkeys_remove(args: argparse.Namespace) -> Any:
    from easyui.cli import Result, load_config  # noqa: PLC0415

    config = load_config(args)
    safe_auth_dir(config)
    store = passkeys.PasskeyStore(config.state.auth)
    removed = store.remove(args.passkey, password_set=authmod.load_hash(config.state.auth) is not None)
    n = SessionStore(config.state.auth).clear()
    text = f"Removed the passkey {removed['name']!r}; it can no longer be used to sign in."
    if removed["password_sign_in_turned_on"]:
        text += " It was the last one, so signing in with the password is on again."
    text += _signed_out_text(n)
    return Result({"ok": True, "app": config.name, "removed": removed, "signed_out": n, **_signin_status(config)}, text)


def _signed_out_text(n: int) -> str:
    return f" {n} browser(s) that were signed in have been signed out and must sign in again." if n else ""


def _cmd_signin(args: argparse.Namespace) -> Any:
    from easyui.cli import Result, load_config  # noqa: PLC0415

    config = load_config(args)
    if args.what is None:
        safe_auth_dir(config)
        st = _signin_status(config)
        return Result(st, "\n".join(_status_lines(st)))
    if args.what != "password" or args.state not in ("on", "off"):
        raise UsageError("Say easy-ui sign-in password on, or easy-ui sign-in password off.", hint="Or easy-ui sign-in to see how sign-in is set up.")
    safe_auth_dir(config)
    store = passkeys.PasskeyStore(config.state.auth)
    on = args.state == "on"
    store.set_password_sign_in(on, password_set=authmod.load_hash(config.state.auth) is not None)
    n = SessionStore(config.state.auth).clear()
    text = (
        "Signing in with the password is on." if on else
        "Signing in with the password is off: only passkeys and recovery codes sign in now. "
        "Turn it back on with: easy-ui sign-in password on"
    ) + _signed_out_text(n)
    return Result({"ok": True, "signed_out": n, **_signin_status(config)}, text)


def _cmd_recovery_codes(args: argparse.Namespace) -> Any:
    from easyui.cli import Result, load_config  # noqa: PLC0415

    config = load_config(args)
    st = config.state
    safe_auth_dir(config)
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
    n = SessionStore(st.auth).clear()
    text = f"Made {len(codes)} new recovery codes and showed them on the terminal. The old codes no longer work."
    text += _signed_out_text(n)
    return Result({"ok": True, "app": config.name, "made": len(codes), "shown_on": "terminal", "signed_out": n}, text)


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
        "--origin",
        action="append",
        metavar="URL",
        help="the exact web address the dashboards are opened with, e.g. https://myserver.example.ts.net or "
        "https://myserver.example.ts.net:8443 (repeatable; also EASYUI_ORIGINS). When given, only these addresses "
        "are accepted; without it, only http://localhost:PORT (and 127.0.0.1:PORT) with the port it listens on",
    )
    p.add_argument(
        "--allow-host",
        action="append",
        metavar="NAME",
        help="shorthand for --origin https://NAME (repeatable; also EASYUI_ALLOW_HOSTS)",
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
        "added on the dashboards page (Sign-in settings), by someone who is signed in; once one exists, adding "
        "another from a password or recovery-code sign-in must be confirmed with a saved passkey. Lost every "
        "passkey? Remove each one here; removing the last turns password sign-in back on, so you can sign in and "
        "add a new one.",
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

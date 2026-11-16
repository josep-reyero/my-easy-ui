"""``easy-ui screenshot``: a picture of a dashboard, for agents, with a locally installed Chromium.

It makes a throwaway static export (``easyui.export``, never encrypted, in a
private temporary folder that is removed afterwards) of the working
dashboard files (or the published ones with ``--published``), opens it in a
headless Chromium-based browser found on this computer, and saves a PNG of
the whole page. No server is started, no password is needed, and the page
makes no network request (its Content-Security-Policy forbids it).

The browser is driven through its DevTools *pipe* (``--remote-debugging-pipe``:
two file descriptors, no network port), which lets easy-ui set the exact
width (phones are narrower than headless Chrome's smallest window), light or
dark mode, reduced motion (so charts are drawn at once), wait until every part
of the page has loaded, and capture the full height.

The browser: ``$EASYUI_CHROMIUM`` when set, else the first of Chromium, Google
Chrome, chrome-headless-shell, Microsoft Edge and Brave found on the ``PATH``,
in the usual macOS application folders, or in Playwright's browser cache. When
there is none it says so (exit 3, code ``browser_missing``) and how to get one;
easy-ui never installs a browser.

Public API
----------
``find_browser(env=None) -> str | None``
``Pipe``                 a minimal DevTools-protocol client over the two pipe descriptors.
``capture(html_path, *, out, width, height, dark, browser, fragment="", timeout=60) -> dict``
``screenshot(config, *, out, dashboard=None, phone=False, dark=False, width=None, published=False,
fixture=False, values=None, browser=None, height=None) -> dict``
``register(subparsers)``  the ``screenshot`` command.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import glob
import json
import os
import select
import shutil
import signal
import tempfile
import time
from pathlib import Path
from typing import Any

from easyui import paths
from easyui.errors import EasyUIError, NotFound, TemporaryFailure, UsageError

DESKTOP = (1280, 900)
PHONE = (390, 844)
MAX_HEIGHT = 12_000
_NAMES = ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "chrome", "chrome-headless-shell",
          "microsoft-edge", "brave-browser")
_MAC_APPS = (
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
)
_PLAYWRIGHT = (
    "~/Library/Caches/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell",
    "~/Library/Caches/ms-playwright/chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium",
    "~/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell",
    "~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome",
)
# Ready when the dashboard says every query has answered and its stylesheets are in (body[data-loaded]), nothing
# is still loading (also inside the dashboard's own tree), the fonts have loaded, or when the page shows a
# message or the list instead of a dashboard.
_READY = """(() => {
  const trees = [document];
  for (const host of document.querySelectorAll('.eui-host')) if (host.shadowRoot) trees.push(host.shadowRoot);
  for (const t of trees) if (t.querySelector('.page-loading, .boot, .sk-wrap, .c.is-updating')) return false;
  if (document.fonts && document.fonts.status !== 'loaded') return false;
  if (document.body.dataset.loaded) return true;
  return Boolean(document.querySelector('.page-message, .home'));
})()"""


def _executable(p: str) -> bool:
    return os.path.isfile(p) and os.access(p, os.X_OK)


def find_browser(env: dict[str, str] | None = None) -> str | None:
    env = os.environ if env is None else env
    chosen = env.get("EASYUI_CHROMIUM")
    if chosen:
        return chosen if _executable(chosen) else None
    for name in _NAMES:
        found = shutil.which(name)
        if found:
            return found
    for p in _MAC_APPS:
        if _executable(p):
            return p
    for pattern in _PLAYWRIGHT:
        for p in sorted(glob.glob(os.path.expanduser(pattern)), reverse=True):
            if _executable(p):
                return p
    return None


def _no_browser() -> NotFound:
    return NotFound(
        "No Chromium-based browser (Chromium, Google Chrome, Edge or Brave) was found on this computer, so no "
        "picture can be taken.",
        hint="Install Chromium or Google Chrome, or point EASYUI_CHROMIUM at one (for example "
        "EASYUI_CHROMIUM=/usr/bin/chromium). easy-ui preview shows the dashboards in any browser without a picture.",
        code="browser_missing",
    )


class Pipe:
    """DevTools protocol over --remote-debugging-pipe: the browser reads fd 3, writes fd 4, messages end in NUL."""

    def __init__(self, browser: str, args: list[str], *, timeout: float) -> None:
        self.deadline = time.monotonic() + timeout
        r_in, w_in = os.pipe()
        r_out, w_out = os.pipe()
        # Above 10, so the moves to 3 and 4 in the browser never trip over each other.
        child_in = fcntl.fcntl(r_in, fcntl.F_DUPFD_CLOEXEC, 10)
        child_out = fcntl.fcntl(w_out, fcntl.F_DUPFD_CLOEXEC, 10)
        devnull = os.open(os.devnull, os.O_RDWR | os.O_CLOEXEC)
        actions = [
            (os.POSIX_SPAWN_DUP2, devnull, 0),
            (os.POSIX_SPAWN_DUP2, devnull, 1),
            (os.POSIX_SPAWN_DUP2, devnull, 2),
            (os.POSIX_SPAWN_DUP2, child_in, 3),
            (os.POSIX_SPAWN_DUP2, child_out, 4),
        ]
        try:
            self.pid = os.posix_spawn(browser, [browser, *args], dict(os.environ), file_actions=actions)
        finally:
            for fd in (r_in, w_out, child_in, child_out, devnull):
                os.close(fd)
        self.w, self.r = w_in, r_out
        self.buf = b""
        self.next_id = 0
        self.events: list[dict[str, Any]] = []

    def _left(self) -> float:
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise TemporaryFailure(
                "The browser took too long to draw the dashboard, so no picture was taken.",
                hint="Try again; if it keeps happening, check that the dashboards work with easy-ui preview.",
            )
        return left

    def _read_message(self) -> dict[str, Any]:
        while b"\0" not in self.buf:
            ready, _, _ = select.select([self.r], [], [], self._left())
            if not ready:
                continue
            chunk = os.read(self.r, 1 << 20)
            if not chunk:
                raise EasyUIError("The browser stopped unexpectedly, so no picture was taken.",
                                  hint="Try another browser with EASYUI_CHROMIUM=/path/to/chromium.", code="browser_failed")
            self.buf += chunk
        raw, self.buf = self.buf.split(b"\0", 1)
        return json.loads(raw)

    def call(self, method: str, params: dict[str, Any] | None = None, session: str | None = None) -> dict[str, Any]:
        self.next_id += 1
        msg: dict[str, Any] = {"id": self.next_id, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        os.write(self.w, json.dumps(msg).encode() + b"\0")
        while True:
            m = self._read_message()
            if m.get("id") == self.next_id:
                if "error" in m:
                    raise EasyUIError(f"The browser refused a step ({method}).", hint="Try another browser.",
                                      detail=str(m["error"]), code="browser_failed")
                return m.get("result", {})
            self.events.append(m)

    def close(self) -> None:
        with contextlib.suppress(OSError):
            os.close(self.w)
        with contextlib.suppress(OSError):
            os.close(self.r)
        with contextlib.suppress(ProcessLookupError):
            os.kill(self.pid, signal.SIGTERM)
        for _ in range(50):
            done, _status = os.waitpid(self.pid, os.WNOHANG)
            if done:
                return
            time.sleep(0.05)
        with contextlib.suppress(ProcessLookupError):
            os.kill(self.pid, signal.SIGKILL)
        with contextlib.suppress(ChildProcessError):
            os.waitpid(self.pid, 0)


def capture(
    html_path: Path,
    *,
    out: Path,
    width: int,
    height: int,
    dark: bool,
    browser: str,
    fragment: str = "",
    mobile: bool = False,
    timeout: float = 60.0,
    top: int | None = None,
) -> dict[str, Any]:
    profile = tempfile.mkdtemp(prefix="easyui-browser-")
    args = [
        "--remote-debugging-pipe", f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
        "--disable-extensions", "--disable-sync", "--disable-background-networking", "--disable-component-update",
        "--disable-default-apps", "--hide-scrollbars", "--mute-audio", "--disable-gpu", "about:blank",
    ]
    if "headless-shell" not in os.path.basename(browser):
        args.insert(0, "--headless=new")
    pipe = Pipe(browser, args, timeout=timeout)
    try:
        target = pipe.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        session = pipe.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
        pipe.call("Emulation.setDeviceMetricsOverride",
                  {"width": width, "height": height, "deviceScaleFactor": 2 if mobile else 1, "mobile": mobile}, session)
        pipe.call("Emulation.setEmulatedMedia", {"features": [
            {"name": "prefers-color-scheme", "value": "dark" if dark else "light"},
            {"name": "prefers-reduced-motion", "value": "reduce"},
        ]}, session)
        pipe.call("Page.enable", {}, session)
        pipe.call("Page.navigate", {"url": html_path.resolve().as_uri() + fragment}, session)
        ready = False
        while not ready:
            pipe._left()
            res = pipe.call("Runtime.evaluate", {"expression": _READY, "returnByValue": True}, session)
            ready = bool(res.get("result", {}).get("value"))
            if not ready:
                time.sleep(0.15)
        time.sleep(0.4)  # let the charts finish their first frame
        metrics = pipe.call("Page.getLayoutMetrics", {}, session)
        size = metrics.get("cssContentSize") or metrics.get("contentSize") or {}
        full = max(height, min(MAX_HEIGHT, int(size.get("height", height)) + 1))
        if top:
            full = min(full, top)
        pipe.call("Emulation.setDeviceMetricsOverride",
                  {"width": width, "height": full, "deviceScaleFactor": 2 if mobile else 1, "mobile": mobile}, session)
        time.sleep(0.3)
        shot = pipe.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False}, session)
        state = pipe.call("Runtime.evaluate", {"expression": "document.title", "returnByValue": True}, session)
    finally:
        pipe.close()
        shutil.rmtree(profile, ignore_errors=True)
    data = base64.b64decode(shot["data"])
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.name}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, out)
    return {"out": str(out), "width": width, "height": full, "bytes": len(data),
            "page_title": state.get("result", {}).get("value")}


def screenshot(
    config: Any,
    *,
    out: str | Path,
    dashboard: str | None = None,
    phone: bool = False,
    dark: bool = False,
    width: int | None = None,
    published: bool = False,
    fixture: bool = False,
    values: dict | None = None,
    browser: str | None = None,
    height: int | None = None,
) -> dict[str, Any]:
    from easyui import export  # noqa: PLC0415

    out = Path(out).expanduser()
    if out.suffix.lower() != ".png":
        raise UsageError("The picture is saved as a PNG, so --out must end in .png.", hint="For example: --out overview.png")
    w, h = PHONE if phone else DESKTOP
    if width is not None:
        if not 240 <= width <= 3840:
            raise UsageError("--width must be from 240 to 3840 pixels.", hint="For example: --width 1280")
        w = width
    if height is not None and not 200 <= height <= MAX_HEIGHT:
        raise UsageError(f"--height must be from 200 to {MAX_HEIGHT} pixels.", hint="For example: --height 900")
    browser = browser or find_browser()
    if not browser:
        raise _no_browser()
    with paths.cleanup_on_signals():  # SIGTERM / SIGHUP still remove the temporary copy of the dashboards
        return _screenshot(config, out=out, dashboard=dashboard, phone=phone, dark=dark, w=w, h=h, height=height,
                           published=published, fixture=fixture, values=values, browser=browser)


def _screenshot(config: Any, *, out: Path, dashboard: str | None, phone: bool, dark: bool, w: int, h: int,
                height: int | None, published: bool, fixture: bool, values: dict | None, browser: str) -> dict[str, Any]:
    from easyui import export  # noqa: PLC0415

    tmp = Path(tempfile.mkdtemp(prefix="easyui-shot-"))
    try:
        # Every dashboard, so the menu looks as it does on the live page; the picture is of the chosen one.
        res = export.export(config, out=tmp / "site", values=values, working=not published, fixture=fixture, picture=True)
        if dashboard and dashboard not in res["dashboards"]:
            raise NotFound(f"There is no dashboard called {dashboard!r}.",
                           hint="The dashboards are: " + ", ".join(res["dashboards"]) + ".")
        slug = dashboard or res["dashboards"][0]
        shot = capture(tmp / "site" / "index.html", out=out, width=w, height=h, dark=dark, browser=browser,
                       fragment=f"#/d/{slug}", mobile=phone, top=height)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"app": config.name, "dashboard": slug, "dark": dark, "phone": phone, "browser": browser,
            "source": res["source"], **shot}


def _cmd_screenshot(args: argparse.Namespace) -> Any:
    from easyui.cli import Result, load_config  # noqa: PLC0415
    from easyui.export import parse_values  # noqa: PLC0415

    config = load_config(args)
    res = screenshot(
        config, out=args.out, dashboard=args.dashboard, phone=args.phone, dark=args.dark, width=args.width,
        published=args.published, fixture=args.fixture, values=parse_values(args.set), height=args.height,
    )
    text = (f"Saved a picture of the dashboard {res['dashboard']} ({res['width']} x {res['height']} pixels, "
            f"{'dark' if res['dark'] else 'light'}, from the {res['source']}) to {paths.display_path(res['out'])}.")
    return Result(res, text)


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "screenshot",
        help="Take a picture of a dashboard (needs a Chromium-based browser on this computer).",
        description="Save a PNG of a dashboard, drawn by a headless Chromium-based browser found on this computer, "
        "from the dashboard files as they are now (or the published ones). No server, no password, no network.",
    )
    p.add_argument("dashboard", nargs="?", help="the dashboard (default: the first one)")
    p.add_argument("--out", required=True, metavar="FILE.png", help="where to save the picture")
    p.add_argument("--phone", action="store_true", help=f"phone width ({PHONE[0]} pixels) instead of a desktop ({DESKTOP[0]})")
    p.add_argument("--width", type=int, help="the page width in pixels")
    p.add_argument("--height", type=int, help="only the top of the page, this many pixels high (default: the whole page)")
    p.add_argument("--dark", action="store_true", help="dark mode")
    p.add_argument("--published", action="store_true", help="the published dashboards instead of the working files")
    p.add_argument("--fixture", action="store_true", help="with the made-up sample data in ui/fixtures/")
    p.add_argument("--set", action="append", metavar="[DASHBOARD.]NAME=VALUE", help="a control value (repeatable)")
    p.set_defaults(func=_cmd_screenshot)

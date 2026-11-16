"""``easy-ui preview``: look at the dashboards on this computer before publishing.

What it does:

1. Builds a throwaway data bundle in a temporary folder (never touching the
   app's real UI state): from the project's real data file when it exists, else
   from a synthetic fixture the project may ship in ``ui/fixtures/`` (a SQLite file
   ending in ``.sqlite``, ``.sqlite3`` or ``.db``; ``--fixture`` forces the
   fixture). When there are several, the one named like ``[ui] data`` wins,
   else the first by name. Only the ``[ui] tables`` are copied,
   exactly as ``easy-ui data build`` does.
2. Serves the dashboards straight from the working folder ``ui/dashboards/``
   on ``127.0.0.1`` only, with a one-time link in place of a password. Each
   link signs in one browser; then a new link is printed (on stderr) for the
   next one.
3. Watches the dashboard files (and the data file): any change changes the
   rev, and a preview page checks the rev every 1.5 seconds, so it shows the
   change within a couple of seconds. The data is rebuilt when the data file
   changes.
4. A dashboard with mistakes does not crash anything: its page shows the
   problems in plain words (a ``text`` component listing each problem), and
   ``problems`` carries the same list as data.

The serving itself is done by the server part (``easyui.server``) through the
hook ``easyui.server.create_app(config, *, source, preview_token)``; it is
imported only when the preview starts. ``source`` is a ``WorkingSource`` (same
methods as ``easyui.publish.PublishedSource``).

Output: the URL is printed as soon as the server listens; with ``--json``
one line ``{"url": ..., "app": ..., "data": ...}``. When it is stopped (Ctrl-C or
SIGTERM) it prints one more line: ``{"stopped": "stopped", "app": ...}``.

Public API
----------
``PROBLEMS_SLUG`` (``"easy-ui-problems"``)  the page listing file-level problems.
``PreviewConfig``  an ``AppConfig`` whose state lives in a temporary folder.
``choose_data(config, *, fixture=False) -> (path, kind)``  kind ``"app data"`` | ``"fixture"``.
``WorkingSource(config)``  ``list()``, ``compiled(slug, today=None)``,
    ``dashboard(slug)``, ``bundle_path()``, ``rev()``, ``refresh()``, ``status()``.
``problem_page(slug, title, file, errors, warnings) -> dict``  a compiled spec showing problems.
``Preview`` (dataclass) ``config``, ``source``, ``token``, ``root``, ``data``, ``close()``.
``prepare(config, *, fixture=False) -> Preview``  builds the data and the source.
``make_url(host, port, token) -> str``
``serve(preview, *, port=0, on_ready=None)``  binds 127.0.0.1 and serves until stopped.
``register(subparsers)``  the ``preview`` command.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as _dt
import hashlib
import importlib
import inspect
import json
import os
import secrets
import shutil
import socket
import stat
import sys
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from easyui import bundle, paths, publish, sandbox, spec
from easyui.appconfig import AppConfig
from easyui.errors import ConfigError, EasyUIError, NotAvailable, NotFound, SpecError, UsageError

HOST = "127.0.0.1"
HEAP_LIMIT = 256_000_000
PROBLEMS_SLUG = "easy-ui-problems"
FIXTURES_DIR = ("ui", "fixtures")
FIXTURE_SUFFIXES = (".sqlite", ".sqlite3", ".db")


# ---------------------------------------------------------------- throwaway state


@dataclass(frozen=True)
class PreviewConfig(AppConfig):
    preview_root: Path | None = None
    source_override: Path | None = None

    @property
    def state(self) -> paths.StatePaths:  # type: ignore[override]
        assert self.preview_root is not None
        return paths.StatePaths(self.preview_root / self.name)

    def source_db(self) -> Path:
        return self.source_override if self.source_override is not None else super().source_db()

    @classmethod
    def wrap(cls, cfg: AppConfig, root: Path, source: Path | None) -> PreviewConfig:
        base = {f.name: getattr(cfg, f.name) for f in dataclasses.fields(AppConfig)}
        return cls(**base, preview_root=root, source_override=source)


def _fixtures(cfg: AppConfig) -> list[Path]:
    d = cfg.app_dir.joinpath(*FIXTURES_DIR)
    out = []
    try:
        if (cfg.app_dir / "ui").is_symlink() or not stat.S_ISDIR(os.lstat(d).st_mode):
            return []
        for e in sorted(os.scandir(d), key=lambda e: e.name):
            if e.name.endswith(FIXTURE_SUFFIXES) and not e.name.startswith(".") and e.is_file(follow_symlinks=False):
                out.append(Path(e.path))
    except OSError:
        return []
    return out


def choose_data(cfg: AppConfig, *, fixture: bool = False) -> tuple[Path, str]:
    real: Path | None = None
    if not fixture:
        with contextlib.suppress(ConfigError):
            real = cfg.source_db()
        if real is not None and real.is_file():
            return real, "app data"
    fx = _fixtures(cfg)
    if fx:
        want = Path(cfg.data).name if cfg.data else None
        chosen = next((f for f in fx if f.name == want), fx[0])
        return chosen, "fixture"
    where = f" ({paths.display_path(real)})" if real is not None else ""
    looked = "looked for files ending in .sqlite, .sqlite3 or .db"
    raise NotFound(
        (f"No sample data was found in ui/fixtures/ ({looked})" if fixture else
         f"The app has no data yet{where}, and no sample data was found in ui/fixtures/ ({looked})")
        + ", so the preview would have nothing to show.",
        hint="Add a small made-up copy of the project's data there, for example ui/fixtures/sample.sqlite with the same "
        "tables, then run easy-ui preview again. Or run the project's job that creates its real data first.",
    )


# ---------------------------------------------------------------- problems as a page


def _problem_line(p: dict[str, Any]) -> str:
    where = []
    if p.get("line"):
        where.append(f"line {p['line']}")
    if p.get("place") or p.get("where"):
        where.append(p.get("place") or p["where"])
    text = (", ".join(where) + ": " if where else "") + p.get("problem", "")
    if p.get("did_you_mean") is not None:
        text += f' Did you mean "{p["did_you_mean"]}"?'
    if p.get("hint"):
        text += f" {p['hint']}"
    allowed = p.get("allowed")
    if allowed and len(allowed) <= 20:
        text += " Allowed: " + spec.allowed_text(allowed) + "."
    return text


def _t(v: str) -> dict[str, Any]:
    return {"t": "text", "v": v}


def problem_page(slug: str, title: str | None, file: str | None, errors: list[dict[str, Any]],
                 warnings: list[dict[str, Any]], *, intro: str | None = None) -> dict[str, Any]:
    """A compiled spec (one text component) that shows the problems in plain words. Never HTML."""
    n = len(errors)
    blocks: list[dict[str, Any]] = [{
        "t": "p",
        "c": [_t(intro or (
            f"{file or slug} has {n} mistake{'s' if n != 1 else ''}, so this dashboard cannot be shown yet. "
            "Fix the file and save it: this page reloads by itself."))],
    }]
    if errors:
        blocks.append({"t": "ul", "items": [[_t(_problem_line(p))] for p in errors[:50]]})
        if len(errors) > 50:
            blocks.append({"t": "p", "c": [_t(f"... and {len(errors) - 50} more. Run easy-ui validate to see all.")]})
    if warnings:
        blocks.append({"t": "h", "level": 3, "c": [_t("Also worth a look (warnings)")]})
        blocks.append({"t": "ul", "items": [[_t(_problem_line(p))] for p in warnings[:20]]})
    return {
        "spec": spec.SPEC_VERSION,
        "slug": slug,
        "title": f"{title or slug}: needs fixing" if file else (title or "Needs fixing"),
        "description": "Preview only: this page lists what to fix before the dashboard can be published.",
        "params": [],
        "queries": {},
        "layout": [[{"id": "r0c0", "type": "text", "title": "What to fix", "blocks": blocks}]],
        "problems": errors + warnings,
    }


# ---------------------------------------------------------------- the working folder as a source


class WorkingSource:
    """Dashboards read live from the project's ``ui/dashboards/`` (validated against the preview data)."""

    preview = True

    def __init__(self, cfg: PreviewConfig, *, log: Callable[[str], None] | None = None) -> None:
        self.cfg = cfg
        self.log = log or (lambda _m: None)
        self._lock = threading.RLock()
        self._fingerprint: str | None = None
        self._data_fp: tuple[int, int, int] | None = self._data_stat()
        self._entries: dict[str, dict[str, Any]] = {}
        self._file_problems: list[dict[str, Any]] = []
        self._notes: list[str] = []
        self._data_problem: dict[str, Any] | None = None
        self._rev = ""

    # -- watching
    def _data_stat(self) -> tuple[int, int, int] | None:
        try:
            s = os.stat(self.cfg.source_db())
        except (OSError, EasyUIError):
            return None
        return (s.st_size, s.st_mtime_ns, s.st_ino)

    def _scan_fingerprint(self) -> str:
        h = hashlib.sha256()
        d = self.cfg.dashboards_dir
        try:
            with os.scandir(d) as it:
                for e in sorted(it, key=lambda e: e.name):
                    try:
                        s = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    h.update(f"{e.name}\0{s.st_size}\0{s.st_mtime_ns}\0{s.st_ino}\0{s.st_mode}\n".encode())
        except OSError:
            h.update(b"no-folder")
        h.update(bundle.data_rev(self.cfg).encode())
        return h.hexdigest()

    def refresh(self) -> bool:
        """Re-read the folder when anything changed; True when it did."""
        with self._lock:
            data_fp = self._data_stat()
            if data_fp != self._data_fp:
                self._data_fp = data_fp
                self._rebuild_data()
            fp = hashlib.sha256(
                (self._scan_fingerprint() + json.dumps(self._data_problem, default=str)).encode()
            ).hexdigest()
            if fp == self._fingerprint:
                return False
            self._fingerprint = fp
            self._reload()
            self._rev = fp[:16]
            return True

    def _rebuild_data(self) -> None:
        try:
            res = bundle.build(self.cfg, keep=2)
        except EasyUIError as e:
            self._data_problem = {"file": "data", "line": None, "where": "", "problem": e.message, "hint": e.hint,
                                  "level": "error"}
            self.log(f"The data could not be rebuilt, so the preview keeps the previous copy: {e.message}")
        else:
            self._data_problem = None
            if not res["unchanged"]:
                self.log("The data file changed; the preview now uses the new data.")

    def _reload(self) -> None:
        current = bundle.current_bundle(self.cfg)
        try:
            files, problems, notes = publish.read_dashboard_dir(self.cfg.dashboards_dir, label_prefix="ui/dashboards/")
        except EasyUIError as e:
            files, problems, notes = [], [publish._problem("ui/dashboards/", e.message, e.hint)], []
        results = publish.validate_sources(files, current)
        self._entries = {r["slug"]: r for r in results}
        self._file_problems = problems
        self._notes = notes
        bad = [r for r in results if not r["ok"]]
        for r in bad:
            first = r["errors"][0] if r["errors"] else {"problem": "it could not be read"}
            self.log(f"{r['file']}: {len(r['errors'])} problem(s), first: {_problem_line(first)}")
        for p in problems:
            self.log(f"{p['file']}: {p['problem']}")
        if not bad and not problems:
            self.log(f"{len(results)} dashboard(s) ok.")

    def _ready(self) -> None:
        self.refresh()  # a stat of the folder; files are only re-read and re-validated when something changed

    # -- the source interface
    def rev(self) -> str:
        self.refresh()
        return self._rev

    def _needs_problem_page(self) -> bool:
        return bool(self._file_problems or self._data_problem or not self._entries)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            self._ready()
            out = []
            for slug, r in self._entries.items():
                title = r["title"] or slug
                out.append({"slug": slug, "title": title if r["ok"] else f"{title} (needs fixing)",
                            "description": r["description"], "ok": r["ok"]})
            if self._needs_problem_page() and PROBLEMS_SLUG not in self._entries:
                out.append({"slug": PROBLEMS_SLUG, "title": "Needs fixing" if self._entries else "No dashboards yet",
                            "description": "Preview only.", "ok": False})
            return out

    def _problems_page(self) -> dict[str, Any]:
        problems = list(self._file_problems) + ([self._data_problem] if self._data_problem else [])
        if not self._entries and not problems:
            intro = ("There are no dashboards yet in ui/dashboards/. Create one, for example ui/dashboards/overview.yaml "
                     "(see: easy-ui components); it appears here as soon as you save it.")
            return problem_page(PROBLEMS_SLUG, "No dashboards yet", None, [], [], intro=intro)
        intro = "Some files in ui/dashboards/ or the data could not be used. Fix them and this page reloads by itself."
        return problem_page(PROBLEMS_SLUG, "Needs fixing", None, problems, [], intro=intro)

    def compiled(self, slug: str, *, today: _dt.date | None = None) -> dict[str, Any]:
        with self._lock:
            self._ready()
            r = self._entries.get(slug) if isinstance(slug, str) else None
            if r is None:
                if slug == PROBLEMS_SLUG and self._needs_problem_page():
                    return self._problems_page()
                raise NotFound(f'There is no dashboard called "{str(slug)[:60]}" in ui/dashboards/.',
                               hint="Check the file name: ui/dashboards/<name>.yaml is the page <name>.")
            if not r["ok"]:
                return problem_page(slug, r["title"], r["file"], r["errors"], r["warnings"])
            return spec.compile_dashboard(r["dashboard"], today=today)

    def dashboard(self, slug: str) -> spec.Dashboard:
        with self._lock:
            self._ready()
            r = self._entries.get(slug) if isinstance(slug, str) else None
            if r is None:
                raise NotFound(f'There is no dashboard called "{str(slug)[:60]}" in ui/dashboards/.',
                               hint="Check the file name: ui/dashboards/<name>.yaml is the page <name>.")
            if not r["ok"]:
                raise SpecError(f"{r['file']} has mistakes, so its queries do not run.",
                                hint="Fix the file; the page lists what is wrong.", problems=r["errors"])
            return r["dashboard"]

    def bundle_path(self) -> Path:
        return bundle.require_current(self.cfg)

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._ready()
            return {
                "rev": self._rev,
                "dashboards": [{k: v for k, v in r.items() if k != "dashboard"} for r in self._entries.values()],
                "problems": list(self._file_problems) + ([self._data_problem] if self._data_problem else []),
                "notes": list(self._notes),
            }


# ---------------------------------------------------------------- starting


@dataclass
class Preview:  # token: the current one-time link token (replaced each time a link is used)
    config: PreviewConfig
    source: WorkingSource
    token: str
    root: Path
    data: dict[str, Any]

    def close(self) -> None:
        for dirpath, _dirs, _files in os.walk(self.root):
            with contextlib.suppress(OSError):
                os.chmod(dirpath, 0o700)
        shutil.rmtree(self.root, ignore_errors=True)


def prepare(cfg: AppConfig, *, fixture: bool = False, log: Callable[[str], None] | None = None) -> Preview:
    src, kind = choose_data(cfg, fixture=fixture)
    root = Path(tempfile.mkdtemp(prefix="easy-ui-preview-"))
    os.chmod(root, 0o700)
    try:
        pcfg = PreviewConfig.wrap(cfg, root, src)
        built = bundle.build(pcfg, keep=2)
        source = WorkingSource(pcfg, log=log)
        source.refresh()
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise
    data = {"from": kind, "file": paths.display_path(src, cfg.app_dir) if kind == "fixture" else paths.display_path(src),
            "tables": {k: v["rows"] for k, v in built["tables"].items()}}
    return Preview(pcfg, source, secrets.token_urlsafe(24), root, data)


def make_url(host: str, port: int, token: str) -> str:
    return f"http://{host}:{port}/?token={token}"


def _server_app(preview: Preview, on_link_used: Callable[[str], None] | None = None) -> Any:
    try:
        server = importlib.import_module("easyui.server")
    except ImportError as e:
        raise NotAvailable(
            "The preview cannot start, because the server part of easy-ui (easyui.server) is not installed here.",
            hint="Reinstall my-easy-ui (for example: uv sync), then try again.",
            detail=f"{type(e).__name__}: {e}",
        ) from None
    factory = getattr(server, "create_app", None)
    try:
        params = inspect.signature(factory).parameters if callable(factory) else {}
    except (TypeError, ValueError):
        params = {}
    if not callable(factory) or "source" not in params or "preview_token" not in params:
        raise NotAvailable(
            "The preview cannot start, because this version of the easy-ui server cannot show dashboards straight "
            "from the working folder.",
            hint="Update my-easy-ui, or publish and use easy-ui serve instead.",
            detail="easyui.server.create_app(config, *, source, preview_token) is missing",
        )
    extra = {"on_preview_link_used": on_link_used} if "on_preview_link_used" in params else {}
    return factory(preview.config, source=preview.source, preview_token=preview.token, **extra)


def _bind(port: int) -> socket.socket:
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise UsageError("--port must be a number from 0 to 65535 (0 picks a free one).", hint="For example: --port 8750")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((HOST, port))
        sock.listen(64)
    except OSError as e:
        sock.close()
        raise UsageError(
            f"Port {port} on this computer is already in use, so the preview cannot listen on it.",
            hint="Pick another one with --port, or leave it out to use a free port.",
            detail=str(e),
        ) from None
    sock.setblocking(False)
    return sock


def serve(preview: Preview, *, port: int = 0, on_ready: Callable[[str], None] | None = None,
          server_holder: list[Any] | None = None, on_link_used: Callable[[str], None] | None = None) -> None:
    """Serve on 127.0.0.1 until stopped (Ctrl-C). ``server_holder`` receives the uvicorn server (for tests).

    While it runs, ``EASYUI_STATE_HOME`` points at the throwaway folder, so anything the server keeps (sessions)
    stays there even when it looks the folder up by app name, never in the project's real UI state.
    ``on_link_used(url)`` is called with the next one-time link each time a link has been used.
    """
    import uvicorn

    sock = _bind(port)
    port_used = sock.getsockname()[1]
    old_state = os.environ.get("EASYUI_STATE_HOME")
    os.environ["EASYUI_STATE_HOME"] = str(preview.root)
    try:
        def link_used(token: str) -> None:
            preview.token = token
            if on_link_used is not None:
                on_link_used(make_url(HOST, port_used, token))

        app = _server_app(preview, link_used)
        sandbox.set_heap_limit(HEAP_LIMIT)
        url = make_url(HOST, port_used, preview.token)
        make_config = getattr(sys.modules.get("easyui.server"), "uvicorn_config", None)
        if make_config is not None:
            config = make_config(app, log_level="warning", timeout_graceful_shutdown=2, date_header=False)
        else:  # a server part without the shared settings (only in tests)
            config = uvicorn.Config(app, log_level="warning", access_log=False, lifespan="on", proxy_headers=False,
                                    server_header=False, timeout_graceful_shutdown=2, limit_concurrency=64)
        server = uvicorn.Server(config)
        if server_holder is not None:
            server_holder.append(server)
        if on_ready is not None:
            on_ready(url)
        server.run(sockets=[sock])
    finally:
        sock.close()
        if old_state is None:
            os.environ.pop("EASYUI_STATE_HOME", None)
        else:
            os.environ["EASYUI_STATE_HOME"] = old_state


# ---------------------------------------------------------------- command line


def _cmd_preview(args: argparse.Namespace) -> Any:
    from easyui.cli import Result, load_config, print_now, safe_text

    cfg = load_config(args)

    def log(msg: str) -> None:
        sys.stderr.write(safe_text(f"preview: {msg}") + "\n")
        sys.stderr.flush()

    pv = prepare(cfg, fixture=args.fixture, log=log)
    try:
        def ready(url: str) -> None:
            print_now(
                {"url": url, "app": cfg.name, "data": pv.data},
                f"Preview of {cfg.title}: open {url}\n"
                f"It shows ui/dashboards/ as you edit (using {pv.data['from']}: {pv.data['file']}); an open page "
                "shows a saved change within a couple of seconds. The link works once; after it is used, a new "
                "one for another browser is shown here. Only this computer can open it. Stop with Ctrl-C.",
                args.json,
            )

        def link_used(url: str) -> None:
            log(f"the link was used to open the preview. To open it in another browser, use: {url}")

        holder: list[Any] = []

        def stop_now() -> None:
            if holder:
                holder[0].should_exit = True

        try:
            stops = importlib.import_module("easyui.server").stop_signals
        except (ImportError, AttributeError):
            stops = lambda _cb: contextlib.nullcontext()  # noqa: E731
        with stops(stop_now):
            serve(pv, port=args.port, on_ready=ready, on_link_used=link_used, server_holder=holder)
    finally:
        pv.close()
    return Result({"stopped": "stopped", "app": cfg.name}, "The preview stopped. Nothing was published.", json_line=True)


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "preview",
        help="Preview the dashboards on this computer before publishing.",
        description="Serve ui/dashboards/ straight from the working folder on 127.0.0.1 with throwaway data, a "
        "one-time link instead of a password, and automatic reload when a file changes. Sample data is read from "
        "ui/fixtures/*.sqlite, *.sqlite3 or *.db when the project has no data yet (or with --fixture).",
    )
    p.add_argument("--port", type=int, default=0, help="port on 127.0.0.1 (default: a free one)")
    p.add_argument("--fixture", action="store_true", help="use the sample data in ui/fixtures/ even if real data exists")
    p.set_defaults(func=_cmd_preview)

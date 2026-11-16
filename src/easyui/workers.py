"""Query workers: every dashboard query runs in a separate process that is killed when its time is up.

SQLite checks the time limit only between its small steps (``easyui.sandbox``),
so one long step, such as a LIKE or ``instr`` over long text or preparing a
query that expands badly, could otherwise run for minutes and hold a slot. A
worker process can always be stopped: when a query has not answered
``KILL_GRACE_S`` after its time limit, its worker is killed (SIGKILL), the
caller gets the usual "took too long" error and the next query gets a fresh
worker.

How a worker is made
--------------------
- It is started on demand with this Python (``-I -S``: no site packages, no
  environment variables, no current folder on the import path) and an empty
  environment, and imports only ``easyui.sandbox`` (with ``easyui.paths`` and ``easyui.errors``)
  and the standard library.
- It talks to the parent over a private socket pair (length-prefixed JSON,
  never pickle). The parent opens the data bundle itself (read-only, without
  following links) and hands the open descriptor over; the worker cannot open
  files by name for its queries.
- Before its first query it caps its own memory (``RLIMIT_AS``/``RLIMIT_DATA``,
  ``MEMORY_LIMIT``), SQLite's heap (``sandbox.HEAP_LIMIT``), the size of any
  file it could write (0) and its open files, and asks the kernel to stop it
  first when memory runs short (``oom_score_adj``), so the server itself
  survives.
- An idle worker is stopped after ``IDLE_S`` seconds, so an unused server
  holds no workers at all; a worker that once needed a lot of memory is
  replaced after its request, so its memory goes back to the system.

Public API
----------
``KILL_GRACE_S`` (0.5), ``IDLE_S`` (30), ``WAIT_S`` (5), ``MEMORY_LIMIT`` (192 MB), ``START_TIMEOUT_S`` (10)
``WorkerPool(size=4, *, idle_s=IDLE_S, wait_s=WAIT_S, memory_limit=MEMORY_LIMIT, heap_limit=sandbox.HEAP_LIMIT)``
    ``session(bundle_path, limits=DEFAULT_LIMITS)`` -> a context manager giving a ``RemoteSandbox``; it waits
    at most ``wait_s`` for a free worker, then raises ``TemporaryFailure`` (code ``busy``, HTTP 503).
    ``close()`` stops every worker; ``stats()`` -> ``{"size", "idle", "busy", "started", "killed"}``;
    ``worker_pids()`` (for tests and measurements).
``RemoteSandbox``  the same ``run(sql, params, *, name)`` / ``columns(...)`` and ``limits`` attribute as
    ``sandbox.Sandbox``, answered by a worker.
``default_pool() -> WorkerPool``  the process-wide pool (size 2) used by commands (``validate``,
    ``publish``, ``export``) when they are not given one. The server makes its own, sized to its query slots.
``main()``  the worker's own loop (run by the worker process only).
"""

from __future__ import annotations

import atexit
import contextlib
import json
import os
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
import weakref
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from easyui import errors as _errors
from easyui import paths, sandbox
from easyui.errors import EasyUIError, NoData, QueryError, QueryRefused, QueryTimeout, TemporaryFailure

KILL_GRACE_S = 0.5
IDLE_S = 30.0
WAIT_S = 5.0
MEMORY_LIMIT = 192 * 1024 * 1024
START_TIMEOUT_S = 10.0
RECYCLE_RSS = 96 * 1024 * 1024  # a worker that once grew past this is replaced after its request
_MAX_REQUEST = 1_000_000
_HEADER = struct.Struct(">I")
_PKG_PARENT = str(Path(__file__).resolve().parent.parent)
_BOOT = "import sys; sys.path.append(sys.argv[1]); from easyui.workers import main; main()"


# ---------------------------------------------------------------- framing


def _send(sock: socket.socket, obj: Any, fds: list[int] | None = None) -> None:
    data = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8", "surrogatepass")
    frame = _HEADER.pack(len(data)) + data
    if fds:
        sent = socket.send_fds(sock, [frame], fds)
        if sent < len(frame):
            sock.sendall(frame[sent:])
    else:
        sock.sendall(frame)


def _recv_exact(sock: socket.socket, n: int, deadline: float | None) -> bytes:
    chunks, got = [], 0
    while got < n:
        if deadline is not None:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError
            sock.settimeout(left)
        chunk = sock.recv(min(n - got, 1 << 20))
        if not chunk:
            raise EOFError
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


def _recv(sock: socket.socket, limit: int, deadline: float | None = None) -> Any:
    size = _HEADER.unpack(_recv_exact(sock, _HEADER.size, deadline))[0]
    if size > limit:
        raise OverflowError(size)
    return json.loads(_recv_exact(sock, size, deadline).decode("utf-8", "surrogatepass"))


# ---------------------------------------------------------------- the worker process


def _limit(which: str, value: int) -> None:
    import resource  # noqa: PLC0415 - POSIX only, and only in the worker

    res = getattr(resource, which, None)
    if res is None:
        return
    with contextlib.suppress(ValueError, OSError):
        _soft, hard = resource.getrlimit(res)
        if hard != resource.RLIM_INFINITY:
            value = min(value, hard)
        resource.setrlimit(res, (value, value))


def _peak_rss() -> int:
    import resource  # noqa: PLC0415

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak) if sys.platform == "darwin" else int(peak) * 1024


def _error_dict(e: BaseException) -> dict[str, Any]:
    if isinstance(e, EasyUIError):
        return {"cls": type(e).__name__, "message": e.message, "hint": e.hint, "code": e.code, "detail": e.detail,
                "token": getattr(e, "token", None)}
    if isinstance(e, MemoryError):
        return {"cls": "QueryRefused", "code": "query_too_big", "message": "The query needs more memory than a "
                "dashboard query may use, so it was stopped.", "hint": "Ask for less data or simplify the query."}
    return {"cls": "QueryError", "message": "The query could not run.", "hint": "Fix the SQL in the dashboard file, "
            "then run: easy-ui validate", "detail": f"{type(e).__name__}: {e}"[:500]}


def _serve(sock: socket.socket, memory_limit: int, heap_limit: int) -> None:
    sb: sandbox.Sandbox | None = None
    while True:
        try:
            header, fds, _flags, _addr = socket.recv_fds(sock, _HEADER.size, 2)
        except OSError:
            return
        if not header:
            return
        try:
            if len(header) < _HEADER.size:
                header += _recv_exact(sock, _HEADER.size - len(header), None)
            size = _HEADER.unpack(header)[0]
            if size > _MAX_REQUEST:
                return
            msg = json.loads(_recv_exact(sock, size, None).decode("utf-8"))
        except (EOFError, OSError, ValueError):
            return
        op = msg.get("op")
        try:
            if op == "open":
                if sb is not None:
                    sb.close()
                    sb = None
                if len(fds) != 1:
                    raise NoData("There is no dashboard data yet, so the dashboards have nothing to show.",
                                 hint="Build it with: easy-ui data build")
                fd = fds.pop()
                sb = sandbox.Sandbox(fd=fd, limits=sandbox.Limits(**msg["limits"]))
                reply: dict[str, Any] = {"ok": {"tables": sorted(sb.tables)}}
            elif op in ("run", "columns"):
                if sb is None:
                    raise QueryError("The data is not open.", hint="Try again.")
                sb.limits = sandbox.Limits(**msg["limits"])
                if op == "run":
                    res = sb.run(msg["sql"], msg.get("params"), name=msg.get("name"))
                    reply = {"ok": {"columns": res.columns, "rows": res.rows, "truncated": res.truncated,
                                    "truncated_reason": res.truncated_reason, "elapsed_ms": res.elapsed_ms,
                                    "size": res.size}}
                    del res
                else:
                    reply = {"ok": sb.columns(msg["sql"], msg.get("params"), name=msg.get("name"))}
            elif op == "close":
                if sb is not None:
                    sb.close()
                    sb = None
                reply = {"ok": None, "recycle": _peak_rss() > RECYCLE_RSS}
            else:
                return
        except BaseException as e:  # noqa: BLE001 - every failure becomes a plain answer
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                return
            reply = {"error": _error_dict(e)}
        finally:
            for extra in fds:
                with contextlib.suppress(OSError):
                    os.close(extra)
        try:
            _send(sock, reply)
        except MemoryError:
            _send(sock, {"error": _error_dict(MemoryError())})
        except (OSError, ValueError):
            return
        del reply


def main() -> None:
    """The worker process: ``python -I -S -c BOOT <package parent> <socket fd> <memory limit> <heap limit>``."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # Ctrl-C reaches the parent, which stops the workers
    if hasattr(signal, "SIGXFSZ"):
        signal.signal(signal.SIGXFSZ, signal.SIG_IGN)  # a write past RLIMIT_FSIZE then fails instead of killing
    fd, memory_limit, heap_limit = int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
    sock = socket.socket(fileno=fd)
    with contextlib.suppress(OSError):
        os.chdir("/")
    with contextlib.suppress(OSError):
        with open("/proc/self/oom_score_adj", "w") as fh:  # stop a worker, not the server, when memory runs out
            fh.write("1000")
    if memory_limit > 0:
        _limit("RLIMIT_AS", memory_limit)
        _limit("RLIMIT_DATA", memory_limit)
    _limit("RLIMIT_FSIZE", 0)
    _limit("RLIMIT_CORE", 0)
    _limit("RLIMIT_NOFILE", 64)
    sandbox.set_heap_limit(heap_limit)
    try:
        _send(sock, {"ready": os.getpid()})
        _serve(sock, memory_limit, heap_limit)
    finally:
        with contextlib.suppress(OSError):
            sock.close()
    os._exit(0)


# ---------------------------------------------------------------- the parent side


class _Worker:
    def __init__(self, memory_limit: int, heap_limit: int) -> None:
        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.proc = subprocess.Popen(  # noqa: S603 - our own interpreter and module, no shell
                [sys.executable, "-I", "-S", "-c", _BOOT, _PKG_PARENT, str(child.fileno()), str(memory_limit),
                 str(heap_limit)],
                pass_fds=(child.fileno(),),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                env={},
                cwd="/",
                close_fds=True,
            )
        except OSError as e:
            parent.close()
            child.close()
            raise _no_worker(e) from None
        child.close()
        self.sock = parent
        self.last_used = time.monotonic()
        try:
            hello = _recv(self.sock, 4096, time.monotonic() + START_TIMEOUT_S)
        except (TimeoutError, EOFError, OSError, ValueError) as e:
            self.kill()
            raise _no_worker(e) from None
        if not isinstance(hello, dict) or hello.get("ready") != self.proc.pid:
            self.kill()
            raise _no_worker(RuntimeError("unexpected greeting"))

    @property
    def pid(self) -> int:
        return self.proc.pid

    def alive(self) -> bool:
        return self.proc.poll() is None

    def call(self, msg: dict[str, Any], *, deadline: float, limit: int, fds: list[int] | None = None) -> Any:
        try:
            self.sock.settimeout(max(0.001, deadline - time.monotonic()))
            _send(self.sock, msg, fds)
            return _recv(self.sock, limit, deadline)
        except BaseException:
            self.kill()
            raise

    def kill(self) -> None:
        with contextlib.suppress(OSError):
            self.proc.kill()
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            self.proc.wait(5)
        with contextlib.suppress(OSError):
            self.sock.close()


def _no_worker(e: BaseException) -> TemporaryFailure:
    return TemporaryFailure(
        "easy-ui could not start a helper process to run the dashboard queries, so nothing was shown.",
        hint="Try again in a moment. If it keeps happening, the computer may be out of memory or processes; run "
        "easy-ui check and look at the server's log.",
        code="worker_failed",
        detail=f"{type(e).__name__}: {e}"[:300],
    )


def _rebuild_error(d: Any) -> EasyUIError:
    if not isinstance(d, dict):
        return QueryError("The query could not run.", hint="Try again.")
    cls = getattr(_errors, str(d.get("cls")), None)
    if not (isinstance(cls, type) and issubclass(cls, EasyUIError)):
        cls = QueryError
    e = cls(str(d.get("message") or "The query could not run."), hint=d.get("hint"), code=d.get("code"),
            detail=d.get("detail"))
    if d.get("token") is not None:
        e.token = d["token"]  # type: ignore[attr-defined]
    return e


def _timeout(name: str | None, limit: float) -> QueryTimeout:
    label = f'The query "{name}" ' if name else "The query "
    return QueryTimeout(
        f"{label}took longer than {limit:g} seconds, so it was stopped to keep the dashboard responsive.",
        hint="Ask for less data (for example a shorter period) or simplify the query. Try again.",
        code="query_timeout",
    )


def _died(name: str | None) -> QueryRefused:
    label = f'The query "{name}" ' if name else "The query "
    return QueryRefused(
        f"{label}needed more memory than a dashboard query may use, so it was stopped.",
        hint="Ask for less data (fewer rows, shorter text) or simplify the query.",
        code="query_too_big",
    )


class RemoteSandbox:
    """A ``sandbox.Sandbox`` whose queries run in a worker process (see the module docstring)."""

    def __init__(self, pool: WorkerPool, bundle_path: str | Path, limits: sandbox.Limits) -> None:
        self.pool = pool
        self.limits = limits
        self.path = Path(os.path.abspath(bundle_path))
        try:
            self._fd = paths.open_nofollow(self.path, 2)
        except OSError:
            self._fd = -1
        if self._fd < 0 or not stat.S_ISREG(os.fstat(self._fd).st_mode):
            if self._fd >= 0:
                os.close(self._fd)
            raise NoData("There is no dashboard data yet, so the dashboards have nothing to show.",
                         hint="Build it with: easy-ui data build")
        self._worker: _Worker | None = None
        self.tables: frozenset[str] = frozenset()

    def _limits(self) -> dict[str, Any]:
        return {"timeout_s": float(self.limits.timeout_s), "max_rows": int(self.limits.max_rows),
                "max_bytes": int(self.limits.max_bytes)}

    def _ready(self) -> _Worker:
        if self._worker is not None and self._worker.alive():
            return self._worker
        self._worker = None
        w = self.pool._take()
        try:
            reply = w.call({"op": "open", "limits": self._limits()}, deadline=time.monotonic() + START_TIMEOUT_S,
                           limit=_MAX_REQUEST, fds=[self._fd])
        except (TimeoutError, EOFError, OSError, ValueError) as e:
            self.pool._lost(w, killed=True)
            raise _no_worker(e) from None
        if "error" in reply:
            self.pool._give_back(w)
            raise _rebuild_error(reply["error"])
        self.tables = frozenset(reply["ok"]["tables"])
        self._worker = w
        return w

    def _call(self, op: str, sql: str, params: dict[str, Any] | None, name: str | None) -> Any:
        w = self._ready()
        limit = max(0.0, float(self.limits.timeout_s))
        deadline = time.monotonic() + limit + KILL_GRACE_S
        msg = {"op": op, "sql": sql, "params": params, "name": name, "limits": self._limits()}
        # The answer may hold escaped text: allow several times the size limit, never unbounded.
        cap = 8 * int(self.limits.max_bytes) + 1_000_000
        try:
            reply = w.call(msg, deadline=deadline, limit=cap)
        except TimeoutError:
            self._worker = None
            self.pool._lost(w, killed=True)
            raise _timeout(name, limit) from None
        except (EOFError, OSError, OverflowError, ValueError):
            self._worker = None
            self.pool._lost(w, killed=True)
            raise _died(name) from None
        if "error" in reply:
            raise _rebuild_error(reply["error"])
        return reply["ok"]

    def run(self, sql: str, params: dict[str, Any] | None = None, *, name: str | None = None) -> sandbox.QueryResult:
        ok = self._call("run", sql, params, name)
        return sandbox.QueryResult(ok["columns"], ok["rows"], ok["truncated"], ok["truncated_reason"],
                                   ok["elapsed_ms"], ok["size"])

    def columns(self, sql: str, params: dict[str, Any] | None = None, *, name: str | None = None) -> list[str]:
        return list(self._call("columns", sql, params, name))

    def close(self) -> None:
        w, self._worker = self._worker, None
        if w is not None:
            try:
                reply = w.call({"op": "close"}, deadline=time.monotonic() + 2.0, limit=4096)
            except (TimeoutError, EOFError, OSError, ValueError):
                self.pool._lost(w, killed=True)
            else:
                if reply.get("recycle"):
                    w.kill()
                    self.pool._lost(w, killed=False)
                else:
                    self.pool._give_back(w)
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def __enter__(self) -> RemoteSandbox:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class WorkerPool:
    """At most ``size`` query workers, started on demand and stopped after ``idle_s`` without use."""

    def __init__(self, size: int = 4, *, idle_s: float = IDLE_S, wait_s: float = WAIT_S,
                 memory_limit: int = MEMORY_LIMIT, heap_limit: int = sandbox.HEAP_LIMIT) -> None:
        self.size = max(1, int(size))
        self.idle_s = float(idle_s)
        self.wait_s = float(wait_s)
        self.memory_limit = int(memory_limit)
        self.heap_limit = int(heap_limit)
        self._slots = threading.BoundedSemaphore(self.size)
        self._lock = threading.Lock()
        self._idle: list[_Worker] = []
        self._busy: set[_Worker] = set()
        self._reaper: threading.Thread | None = None
        self._wake = threading.Event()
        self._closed = False
        self.started = 0
        self.killed = 0
        _pools.add(self)

    # --- used by RemoteSandbox (the caller holds a slot) ---------------------

    def _take(self) -> _Worker:
        with self._lock:
            while self._idle:
                w = self._idle.pop()
                if w.alive():
                    self._busy.add(w)
                    return w
                w.kill()
        w = _Worker(self.memory_limit, self.heap_limit)
        with self._lock:
            self.started += 1
            self._busy.add(w)
        return w

    def _give_back(self, w: _Worker) -> None:
        with self._lock:
            self._busy.discard(w)
            if self._closed or not w.alive():
                w.kill()
                return
            w.last_used = time.monotonic()
            self._idle.append(w)
            if self._reaper is None or not self._reaper.is_alive():
                self._reaper = threading.Thread(target=self._reap, name="easyui-worker-reaper", daemon=True)
                self._reaper.start()

    def _lost(self, w: _Worker, *, killed: bool) -> None:
        w.kill()
        with self._lock:
            self._busy.discard(w)
            if killed:
                self.killed += 1

    def _reap(self) -> None:
        while True:
            with self._lock:
                if not self._idle or self._closed:
                    self._reaper = None
                    return
                now = time.monotonic()
                old = [w for w in self._idle if now - w.last_used >= self.idle_s]
                self._idle = [w for w in self._idle if w not in old]
                wait = min((self.idle_s - (now - w.last_used) for w in self._idle), default=0.0)
            for w in old:
                w.kill()
            if wait > 0:
                self._wake.wait(wait)
                self._wake.clear()

    # --- public ---------------------------------------------------------------

    @contextlib.contextmanager
    def session(self, bundle_path: str | Path, limits: sandbox.Limits = sandbox.DEFAULT_LIMITS) -> Iterator[RemoteSandbox]:
        if not self._slots.acquire(timeout=self.wait_s):
            raise TemporaryFailure(
                "The dashboards are busy with other requests right now, so this one was not run.",
                hint="Try again in a few seconds.",
                code="busy",
            )
        try:
            sb = RemoteSandbox(self, bundle_path, limits)
            try:
                yield sb
            finally:
                sb.close()
        finally:
            self._slots.release()

    def worker_pids(self) -> list[int]:
        with self._lock:
            return sorted(w.pid for w in [*self._idle, *self._busy] if w.alive())

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"size": self.size, "idle": len(self._idle), "busy": len(self._busy), "started": self.started,
                    "killed": self.killed}

    def close(self) -> None:
        with self._lock:
            self._closed = True
            workers = [*self._idle, *self._busy]
            self._idle = []
        self._wake.set()
        for w in workers:
            w.kill()


_pools: weakref.WeakSet[WorkerPool] = weakref.WeakSet()


@atexit.register
def _close_all() -> None:
    for pool in list(_pools):
        pool.close()


_default: WorkerPool | None = None
_default_lock = threading.Lock()


def default_pool() -> WorkerPool:
    global _default
    with _default_lock:
        if _default is None or _default._closed:
            _default = WorkerPool(2)
        return _default

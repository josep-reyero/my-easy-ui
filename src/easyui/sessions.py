"""Server-side sign-in sessions, kept on disk in ``<state>/auth/sessions.json``.

A session id is 32 random bytes (256 bits, ``secrets.token_urlsafe(32)``). The
browser holds the id in an HttpOnly cookie; the server stores only its SHA-256
hash, so a copy of the state folder does not contain usable sessions. The
lookup is by that hash: an attacker cannot choose the hash of a guess, so the
timing of the lookup tells them nothing useful about a real id.

Every session has two lifetimes: it ends after ``idle`` seconds without use
(default 7 days) and in any case ``absolute`` seconds after sign-in (default 30
days). Sign-in always creates a new session (the old one, if the browser sent
one, is removed first), and sign-out removes it. Each session remembers how it
was made (``how``: ``password``, ``passkey``, ``recovery`` or ``preview``). Setting a
new password removes every session; changing how signing in works (removing a
passkey, turning password sign-in on or off, making new recovery codes) ends
every other session (``clear_others``) on the page, and every session from the
command line.

The file is written atomically with owner-only permissions (0600) under a lock
file, and the store re-reads it whenever it changes on disk, so
``easy-ui set-password`` signs out a running server's browsers at once.
"Last used" is written at most once a minute per session, to keep disk writes low.

Public API
----------
``IDLE_SECONDS``, ``ABSOLUTE_SECONDS``, ``MAX_SESSIONS``, ``TOUCH_EVERY``
``new_secret() -> str``                 a fresh 256-bit URL-safe token.
``digest(secret) -> str``               SHA-256 hex of a token.
``read_json(path, default) -> Any``     reads a JSON file; ``default`` when missing or broken.
``write_private_json(path, data)``      atomic write, mode 0600, missing folders created 0700.
``ensure_private_dir(path) -> Path``    creates missing folders with mode 0700.
``locked(path)``                        context manager: an exclusive ``fcntl`` lock on ``path``.
``Session`` (frozen dataclass): ``key`` (the stored hash), ``created``, ``seen``, ``device``, ``how``.
``SessionStore(auth_dir, *, idle=IDLE_SECONDS, absolute=ABSOLUTE_SECONDS, clock=time.time)``
    ``create(device=None, how=None) -> (secret, Session)``, ``get(secret) -> Session | None``
    (also refreshes "last used"), ``delete(secret) -> bool``, ``clear() -> int``,
    ``clear_others(keep_key) -> int`` (ends every session but the one with that stored hash),
    ``count() -> int``, ``max_age() -> int`` (cookie lifetime in seconds).
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import secrets
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

IDLE_SECONDS = 7 * 24 * 3600
ABSOLUTE_SECONDS = 30 * 24 * 3600
MAX_SESSIONS = 50
TOUCH_EVERY = 60
SESSIONS_FILE = "sessions.json"
LOCK_FILE = ".lock"
_SECRET_MAX = 200


def new_secret() -> str:
    return secrets.token_urlsafe(32)


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8", "surrogatepass")).hexdigest()


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def ensure_private_dir(path: Path) -> Path:
    """Create ``path`` and any missing parent folders, each with mode 0700 (``mkdir -p`` would not)."""
    path = Path(path)
    missing = []
    p = path
    while not p.exists():
        missing.append(p)
        if p.parent == p:
            break
        p = p.parent
    for d in reversed(missing):
        try:
            d.mkdir(mode=0o700)
        except FileExistsError:
            pass
    return path


def write_private_json(path: Path, data: Any) -> None:
    path = Path(path)
    ensure_private_dir(path.parent)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"), sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


_thread_locks: dict[str, threading.Lock] = {}
_thread_locks_guard = threading.Lock()


@contextlib.contextmanager
def locked(path: Path) -> Iterator[None]:
    """An exclusive lock shared by threads of this process and by other processes."""
    path = Path(path)
    with _thread_locks_guard:
        tl = _thread_locks.setdefault(str(path), threading.Lock())
    with tl:
        ensure_private_dir(path.parent)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)


@dataclass(frozen=True)
class Session:
    key: str
    created: float
    seen: float
    device: str | None = None
    how: str | None = None


class SessionStore:
    def __init__(
        self,
        auth_dir: Path,
        *,
        idle: int = IDLE_SECONDS,
        absolute: int = ABSOLUTE_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.dir = Path(auth_dir)
        self.path = self.dir / SESSIONS_FILE
        self.lock_path = self.dir / LOCK_FILE
        self.idle = int(idle)
        self.absolute = int(absolute)
        self.clock = clock
        self._cache: dict[str, dict[str, Any]] = {}
        self._stamp: tuple[int, int, int] | None = None

    # --- file ----------------------------------------------------------------

    def _file_stamp(self) -> tuple[int, int, int] | None:
        try:
            s = self.path.stat()
        except OSError:
            return None
        return (s.st_ino, s.st_mtime_ns, s.st_size)

    def _load(self) -> dict[str, dict[str, Any]]:
        stamp = self._file_stamp()
        if stamp != self._stamp or stamp is None:
            data = read_json(self.path, {}) if stamp is not None else {}
            raw = data.get("sessions", {}) if isinstance(data, dict) else {}
            self._cache = {
                k: v for k, v in raw.items() if isinstance(k, str) and len(k) == 64 and isinstance(v, dict)
            }
            self._stamp = stamp
        return self._cache

    def _save(self, sessions: dict[str, dict[str, Any]]) -> None:
        write_private_json(self.path, {"v": 1, "sessions": sessions})
        self._cache = sessions
        self._stamp = self._file_stamp()

    def _alive(self, rec: dict[str, Any], now: float) -> bool:
        try:
            created, seen = float(rec["created"]), float(rec["seen"])
        except (KeyError, TypeError, ValueError):
            return False
        return now - created < self.absolute and now - seen < self.idle and created <= now + 60

    def _sweep(self, sessions: dict[str, dict[str, Any]], now: float) -> dict[str, dict[str, Any]]:
        live = {k: v for k, v in sessions.items() if self._alive(v, now)}
        if len(live) > MAX_SESSIONS:
            newest = sorted(live.items(), key=lambda kv: float(kv[1]["seen"]), reverse=True)[:MAX_SESSIONS]
            live = dict(newest)
        return live

    # --- API -------------------------------------------------------------------

    def max_age(self) -> int:
        return self.absolute

    def create(self, device: str | None = None, how: str | None = None) -> tuple[str, Session]:
        secret = new_secret()
        key = digest(secret)
        now = self.clock()
        with locked(self.lock_path):
            sessions = self._sweep(dict(self._load()), now)
            sessions[key] = {"created": now, "seen": now, "device": device, "how": how}
            if len(sessions) > MAX_SESSIONS:
                sessions = self._sweep(sessions, now)
            self._save(sessions)
        return secret, Session(key, now, now, device, how)

    def get(self, secret: str | None) -> Session | None:
        if not secret or not isinstance(secret, str) or len(secret) > _SECRET_MAX:
            return None
        key = digest(secret)
        now = self.clock()
        sessions = self._load()
        rec = sessions.get(key)
        if rec is None:
            return None
        if not self._alive(rec, now):
            with locked(self.lock_path):
                current = dict(self._load())
                current.pop(key, None)
                self._save(self._sweep(current, now))
            return None
        seen = float(rec["seen"])
        if now - seen >= TOUCH_EVERY:
            with locked(self.lock_path):
                current = dict(self._load())
                if key in current:
                    current[key] = {**current[key], "seen": now}
                    self._save(current)
                    seen = now
        how = rec.get("how")
        return Session(key, float(rec["created"]), seen, rec.get("device"), how if isinstance(how, str) else None)

    def delete(self, secret: str | None) -> bool:
        if not secret or not isinstance(secret, str) or len(secret) > _SECRET_MAX:
            return False
        key = digest(secret)
        with locked(self.lock_path):
            current = dict(self._load())
            if key not in current:
                return False
            del current[key]
            self._save(current)
        return True

    def clear(self) -> int:
        with locked(self.lock_path):
            n = len(self._load())
            self._save({})
        return n

    def clear_others(self, keep_key: str) -> int:
        """End every session except the one whose stored hash is ``keep_key``; how many were ended."""
        with locked(self.lock_path):
            current = dict(self._load())
            kept = {k: v for k, v in current.items() if k == keep_key}
            n = len(current) - len(kept)
            if n:
                self._save(kept)
        return n

    def count(self) -> int:
        now = self.clock()
        return sum(1 for v in self._load().values() if self._alive(v, now))

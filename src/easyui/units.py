"""systemd units that run one app's dashboard server on a server you own.

``easy-ui units --render-only [--out DIR]`` writes (or prints) two files:

- ``easy-ui-<app>.socket`` listens on a unix socket (owner-only, mode 0600;
  by default owned by root so only the local proxy, for example Tailscale
  serve, can connect). systemd starts the service on the first connection (the
  service has no ``[Install]`` section, so it never runs at boot by itself).
- ``easy-ui-<app>.service`` runs ``easy-ui serve`` as a dedicated account with
  no privileges: no new privileges, a read-only system, no home folders, a
  private /tmp and /dev, no network of its own (``PrivateNetwork=yes``, only
  ``AF_UNIX``), no capabilities, the ``@system-service`` system-call set, at most
  256 MB of memory, ``UMask=0077``. Other apps' state is hidden; only this app's
  published dashboards (``specs/``) and data bundles (``data/``) are bound in
  read-only, and its ``auth/`` folder (password hash, sessions) read-write. The
  app's own raw data folder is made inaccessible. ``ExecStart`` runs the
  program directly, never through a shell.

The server takes the listening socket from systemd (``LISTEN_FDS``), so
``ExecStart`` is just ``<exec> serve --app-dir <app>``.

Every value put into a unit is validated against a strict pattern (names: the
app-name / account-name patterns; paths: absolute, normalised, only
``[A-Za-z0-9._/+-]``; memory: ``<n>[KMG]``) and then escaped for systemd
(``%`` specifiers, ``$`` expansion, backslashes), and free text such as the app
title loses control characters, so nothing an app provides can add a directive.

Installing the files needs an administrator, so easy-ui only renders them.

Public API
----------
``DEFAULT_EXEC``, ``DEFAULT_STATE_HOME`` (``/var/lib/easy-ui``),
``DEFAULT_DATA_HOME`` (``/var/lib/mes``), ``DEFAULT_SOCKET_DIR`` (``/run/easy-ui``),
``DEFAULT_MEMORY_MAX`` (``256M``), ``USER_RE``, ``PATH_RE``.
``check_path(value, *, what) -> str``     validated absolute path (raises ``UsageError``).
``check_user(value) -> str``              validated account name.
``check_service_user(value) -> str``      also refuses root and shared system accounts.
``escape_value(value) -> str``            systemd escaping of an already-validated value.
``clean_text(value) -> str``              one safe line of free text (for Description=).
``unit_names(app) -> (socket_name, service_name)``
``render_units(app, *, title=None, user=None, exec_path=DEFAULT_EXEC, app_path, state_home=...,
data_home=..., socket_path=None, socket_owner="root", memory_max=DEFAULT_MEMORY_MAX, allow_hosts=()) -> dict[str, str]``
    Refuses root and shared system accounts for ``user`` (``REFUSED_ACCOUNTS``), shared accounts other than
    root for ``socket_owner``, and a program path under /tmp, /var/tmp or /dev/shm. ``allow_hosts`` become
    ``EASYUI_ALLOW_HOSTS`` (the names the private network's proxy uses, which the server must accept).
    ``{file name: unit text}``.
``write_units(units, out_dir) -> list[str]``  writes without following links.
``register(subparsers)``                  the ``units`` command.
"""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any

from easyui import paths
from easyui.errors import NeedsHuman, UsageError

DEFAULT_EXEC = "/usr/local/bin/easy-ui"
DEFAULT_STATE_HOME = "/var/lib/easy-ui"
DEFAULT_DATA_HOME = "/var/lib/mes"
DEFAULT_SOCKET_DIR = "/run/easy-ui"
DEFAULT_MEMORY_MAX = "256M"
USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}\Z")
PATH_RE = re.compile(r"^/[A-Za-z0-9._/+-]{0,4000}\Z")
MEMORY_RE = re.compile(r"^[1-9][0-9]{0,6}[KMG]\Z")
_HIDDEN_BY_PROTECT_HOME = ("/home", "/root", "/run/user")
# Accounts a service must never run as: the administrator and shared system accounts.
REFUSED_ACCOUNTS = frozenset({
    "root", "toor", "nobody", "nogroup", "daemon", "bin", "sys", "sync", "adm", "wheel", "sudo", "admin",
    "operator", "staff", "users", "systemd-network", "systemd-resolve", "messagebus", "www-data",
})
# Folders anyone can write to: a program there could be replaced by another account.
_WORLD_WRITABLE = ("/tmp", "/var/tmp", "/dev/shm", "/run/shm")
HOST_RE = re.compile(r"^(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9-]{0,62})(?:\.[a-z0-9](?:[a-z0-9-]{0,62}))*\Z")


def check_path(value: Any, *, what: str) -> str:
    bad = UsageError(
        f"{what} is {str(value)[:80]!r}, which easy-ui will not put into a service file.",
        hint="Use an absolute path made of letters, digits and . _ / + - only (no spaces, quotes, $, %, : or "
        "'..'), for example /srv/apps/fitness.",
    )
    if not isinstance(value, str) or not PATH_RE.match(value):
        raise bad
    parts = PurePosixPath(value).parts
    if value != "/" and (value.endswith("/") or "//" in value or any(p in (".", "..") for p in value.split("/")[1:])):
        raise bad
    if len(parts) < 2:
        raise bad
    return value


def check_user(value: Any) -> str:
    if not isinstance(value, str) or not USER_RE.match(value):
        raise UsageError(
            f"{str(value)[:40]!r} is not an account name easy-ui will put into a service file.",
            hint="Use up to 32 lowercase letters, digits, _ and -, starting with a letter or _, for example easyui-fitness.",
        )
    return value


def check_service_user(value: Any) -> str:
    """An account the server may run as: a valid name that is not root or a shared system account."""
    value = check_user(value)
    if value in REFUSED_ACCOUNTS:
        raise UsageError(
            f"easy-ui will not write a service that runs as {value!r}, because that account is the administrator or "
            "is shared with other programs. A mistake in a web server running as it could reach far more than this "
            "app's dashboards.",
            hint="Use a dedicated account for this app, for example the default easyui-<app> (leave --user out).",
            code="account_refused",
        )
    return value


def check_hosts(values: Any) -> list[str]:
    out = []
    for v in values or []:
        h = str(v).strip().lower().rstrip(".")
        if not HOST_RE.match(h):
            raise UsageError(
                f"{str(v)[:80]!r} is not a host name easy-ui will put into a service file.",
                hint="Give the name the dashboards are opened under, for example myserver.example.ts.net.",
            )
        out.append(h)
    return list(dict.fromkeys(out))


def escape_value(value: str) -> str:
    """Escape for systemd: specifiers (%), variable expansion ($) and backslashes; refuses line breaks."""
    if any(c in value for c in "\n\r\x00"):
        raise UsageError("A value for a service file contains a line break.", hint="Remove it.")
    return value.replace("\\", "\\\\").replace("%", "%%").replace("$", "$$")


def clean_text(value: Any, limit: int = 80) -> str:
    """One line of plain text for Description=: no control characters, escaped, short."""
    s = "".join(c if c.isprintable() else " " for c in str(value))
    s = " ".join(s.split())[:limit]
    return escape_value(s)


def unit_names(app: str) -> tuple[str, str]:
    if not isinstance(app, str) or not paths.APP_NAME_RE.match(app):
        raise UsageError(
            f"{str(app)[:40]!r} is not a valid app name, so no service files were made.",
            hint="App names use 2 to 31 lowercase letters, digits and dashes, starting with a letter.",
        )
    return f"easy-ui-{app}.socket", f"easy-ui-{app}.service"


def _under(path: str, roots: tuple[str, ...]) -> bool:
    return any(path == r or path.startswith(r + "/") for r in roots)


def render_units(
    app: str,
    *,
    title: str | None = None,
    user: str | None = None,
    exec_path: str = DEFAULT_EXEC,
    app_path: str,
    state_home: str = DEFAULT_STATE_HOME,
    data_home: str = DEFAULT_DATA_HOME,
    socket_path: str | None = None,
    socket_owner: str = "root",
    memory_max: str = DEFAULT_MEMORY_MAX,
    allow_hosts: list[str] | tuple[str, ...] = (),
) -> dict[str, str]:
    socket_name, service_name = unit_names(app)
    if user is None:
        user = f"easyui-{app}"
        if len(user) > 32:
            raise UsageError(
                f"The default account name {user!r} is longer than 32 characters.",
                hint="Choose a shorter one with --user, for example --user eui-" + app[:20],
            )
    user = check_service_user(user)
    owner = check_user(socket_owner)
    if owner in REFUSED_ACCOUNTS - {"root"}:
        raise UsageError(
            f"The socket owner {owner!r} is an account shared with other programs, so every one of them could reach "
            "the dashboards.",
            hint="Leave --socket-owner out (root: only the local proxy, which runs as root, can connect), or name the "
            "proxy's own account.",
            code="account_refused",
        )
    hosts = check_hosts(allow_hosts)
    exec_path = check_path(exec_path, what="The program path (--exec)")
    if _under(exec_path, _WORLD_WRITABLE):
        raise UsageError(
            f"The program path {exec_path} is in a folder every account can write to, so another program could "
            "replace easy-ui there.",
            hint="Install easy-ui somewhere only an administrator can change, for example /usr/local/bin or /opt.",
            code="path_refused",
        )
    app_path = check_path(app_path, what="The app folder on the server (--app-path)")
    state_home = check_path(state_home, what="The easy-ui state folder (--state-home)")
    data_home = check_path(data_home, what="The app data folder (--data-home)")
    socket_path = check_path(socket_path or f"{DEFAULT_SOCKET_DIR}/{app}.sock", what="The socket path (--socket)")
    if not isinstance(memory_max, str) or not MEMORY_RE.match(memory_max):
        raise UsageError(f"{str(memory_max)[:20]!r} is not a memory size.", hint="Use a number and K, M or G, e.g. 256M.")
    for p, what in ((app_path, "--app-path"), (state_home, "--state-home"), (exec_path, "--exec")):
        if _under(p, _HIDDEN_BY_PROTECT_HOME):
            raise UsageError(
                f"{p} ({what}) is inside a home folder, which the service cannot see (ProtectHome=yes hides home "
                "folders), so the server would not start.",
                hint="Put the app and easy-ui somewhere like /srv or /opt, and the state in /var/lib.",
            )
    for a, b in ((app_path, state_home), (data_home, state_home)):
        if _under(a, (b,)) or _under(b, (a,)):
            raise UsageError(
                f"{a} and {b} overlap, but the app folder, the app data folder and the easy-ui state folder must be "
                "separate (the service hides the app data folder and shows only this app's part of the state folder).",
                hint="For example --app-path /srv/apps/<app>, --data-home /var/lib/mes and --state-home /var/lib/easy-ui.",
            )

    e = escape_value
    root = f"{state_home}/{app}"
    desc = clean_text(f"easy-ui dashboards for {title or app} ({app})", limit=120)
    header = (
        f"# Rendered by easy-ui units for the app {app}. Install as an administrator.\n"
        f"# The account {user} must exist with no login shell (for example: useradd --system --no-create-home "
        f"--shell /usr/sbin/nologin {user})\n"
        f"# and be able to read {root}/specs and {root}/data and write {root}/auth.\n"
    )
    socket = header + "\n".join([
        "",
        "[Unit]",
        f"Description={desc} (socket)",
        "",
        "[Socket]",
        f"ListenStream={e(socket_path)}",
        f"SocketUser={e(owner)}",
        f"SocketGroup={e(owner)}",
        "SocketMode=0600",
        "DirectoryMode=0711",
        "RemoveOnStop=yes",
        "Accept=no",
        "",
        "[Install]",
        "WantedBy=sockets.target",
        "",
    ])
    service = header + "\n".join([
        "",
        "[Unit]",
        f"Description={desc}",
        f"Requires={socket_name}",
        f"After={socket_name}",
        "",
        "[Service]",
        "Type=simple",
        f"ExecStart={e(exec_path)} serve --app-dir {e(app_path)}",
        f"User={e(user)}",
        f"Group={e(user)}",
        "WorkingDirectory=/",
        f'Environment="EASYUI_STATE_HOME={e(state_home)}" "MES_DATA_HOME={e(data_home)}" "HOME=/nonexistent"',
        *([f'Environment="EASYUI_ALLOW_HOSTS={e(",".join(hosts))}"'] if hosts else []),
        "UMask=0077",
        "Restart=on-failure",
        "RestartSec=2",
        "",
        "# No way to gain privileges or capabilities.",
        "NoNewPrivileges=yes",
        "CapabilityBoundingSet=",
        "AmbientCapabilities=",
        "RestrictSUIDSGID=yes",
        "",
        "# Read-only system, no home folders, private /tmp and /dev.",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "PrivateTmp=yes",
        "PrivateDevices=yes",
        "DevicePolicy=closed",
        "ProtectKernelTunables=yes",
        "ProtectKernelModules=yes",
        "ProtectKernelLogs=yes",
        "ProtectControlGroups=yes",
        "ProtectClock=yes",
        "ProtectHostname=yes",
        "ProtectProc=invisible",
        "ProcSubset=pid",
        "KeyringMode=private",
        "RemoveIPC=yes",
        "",
        "# Only this app's published dashboards and data bundles, read-only; other apps' state is hidden.",
        f"TemporaryFileSystem={e(state_home)}:ro",
        f"BindReadOnlyPaths={e(root)}/specs {e(root)}/data",
        f"BindPaths={e(root)}/auth",
        f"InaccessiblePaths=-{e(data_home)}",
        "",
        "# No network of its own: requests arrive only through the socket.",
        "PrivateNetwork=yes",
        "RestrictAddressFamilies=AF_UNIX",
        "IPAddressDeny=any",
        "",
        "# System calls and resources.",
        "SystemCallFilter=@system-service",
        "SystemCallErrorNumber=EPERM",
        "SystemCallArchitectures=native",
        "RestrictNamespaces=yes",
        "RestrictRealtime=yes",
        "LockPersonality=yes",
        "MemoryDenyWriteExecute=yes",
        f"MemoryMax={e(memory_max)}",
        "TasksMax=64",
        "LimitNOFILE=1024",
        "LimitCORE=0",
        "",
        "# No [Install] section: the service starts only when the socket gets a connection.",
        "",
    ])
    return {socket_name: socket, service_name: service}


def write_units(units: dict[str, str], out_dir: str | Path) -> list[str]:
    out = Path(out_dir)
    if out.is_symlink() or (out.exists() and not out.is_dir()):
        raise UsageError(f"{out} is not a folder easy-ui writes into.", hint="Pass --out with a real folder.")
    out.mkdir(parents=True, exist_ok=True, mode=0o755)
    written = []
    for name, text in units.items():
        try:
            fd = os.open(out / name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o644)
        except OSError as e:
            raise UsageError(
                f"{out / name} could not be written (it may be a link, which easy-ui does not follow).",
                hint="Remove that file or pick another --out folder.",
                detail=str(e),
            ) from None
        try:
            os.write(fd, text.encode("utf-8"))
        finally:
            os.close(fd)
        written.append(str(out / name))
    return written


def _cmd_units(args: argparse.Namespace) -> Any:
    from easyui.cli import Result, load_config

    if not args.render_only:
        raise NeedsHuman(
            "Installing service files needs an administrator, so easy-ui does not install them itself.",
            hint="Run: easy-ui units --render-only --out DIR, then ask the person who runs the server to install "
            "them (or let mes deploy do it).",
        )
    cfg = load_config(args)
    units = render_units(
        cfg.name,
        title=cfg.title,
        user=args.user,
        exec_path=args.exec_path,
        app_path=args.app_path or str(cfg.app_dir),
        state_home=args.state_home,
        data_home=args.data_home,
        socket_path=args.socket,
        socket_owner=args.socket_owner,
        allow_hosts=args.allow_host or [],
    )
    if args.out:
        written = write_units(units, args.out)
        data = {"app": cfg.name, "written": written, "units": units}
        text = "Wrote " + " and ".join(written) + ". An administrator installs them (copy to /etc/systemd/system, " \
            "then systemctl daemon-reload and systemctl enable --now " + next(iter(units)) + ")."
        return Result(data, text)
    text = "\n".join(f"### {name}\n{body}" for name, body in units.items())
    return Result({"app": cfg.name, "written": [], "units": units}, text)


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "units",
        help="Show the service files that run the server.",
        description="Render the systemd socket and service files that run this app's dashboard server as a locked-down "
        "account. easy-ui only renders them; an administrator installs them.",
    )
    p.add_argument("--render-only", action="store_true", help="render the files (required: easy-ui never installs them)")
    p.add_argument("--out", metavar="DIR", help="write the files into DIR instead of printing them")
    p.add_argument("--user", help="the account the server runs as (default: easyui-<app>)")
    p.add_argument("--exec", dest="exec_path", default=DEFAULT_EXEC, help=f"path of easy-ui on the server (default {DEFAULT_EXEC})")
    p.add_argument("--app-path", help="the app's folder on the server (default: this app's folder)")
    p.add_argument("--state-home", default=DEFAULT_STATE_HOME, help=f"easy-ui state folder on the server (default {DEFAULT_STATE_HOME})")
    p.add_argument("--data-home", default=DEFAULT_DATA_HOME, help=f"app data folder on the server (default {DEFAULT_DATA_HOME})")
    p.add_argument("--socket", help=f"the socket path (default {DEFAULT_SOCKET_DIR}/<app>.sock)")
    p.add_argument("--socket-owner", default="root", help="the account that may connect to the socket (default root)")
    p.add_argument(
        "--allow-host",
        action="append",
        metavar="NAME",
        help="a host name the dashboards are opened under, e.g. the private network name (repeatable); without it "
        "the server refuses requests that arrive under that name",
    )
    p.set_defaults(func=_cmd_units)

"""systemd units that run one project's dashboard server on a server you own (the design, "Accounts on a server").

``easy-ui units --render-only [--out DIR]`` writes (or prints) two **system** units:

- ``mes-<project>-web.socket`` listens on a unix socket (mode 0600, owned by root
  by default, so only the local proxy, for example Tailscale serve, can connect).
  systemd starts the service on the first connection (the service has no
  ``[Install]`` section, so it never runs at boot by itself).
- ``mes-<project>-web.service`` runs ``easy-ui serve --ui-dir <root>/<project>/ui``
  as the account ``mes-<project>-web`` (no password, no sudo, no login shell):
  no new privileges, a read-only system, no home folders, a private /tmp and
  /dev, no network of its own (``PrivateNetwork=yes``, only ``AF_UNIX``), no
  capabilities, the ``@system-service`` system-call set, at most 256 MB of
  memory, ``UMask=0077``. ``<root>`` (``/var/lib/mes``) is replaced by an empty
  read-only folder, and only this project's ``ui/`` is put back, read-only,
  with ``<root>/<project>/auth/`` (password hash, passkeys, recovery codes,
  sessions; beside ``ui/``, never inside it, because the project's account
  writes ``ui/``) the one writable place. So the web account sees nothing else of the project: not its
  code, data, state, saved logins or store, and nothing of other projects. It
  never reads ``project.toml`` (``ui/project.json``, written by ``data build``
  and ``publish``, tells it the project's name and title). ``ExecStart`` runs
  the program directly, never through a shell.

The folders the units expect, which the administrator's script creates (``mes
server plan`` does this), are listed in the result as ``folders`` (path,
owner, group, mode, why) and in the units' header comment:

- ``<root>/<project>``           ``root:root``  0711 (only root may change what is in it)
- ``<root>/<project>/ui``        ``mes-<project>:mes-<project>-web``  2750 (setgid: new files get the web group)
- ``<root>/<project>/ui/data``   ``mes-<project>:mes-<project>-web``  2750
- ``<root>/<project>/ui/specs``  ``mes-<project>:mes-<project>-web``  2750
- ``<root>/<project>/auth``      ``mes-<project>-web:mes-<project>-web``  0700

Sign-in data is never read from ``<root>/<project>/ui``: the project's account
can change that folder, so the sign-in lives beside it, in ``auth``.

``mes-<project>`` runs ``easy-ui data build`` and ``easy-ui publish`` (with
``MES_ROOT=<root>``); the person runs ``easy-ui set-password``, ``passkeys``,
``sign-in`` and ``recovery-codes`` as the web account, for example
``sudo -u mes-<project>-web easy-ui set-password --ui-dir <root>/<project>/ui
--auth-dir <root>/<project>/auth`` (with ``MES_ROOT=<root>`` the sign-in folder
is found without ``--auth-dir``).

Every value put into a unit is validated against a strict pattern (the
project name, the account names, absolute normalised paths of
``[A-Za-z0-9._/+-]``, ``<n>[KMG]`` memory, web addresses) and then escaped for
systemd (``%`` specifiers, ``$`` expansion, backslashes); free text such as
the title loses control characters. A project name whose account names would
be longer than 32 characters (the limit for account names) is refused, as are
programs under ``/tmp``-like folders, under ``<root>`` (the project's account
could replace them) or in home folders.

Installing the files needs an administrator, so easy-ui only renders them.

Public API
----------
``DEFAULT_EXEC``, ``DEFAULT_ROOT`` (``/var/lib/mes``), ``DEFAULT_SOCKET_DIR`` (``/run/mes``),
``DEFAULT_MEMORY_MAX`` (``256M``), ``USER_RE``, ``PATH_RE``, ``MAX_ACCOUNT_LENGTH``.
``check_path(value, *, what) -> str``     validated absolute path (raises ``UsageError``).
``check_user(value) -> str``              validated account name.
``check_service_user(value) -> str``      also refuses root and shared system accounts.
``escape_value(value) -> str``            systemd escaping of an already-validated value.
``clean_text(value) -> str``              one safe line of free text (for Description=).
``accounts(project) -> (project_account, web_account)``  ``mes-<project>``, ``mes-<project>-web``.
``unit_names(project) -> (socket_name, service_name)``
``folders(project, *, root=DEFAULT_ROOT) -> list[dict]``
``unit_params(project, *, origins=(), allow_hosts=(), exec_path=..., root=..., socket_path=None, socket_owner="root",
memory_max=...) -> dict``  every value the units use, validated: ``project``, ``accounts`` ({project, web}),
    ``unit_names`` ({socket, service}), ``exec``, ``root``, ``ui_dir``, ``auth_dir``, ``socket``, ``socket_owner``,
    ``origins``, ``memory_max``, ``exec_start`` (the service's command as a list of arguments).
``check_origins(origins=(), allow_hosts=()) -> list[str]``  the web addresses, validated; at least one.
``render_units(project, *, origins=(), title=None, exec_path=DEFAULT_EXEC, root=DEFAULT_ROOT, socket_path=None,
socket_owner="root", memory_max=DEFAULT_MEMORY_MAX, allow_hosts=()) -> dict[str, str]``
    ``{file name: unit text}``. ``origins`` (``https://NAME[:PORT]``) and ``allow_hosts`` (``NAME``,
    shorthand for ``https://NAME``) are the exact web addresses the dashboards are opened with (the
    private network's proxy); at least one is needed, and each becomes an ``--origin`` argument of
    ``easy-ui serve`` in ``ExecStart=``. The server then accepts requests and passkeys for exactly
    those addresses and nothing else.
``write_units(units, out_dir) -> list[str]``  writes without following links.
``register(subparsers)``                  the ``units`` command.
"""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any

from easyui import paths, security
from easyui.errors import NeedsHuman, UsageError

DEFAULT_EXEC = "/usr/local/bin/easy-ui"
DEFAULT_ROOT = "/var/lib/mes"
DEFAULT_SOCKET_DIR = "/run/mes"
DEFAULT_MEMORY_MAX = "256M"
MAX_ACCOUNT_LENGTH = 32
USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}\Z")
PATH_RE = re.compile(r"^/[A-Za-z0-9._/+-]{0,4000}\Z")
MEMORY_RE = re.compile(r"^[1-9][0-9]{0,6}[KMG]\Z")
_HIDDEN_BY_PROTECT_HOME = ("/home", "/root", "/run/user")
# Accounts a service must never run as or hand its socket to: the administrator and shared system accounts.
REFUSED_ACCOUNTS = frozenset({
    "root", "toor", "nobody", "nogroup", "daemon", "bin", "sys", "sync", "adm", "wheel", "sudo", "admin",
    "operator", "staff", "users", "systemd-network", "systemd-resolve", "messagebus", "www-data",
})
# Folders anyone can write to: a program there could be replaced by another account.
_WORLD_WRITABLE = ("/tmp", "/var/tmp", "/dev/shm", "/run/shm")
EXEC_ARG_RE = re.compile(r"^[A-Za-z0-9._/+:\[\]-]+\Z")
HOST_RE = re.compile(r"^(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9-]{0,62})(?:\.[a-z0-9](?:[a-z0-9-]{0,62}))*\Z")


def check_path(value: Any, *, what: str) -> str:
    bad = UsageError(
        f"{what} is {str(value)[:80]!r}, which easy-ui will not put into a service file.",
        hint="Use an absolute path made of letters, digits and . _ / + - only (no spaces, quotes, $, %, : or "
        "'..'), for example /usr/local/bin/easy-ui.",
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
            hint="Use up to 32 lowercase letters, digits, _ and -, starting with a letter or _.",
        )
    return value


def check_service_user(value: Any) -> str:
    """An account the server may run as: a valid name that is not root or a shared system account."""
    value = check_user(value)
    if value in REFUSED_ACCOUNTS:
        raise UsageError(
            f"easy-ui will not write a service that runs as {value!r}, because that account is the administrator or "
            "is shared with other programs. A mistake in a web server running as it could reach far more than this "
            "project's dashboards.",
            hint="Use the project's own web account, mes-<project>-web.",
            code="account_refused",
        )
    return value


def check_origins(origins: Any = (), allow_hosts: Any = ()) -> list[str]:
    """The exact web addresses for the service, validated and normalised; at least one is needed."""
    out = []
    for v, parse in [(o, security.parse_origin) for o in origins or []] + [(h, security.origin_for_host) for h in allow_hosts or []]:
        try:
            o = parse(str(v).strip())
        except ValueError as e:
            raise UsageError(
                f"{e} So it was not put into a service file.",
                hint="Give the address the dashboards are opened with, for example --origin "
                "https://myserver.example.ts.net (or --allow-host myserver.example.ts.net).",
                code="bad_origin",
            ) from None
        if not (HOST_RE.match(o.host) or o.is_ip):
            raise UsageError(f"{str(v)[:80]!r} is not a web address easy-ui will put into a service file.",
                             hint="Use letters, digits, dots and dashes in the name.", code="bad_origin")
        out.append(str(o))
    if not out:
        raise UsageError(
            "The service needs the web address the dashboards will be opened with, so no service files were made. "
            "Without it the server would refuse every request.",
            hint="Add it, for example: easy-ui units --render-only --origin https://myserver.example.ts.net",
            code="origin_needed",
        )
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


def _check_project(project: Any) -> str:
    if not isinstance(project, str) or not paths.APP_NAME_RE.match(project):
        raise UsageError(
            f"{str(project)[:40]!r} is not a valid project name, so no service files were made.",
            hint="Project names use 2 to 31 lowercase letters, digits and dashes, starting with a letter.",
        )
    return project


def accounts(project: str) -> tuple[str, str]:
    project = _check_project(project)
    owner, web = f"mes-{project}", f"mes-{project}-web"
    if len(web) > MAX_ACCOUNT_LENGTH:
        raise UsageError(
            f"The project name {project!r} is too long for a server: its web account would be called {web!r}, "
            f"longer than the {MAX_ACCOUNT_LENGTH} characters an account name may have.",
            hint=f"Use a project name of at most {MAX_ACCOUNT_LENGTH - 8} characters for projects that run on a server.",
            code="name_too_long",
        )
    return check_user(owner), check_service_user(web)


def unit_names(project: str) -> tuple[str, str]:
    _owner, web = accounts(project)
    return f"{web}.socket", f"{web}.service"


def _under(path: str, roots: tuple[str, ...]) -> bool:
    return any(path == r or path.startswith(r + "/") for r in roots)


def folders(project: str, *, root: str = DEFAULT_ROOT) -> list[dict[str, str]]:
    owner, web = accounts(project)
    root = check_path(root, what="The folder for all projects (--root)")
    base = f"{root}/{project}"
    shared = "the project's account writes it; the web account only reads it (through its group)"
    return [
        {"path": base, "owner": "root", "group": "root", "mode": "0711",
         "why": "only root may add, rename or replace what is in it (so the project's account cannot touch auth/); "
         "other accounts may pass through, and the folders inside decide who may enter"},
        {"path": f"{base}/ui", "owner": owner, "group": web, "mode": "2750", "why": "published dashboards and data: " + shared},
        {"path": f"{base}/ui/data", "owner": owner, "group": web, "mode": "2750", "why": "dashboard data copies: " + shared},
        {"path": f"{base}/ui/specs", "owner": owner, "group": web, "mode": "2750", "why": "published dashboard versions: " + shared},
        {"path": f"{base}/auth", "owner": web, "group": web, "mode": "0700",
         "why": "sign-in (password hash, passkeys, recovery codes, sessions): the web account only, beside ui/ and "
         "never inside it"},
    ]


def unit_params(
    project: str,
    *,
    origins: list[str] | tuple[str, ...] = (),
    allow_hosts: list[str] | tuple[str, ...] = (),
    exec_path: str = DEFAULT_EXEC,
    root: str = DEFAULT_ROOT,
    socket_path: str | None = None,
    socket_owner: str = "root",
    memory_max: str = DEFAULT_MEMORY_MAX,
) -> dict[str, Any]:
    """Every value the two units need, validated (raises ``UsageError``). ``easy-ui units --json`` shows them, so
    a tool that renders the units from its own template gets exactly these values."""
    owner_account, web = accounts(project)
    socket_name, service_name = unit_names(project)
    sock_owner = check_user(socket_owner)
    if sock_owner in REFUSED_ACCOUNTS - {"root"} or sock_owner == owner_account:
        raise UsageError(
            f"The socket owner {sock_owner!r} is an account shared with other programs or the project's own account, "
            "so it could reach the dashboards without signing in through the private network's proxy.",
            hint="Leave --socket-owner out (root: only the local proxy, which runs as root, can connect), or name the "
            "proxy's own account.",
            code="account_refused",
        )
    site = check_origins(origins, allow_hosts)
    exec_path = check_path(exec_path, what="The program path (--exec)")
    root = check_path(root, what="The folder for all projects (--root)")
    if _under(exec_path, _WORLD_WRITABLE):
        raise UsageError(
            f"The program path {exec_path} is in a folder every account can write to, so another program could "
            "replace easy-ui there.",
            hint="Install easy-ui somewhere only an administrator can change, for example /usr/local/bin or /opt.",
            code="path_refused",
        )
    if _under(exec_path, (root,)):
        raise UsageError(
            f"The program path {exec_path} is inside {root}, where the projects' own accounts can write, and which the "
            "web service cannot see anyway.",
            hint="Install easy-ui for the whole server, somewhere only an administrator can change, for example "
            "/usr/local/bin/easy-ui or /opt/easy-ui/bin/easy-ui.",
            code="path_refused",
        )
    for p, what in ((exec_path, "--exec"), (root, "--root")):
        if _under(p, _HIDDEN_BY_PROTECT_HOME):
            raise UsageError(
                f"{p} ({what}) is inside a home folder, which the service cannot see (ProtectHome=yes hides home "
                "folders), so the server would not start.",
                hint="Install easy-ui somewhere like /usr/local/bin or /opt, and keep the projects in /var/lib/mes.",
            )
    socket_path = check_path(socket_path or f"{DEFAULT_SOCKET_DIR}/{project}-web.sock", what="The socket path (--socket)")
    if _under(socket_path, (root,)):
        raise UsageError("The socket must not be inside the projects' folder.", hint=f"Use for example {DEFAULT_SOCKET_DIR}/{project}-web.sock.")
    if not isinstance(memory_max, str) or not MEMORY_RE.match(memory_max):
        raise UsageError(f"{str(memory_max)[:20]!r} is not a memory size.", hint="Use a number and K, M or G, e.g. 256M.")
    ui, auth = f"{root}/{project}/ui", f"{root}/{project}/auth"
    argv = [exec_path, "serve", "--ui-dir", ui, "--auth-dir", auth]
    for o in site:
        argv += ["--origin", o]
    for a in argv:  # nothing systemd would expand or unquote: every value was checked against a strict pattern
        if not EXEC_ARG_RE.match(a):  # pragma: no cover - the checks above make this impossible
            raise UsageError(f"{a[:80]!r} cannot go into a service file.", hint="Use plain paths and addresses.")
    return {
        "project": project,
        "accounts": {"project": owner_account, "web": web},
        "unit_names": {"socket": socket_name, "service": service_name},
        "exec": exec_path,
        "root": root,
        "ui_dir": ui,
        "auth_dir": auth,
        "socket": socket_path,
        "socket_owner": sock_owner,
        "origins": site,
        "memory_max": memory_max,
        "exec_start": argv,
    }


def render_units(
    project: str,
    *,
    origins: list[str] | tuple[str, ...] = (),
    title: str | None = None,
    exec_path: str = DEFAULT_EXEC,
    root: str = DEFAULT_ROOT,
    socket_path: str | None = None,
    socket_owner: str = "root",
    memory_max: str = DEFAULT_MEMORY_MAX,
    allow_hosts: list[str] | tuple[str, ...] = (),
) -> dict[str, str]:
    prm = unit_params(project, origins=origins, allow_hosts=allow_hosts, exec_path=exec_path, root=root,
                      socket_path=socket_path, socket_owner=socket_owner, memory_max=memory_max)
    owner_account, web = prm["accounts"]["project"], prm["accounts"]["web"]
    socket_name, service_name = prm["unit_names"]["socket"], prm["unit_names"]["service"]
    sock_owner, socket_path, memory_max, root = prm["socket_owner"], prm["socket"], prm["memory_max"], prm["root"]
    auth = prm["auth_dir"]
    e = escape_value
    ui = prm["ui_dir"]
    desc = clean_text(f"easy-ui dashboards for {title or project} ({project})", limit=120)
    layout = "\n".join(
        f"#   {f['path']:<40} {f['owner']}:{f['group']} {f['mode']}" for f in folders(project, root=root)
    )
    header = (
        f"# Rendered by easy-ui units for the project {project}. Install as an administrator (a system unit).\n"
        f"# The accounts {owner_account} and {web} must exist, without a password, sudo or a login shell, and\n"
        f"# these folders with these owners and modes:\n{layout}\n"
        f"# An old {ui}/auth is not used (its contents cannot be trusted): set the password and add passkeys again.\n"
    )
    socket = header + "\n".join([
        "",
        "[Unit]",
        f"Description={desc} (socket)",
        "",
        "[Socket]",
        f"ListenStream={e(socket_path)}",
        f"SocketUser={e(sock_owner)}",
        f"SocketGroup={e(sock_owner)}",
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
        "ExecStart=" + " ".join(prm["exec_start"]),
        f"User={e(web)}",
        f"Group={e(web)}",
        "WorkingDirectory=/",
        "Environment=HOME=/nonexistent MES_CONFIG=/nonexistent",
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
        "# Only this project's ui/ folder, read-only, and its sign-in folder auth/ (beside ui/), the one writable",
        "# place. The project's code, data, state, saved logins and store, and every other project, are not there.",
        f"TemporaryFileSystem={e(root)}:ro",
        f"BindReadOnlyPaths={e(ui)}",
        f"BindPaths={e(auth)}",
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
        "# Queries run in worker processes (at most 4, each capped far below MemoryMax); when memory runs out the",
        "# kernel stops a worker first, and the server answers that query with an error and keeps running.",
        "OOMPolicy=continue",
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
            "them (mes server plan includes them in its script).",
        )
    cfg = load_config(args)
    kw: dict[str, Any] = {
        "exec_path": args.exec_path, "root": args.root, "socket_path": args.socket, "socket_owner": args.socket_owner,
        "memory_max": args.memory_max, "origins": args.origin or [], "allow_hosts": args.allow_host or [],
    }
    units = render_units(cfg.name, title=cfg.title, **kw)
    prm = unit_params(cfg.name, **kw)
    data = {
        "app": cfg.name,
        **prm,
        "folders": folders(cfg.name, root=prm["root"]),
        "enable": [prm["unit_names"]["socket"]],
        "written": [],
        "units": units,
    }
    socket_name = prm["unit_names"]["socket"]
    if args.out:
        data["written"] = write_units(units, args.out)
        text = "Wrote " + " and ".join(data["written"]) + ". An administrator installs them (copy to /etc/systemd/system, " \
            f"then systemctl daemon-reload and systemctl enable --now {socket_name})."
        return Result(data, text)
    text = "\n".join(f"### {name}\n{body}" for name, body in units.items())
    return Result(data, text)


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "units",
        help="Show the service files that run the server.",
        description="Render the systemd system units (socket and service) that run this project's dashboard server "
        "as its locked-down web account mes-<project>-web. easy-ui only renders them; an administrator installs them.",
    )
    p.add_argument("--render-only", action="store_true", help="render the files (required: easy-ui never installs them)")
    p.add_argument("--out", metavar="DIR", help="write the files into DIR instead of printing them")
    p.add_argument("--exec", dest="exec_path", default=DEFAULT_EXEC, help=f"path of easy-ui on the server (default {DEFAULT_EXEC})")
    p.add_argument("--root", default=DEFAULT_ROOT, help=f"the folder holding every project on the server (default {DEFAULT_ROOT})")
    p.add_argument("--socket", help=f"the socket path (default {DEFAULT_SOCKET_DIR}/<project>-web.sock)")
    p.add_argument("--socket-owner", default="root", help="the account that may connect to the socket (default root)")
    p.add_argument("--memory-max", default=DEFAULT_MEMORY_MAX, help=f"memory limit (default {DEFAULT_MEMORY_MAX})")
    p.add_argument(
        "--origin",
        action="append",
        metavar="URL",
        help="the exact web address the dashboards are opened with, e.g. https://myserver.example.ts.net "
        "(repeatable; at least one --origin or --allow-host is needed). The server accepts only these addresses",
    )
    p.add_argument(
        "--allow-host",
        action="append",
        metavar="NAME",
        help="shorthand for --origin https://NAME (repeatable)",
    )
    p.set_defaults(func=_cmd_units)

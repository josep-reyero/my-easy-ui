"""The project model (contract sections 1-3): project.toml, --project-dir, the per-machine data_dir
override, the server layout under MES_ROOT, and serving from the ui folder alone (--ui-dir)."""

from __future__ import annotations

import json
import os
import shutil
import stat

import pytest
from conftest import run_cli, run_cli_json

from easyui import appconfig, auth, bundle, paths, publish, server
from easyui.errors import ConfigError, NotFound


def test_project_toml_keys(tmp_path):
    (tmp_path / "project.toml").write_text(
        '[project]\nname = "fitdata"\ntitle = "Fitness"\nversion = "0.1.0"\n\n'
        '[apps.fitdata]\ncli = "fitdata"\n\n[jobs.sync]\nrun = "fitdata sync"\nschedule = "*:0/10"\n\n'
        '[ui]\ndata = "fitdata.sqlite"\ntables = ["days"]\ndashboards = ["today", "sleep"]\nrefresh_after = ["sync"]\n\n'
        '[storage]\nbucket = "archive"\n'
    )
    cfg = appconfig.load(tmp_path)
    assert (cfg.name, cfg.title, cfg.version, cfg.dashboards, cfg.refresh_after) == (
        "fitdata", "Fitness", "0.1.0", ("today", "sleep"), ("sync",),
    )


@pytest.mark.parametrize(
    "text, words",
    [
        ('[app]\nname = "demo"\n', "Rename the [app] table"),
        ('[project]\nname = "demo"\nversion = "1 2"\n', "version"),
        ('[project]\nname = "demo"\n[ui]\nrefresh_after = ["Sync Job"]\n', "job name"),
        ('[project]\nname = "demo"\n[ui]\nrefresh_after = "sync"\n', "list of job names"),
    ],
)
def test_project_toml_errors(tmp_path, text, words):
    (tmp_path / "project.toml").write_text(text)
    with pytest.raises(ConfigError) as e:
        appconfig.load(tmp_path)
    assert words in e.value.message + (e.value.hint or "")


def test_old_app_toml_gets_a_plain_message(tmp_path, monkeypatch):
    (tmp_path / "app.toml").write_text('[app]\nname = "demo"\n')
    monkeypatch.chdir(tmp_path)
    code, data = run_cli_json("validate")
    assert code == 3 and data["error"]["code"] == "old_manifest"
    assert "Rename app.toml to project.toml" in data["error"]["hint"]
    code, data = run_cli_json("validate", "--project-dir", str(tmp_path))
    assert code == 3 and data["error"]["code"] == "old_manifest"


def test_project_dir_and_its_old_name(demo_app, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for opt in ("--project-dir", "--app-dir"):
        code, data = run_cli_json("data", "list", opt, str(demo_app.app_dir))
        assert code == 0 and data["app"] == "demo", opt
    code, data = run_cli_json(f"--project-dir={demo_app.app_dir}", "data", "list")
    assert code == 0
    code, data = run_cli_json("data", "list", "--project-dir")
    assert code == 2


def test_data_dir_override_from_the_machine_config(demo_app, tmp_path, monkeypatch):
    archive = tmp_path / "my archive"
    archive.mkdir()
    shutil.copy(demo_app.db_path, archive / "demo.sqlite")
    os.remove(demo_app.db_path)
    cfgfile = tmp_path / "config.toml"
    cfgfile.write_text(f'[projects.demo]\ndata_dir = "{archive}"\n\n[projects.other]\ndata_dir = "/elsewhere"\n')
    monkeypatch.setenv("MES_CONFIG", str(cfgfile))
    assert paths.app_data_dir("demo") == archive
    assert demo_app.config().source_db() == archive / "demo.sqlite"
    code, data = run_cli_json("data", "build")
    assert code == 0 and data["tables"]["days"]["rows"] == 120


@pytest.mark.parametrize(
    "text, words",
    [
        ('[projects.demo]\ndata_dir = "relative/path"\n', "not an absolute folder"),
        ("[projects.demo]\ndata_dir = 5\n", "folder name in quotes"),
        ("[projects.demo\n", "typing mistake"),
    ],
)
def test_bad_data_dir_override(demo_app, tmp_path, monkeypatch, text, words):
    f = tmp_path / "config.toml"
    f.write_text(text)
    monkeypatch.setenv("MES_CONFIG", str(f))
    with pytest.raises(ConfigError) as e:
        paths.app_data_dir("demo")
    assert words in e.value.message


def test_home_override_keeps_tilde(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    f = tmp_path / "config.toml"
    f.write_text('[projects.demo]\ndata_dir = "~/fit"\n')
    monkeypatch.setenv("MES_CONFIG", str(f))
    assert paths.app_data_dir("demo") == tmp_path / "fit"


def test_server_layout(tmp_path, monkeypatch):
    monkeypatch.setenv("MES_ROOT", str(tmp_path / "mes"))
    assert paths.app_data_dir("fitdata") == tmp_path / "mes/fitdata/data"
    assert paths.ui_dir("fitdata") == tmp_path / "mes/fitdata/ui"
    monkeypatch.setenv("MES_ROOT", "relative")
    with pytest.raises(ConfigError):
        paths.ui_dir("fitdata")


def test_shared_ui_folder_gets_group_readable_folders_whatever_the_umask(built_app, tmp_path, monkeypatch):
    """On a server the ui folder is 2750 for the web group; what data build and publish make stays readable."""
    root = tmp_path / "srv"
    ui = root / "demo" / "ui"
    ui.mkdir(parents=True)
    os.chmod(ui, 0o2750)
    monkeypatch.setenv("MES_ROOT", str(root))
    monkeypatch.setenv("MES_DATA_HOME", str(built_app.data_home))
    shutil.copytree(built_app.data_home / "demo", root / "demo" / "data")
    old = os.umask(0o077)
    try:
        cfg = appconfig.load(built_app.app_dir)
        assert cfg.state.root == ui
        bundle.build(cfg)
        publish.publish(cfg)
    finally:
        os.umask(old)
    for d in (ui / "data", ui / "specs", ui / "specs" / "versions"):
        assert stat.S_IMODE(d.stat().st_mode) & 0o050 == 0o050, d
    # Every file the web account serves is readable by its group, whatever the umask of the job.
    served = [p for p in list((ui / "data").rglob("*")) + list((ui / "specs").rglob("*"))
              if p.is_file() and not p.name.endswith(".lock")]  # the locks are only for the job's own writers
    assert served
    unreadable = [str(p.relative_to(ui)) for p in served if stat.S_IMODE(p.stat().st_mode) & 0o040 == 0]
    assert unreadable == [], unreadable
    for p in [d for d in (ui / "specs").rglob("*") if d.is_dir()]:
        assert stat.S_IMODE(p.stat().st_mode) & 0o050 == 0o050, p
    assert stat.S_IMODE((ui / "auth").stat().st_mode) == 0o700
    assert stat.S_IMODE((ui / "project.json").stat().st_mode) == 0o644
    snap = json.loads((ui / "project.json").read_text())
    assert snap == {"v": 1, "name": "demo", "title": "Demo app", "accent": "#2f6fdf",
                    "dashboards": ["overview", "activities"], "version": "0.1.0"}
    # The checklist accepts the shared layout (auth/ private, the ui folder closed to others).
    auth.set_password(ui / "auth", "a long synthetic password")
    report = server.run_checks(appconfig.load(ui_dir=ui))
    state_item = next(i for i in report["items"] if i["id"] == "state_private")
    assert state_item["status"] == "ok", state_item
    os.chmod(ui, 0o2755)
    report = server.run_checks(appconfig.load(ui_dir=ui))
    assert next(i for i in report["items"] if i["id"] == "state_private")["status"] == "fail"


def test_serving_from_the_ui_folder_alone(built_app, tmp_path, monkeypatch):
    """The web account cannot read the project's folder: --ui-dir with project.json is enough."""
    cfg = built_app.config()
    publish.publish(cfg)
    ui = cfg.state.root
    elsewhere = tmp_path / "nowhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    loaded = appconfig.load(ui_dir=ui)
    assert (loaded.name, loaded.title, loaded.accent, loaded.dashboards) == ("demo", "Demo app", "#2f6fdf", ("overview", "activities"))
    assert loaded.from_ui_dir and loaded.state.root == ui.resolve()
    with pytest.raises(ConfigError):
        loaded.source_db()  # never reaches for the project's data
    assert [d["slug"] for d in publish.list_published(loaded)] == ["activities", "overview"]
    code, data = run_cli_json("--ui-dir", str(ui), "versions")
    assert code == 0 and data["current"] == 1
    code, data = run_cli_json("check", "--ui-dir", str(ui))
    assert data["app"] == "demo"
    code, out, _ = run_cli("passkeys", "--ui-dir", str(ui))
    assert code == 0 and "Passkeys: none yet" in out


def test_ui_folder_without_project_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ui = tmp_path / "srv" / "fitdata" / "ui"
    ui.mkdir(parents=True)
    assert appconfig.load(ui_dir=ui).name == "fitdata"  # named after its project folder
    odd = tmp_path / "Not A Name"
    odd.mkdir()
    with pytest.raises(NotFound):
        appconfig.load(ui_dir=odd)
    (ui / "project.json").write_text("{broken")
    with pytest.raises(NotFound):
        appconfig.load(ui_dir=ui)
    (ui / "project.json").write_text(json.dumps({"name": "../../etc"}))
    with pytest.raises(NotFound):
        appconfig.load(ui_dir=ui)
    (ui / "project.json").write_text(json.dumps({"name": "fitdata", "title": "x" * 500, "accent": "red"}))
    assert appconfig.load(ui_dir=ui).title == "fitdata"  # a damaged snapshot falls back to the name only


def test_publish_and_build_write_the_snapshot(built_app):
    cfg = built_app.config()
    snap = json.loads(cfg.state.project_file.read_text())
    assert snap["name"] == "demo"
    (built_app.app_dir / "project.toml").write_text(
        (built_app.app_dir / "project.toml").read_text().replace('title = "Demo app"', 'title = "Renamed"')
    )
    publish.publish(built_app.config())
    assert json.loads(cfg.state.project_file.read_text())["title"] == "Renamed"

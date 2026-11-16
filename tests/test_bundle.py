"""Data bundles (build, current link, pruning, rev), project.toml reading and folders."""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import sqlite3
import stat

import pytest

from conftest import DEMO_APP_TOML, make_demo_app
from easyui import appconfig, bundle, paths
from easyui.errors import ConfigError, DataBuildError, NoData, NotFound, TemporaryFailure, UsageError


def change_data(app, steps: int) -> None:
    conn = sqlite3.connect(app.db_path)
    conn.execute("UPDATE days SET steps = ? WHERE day = '2026-01-01'", (steps,))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------- build


def test_build_copies_only_allowlisted_tables(demo_app):
    res = demo_app.build()
    assert res["unchanged"] is False and res["previous"] is None
    assert list(res["tables"]) == ["days", "activities", "weekly"]
    assert res["tables"]["days"]["rows"] == 120
    path = bundle.require_current(demo_app.config())
    conn = sqlite3.connect(path)
    objects = dict(conn.execute("SELECT name, type FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'").fetchall())
    conn.close()
    assert objects == {"days": "table", "activities": "table", "weekly": "table", "activities_day": "index"}
    assert b"not for dashboards" not in path.read_bytes()
    assert b"synthetic secret" not in path.read_bytes()


def test_views_are_materialised_with_types(built_app):
    path = bundle.require_current(built_app.config())
    (weekly,) = bundle.describe_tables(path, only=["weekly"])
    assert weekly["kind"] == "table"
    assert weekly["rows"] == 18
    assert [c["type"] for c in weekly["columns"]] == ["TEXT", "INTEGER", "REAL"]


def test_bundle_is_read_only_and_described(built_app):
    path = bundle.require_current(built_app.config())
    assert stat.S_IMODE(path.stat().st_mode) == 0o444
    info = bundle.bundle_info(path)
    assert info["bundle"] == path.stem and info["app"] == "demo" and info["source"] == "demo.sqlite"
    assert info["built_at"].endswith("Z")
    assert bundle.BUNDLE_RE.match(path.name)


def test_current_link_is_relative_and_atomic(built_app):
    st = built_app.config().state
    target = os.readlink(st.current)
    assert "/" not in target and bundle.BUNDLE_RE.match(target)
    assert not [p for p in st.data.iterdir() if p.name.startswith(".tmp-")]


def test_unchanged_data_keeps_the_current_bundle(built_app):
    cfg = built_app.config()
    first = bundle.require_current(cfg)
    rev = bundle.rev(cfg)
    res = bundle.build(cfg)
    assert res["unchanged"] is True and res["bundle"] == first.stem
    assert bundle.require_current(cfg) == first
    assert bundle.rev(cfg) == rev


def test_changed_data_switches_and_changes_rev(built_app):
    cfg = built_app.config()
    first = bundle.require_current(cfg)
    rev = bundle.rev(cfg)
    change_data(built_app, 1)
    res = bundle.build(cfg)
    assert res["unchanged"] is False and res["previous"] == first.stem
    assert bundle.require_current(cfg) != first
    assert first.exists()  # kept for requests still reading it
    assert bundle.rev(cfg) != rev


def test_prune_keeps_newest_and_current(built_app):
    cfg = built_app.config()
    t0 = dt.datetime(2099, 5, 1, tzinfo=dt.UTC)  # after the fixture's own build
    for i in range(5):
        change_data(built_app, 100 + i)
        bundle.build(cfg, keep=3, now=t0 + dt.timedelta(minutes=i))
    items = bundle.list_bundles(cfg)
    assert len(items) == 3
    assert items[0]["current"] is True
    assert sum(1 for b in items if b["current"]) == 1
    jsons = [p for p in cfg.state.data.iterdir() if p.suffix == ".json"]
    assert len(jsons) == 3


def test_prune_never_removes_current(built_app):
    cfg = built_app.config()
    current = bundle.require_current(cfg)
    # make the current bundle the oldest by adding newer-looking files
    for i in range(3):
        name = f"bundle-29991231T23595{i}000000Z-{i:012x}.sqlite"
        (cfg.state.data / name).write_bytes(b"")
    removed = bundle.prune(cfg, keep=1)
    assert current.exists()
    assert len(removed) == 2


def test_missing_table_is_reported_with_closest_match(demo_app):
    toml = DEMO_APP_TOML.format(name="demo").replace('"weekly"]', '"weekley", "privat_notes"]')
    (demo_app.app_dir / "project.toml").write_text(toml)
    with pytest.raises(NotFound) as e:
        demo_app.build()
    assert 'did you mean "weekly"' in e.value.message
    assert 'did you mean "private_notes"' in e.value.message
    assert "previous data stays in use" in e.value.message
    assert bundle.current_bundle(demo_app.config()) is None


def test_failed_build_leaves_current_and_no_temp_files(built_app, monkeypatch):
    cfg = built_app.config()
    before = bundle.require_current(cfg)
    change_data(built_app, 5)

    def boom(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(bundle, "_copy_table", boom)
    with pytest.raises(DataBuildError) as e:
        bundle.build(cfg)
    assert "previous data stays in use" in e.value.message
    assert bundle.require_current(cfg) == before
    assert not [p for p in cfg.state.data.iterdir() if p.name.startswith(".tmp-")]


def test_missing_source_file(demo_app):
    demo_app.db_path.unlink()
    with pytest.raises(NotFound) as e:
        demo_app.build()
    assert "does not exist yet" in e.value.message


def test_concurrent_build_is_temporary(demo_app):
    st = demo_app.config().state.ensure()
    with open(st.data / ".build.lock", "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        with pytest.raises(TemporaryFailure) as e:
            demo_app.build()
    assert e.value.exit_code == 75


def test_view_with_repeated_column_names_gets_distinct_names(demo_app):
    conn = sqlite3.connect(demo_app.db_path)
    conn.execute("DROP VIEW weekly")
    conn.execute("CREATE VIEW weekly AS SELECT day AS a, steps AS a FROM days")
    conn.commit()
    conn.close()
    res = demo_app.build()
    names = [c["name"] for c in res["tables"]["weekly"]["columns"]]
    assert len(set(names)) == 2


def test_source_in_wal_mode_is_read_without_changes(demo_app):
    conn = sqlite3.connect(demo_app.db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("INSERT INTO days (day, steps) VALUES ('2026-05-01', 1)")
    conn.commit()
    res = demo_app.build()  # while the app still holds a connection
    conn.close()
    assert res["tables"]["days"]["rows"] == 121


# ---------------------------------------------------------------- current link safety


@pytest.mark.parametrize("target", ["/etc/hosts", "../../../etc/hosts", "bundle-x.sqlite", "current"])
def test_current_link_never_follows_odd_targets(built_app, target):
    st = built_app.config().state
    st.current.unlink()
    os.symlink(target, st.current)
    assert bundle.current_bundle(st) is None
    with pytest.raises(NoData):
        bundle.require_current(st)
    assert bundle.data_rev(st) == "none"


def test_no_data_message(demo_app):
    with pytest.raises(NoData) as e:
        bundle.require_current(demo_app.config())
    assert "easy-ui data build" in e.value.hint
    assert e.value.exit_code == 3 and e.value.http_status == 503


def test_rev_changes_with_published_specs(built_app):
    cfg = built_app.config()
    r1 = bundle.rev(cfg)
    cfg.state.specs.mkdir(parents=True, exist_ok=True)
    (cfg.state.specs / "v1").mkdir()
    r2 = bundle.rev(cfg)
    (cfg.state.specs / "v1" / "overview.yaml").write_text("x")
    r3 = bundle.rev(cfg)
    os.symlink("v1", cfg.state.specs / "current")
    r4 = bundle.rev(cfg)
    assert len({r1, r2, r3, r4}) == 4
    assert len(r1) == 16
    assert bundle.rev(cfg) == r4


def test_describe_tables_samples(built_app):
    path = bundle.require_current(built_app.config())
    tables = {t["name"]: t for t in bundle.describe_tables(path, samples=2)}
    assert set(tables) == {"days", "activities", "weekly"}
    kind = next(c for c in tables["activities"]["columns"] if c["name"] == "kind")
    assert len(kind["samples"]) == 2 and all(isinstance(v, str) for v in kind["samples"])
    assert bundle.describe_tables(path, samples=0)[0]["columns"][0]["samples"] == []
    json.dumps(tables)


# ---------------------------------------------------------------- project.toml


def write_toml(tmp_path, text):
    (tmp_path / "project.toml").write_text(text)
    return tmp_path


def test_appconfig_reads_app_and_ui(demo_app):
    cfg = demo_app.config()
    assert (cfg.name, cfg.title, cfg.data, cfg.tables, cfg.accent) == (
        "demo", "Demo app", "demo.sqlite", ("days", "activities", "weekly"), "#2f6fdf",
    )
    assert cfg.public() == {"name": "demo", "title": "Demo app", "accent": "#2f6fdf"}
    assert cfg.source_db() == demo_app.db_path
    assert cfg.dashboards_dir == demo_app.app_dir / "ui" / "dashboards"
    assert cfg.state.root == demo_app.state_home / "demo"


def test_appconfig_found_from_a_subfolder(demo_app, monkeypatch):
    monkeypatch.chdir(demo_app.app_dir / "ui" / "dashboards")
    assert appconfig.load().app_dir == demo_app.app_dir.resolve()
    assert appconfig.load(demo_app.app_dir / "project.toml").name == "demo"


def test_appconfig_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(NotFound) as e:
        appconfig.load()
    assert "--project-dir" in e.value.hint
    with pytest.raises(NotFound):
        appconfig.load(tmp_path)


@pytest.mark.parametrize(
    "ui, words",
    [
        ('datta = "x.sqlite"', 'Did you mean "data"'),
        ('data = "../other/x.sqlite"', "not a plain path"),
        ('data = "/etc/passwd"', "not a plain path"),
        ('data = "~/x.sqlite"', "not a plain path"),
        ("data = 3", "file name in quotes"),
        ('tables = "days"', "list of table names"),
        ('tables = ["days; DROP"]', "cannot use"),
        ('tables = ["days", "days"]', "more than once"),
        ('accent = "red"', "colour"),
        ('accent = "#12345"', "colour"),
    ],
)
def test_appconfig_ui_errors(tmp_path, ui, words):
    write_toml(tmp_path, f'[project]\nname = "demo"\n\n[ui]\n{ui}\n')
    with pytest.raises(ConfigError) as e:
        appconfig.load(tmp_path)
    assert words in e.value.message + (e.value.hint or "")


@pytest.mark.parametrize(
    "text, words",
    [
        ("[ui]\n", "no [project]"),
        ('[project]\nname = "Bad Name"\n', "not valid"),
        ('[project]\nname = "demo"\ntitle = ""\n', "short piece of text"),
        ('[app\nname = "demo"', "typing mistake on line 1"),
    ],
)
def test_appconfig_app_errors(tmp_path, text, words):
    write_toml(tmp_path, text)
    with pytest.raises(ConfigError) as e:
        appconfig.load(tmp_path)
    assert words in e.value.message


def test_appconfig_ignores_other_tables_and_needs_ui_for_data(tmp_path):
    write_toml(tmp_path, '[project]\nname = "demo"\nversion = "1"\nfuture = 1\n\n[jobs.x]\nrun = "x"\n')
    cfg = appconfig.load(tmp_path)
    assert cfg.title == "demo" and cfg.data is None and cfg.tables == ()
    with pytest.raises(ConfigError) as e:
        cfg.source_db()
    assert "[ui]" in e.value.hint
    with pytest.raises(ConfigError):
        cfg.require_tables()


# ---------------------------------------------------------------- folders


def test_paths_env_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("MES_DATA_HOME", str(tmp_path / "d"))
    monkeypatch.setenv("EASYUI_STATE_HOME", str(tmp_path / "s"))
    assert paths.app_data_dir("demo") == tmp_path / "d" / "demo"
    sp = paths.state_paths("demo", create=True)
    assert (sp.data, sp.specs, sp.auth, sp.current) == (
        tmp_path / "s/demo/data", tmp_path / "s/demo/specs", tmp_path / "s/demo/auth", tmp_path / "s/demo/data/current",
    )
    assert stat.S_IMODE(sp.auth.stat().st_mode) == 0o700


def test_paths_defaults(monkeypatch, tmp_path):
    for k in ("MES_DATA_HOME", "EASYUI_STATE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "MES_STATE_HOME", "MES_CONFIG"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert paths.data_home() == tmp_path / ".local/share/mes"
    assert paths.state_home() == tmp_path / ".local/state/mes"
    assert paths.app_data_dir("demo") == tmp_path / ".local/share/mes/demo"
    assert paths.ui_dir("demo") == tmp_path / ".local/state/mes/demo/ui"
    assert paths.config_path() == tmp_path / ".config/mes/config.toml"


@pytest.mark.parametrize("name", ["../x", "a/b", "", "X", "a", "-a"])
def test_paths_refuse_bad_app_names(name):
    with pytest.raises(UsageError):
        paths.state_paths(name)


def test_make_demo_app_is_reusable(tmp_path):
    app = make_demo_app(tmp_path / "other", name="other-app")
    assert app.env()["MES_DATA_HOME"].endswith("data")
    assert (app.app_dir / "ui" / "dashboards" / "overview.yaml").is_file()
    assert app.db_path.name == "demo.sqlite"

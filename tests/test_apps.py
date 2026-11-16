"""Finding a project's apps (apps.lock.json or apps/<package>/), reading their app.toml, and reading inside an app
folder without ever leaving it (apps contract sections 2.5, 4.6 and 6.5). All data here is made up."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from easyui import appconfig, compose, project
from easyui.errors import ConfigError

PROJECT = """\
[project]
name = "fitlab"
title = "Fit lab"
apps = [{apps}]

[tables.activities]
key = ["id"]
columns = {{ id = "text", date = "text" }}

[views.weekly]
sql = "SELECT 1 AS one"
{extra}"""

WALKS = """\
[app]
name = "fitlab-walks"
title = "Walks"
cli = "fitlab_walks.cli:app"

[data]
writes = ["activities"]
tables = ["walks_log"]

[jobs.rebuild]
run = "rebuild"
manual = true

[ui]
dashboards = ["walks"]
tables = ["walks_log"]

[ui.contributes.overview]
sections = "dashboards/overview-walks.yaml"
"""

RIDES = """\
[app]
name = "fitlab-rides"
title = "Rides"
cli = "fitlab_rides.cli:app"

[data]
writes = ["activities"]
"""

DASH = """\
spec: easy-ui/v1
title: Walks
queries:
  q:
    sql: SELECT 1 AS a
layout:
  - - type: stat
      title: A
      query: q
      value: a
"""


def make(tmp: Path, apps: dict[str, str] | None = None, *, extra: str = "", installed: dict[str, str] | None = None,
         order: list[str] | None = None) -> Path:
    """A project folder with in-project apps (apps/<package>/) and, optionally, 'installed' ones elsewhere."""
    root = tmp / "fitlab"
    apps = {"fitlab_walks": WALKS} if apps is None else apps
    installed = installed or {}
    order = order or [*apps, *installed]
    root.mkdir(parents=True, exist_ok=True)
    (root / "project.toml").write_text(PROJECT.format(apps=", ".join(f'"{p}"' for p in order), extra=extra))
    for pkg, text in apps.items():
        d = root / "apps" / pkg
        (d / "dashboards").mkdir(parents=True, exist_ok=True)
        (d / "__init__.py").write_text("")
        (d / "app.toml").write_text(text)
        (d / "dashboards" / "walks.yaml").write_text(DASH)
        (d / "dashboards" / "overview-walks.yaml").write_text("spec: easy-ui/sections-v1\nsections: []\n")
    for pkg, text in installed.items():
        d = tmp / "site-packages" / pkg
        d.mkdir(parents=True, exist_ok=True)
        (d / "__init__.py").write_text("")
        (d / "app.toml").write_text(text)
    return root


def write_lock(root: Path, tmp: Path, *, packages: list[str] | None = None, **change) -> dict:
    cfg_bytes = (root / "project.toml").read_bytes()
    pkgs = packages or appconfig.load(root).packages
    entries = []
    for pkg in pkgs:
        in_project = (root / "apps" / pkg).is_dir()
        path = root / "apps" / pkg if in_project else tmp / "site-packages" / pkg
        name = json.loads(json.dumps(None))
        import tomllib

        name = tomllib.loads((path / "app.toml").read_text())["app"]["name"]
        entries.append({"package": pkg, "name": name, "path": str(path), "in_project": in_project, "program": name})
    lock = {"v": 1, "project": "fitlab", "project_toml_sha256": hashlib.sha256(cfg_bytes).hexdigest(), "apps": entries}
    lock.update(change)
    (root / project.LOCK_FILE).write_text(json.dumps(lock))
    return lock


# ---------------------------------------------------------------- finding the apps


def test_without_a_lock_in_project_apps_are_read(tmp_path):
    root = make(tmp_path, {"fitlab_walks": WALKS, "fitlab_rides": RIDES})
    cfg = appconfig.load(root)
    apps = cfg.apps
    assert [a.name for a in apps] == ["fitlab-walks", "fitlab-rides"]
    walks = apps[0]
    assert (walks.package, walks.short, walks.prefix, walks.in_project, walks.label) == (
        "fitlab_walks", "walks", "walks_", True, "apps/fitlab_walks")
    assert walks.dashboards == ("walks",) and walks.contributes == (("overview", "dashboards/overview-walks.yaml"),)
    assert walks.tables == ("walks_log",) and walks.ui_tables == ("walks_log",)
    assert apps[1].title == "Rides" and apps[1].dashboards == ()
    assert project.lock_state(cfg) == "files"
    # The allowlist: shared views (file order), then [views], then the apps' [ui] tables (apps order).
    assert cfg.tables == ("activities", "weekly", "walks_log")


def test_an_app_missing_from_the_apps_folder_needs_mes_apps_sync(tmp_path):
    root = make(tmp_path, {"fitlab_walks": WALKS}, order=["fitlab_walks", "fitlab_rides"])
    with pytest.raises(ConfigError) as e:
        appconfig.load(root).apps
    assert e.value.message == ("The app fitlab_rides is not inside this project's apps folder and the list of "
                               "installed apps has not been written yet.")
    assert e.value.hint == "Run: mes apps sync" and e.value.code == "app_not_found"


def test_a_linked_apps_folder_is_not_followed(tmp_path):
    root = make(tmp_path, {})
    elsewhere = tmp_path / "elsewhere" / "fitlab_walks"
    elsewhere.mkdir(parents=True)
    (elsewhere / "app.toml").write_text(WALKS)
    (root / "apps").symlink_to(tmp_path / "elsewhere")
    (root / "project.toml").write_text(PROJECT.format(apps='"fitlab_walks"', extra=""))
    with pytest.raises(ConfigError) as e:
        appconfig.load(root).apps
    assert e.value.code == "app_not_found"


def test_a_linked_app_folder_or_app_toml_is_not_followed(tmp_path):
    root = make(tmp_path, {"fitlab_rides": RIDES})
    other = tmp_path / "other"
    other.mkdir()
    (other / "app.toml").write_text(WALKS)
    (root / "apps" / "fitlab_walks").symlink_to(other)
    (root / "project.toml").write_text(PROJECT.format(apps='"fitlab_walks", "fitlab_rides"', extra=""))
    with pytest.raises(ConfigError):
        appconfig.load(root).apps
    os.unlink(root / "apps" / "fitlab_walks")
    (root / "apps" / "fitlab_walks").mkdir()
    (root / "apps" / "fitlab_walks" / "app.toml").symlink_to(other / "app.toml")
    with pytest.raises(ConfigError):
        appconfig.load(root).apps


def test_the_lock_finds_installed_apps(tmp_path):
    root = make(tmp_path, {"fitlab_walks": WALKS}, installed={"fitlab_rides": RIDES})
    write_lock(root, tmp_path)
    cfg = appconfig.load(root)
    assert project.lock_state(cfg) == "lock"
    walks, rides = cfg.apps
    assert walks.in_project and not rides.in_project
    assert rides.folder == tmp_path / "site-packages" / "fitlab_rides"
    assert rides.label == "fitlab_rides (installed)" and rides.file_label("dashboards/x.yaml") == \
        "fitlab_rides (installed)/dashboards/x.yaml"


STALE = [
    ({"v": 2}, '"v": 1'),
    ({"project": "other"}, "for the project 'other'"),
    ({"project_toml_sha256": "0" * 64}, "project.toml changed"),
    ({"apps": "nope"}, 'no "apps" list'),
]


@pytest.mark.parametrize("change, detail", STALE)
def test_a_stale_lock_is_refused(tmp_path, change, detail):
    root = make(tmp_path, {"fitlab_walks": WALKS}, installed={"fitlab_rides": RIDES})
    write_lock(root, tmp_path, **change)
    with pytest.raises(ConfigError) as e:
        appconfig.load(root).apps
    if "project.toml changed" in detail:
        assert e.value.message.startswith("project.toml changed after the list of apps")
    else:
        assert e.value.message == project.STALE_LOCK and e.value.hint == "Run: mes apps sync"
    assert detail in e.value.detail and e.value.code == "apps_lock_stale"


def test_a_lock_made_stale_by_editing_project_toml_is_ignored_when_every_app_is_in_apps(tmp_path):
    root = make(tmp_path, {"fitlab_walks": WALKS, "fitlab_rides": RIDES})
    write_lock(root, tmp_path)
    assert [a.name for a in appconfig.load(root).apps] == ["fitlab-walks", "fitlab-rides"]
    (root / "project.toml").write_text((root / "project.toml").read_text() + "# touch\n")
    assert [a.name for a in appconfig.load(root).apps] == ["fitlab-walks", "fitlab-rides"]
    (root / project.LOCK_FILE).write_text("{not json")  # a damaged lock is still refused
    with pytest.raises(ConfigError) as e:
        appconfig.load(root).apps
    assert e.value.code == "apps_lock_stale"


@pytest.mark.parametrize("name", ["fitlab-sqlite", "fitlab-mes", "fitlab-sqlite-x", "mes-tools"])
def test_an_app_may_not_get_a_table_prefix_of_the_database_itself(tmp_path, name):
    """Malformed input: such an app could list sqlite_master as one of its own tables."""
    text = RIDES.replace('name = "fitlab-rides"', f'name = "{name}"')
    root = make(tmp_path, {"fitlab_rides": text})
    with pytest.raises(ConfigError) as e:
        appconfig.load(root).apps
    assert "keeps for itself" in e.value.message or "sqlite_ or mes_" in e.value.message


def test_the_lock_must_list_the_packages_in_order(tmp_path):
    root = make(tmp_path, {"fitlab_walks": WALKS}, installed={"fitlab_rides": RIDES})
    write_lock(root, tmp_path, packages=["fitlab_rides", "fitlab_walks"])
    with pytest.raises(ConfigError) as e:
        appconfig.load(root).apps
    assert "in that order" in e.value.detail


@pytest.mark.parametrize("bad", [
    "relative/path", "/abs/../path", "/abs//path", "/abs/path/", "/with\nnewline", "",
])
def test_lock_paths_must_be_absolute_and_normalised(tmp_path, bad):
    root = make(tmp_path, {"fitlab_walks": WALKS}, installed={"fitlab_rides": RIDES})
    lock = write_lock(root, tmp_path)
    lock["apps"][1]["path"] = bad
    (root / project.LOCK_FILE).write_text(json.dumps(lock))
    with pytest.raises(ConfigError) as e:
        appconfig.load(root).apps
    assert e.value.code == "apps_lock_stale"


def test_the_lock_is_checked_against_each_app_toml(tmp_path):
    root = make(tmp_path, {"fitlab_walks": WALKS}, installed={"fitlab_rides": RIDES})
    lock = write_lock(root, tmp_path)
    lock["apps"][1]["name"] = "fitlab-cycling"
    (root / project.LOCK_FILE).write_text(json.dumps(lock))
    with pytest.raises(ConfigError) as e:
        appconfig.load(root).apps
    assert "apps.lock.json says 'fitlab-cycling'" in e.value.detail
    # An in-project app must be exactly at apps/<package>.
    lock = write_lock(root, tmp_path)
    lock["apps"][0]["path"] = str(tmp_path / "site-packages" / "fitlab_rides")
    (root / project.LOCK_FILE).write_text(json.dumps(lock))
    with pytest.raises(ConfigError) as e:
        appconfig.load(root).apps
    assert "is not at apps/fitlab_walks" in e.value.detail


def test_a_linked_installed_folder_and_a_broken_lock_are_refused(tmp_path):
    root = make(tmp_path, {"fitlab_walks": WALKS}, installed={"fitlab_rides": RIDES})
    real = tmp_path / "site-packages" / "fitlab_rides"
    moved = tmp_path / "real_rides"
    real.rename(moved)
    real.symlink_to(moved)
    write_lock(root, tmp_path)
    with pytest.raises(ConfigError) as e:
        appconfig.load(root).apps
    assert e.value.code == "apps_lock_stale" and "not a real folder" in e.value.detail
    (root / project.LOCK_FILE).write_text("{not json")
    with pytest.raises(ConfigError) as e:
        appconfig.load(root).apps
    assert "not valid JSON" in e.value.detail
    (root / project.LOCK_FILE).write_text(" " * (project.MAX_LOCK_BYTES + 10))
    with pytest.raises(ConfigError) as e:
        appconfig.load(root).apps
    assert "larger than 64 KB" in e.value.detail
    (root / project.LOCK_FILE).unlink()
    (tmp_path / "lock.json").write_text("{}")
    (root / project.LOCK_FILE).symlink_to(tmp_path / "lock.json")
    with pytest.raises(ConfigError) as e:
        appconfig.load(root).apps
    assert "not a plain file" in e.value.detail


# ---------------------------------------------------------------- app.toml


@pytest.mark.parametrize("text, words", [
    (WALKS + "\n[tests]\nx = 1\n", 'setting "tests" at the top'),
    (WALKS.replace("[ui]\n", "[ui]\ndashbords = []\n"), 'Did you mean "dashboards"'),
    (WALKS.replace('tables = ["walks_log"]\n\n[jobs', 'tables = ["log"]\n\n[jobs'), "must start with walks_"),
    (WALKS.replace('[ui]\ndashboards = ["walks"]\ntables = ["walks_log"]', '[ui]\ndashboards = ["walks"]\ntables = ["walks_other"]'),
     "not one of the app's own tables"),
    (WALKS.replace('sections = "dashboards/overview-walks.yaml"', 'sections = "../../etc/passwd"'),
     "must be a file in the app's dashboards/ folder"),
    (WALKS.replace('sections = "dashboards/overview-walks.yaml"', 'sections = "dashboards/walks.yaml"'),
     "also one of the app's own dashboards"),
    (WALKS.replace('[ui.contributes.overview]\nsections', '[ui.contributes.overview]\nsection'), 'Did you mean "sections"'),
    (WALKS.replace('name = "fitlab-walks"', 'name = "fitlab"'), "like the project itself"),
    (WALKS.replace('name = "fitlab-walks"', 'name = "Fitlab Walks"'), "missing or not valid"),
    (WALKS.replace('name = "fitlab-walks"', 'name = "fitlab-help"'), "keeps for itself"),
    (WALKS.replace('title = "Walks"', 'title = "two\\nlines"'), "one line of text"),
    (WALKS.replace("[app]\n", "[app\n"), "apps/fitlab_walks/app.toml has a typing mistake on line 1"),
    ('[data]\ntables = []\n', "has no [app] table"),
])
def test_app_toml_mistakes_name_the_file(tmp_path, text, words):
    root = make(tmp_path, {"fitlab_walks": text})
    with pytest.raises(ConfigError) as e:
        appconfig.load(root).apps
    assert words in e.value.message + (e.value.hint or "")


def test_apps_may_not_share_a_word_or_a_table_prefix(tmp_path):
    clash = RIDES.replace('name = "fitlab-rides"', 'name = "walks"')  # short name "walks", like fitlab-walks
    root = make(tmp_path, {"fitlab_walks": WALKS, "fitlab_rides": clash})
    with pytest.raises(ConfigError) as e:
        appconfig.load(root).apps
    assert "both use the word 'walks'" in e.value.message
    nested = RIDES.replace('name = "fitlab-rides"', 'name = "fitlab-walks-x"')  # walks_x_ starts like walks_
    root = make(tmp_path / "b", {"fitlab_walks": WALKS, "fitlab_rides": nested})
    with pytest.raises(ConfigError) as e:
        appconfig.load(root).apps
    assert "start like those of fitlab-walks" in e.value.message


def test_an_oversized_app_toml_is_not_read(tmp_path):
    root = make(tmp_path, {"fitlab_walks": WALKS + "#" * (project.MAX_APP_TOML_BYTES + 10) + "\n"})
    with pytest.raises(ConfigError) as e:
        appconfig.load(root).apps
    assert "more than the 64,000 bytes" in e.value.message


def test_project_ui_tables_narrow_the_allowlist(tmp_path):
    root = make(tmp_path, extra='\n[ui]\ntables = ["walks_log", "activities"]\n')
    assert appconfig.load(root).tables == ("walks_log", "activities")
    root = make(tmp_path / "b", extra='\n[ui]\ntables = ["walks_logs"]\n')
    with pytest.raises(ConfigError) as e:
        appconfig.load(root).tables
    assert 'Did you mean "walks_log"' in e.value.message and e.value.code == "config_table_not_allowed"


# ---------------------------------------------------------------- reading inside an app folder


def test_reads_never_leave_the_app_folder(tmp_path):
    root = make(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("synthetic secret outside the app")
    d = root / "apps" / "fitlab_walks"
    (d / "dashboards" / "link.yaml").symlink_to(secret)
    (d / "sub").symlink_to(tmp_path)
    (d / "big.yaml").write_text("x" * 5000)
    app = appconfig.load(root).apps[0]
    with app.open() as folder:
        for rel in ("../secret.txt", "/etc/hosts", "dashboards/../../secret.txt", "dashboards/link.yaml",
                    "sub/secret.txt", ".hidden", "dashboards//walks.yaml", "dashboards\\walks.yaml"):
            got = folder.read(rel, 1000, "a file")
            assert isinstance(got, dict), rel
            assert "synthetic secret" not in json.dumps(got)
        assert isinstance(folder.read("big.yaml", 1000, "a dashboard file"), dict)
        assert folder.read("dashboards/walks.yaml", 10_000, "a dashboard file") == DASH.encode()
        assert not folder.exists("sub/secret.txt") and folder.exists("dashboards/walks.yaml")
        assert folder.open_dir("sub") is None


def test_a_linked_dashboard_of_an_app_is_a_problem_not_a_read(tmp_path):
    root = make(tmp_path)
    secret = tmp_path / "secret.yaml"
    secret.write_text(DASH.replace("Walks", "Synthetic secret"))
    f = root / "apps" / "fitlab_walks" / "dashboards" / "walks.yaml"
    f.unlink()
    f.symlink_to(secret)
    comp = compose.compose(appconfig.load(root))
    assert comp.get("walks") is None
    assert any("is a link" in p["problem"] and p["file"] == "apps/fitlab_walks/dashboards/walks.yaml"
               for p in comp.problems)
    assert "Synthetic secret" not in json.dumps(comp.to_dict())

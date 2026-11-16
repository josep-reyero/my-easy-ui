"""Dashboards from a project and its apps (apps contract section 6): discovery, sections, anchors, overrides,
composition, problems that name the file each part came from, and the fitlab example's steps 5 to 7.

``tests/fitlab/`` holds the contract's example files verbatim (section 9): ``fitlab-walks`` lives in the project,
``fitlab-rides`` in a folder of its own that apps.lock.json points at (as an installed package would). All data is
made up.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import time
import tomllib
from pathlib import Path

import pytest
from conftest import run_cli, run_cli_json

from easyui import appconfig, bundle, compose, demo, preview, publish, spec
from easyui.errors import ConfigError

FITLAB = Path(__file__).resolve().parent / "fitlab"


@pytest.fixture
def fitlab(tmp_path, monkeypatch):
    root = tmp_path / "fitlab"
    shutil.copytree(FITLAB, root, ignore=shutil.ignore_patterns("installed", "schema.sql"))
    site = tmp_path / "site-packages"
    shutil.copytree(FITLAB / "installed", site)
    monkeypatch.setenv("MES_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("EASYUI_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.chdir(root)
    lock(root, site)
    make_db(root, tmp_path / "data" / "fitlab" / "fitlab.sqlite")
    return root


def site(root: Path) -> Path:
    """Where the 'installed' rides app lives (as site-packages would hold it)."""
    return root.parent / "site-packages"


def lock(root: Path, site: Path) -> None:
    """apps.lock.json as mes apps sync writes it (contract 2.5)."""
    entries = [
        {"package": "fitlab_walks", "name": "fitlab-walks", "path": str(root / "apps" / "fitlab_walks"),
         "in_project": True, "program": "fitlab-walks"},
        {"package": "fitlab_rides", "name": "fitlab-rides", "path": str(site / "fitlab_rides"),
         "in_project": False, "program": "fitlab-rides"},
    ]
    sha = hashlib.sha256((root / "project.toml").read_bytes()).hexdigest()
    (root / "apps.lock.json").write_text(json.dumps({"v": 1, "project": "fitlab", "project_toml_sha256": sha,
                                                     "apps": entries}))


def make_db(root: Path, path: Path) -> None:
    """The shared database after the contract's step 2 and 3 (made-up walks and rides)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(demo.schema_sql(tomllib.loads((root / "project.toml").read_text()),
                                       ["fitlab-walks", "fitlab-rides"]))
    conn.execute("INSERT INTO activities__rows VALUES ('fitlab-walks', 'walk-1', '2026-01-05', 'walking', 4200, 3000, NULL)")
    conn.execute("INSERT INTO activities__rows VALUES ('fitlab-rides', 'ride-1', '2026-01-05', 'cycling', 21500, 3600, 240)")
    conn.execute("INSERT INTO days__rows (source, date, walk_km, weather) VALUES ('fitlab-walks', '2026-01-05', 4.2, 'sunny')")
    conn.execute("INSERT INTO days__rows (source, date, ride_km, weather) VALUES ('fitlab-rides', '2026-01-05', 21.5, 'cloudy')")
    conn.execute("CREATE TABLE walks_log (line INTEGER PRIMARY KEY, date TEXT, km REAL, minutes REAL, weather TEXT)")
    conn.execute("INSERT INTO walks_log VALUES (1, '2026-01-05', 4.2, 50, 'sunny')")
    conn.execute("CREATE TABLE garmin_private (x TEXT)")
    conn.execute("INSERT INTO garmin_private VALUES ('synthetic secret')")
    conn.commit()
    conn.close()


def cfg_of(root: Path):
    return appconfig.load(root)


# ---------------------------------------------------------------- the contract's example


def test_the_schema_script_is_the_contracts_exact_text():
    """demo.schema_sql (test data only; mes owns the real one) prints section 9.6 for fitlab, byte for byte."""
    text = demo.schema_sql(tomllib.loads((FITLAB / "project.toml").read_text()), ["fitlab-walks", "fitlab-rides"])
    assert text == (FITLAB / "schema.sql").read_text()


def test_the_merge_view_takes_each_column_from_the_preferred_app(fitlab):
    conn = sqlite3.connect(appconfig.load(fitlab).source_db())
    assert conn.execute("SELECT source, date, walk_km, ride_km, weather FROM days").fetchall() == [
        ("fitlab-rides", "2026-01-05", 4.2, 21.5, "cloudy")]
    assert conn.execute("SELECT count(*) FROM activities").fetchone() == (2,)
    assert conn.execute("SELECT count(*) FROM weekly").fetchone() == (2,)


def test_step_5_compose(fitlab):
    code, data = run_cli_json("compose")
    assert code == 0, data
    assert data["order"] == ["overview", "walks"]
    by = {d["slug"]: d for d in data["dashboards"]}
    assert by["overview"]["from"] == "project" and by["walks"]["from"] == "fitlab-walks"
    assert by["overview"]["sections"] == [
        {"app": "fitlab-walks", "id": "walks", "file": "apps/fitlab_walks/dashboards/overview-walks.yaml",
         "placed": "after:activity"},
        {"app": "fitlab-rides", "id": "rides", "file": "fitlab_rides (installed)/dashboards/overview-rides.yaml",
         "placed": "end"},
    ]
    assert data["warnings"] == []
    # Composed overview: the controls and stats row, the walks section's row (at the anchor), the recent-activities
    # table, the rides section's row (at the end).
    d = compose.compose(cfg_of(fitlab)).get("overview").dashboard
    assert [[c["title"] or c["type"] for c in row] for row in d.layout] == [
        ["controls", "Activities", "Distance (km)"], ["Walking per week"], ["Recent activities"],
        ["Riding per week", "Days"]]
    assert [c["app"] for row in d.layout for c in row] == [None, None, None, "fitlab-walks", None, "fitlab-rides",
                                                           "fitlab-rides"]
    assert list(d.queries) == ["totals", "recent", "walks_per_week", "rides_per_week", "rides_days"]
    walks = compose.compose(cfg_of(fitlab)).get("walks")
    assert walks.dashboard.app == "fitlab-walks" and spec.compile_dashboard(walks.dashboard)["app"] == "fitlab-walks"


def test_step_6_data_build_validate_and_publish(fitlab):
    code, data = run_cli_json("data", "build")
    assert code == 0 and list(data["tables"]) == ["activities", "days", "weekly", "walks_log"]
    path = bundle.require_current(cfg_of(fitlab))
    raw = path.read_bytes()
    assert b"synthetic secret" not in raw and b"days__rows" not in raw and b"activities__rows" not in raw
    code, data = run_cli_json("validate")
    assert code == 0 and data["ok"], data
    assert data["order"] == ["overview", "walks"]
    code, data = run_cli_json("publish")
    assert code == 0 and data["published"], data
    m = json.loads((cfg_of(fitlab).state.specs / "versions" / "1" / "manifest.json").read_text())
    by = {d["slug"]: d for d in m["dashboards"]}
    assert by["walks"]["from"] == "fitlab-walks"
    assert [s["file"] for s in by["walks"]["sources"]] == ["apps/fitlab_walks/dashboards/walks.yaml"]
    assert [(s["from"], s["file"]) for s in by["overview"]["sources"]] == [
        ("project", "ui/dashboards/overview.yaml"),
        ("fitlab-walks", "apps/fitlab_walks/dashboards/overview-walks.yaml"),
        ("fitlab-rides", "fitlab_rides (installed)/dashboards/overview-rides.yaml")]
    # The server reads the composed files, unchanged: the rides' query runs on the published dashboard.
    dash = publish.load_published_dashboard(cfg_of(fitlab), "overview")
    res = spec.run_queries(dash, ["rides_days"], {"days": 30}, bundle_path=path)
    assert res["results"]["rides_days"]["rows"] == [["2026-01-05", 4.2, 21.5, "cloudy", "fitlab-rides"]]


def test_step_7_overrides(fitlab):
    sections = fitlab / "ui" / "sections"
    sections.mkdir(parents=True)
    (sections / "fitlab-rides.overview.yaml").write_text("spec: easy-ui/sections-v1\nsections: []\n")
    (fitlab / "ui" / "dashboards" / "walks.yaml").write_text(
        (FITLAB / "apps/fitlab_walks/dashboards/walks.yaml").read_text().replace("title: Walks", "title: My walks"))
    comp = compose.compose(cfg_of(fitlab))
    assert comp.ok, comp.errors()
    over = comp.get("overview")
    assert [s["id"] for s in over.sections] == ["walks"]  # the rides section is gone
    walks = comp.get("walks")
    assert (walks.origin, walks.replaces, walks.title) == ("project", "fitlab-walks", "My walks")
    assert walks.dashboard.app is None  # the project's own file: the app's stylesheets no longer apply
    assert comp.order == ["overview", "walks"]  # it keeps the place where the app put it
    assert any("ui/dashboards/walks.yaml replaces the dashboard walks of fitlab-walks" in n for n in comp.notes)
    code, data = run_cli_json("dashboards")
    assert code == 0
    assert {"file": "ui/dashboards/walks.yaml", "replaces": "apps/fitlab_walks/dashboards/walks.yaml",
            "app": "fitlab-walks", "dashboard": "walks"} in data["overrides"]
    assert {"file": "ui/sections/fitlab-rides.overview.yaml",
            "replaces": "fitlab_rides (installed)/dashboards/overview-rides.yaml", "app": "fitlab-rides",
            "dashboard": "overview", "removes": True} in data["overrides"]
    code, out, _err = run_cli("dashboards")
    assert "walks: from the project (ui/dashboards/walks.yaml)" in out
    assert "replaces the app fitlab-walks's dashboard (apps/fitlab_walks/dashboards/walks.yaml)" in out
    assert "ui/sections/fitlab-rides.overview.yaml removes the sections of" in out
    # A project file that replaces a section with its own version keeps the app's slot and prefix rule.
    (sections / "fitlab-rides.overview.yaml").write_text(
        (FITLAB / "installed/fitlab_rides/dashboards/overview-rides.yaml").read_text().replace(
            "title: Riding per week", "title: Rides each week"))
    over = compose.compose(cfg_of(fitlab)).get("overview")
    assert over.sections[1] == {"app": "fitlab-rides", "id": "rides", "file": "ui/sections/fitlab-rides.overview.yaml",
                                "placed": "end", "from": "project"}
    assert over.dashboard.layout[-1][0]["title"] == "Rides each week" and over.dashboard.layout[-1][0]["app"] == \
        "fitlab-rides"


# ---------------------------------------------------------------- problems point at their own file


def test_a_mistake_in_a_section_names_the_section_file_and_line(fitlab):
    f = fitlab / "apps/fitlab_walks/dashboards/overview-walks.yaml"
    f.write_text(f.read_text().replace("y: Walked km", "y: Walked kms"))
    run_cli_json("data", "build")
    code, data = run_cli_json("validate")
    assert code == 1
    probs = [p for d in data["dashboards"] for p in d["errors"]]
    assert len(probs) == 1
    p = probs[0]
    assert p["file"] == "apps/fitlab_walks/dashboards/overview-walks.yaml"
    assert p["where"] == "sections[0].layout[0][0].y" and p["did_you_mean"] == "Walked km"
    assert p["place"] == 'section "walks", row 1, first component (bar), setting y'
    assert p["line"] == f.read_text().splitlines().index("          y: Walked kms") + 1
    # The same mistake stops publish, naming the app's file; the dashboards people see stay as they are.
    code, data = run_cli_json("publish")
    assert code == 1 and "apps/fitlab_walks/dashboards/overview-walks.yaml" in data["error"]["message"]


def test_a_mistake_in_a_sections_query_points_at_its_sql_line(fitlab):
    f = site(fitlab) / "fitlab_rides" / "dashboards" / "overview-rides.yaml"
    f.write_text(f.read_text().replace("FROM days\n", "FROM dayz\n"))
    run_cli_json("data", "build")
    comp = compose.compose(cfg_of(fitlab), bundle_path=bundle.current_bundle(cfg_of(fitlab)))
    (p,) = comp.get("overview").errors
    assert p["file"] == "fitlab_rides (installed)/dashboards/overview-rides.yaml"
    assert p["where"] == "sections[0].queries.rides_days.sql"
    assert p["line"] == f.read_text().splitlines().index("          FROM dayz") + 1
    assert "days" in json.dumps(p)  # did you mean


def test_the_base_dashboards_rows_keep_their_own_lines(fitlab):
    f = fitlab / "ui/dashboards/overview.yaml"
    f.write_text(f.read_text().replace("title: Recent activities", "title: Recent activities\n      limt: 5"))
    (p,) = compose.compose(cfg_of(fitlab)).get("overview").errors
    assert p["file"] == "ui/dashboards/overview.yaml" and p["where"] == "layout[2][0].limt"
    assert p["line"] == f.read_text().splitlines().index("      limt: 5") + 1
    assert p["did_you_mean"] == "limit"


# ---------------------------------------------------------------- the rules of sections


def section_file(fitlab: Path, body: str) -> Path:
    f = fitlab / "apps/fitlab_walks/dashboards/overview-walks.yaml"
    f.write_text("spec: easy-ui/sections-v1\nsections:\n" + body)
    return f


ROW = """    layout:
      - - type: text
          body: Hello
"""


@pytest.mark.parametrize("body, words", [
    ("  - id: walks\n    position: after:activity\n    params: {x: {type: text, default: a}}\n" + ROW,
     "cannot declare params"),
    ("  - id: walks\n    position: middle\n" + ROW, "position 'middle' is not one easy-ui knows"),
    ("  - id: Walks\n    position: end\n" + ROW, "section id 'Walks' cannot be used"),
    ("  - id: walks\n    position: end\n    queries:\n      weekly:\n        sql: SELECT 1 AS a\n" + ROW,
     "does not start with walks_"),
    ("  - id: walks\n    position: end\n    queries:\n      totals:\n        sql: SELECT 1 AS a\n" + ROW,
     "does not start with walks_"),
    ("  - id: walks\n    position: end\n    layout:\n      - anchor: x\n", "Anchors belong in dashboards"),
    ("  - id: walks\n    position: end\n    layout: []\n", "layout must be a list of rows"),
    ("  - id: walks\n    position: end\n" + ROW + "  - id: walks\n    position: start\n" + ROW,
     'Two sections in this file are called "walks"'),
    ("  - id: walks\n    position: end\n    layout:\n      - - type: text\n          body: Hi\n          app: fitlab-rides\n",
     "says it comes from the app 'fitlab-rides'"),
])
def test_section_rules(fitlab, body, words):
    section_file(fitlab, body)
    errs = compose.compose(cfg_of(fitlab)).get("overview").errors
    assert any(words in e["problem"] + e.get("hint", "") for e in errs), errs
    assert all(e["file"] == "apps/fitlab_walks/dashboards/overview-walks.yaml" for e in errs)


def test_a_sections_query_may_not_take_a_name_the_dashboard_has(fitlab):
    f = fitlab / "ui/dashboards/overview.yaml"
    f.write_text(f.read_text().replace("  recent:\n", "  walks_per_week:\n    sql: SELECT 1 AS x\n  recent:\n"))
    errs = compose.compose(cfg_of(fitlab)).get("overview").errors
    assert any('already has a query called "walks_per_week"' in e["problem"]
               and e["file"] == "apps/fitlab_walks/dashboards/overview-walks.yaml" for e in errs)


def test_an_unknown_anchor_is_a_warning_and_the_section_goes_to_the_end(fitlab):
    f = fitlab / "apps/fitlab_walks/dashboards/overview-walks.yaml"
    f.write_text(f.read_text().replace("after:activity", "after:nowhere"))
    over = compose.compose(cfg_of(fitlab)).get("overview")
    assert over.ok and [s["placed"] for s in over.sections] == ["end", "end"]
    assert [row[0]["title"] for row in over.dashboard.layout][-2:] == ["Walking per week", "Riding per week"]
    (w,) = over.warnings
    assert w["level"] == "warning" and 'no such anchor, so it was put at the end' in w["problem"]
    assert "activity" in w["hint"] and w["file"].endswith("overview-walks.yaml")


def test_start_sections_keep_the_apps_order(fitlab):
    for f in (fitlab / "apps/fitlab_walks/dashboards/overview-walks.yaml",
              site(fitlab) / "fitlab_rides/dashboards/overview-rides.yaml"):
        f.write_text(f.read_text().replace("position: after:activity", "position: start")
                     .replace("position: end", "position: start"))
    d = compose.compose(cfg_of(fitlab)).get("overview").dashboard
    assert [row[0]["title"] or row[0]["type"] for row in d.layout] == [
        "Walking per week", "Riding per week", "controls", "Recent activities"]


def test_section_ids_are_unique_across_apps(fitlab):
    f = site(fitlab) / "fitlab_rides/dashboards/overview-rides.yaml"
    f.write_text(f.read_text().replace("id: rides", "id: walks"))
    errs = compose.compose(cfg_of(fitlab)).get("overview").errors
    assert any('already has a section called "walks"' in e["problem"] and "overview-rides" in e["file"] for e in errs)


def test_a_contribution_to_a_missing_dashboard_is_a_warning(fitlab):
    (fitlab / "ui/dashboards/overview.yaml").unlink()
    run_cli_json("data", "build")
    comp = compose.compose(cfg_of(fitlab))
    assert [d.slug for d in comp.dashboards] == ["walks"]
    warns = [w["problem"] for w in comp.warnings]
    assert sum('adds sections to the dashboard "overview", but this project has no dashboard' in w for w in warns) == 2
    # [ui] dashboards still names overview: that one is a mistake (contract 1.5).
    assert any('[ui] dashboards lists "overview"' in p["problem"] for p in comp.problems)


def test_an_override_that_overrides_nothing_is_a_warning(fitlab):
    sections = fitlab / "ui" / "sections"
    sections.mkdir(parents=True)
    for name in ("fitlab-walks.walks", "nobody.overview", "fitlab-walks.nowhere"):
        (sections / f"{name}.yaml").write_text("spec: easy-ui/sections-v1\nsections: []\n")
    (sections / "badname.yaml").write_text("x")
    comp = compose.compose(cfg_of(fitlab))
    warns = {w["file"]: w["problem"] for w in comp.warnings}
    assert "adds no sections" in warns["ui/sections/fitlab-walks.walks.yaml"]
    assert "no app called nobody" in warns["ui/sections/nobody.overview.yaml"]
    assert "adds no sections" in warns["ui/sections/fitlab-walks.nowhere.yaml"]
    assert any("does not say which app and dashboard" in p["problem"] for p in comp.problems)


def test_two_apps_with_the_same_dashboard(fitlab):
    rides = site(fitlab) / "fitlab_rides"
    toml = rides / "app.toml"
    toml.write_text(toml.read_text().replace("[ui.contributes.overview]", '[ui]\ndashboards = ["walks"]\n\n'
                                             "[ui.contributes.overview]"))
    shutil.copy(fitlab / "apps/fitlab_walks/dashboards/walks.yaml", rides / "dashboards" / "walks.yaml")
    lock(fitlab, site(fitlab))
    comp = compose.compose(cfg_of(fitlab))
    (p,) = comp.problems
    assert p["problem"] == "The apps fitlab-walks and fitlab-rides both have a dashboard called walks."
    assert p["hint"] == "Add ui/dashboards/walks.yaml to the project to decide what it shows."
    shutil.copy(fitlab / "apps/fitlab_walks/dashboards/walks.yaml", fitlab / "ui/dashboards/walks.yaml")
    comp = compose.compose(cfg_of(fitlab))
    assert comp.ok and comp.get("walks").origin == "project" and comp.get("walks").replaces == "fitlab-walks"


def test_anchors(fitlab):
    f = fitlab / "ui/dashboards/overview.yaml"
    text = f.read_text()
    f.write_text(text.replace("  - anchor: activity\n", "  - anchor: activity\n  - anchor: activity\n"))
    errs = compose.compose(cfg_of(fitlab)).get("overview").errors
    assert any('two anchors called "activity"' in e["problem"] and e["file"] == "ui/dashboards/overview.yaml"
               for e in errs)
    f.write_text(text.replace("  - anchor: activity\n", "  - anchor: activity\n    title: x\n"))
    errs = compose.compose(cfg_of(fitlab)).get("overview").errors
    assert any("holds only anchor" in e["problem"] for e in errs)
    # Loading a dashboard file directly ignores its anchors.
    d = spec.loads(text, slug="overview")
    assert len(d.layout) == 2 and d.layout[1][0]["id"] == "r1c0"


def test_an_app_dashboard_may_not_claim_another_app(fitlab):
    f = fitlab / "apps/fitlab_walks/dashboards/walks.yaml"
    f.write_text("app: fitlab-rides\n" + f.read_text())
    errs = compose.compose(cfg_of(fitlab)).get("walks").errors
    assert any("but this dashboard comes from the app fitlab-walks" in e["problem"] for e in errs)


def test_a_dashboard_without_sections_or_anchors_is_published_as_it_is(fitlab):
    comp = compose.compose(cfg_of(fitlab))
    over = comp.get("overview")
    assert over.data.startswith(b"# Composed by easy-ui from ui/dashboards/overview.yaml, apps/fitlab_walks/")
    # The composed text means exactly the composed dashboard.
    again = spec.loads(over.data.decode(), slug="overview")
    assert spec.compile_dashboard(again) == spec.compile_dashboard(over.dashboard)
    (fitlab / "ui/dashboards/plain.yaml").write_text((FITLAB / "apps/fitlab_walks/dashboards/walks.yaml").read_text())
    plain = compose.compose(cfg_of(fitlab)).get("plain")
    assert plain.data == (fitlab / "ui/dashboards/plain.yaml").read_bytes() and plain.origin == "project"
    assert compose.compose(cfg_of(fitlab)).get("walks").data.startswith(b"# Composed")  # it gains app:


def test_compose_out_writes_the_composed_files(fitlab, tmp_path):
    out = tmp_path / "composed"
    code, data = run_cli_json("compose", "--out", str(out))
    assert code == 0 and sorted(Path(p).name for p in data["written"]) == ["overview.yaml", "walks.yaml"]
    assert spec.load_file(out / "overview.yaml").layout[-1][0]["app"] == "fitlab-rides"
    link = tmp_path / "link"
    link.symlink_to(out)
    code, data = run_cli_json("compose", "--out", str(link))
    assert code == 5 and data["error"]["code"] == "out_refused"


def test_menu_order_follows_ui_dashboards_then_discovery(fitlab):
    toml = fitlab / "project.toml"
    toml.write_text(toml.read_text().replace('dashboards = ["overview", "walks"]', 'dashboards = ["walks"]'))
    lock(fitlab, site(fitlab))
    (fitlab / "ui/dashboards/aaa.yaml").write_text((FITLAB / "apps/fitlab_walks/dashboards/walks.yaml").read_text())
    comp = compose.compose(cfg_of(fitlab))
    # walks (listed) first, then discovery order: the apps' dashboards, then the project's by file name.
    assert comp.order == ["walks", "aaa", "overview"]


def test_a_stale_lock_stops_composition_with_a_plain_message(fitlab):
    toml = fitlab / "project.toml"
    toml.write_text(toml.read_text() + "\n# changed\n")
    code, data = run_cli_json("compose")
    assert code == 1 and data["error"]["message"] == (
        "The list of installed apps (apps.lock.json) is out of date, so the apps cannot be found safely.")
    assert data["error"]["hint"] == "Run: mes apps sync"
    with pytest.raises(ConfigError):
        cfg_of(fitlab).tables


def test_validate_a_sections_file_on_its_own(fitlab):
    code, data = run_cli_json("validate", "apps/fitlab_walks/dashboards/overview-walks.yaml")
    assert code == 0 and data["dashboards"][0]["kind"] == "sections"
    f = fitlab / "apps/fitlab_walks/dashboards/overview-walks.yaml"
    f.write_text(f.read_text().replace("position: after:activity", "position: later"))
    code, data = run_cli_json("validate", str(f))
    assert code == 1 and "later" in data["dashboards"][0]["errors"][0]["problem"]


# ---------------------------------------------------------------- preview, publish, restore


def test_the_preview_reloads_when_an_app_file_changes(fitlab):
    pv = preview.prepare(cfg_of(fitlab))
    try:
        assert [d["slug"] for d in pv.source.list()] == ["overview", "walks"]
        rev = pv.source.rev()
        f = site(fitlab) / "fitlab_rides/dashboards/overview-rides.yaml"
        time.sleep(0.01)
        f.write_text(f.read_text().replace("title: Riding per week", "title: Rides per week"))
        assert pv.source.rev() != rev
        layout = pv.source.compiled("overview")["layout"]
        assert layout[-1][0]["title"] == "Rides per week" and layout[-1][0]["app"] == "fitlab-rides"
        # A mistake in an app's file shows on the dashboard's own problem page, naming that file.
        f.write_text(f.read_text().replace("query: rides_per_week", "query: rides_per_wek"))
        page = pv.source.compiled("overview")
        assert page["title"].endswith("needs fixing")
        assert any("overview-rides.yaml" in p["file"] for p in page["problems"])
        # An app's stylesheet appears in the preview's look as soon as it is saved.
        styles_dir = fitlab / "apps/fitlab_walks/styles"
        styles_dir.mkdir()
        (styles_dir / "walks.css").write_text(".eui-title { color: #225; }\n")
        look = pv.source.styles()
        assert look.public()["app_stylesheets"] == ["apps/fitlab-walks/styles/walks.css"]
        # project.toml changes are read again (here: the lock is then out of date, which the page says).
        toml = fitlab / "project.toml"
        toml.write_text(toml.read_text() + "\n# changed\n")
        assert PROBLEMS in [d["slug"] for d in pv.source.list()]
        assert "apps.lock.json" in json.dumps(pv.source.compiled(PROBLEMS))
        lock(fitlab, site(fitlab))
        assert PROBLEMS not in [d["slug"] for d in pv.source.list()], json.dumps(pv.source.compiled(PROBLEMS))[:3000]
    finally:
        pv.close()


PROBLEMS = preview.PROBLEMS_SLUG


def test_restore_files_puts_back_the_projects_own_files_only(fitlab):
    run_cli_json("data", "build")
    sections = fitlab / "ui" / "sections"
    sections.mkdir(parents=True)
    override = sections / "fitlab-rides.overview.yaml"
    override.write_text("spec: easy-ui/sections-v1\nsections: []\n")
    assert run_cli_json("publish")[0] == 0
    app_file = fitlab / "apps/fitlab_walks/dashboards/overview-walks.yaml"
    before = app_file.read_text()
    override.unlink()
    (fitlab / "ui/dashboards/overview.yaml").write_text(
        (fitlab / "ui/dashboards/overview.yaml").read_text().replace("title: Overview", "title: Changed"))
    app_file.write_text(before.replace("Walking per week", "Walking each week"))
    assert run_cli_json("publish")[0] == 0
    assert not publish.working_matches(cfg_of(fitlab), 1)
    code, data = run_cli_json("rollback", "1", "--restore-files")
    assert code == 0, data
    assert override.read_text() == "spec: easy-ui/sections-v1\nsections: []\n"
    assert "title: Overview" in (fitlab / "ui/dashboards/overview.yaml").read_text()
    assert app_file.read_text() == before.replace("Walking per week", "Walking each week")  # the app's own: untouched
    assert "ui/sections/fitlab-rides.overview.yaml" in data["files"]["restored"]


def test_working_matches_sees_a_change_in_an_app(fitlab):
    run_cli_json("data", "build")
    run_cli_json("publish")
    assert publish.working_matches(cfg_of(fitlab), 1)
    f = site(fitlab) / "fitlab_rides/dashboards/overview-rides.yaml"
    f.write_text(f.read_text().replace("Riding per week", "Rides"))
    assert not publish.working_matches(cfg_of(fitlab), 1)
    code, out, _ = run_cli("rollback", "1")
    assert "running easy-ui publish would show them again" in out or code == 0


def test_the_demo_shows_two_apps_contributing_to_one_overview(demo_app):
    comp = compose.compose(demo_app.config())
    over = comp.get("overview")
    assert over.ok and [(s["app"], s["placed"]) for s in over.sections] == [
        ("demo-moves", "after:movement"), ("demo-sleep", "end")]
    assert comp.order == ["overview", "activities"]

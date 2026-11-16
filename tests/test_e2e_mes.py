"""End to end with my-easy-software: the fitlab example made real by mes, then shown by easy-ui.

mes owns the apps lock and the shared database; easy-ui only reads them. This test installs a copy
of my-easy-software's ``examples/fitlab`` (two apps: one in the project, one installed from
``vendor/``), lets mes write ``apps.lock.json`` and both apps' rows through their jobs, and then
runs easy-ui's own commands on it: compose, data build, validate, publish.

It needs a my-easy-software checkout (``EASYUI_MES_DIR``, or ``../my-easy-software`` next to this
repository) and ``uv`` with a warm cache (``uv sync --offline``); without them it is skipped,
saying why. All data is made up.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
EASY_UI = Path(sys.executable).parent / "easy-ui"


def _framework() -> Path | None:
    for candidate in (os.environ.get("EASYUI_MES_DIR"), REPO.parent / "my-easy-software"):
        if candidate and (Path(candidate) / "examples" / "fitlab" / "project.toml").is_file():
            return Path(candidate).resolve()
    return None


@pytest.fixture(scope="module")
def fitlab(tmp_path_factory):
    framework = _framework()
    if framework is None:
        pytest.skip("my-easy-software is not next to this repository (set EASYUI_MES_DIR to its checkout)")
    uv = shutil.which("uv")
    if uv is None or not EASY_UI.is_file():
        pytest.skip("uv or this repository's easy-ui program is not installed")
    base = tmp_path_factory.mktemp("e2e")
    root = base / "fitlab"
    shutil.copytree(framework / "examples" / "fitlab", root,
                    ignore=shutil.ignore_patterns(".venv", "apps.lock.json", "uv.lock", "__pycache__"))
    py = root / "pyproject.toml"
    py.write_text(py.read_text().replace('path = "../.."', f'path = "{framework.as_posix()}"'))
    env = {k: v for k, v in os.environ.items()
           if k not in ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "MES_ALLOW", "MES_PROJECT", "MES_PROJECT_DIR",
                        "MES_DATA_DIR", "MES_STATE_DIR", "MES_EASY_UI")}
    env.update({"MES_DATA_HOME": str(base / "data"), "MES_STATE_HOME": str(base / "state"),
                "MES_CONFIG": str(base / "config.toml"), "EASYUI_STATE_HOME": str(base / "easy-ui")})
    done = subprocess.run([uv, "sync", "--offline", "--quiet"], cwd=root, env=env, capture_output=True, text=True,
                          timeout=300)
    if done.returncode != 0:
        pytest.skip(f"uv sync --offline did not work here: {done.stderr[-300:]}")

    def run(*argv):
        return subprocess.run([str(root / ".venv" / "bin" / argv[0]), *argv[1:]], cwd=root, env=env,
                              capture_output=True, text=True, timeout=180)

    def ui(*argv):
        p = subprocess.run([str(EASY_UI), "--json", "--project-dir", str(root), *argv], cwd=root, env=env,
                           capture_output=True, text=True, timeout=180)
        assert p.returncode == 0, p.stdout + p.stderr
        return json.loads(p.stdout)

    for argv in (("mes", "apps", "sync", "--project-dir", str(root)),
                 ("fitlab", "walks", "add", "2026-01-05", "4.2", "50", "--weather", "sunny"),
                 ("fitlab", "rides", "add", "2026-01-05", "21.5", "60", "--climb", "240", "--weather", "cloudy"),
                 ("mes", "jobs", "run", "fitlab-walks.rebuild", "--project-dir", str(root))):
        p = run(*argv)
        assert p.returncode == 0, p.stdout + p.stderr
    return root, ui


def test_easy_ui_reads_what_mes_wrote(fitlab):
    root, ui = fitlab
    lock = json.loads((root / "apps.lock.json").read_text())
    assert [(a["package"], a["in_project"]) for a in lock["apps"]] == [("fitlab_walks", True), ("fitlab_rides", False)]

    out = ui("compose")
    assert out["order"] == ["overview", "walks"] and out["warnings"] == []
    over = next(d for d in out["dashboards"] if d["slug"] == "overview")
    assert [(s["app"], s["placed"]) for s in over["sections"]] == [("fitlab-walks", "after:activity"),
                                                                  ("fitlab-rides", "end")]

    built = ui("data", "build")
    assert sorted(built["tables"]) == ["activities", "days", "walks_log", "weekly"]
    copy = sqlite3.connect(built["path"])
    try:
        assert {r[0] for r in copy.execute("SELECT source FROM activities")} == {"fitlab-walks", "fitlab-rides"}
        assert copy.execute("SELECT date, walk_km, ride_km, weather, source FROM days").fetchall() == [
            ("2026-01-05", 4.2, 21.5, "cloudy", "fitlab-rides")]
        names = {r[0] for r in copy.execute("SELECT name FROM sqlite_master")}
        assert not any("__rows" in n for n in names)
    finally:
        copy.close()

    assert ui("validate")["ok"] is True
    published = ui("publish")
    assert published.get("version") or published.get("published"), published

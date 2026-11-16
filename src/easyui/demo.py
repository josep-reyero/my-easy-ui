"""``easy-ui demo DIR``: a small made-up project to try easy-ui with, and the same app for the tests.

Everything here is synthetic (seeded random numbers): 120 days of movement, about
70 activities, a ``weekly`` view, and a ``private_notes`` table that is NOT in the
app's ``[ui] tables`` (so it can be shown that it never reaches dashboards).

``easy-ui demo DIR`` writes::

    DIR/app/project.toml                the project ([project] and [ui])
    DIR/app/ui/dashboards/*.yaml        two dashboards (overview, activities)
    DIR/app/ui/fixtures/demo.sqlite     the made-up data, for easy-ui preview
    DIR/data/demo/demo.sqlite           the same data where easy-ui looks for app data
                                        when MES_DATA_HOME=DIR/data

and prints the next steps. It refuses to write into a folder that is not empty.

Public API
----------
``DEMO_APP_TOML``, ``OVERVIEW_YAML``, ``ACTIVITIES_YAML``, ``DEMO_TABLES``
``make_demo_db(path) -> Path``
``write_demo(base, *, name="demo", dashboards=True, fixture=False) -> dict``
``register(subparsers)``  the ``demo`` command.
"""

from __future__ import annotations

import argparse
import datetime as dt
import random
import sqlite3
from pathlib import Path
from typing import Any

from easyui.errors import Refused

DEMO_TABLES = ("days", "activities", "weekly")

DEMO_APP_TOML = """\
[project]
name = "{name}"
title = "Demo app"
version = "0.1.0"

[ui]
data = "demo.sqlite"
tables = ["days", "activities", "weekly"]
accent = "#2F6FDF"

dashboards = ["overview", "activities"]

[jobs.build]
run = "demo build"
manual = true
"""
DEMO_PROJECT_TOML = DEMO_APP_TOML

OVERVIEW_YAML = """\
spec: easy-ui/v1
title: Overview
description: Daily movement at a glance (synthetic demo data).
params:
  since:
    type: date
    label: From
    default: "2026-01-01"
  period:
    type: choice
    label: Group by
    default: day
    options:
      - {value: day, label: Day}
      - {value: week, label: Week}
queries:
  totals:
    sql: |
      SELECT sum(distance_m) AS distance, sum(steps) AS steps,
             avg(resting_hr) AS resting_hr, count(*) AS days
      FROM days WHERE day >= :since
  daily:
    sql: |
      SELECT CASE WHEN :period = 'week' THEN strftime('%Y-%W', day) ELSE day END AS day,
             sum(distance_m) AS "Distance", sum(steps) AS "Steps"
      FROM days WHERE day >= :since
      GROUP BY 1 ORDER BY 1
  heart:
    sql: SELECT day, resting_hr AS "Resting heart rate" FROM days WHERE day >= :since ORDER BY day
layout:
  - - type: controls
  - - type: stat
      title: Distance
      query: totals
      value: distance
      format: distance
      hint: Walking, running and riding together.
    - type: stat
      title: Steps
      query: totals
      value: steps
      format: integer
    - type: stat
      title: Resting heart rate
      query: totals
      value: resting_hr
      format: integer
  - - type: bar
      title: Distance
      query: daily
      x: day
      y: Distance
      format: distance
    - type: line
      title: Resting heart rate
      query: heart
      x: day
      y: [Resting heart rate]
      unit: bpm
  - - type: text
      body: |
        ## About this page

        Numbers are **made up** for the demo. Read the *format guide* at
        [the project page](https://example.org/easy-ui).

        - Distance is in kilometres
        - Steps are whole numbers
"""

ACTIVITIES_YAML = """\
spec: easy-ui/v1
title: Activities
params:
  kind:
    type: choice
    label: Kind
    default: all
    options: [all, run, ride, walk, swim]
  min_km:
    type: int
    label: At least (km)
    default: 0
    min: 0
    max: 200
  search:
    type: text
    label: Search
    default: ""
queries:
  recent:
    sql: |
      SELECT day, kind, distance_m, duration_s,
             duration_s * 1000.0 / nullif(distance_m, 0) AS pace, avg_hr
      FROM activities
      WHERE (:kind = 'all' OR kind = :kind)
        AND distance_m >= :min_km * 1000
        AND (:search = '' OR kind LIKE '%' || :search || '%')
      ORDER BY start DESC
  by_kind:
    sql: |
      SELECT kind, count(*) AS "Count", sum(distance_m) AS "Distance"
      FROM activities WHERE (:kind = 'all' OR kind = :kind)
      GROUP BY kind ORDER BY kind
  weekly:
    sql: SELECT week, distance_m AS "Distance" FROM weekly ORDER BY week
layout:
  - - type: controls
      title: Filter
      params: [kind, min_km, search]
  - - type: table
      title: Recent activities
      query: recent
      limit: 50
      columns:
        - {column: day, label: Day, format: date}
        - kind
        - {column: distance_m, label: Distance, format: distance}
        - {column: duration_s, label: Time, format: duration}
        - {column: pace, label: Pace, format: pace}
        - {column: avg_hr, label: Heart rate, format: integer}
  - - type: bar
      title: By kind
      query: by_kind
      x: kind
      y: [Distance]
      format: distance
      stacked: false
    - type: area
      title: Weekly distance
      query: weekly
      x: week
      y: Distance
      format: distance
      partial: last
"""


def make_demo_db(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(7)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE days (day TEXT PRIMARY KEY, steps INTEGER, distance_m REAL, active_s INTEGER,
                               resting_hr INTEGER, sleep_s INTEGER);
            CREATE TABLE activities (id INTEGER PRIMARY KEY, day TEXT, start TEXT, kind TEXT, distance_m REAL,
                                     duration_s INTEGER, avg_hr INTEGER);
            CREATE INDEX activities_day ON activities(day);
            CREATE VIEW weekly AS
                SELECT strftime('%Y-%W', day) AS week, sum(steps) AS steps, sum(distance_m) AS distance_m
                FROM days GROUP BY week;
            CREATE TABLE private_notes (id INTEGER PRIMARY KEY, note TEXT);
            """
        )
        start = dt.date(2026, 1, 1)
        act_id = 0
        speeds = {"run": 2.8, "ride": 6.5, "walk": 1.4, "swim": 0.6}
        for i in range(120):
            day = (start + dt.timedelta(days=i)).isoformat()
            steps = rng.randint(2500, 16000)
            conn.execute(
                "INSERT INTO days VALUES (?, ?, ?, ?, ?, ?)",
                (day, steps, round(steps * 0.75, 1), rng.randint(900, 7200), rng.randint(48, 62), rng.randint(19800, 30600)),
            )
            if i % 2 == 0 or i % 7 == 3:
                act_id += 1
                kind = rng.choice(list(speeds))
                dur = rng.randint(900, 5400)
                conn.execute(
                    "INSERT INTO activities VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (act_id, day, f"{day}T{rng.randint(6, 19):02d}:{rng.randint(0, 59):02d}:00", kind,
                     round(dur * speeds[kind], 1), dur, rng.randint(105, 165)),
                )
        conn.executemany("INSERT INTO private_notes (note) VALUES (?)", [("not for dashboards",), ("synthetic secret",)])
        conn.commit()
    finally:
        conn.close()
    return path


def write_demo(base: Path, *, name: str = "demo", dashboards: bool = True, fixture: bool = False) -> dict[str, Any]:
    app_dir = base / "app"
    data_home = base / "data"
    state_home = base / "state"
    (app_dir / "ui" / "dashboards").mkdir(parents=True, exist_ok=True)
    (app_dir / "project.toml").write_text(DEMO_APP_TOML.format(name=name), encoding="utf-8")
    db = make_demo_db(data_home / name / "demo.sqlite")
    if dashboards:
        (app_dir / "ui" / "dashboards" / "overview.yaml").write_text(OVERVIEW_YAML, encoding="utf-8")
        (app_dir / "ui" / "dashboards" / "activities.yaml").write_text(ACTIVITIES_YAML, encoding="utf-8")
    if fixture:
        make_demo_db(app_dir / "ui" / "fixtures" / "demo.sqlite")
    return {"app_dir": app_dir, "data_home": data_home, "state_home": state_home, "db": db}


def _cmd_demo(args: argparse.Namespace) -> Any:
    from easyui.cli import Result

    base = Path(args.dir).expanduser().resolve()
    if base.is_symlink() or (base.exists() and (not base.is_dir() or any(base.iterdir()))):
        raise Refused(
            f"{base} already exists and is not an empty folder, so the demo was not written there (nothing is "
            "overwritten).",
            hint="Pick a new folder, for example: easy-ui demo ./easy-ui-demo",
            code="not_empty",
        )
    made = write_demo(base, fixture=True)
    app, data, state = made["app_dir"], made["data_home"], made["state_home"]
    text = "\n".join([
        f"Wrote a demo project with made-up data in {base}.",
        "Look at it straight away (no password, nothing published):",
        f"  cd {app} && easy-ui preview",
        "Or try the whole journey, with the demo's own data and state folders:",
        f"  export MES_DATA_HOME={data} EASYUI_STATE_HOME={state}",
        f"  cd {app}",
        "  easy-ui data build && easy-ui validate && easy-ui publish",
        "  easy-ui set-password     # you type a password (12 characters or more)",
        "  easy-ui check && easy-ui serve",
    ])
    return Result({"dir": str(base), "app_dir": str(app), "data_home": str(data), "state_home": str(state),
                   "env": {"MES_DATA_HOME": str(data), "EASYUI_STATE_HOME": str(state)}}, text)


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "demo",
        help="Write a small demo project with made-up data, to try easy-ui.",
        description="Write a demo project (project.toml, two dashboards and made-up data) into a new folder, and print how "
        "to preview, publish and serve it.",
    )
    p.add_argument("dir", help="a new or empty folder to write the demo into")
    p.set_defaults(func=_cmd_demo)

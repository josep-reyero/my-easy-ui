"""``easy-ui demo DIR``: a small made-up project with two apps, to try easy-ui with, and the same project for the tests.

Everything here is synthetic (seeded random numbers): 120 days of movement and
sleep, about 70 activities, a ``weekly`` view, and a ``private_notes`` table
that no dashboard may read (so it can be shown that it never reaches them).

The project has two tiny apps inside it (no programs, only what easy-ui reads):
``<name>-moves`` writes the shared tables ``days`` and ``activities`` and adds a
section to the overview; ``<name>-sleep`` writes the sleep column of ``days``
and its own table ``sleep_nights``, adds a section to the overview and ships
a stylesheet scoped to that section. The shared database follows the apps
contract: ``days__rows`` and ``activities__rows`` with a ``source`` column,
read through the views ``days`` and ``activities``.

``easy-ui demo DIR`` writes::

    DIR/app/project.toml                            the project ([project], [tables], [views], [ui])
    DIR/app/apps/<name>_moves/app.toml              the moves app, and dashboards/overview-moves.yaml
    DIR/app/apps/<name>_sleep/app.toml              the sleep app, dashboards/overview-sleep.yaml, styles/sleep.css
    DIR/app/ui/dashboards/*.yaml                    two dashboards (overview, activities)
    DIR/app/ui/fixtures/demo.sqlite                 the made-up data, for easy-ui preview
    DIR/data/<name>/<name>.sqlite                   the same data where easy-ui looks for it with MES_DATA_HOME=DIR/data

and prints the next steps. It refuses to write into a folder that is not empty.

Public API
----------
``DEMO_APP_TOML`` (``DEMO_PROJECT_TOML``), ``OVERVIEW_YAML``, ``ACTIVITIES_YAML``, ``DEMO_TABLES``,
``MOVES_APP_TOML``, ``SLEEP_APP_TOML``, ``MOVES_SECTIONS_YAML``, ``SLEEP_SECTIONS_YAML``, ``SLEEP_CSS``
``schema_sql(project, app_names) -> str``   the shared database's schema script (contract 4.3) for a parsed
    project.toml; my-easy-software owns the real one (``mes db schema --sql``), this one makes test data.
``make_demo_db(path, *, name="demo") -> Path``
``write_demo(base, *, name="demo", dashboards=True, fixture=False) -> dict``
``register(subparsers)``  the ``demo`` command.
"""

from __future__ import annotations

import argparse
import datetime as dt
import random
import sqlite3
import tomllib
from pathlib import Path
from typing import Any

from easyui.errors import Refused

DEMO_TABLES = ("days", "activities", "weekly", "sleep_nights")

DEMO_APP_TOML = """\
[project]
name = "{name}"
title = "Demo project"
version = "0.1.0"
apps = ["{module}_moves", "{module}_sleep"]   # two apps inside the project, in apps/; the order matters

[tables.days]                                # one row per day; both apps write it
key = ["day"]
prefer = ["{name}-moves"]                    # rows of both apps for the same day become one row
[tables.days.columns]
day = "text"
steps = "integer"
distance_m = "real"
active_s = "integer"
resting_hr = "integer"
sleep_s = "integer"

[tables.activities]                          # one row per activity
key = ["id"]
columns = {{ id = "integer", day = "text", start = "text", kind = "text", distance_m = "real", duration_s = "integer", avg_hr = "integer" }}

[views.weekly]
sql = \"\"\"
SELECT strftime('%Y-%W', day) AS week, sum(steps) AS steps, sum(distance_m) AS distance_m
FROM days
GROUP BY week
\"\"\"

[ui]
accent = "#2F6FDF"
dashboards = ["overview", "activities"]
"""
DEMO_PROJECT_TOML = DEMO_APP_TOML

MOVES_APP_TOML = """\
# A demo app: only what easy-ui reads. With my-easy-software an app also has a program (cli or command).
[app]
name = "{name}-moves"
title = "Moves"
command = "{name}-moves"

[data]
writes = ["days", "activities"]

[ui.contributes.overview]
sections = "dashboards/overview-moves.yaml"
"""

SLEEP_APP_TOML = """\
# A demo app: only what easy-ui reads. With my-easy-software an app also has a program (cli or command).
[app]
name = "{name}-sleep"
title = "Sleep"
command = "{name}-sleep"

[data]
writes = ["days"]
tables = ["sleep_nights"]

[ui]
tables = ["sleep_nights"]                    # dashboards may read this table of the app

[ui.contributes.overview]
sections = "dashboards/overview-sleep.yaml"
"""

MOVES_SECTIONS_YAML = """\
spec: easy-ui/sections-v1
sections:
  - id: kinds
    position: after:movement
    queries:
      moves_by_kind:
        sql: |
          SELECT kind, count(*) AS "Activities", sum(distance_m) AS "Distance"
          FROM activities WHERE day >= :since
          GROUP BY kind ORDER BY 3 DESC
    layout:
      - - type: bar
          title: Distance by kind
          query: moves_by_kind
          x: kind
          y: Distance
          format: distance
"""

SLEEP_SECTIONS_YAML = """\
spec: easy-ui/sections-v1
sections:
  - id: sleep
    position: end
    queries:
      sleep_nightly:
        sql: |
          SELECT night, round(asleep_s / 3600.0, 2) AS "Hours asleep",
                 round(avg(asleep_s) OVER (ORDER BY night ROWS 6 PRECEDING) / 3600.0, 2) AS "7-night average"
          FROM sleep_nights WHERE night >= :since ORDER BY night
      sleep_average:
        sql: |
          SELECT avg(asleep_s) AS asleep, avg(score) AS score
          FROM sleep_nights WHERE night >= :since
    layout:
      - - type: line
          id: sleep-hours
          title: Sleep per night
          query: sleep_nightly
          x: night
          y: [Hours asleep, 7-night average]
          dashed: [7-night average]
          reference: {value: 8, label: Goal}
          format: decimal1
          unit: h
        - type: stat
          title: Asleep on average
          query: sleep_average
          value: asleep
          format: duration
          hint: From the sleep app.
"""

SLEEP_CSS = """\
/* The sleep app's own look. easy-ui applies it only to what this app shows (its sections). */
.eui-component {
  --eui-chart-1: #6b5fb5;
}
.eui-app[data-theme="dark"] .eui-component {
  --eui-chart-1: #a99bff; /* lighter, so it stands out on a dark panel */
}
.eui-title {
  letter-spacing: 0.02em;
}
"""

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
    sql: |
      SELECT day, resting_hr AS "Resting heart rate",
             round(avg(resting_hr) OVER (ORDER BY day ROWS 27 PRECEDING) - 4, 1) AS usual_low,
             round(avg(resting_hr) OVER (ORDER BY day ROWS 27 PRECEDING) + 4, 1) AS usual_high
      FROM days WHERE day >= :since ORDER BY day
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
  - anchor: movement
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
      band: {low: usual_low, high: usual_high}
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
  latest:
    sql: |
      SELECT start, datetime(start, '+' || duration_s || ' seconds') AS until, kind
      FROM activities
      WHERE day > (SELECT date(max(day), '-14 days') FROM activities) AND (:kind = 'all' OR kind = :kind)
      ORDER BY start
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
      colors_by: kind
    - type: area
      title: Weekly distance
      query: weekly
      x: week
      y: Distance
      format: distance
      partial: last
  - - type: timeline
      title: The last two weeks
      query: latest
      start: start
      end: until
      category: kind
      order: [run, ride, walk, swim]
"""


_TYPES = {"text": "TEXT", "integer": "INTEGER", "real": "REAL", "number": "", "boolean": "INTEGER", "json": "TEXT"}


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def schema_sql(project: dict[str, Any], app_names: list[str] | tuple[str, ...]) -> str:
    """The shared database's schema script of contract section 4.3 (exact text), for test and demo data."""
    tables = project.get("tables") or {}
    views = project.get("views") or {}
    apps = ", ".join(f"'{a}'" for a in app_names)
    out: list[str] = []
    for t, d in tables.items():
        key = list(d["key"])
        cols = dict(d["columns"])
        lines = [f'CREATE TABLE IF NOT EXISTS {_q(t + "__rows")} (', '  "source" TEXT NOT NULL,']
        lines += [f"  {_q(k)} {_TYPES[cols[k]]} NOT NULL," for k in key]
        lines += [f"  {_q(c)} {_TYPES[ty]},".replace(" ,", ",") for c, ty in cols.items() if c not in key]
        lines.append(f'  PRIMARY KEY ("source", {", ".join(_q(k) for k in key)})')
        lines.append(");")
        out += lines
        out.append(f'CREATE INDEX IF NOT EXISTS {_q(t + "__rows_key")} ON {_q(t + "__rows")} '
                   f'({", ".join(_q(k) for k in key)});')
    for t, d in tables.items():
        key = list(d["key"])
        cols = dict(d["columns"])
        others = [c for c in cols if c not in key]
        out.append(f"DROP VIEW IF EXISTS {_q(t)};")
        out.append(f"CREATE VIEW {_q(t)} AS")
        prefer = list(d.get("prefer") or [])
        if not prefer:
            out.append("SELECT " + ", ".join(_q(c) for c in ["source", *key, *others]))
            out.append(f"FROM {_q(t + '__rows')}")
            out.append(f'WHERE "source" IN ({apps});')
            continue
        ranked = prefer + [a for a in app_names if a not in prefer]
        rank = "CASE r.\"source\" " + " ".join(f"WHEN '{a}' THEN {i}" for i, a in enumerate(ranked)) + " END"
        match = " AND ".join(f"r.{_q(k)} = k.{_q(k)}" for k in key)
        items = []
        for c in ["source", *key, *others]:
            if c in key:
                items.append(f"  k.{_q(c)} AS {_q(c)}")
                continue
            nn = "" if c == "source" else f" AND r.{_q(c)} IS NOT NULL"
            items.append(f"  (SELECT r.{_q(c)} FROM {_q(t + '__rows')} r WHERE {match} AND r.\"source\" IN ({apps})"
                         f"{nn} ORDER BY {rank} LIMIT 1) AS {_q(c)}")
        out.append("SELECT")
        out += [x + ("," if i < len(items) - 1 else "") for i, x in enumerate(items)]
        out.append(f"FROM (SELECT DISTINCT {', '.join(_q(k) for k in key)} FROM {_q(t + '__rows')} "
                   f'WHERE "source" IN ({apps})) k;')
    for v, d in views.items():
        out.append(f"DROP VIEW IF EXISTS {_q(v)};")
        out.append(f"CREATE VIEW {_q(v)} AS")
        out.append(d["sql"].strip() + ";")
    return "\n".join(out) + "\n"


def make_demo_db(path: Path, *, name: str = "demo") -> Path:
    """The demo's shared database: the rows of both apps, an app table, and a table no dashboard may read."""
    path.parent.mkdir(parents=True, exist_ok=True)
    project = tomllib.loads(DEMO_APP_TOML.format(name=name, module=name.replace("-", "_")))
    moves, sleep = f"{name}-moves", f"{name}-sleep"
    rng = random.Random(7)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(schema_sql(project, [moves, sleep]))
        conn.executescript(
            """
            CREATE TABLE sleep_nights (night TEXT PRIMARY KEY, asleep_s INTEGER, score INTEGER);
            CREATE TABLE private_notes (id INTEGER PRIMARY KEY, note TEXT);
            """
        )
        start = dt.date(2026, 1, 1)
        act_id = 0
        speeds = {"run": 2.8, "ride": 6.5, "walk": 1.4, "swim": 0.6}
        for i in range(120):
            day = (start + dt.timedelta(days=i)).isoformat()
            steps = rng.randint(2500, 16000)
            distance, active, hr, asleep = (round(steps * 0.75, 1), rng.randint(900, 7200), rng.randint(48, 62),
                                            rng.randint(19800, 30600))
            conn.execute('INSERT INTO "days__rows" (source, day, steps, distance_m, active_s, resting_hr) '
                         "VALUES (?, ?, ?, ?, ?, ?)", (moves, day, steps, distance, active, hr))
            conn.execute('INSERT INTO "days__rows" (source, day, sleep_s) VALUES (?, ?, ?)', (sleep, day, asleep))
            conn.execute("INSERT INTO sleep_nights VALUES (?, ?, ?)", (day, asleep, 50 + (asleep - 19800) * 45 // 10800))
            if i % 2 == 0 or i % 7 == 3:
                act_id += 1
                kind = rng.choice(list(speeds))
                dur = rng.randint(900, 5400)
                conn.execute(
                    'INSERT INTO "activities__rows" VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                    (moves, act_id, day, f"{day}T{rng.randint(6, 19):02d}:{rng.randint(0, 59):02d}:00", kind,
                     round(dur * speeds[kind], 1), dur, rng.randint(105, 165)),
                )
        conn.executemany("INSERT INTO private_notes (note) VALUES (?)", [("not for dashboards",), ("synthetic secret",)])
        conn.commit()
    finally:
        conn.close()
    return path


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_demo(base: Path, *, name: str = "demo", dashboards: bool = True, fixture: bool = False) -> dict[str, Any]:
    app_dir = base / "app"
    data_home = base / "data"
    state_home = base / "state"
    module = name.replace("-", "_")
    (app_dir / "ui" / "dashboards").mkdir(parents=True, exist_ok=True)
    _write(app_dir / "project.toml", DEMO_APP_TOML.format(name=name, module=module))
    moves = app_dir / "apps" / f"{module}_moves"
    sleep = app_dir / "apps" / f"{module}_sleep"
    _write(moves / "__init__.py", "")
    _write(moves / "app.toml", MOVES_APP_TOML.format(name=name))
    _write(moves / "dashboards" / "overview-moves.yaml", MOVES_SECTIONS_YAML)
    _write(sleep / "__init__.py", "")
    _write(sleep / "app.toml", SLEEP_APP_TOML.format(name=name))
    _write(sleep / "dashboards" / "overview-sleep.yaml", SLEEP_SECTIONS_YAML)
    _write(sleep / "styles" / "sleep.css", SLEEP_CSS)
    db = make_demo_db(data_home / name / f"{name}.sqlite", name=name)
    if dashboards:
        (app_dir / "ui" / "dashboards" / "overview.yaml").write_text(OVERVIEW_YAML, encoding="utf-8")
        (app_dir / "ui" / "dashboards" / "activities.yaml").write_text(ACTIVITIES_YAML, encoding="utf-8")
    if fixture:
        make_demo_db(app_dir / "ui" / "fixtures" / "demo.sqlite", name=name)
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
        f"Wrote a demo project with made-up data in {base}: two apps (moves and sleep) add their sections to the "
        "overview dashboard, and the sleep app brings its own stylesheet for them.",
        "Look at it straight away (no password, nothing published):",
        f"  cd {app} && easy-ui preview",
        "Or try the whole journey, with the demo's own data and state folders:",
        f"  export MES_DATA_HOME={data} EASYUI_STATE_HOME={state}",
        f"  cd {app}",
        "  easy-ui dashboards       # where each dashboard and section comes from",
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
        description="Write a demo project (project.toml, two small apps that add sections to its overview, two "
        "dashboards and made-up data) into a new folder, and print how to preview, publish and serve it.",
    )
    p.add_argument("dir", help="a new or empty folder to write the demo into")
    p.set_defaults(func=_cmd_demo)

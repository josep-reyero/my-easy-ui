"""Regression tests for the security audit findings: each one first reproduces the attack, then checks the fix.

Findings: (1) one long SQLite step outliving the time limit, (2) memory blown up by a query, (3) temporary
files spilled without bound, (4) what link mode protects against and the single-file export, (5) data build
trusting the project's file, (6) an export holding more than it lists, (7) a WITH name hiding a real table,
(8) cookie flags following a header, (9) queries that blow up while being prepared, (10) rev following a
linked specs folder, (11) validate reading links and FIFOs, (12) the smaller ones.
All data is made up (tests/conftest.py).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import tracemalloc
from pathlib import Path

import pytest
from conftest import run_cli, run_cli_json
from test_server import DirSource, env, fast_hash, login, web_dir  # noqa: F401 - fixtures

from easyui import appconfig, bundle, export, paths, publish, sandbox, security, server, spec, workers
from easyui.errors import DataBuildError, QueryRefused, QueryTimeout, Refused, SpecError
from easyui.sandbox import Limits, Sandbox

FAST = Limits(timeout_s=0.3)
ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "src" / "easyui" / "web"


def finishes_within(seconds: float, fn, *args, **kwargs):
    """Run fn in a thread; fail (without hanging the suite) when it has not finished in time."""
    box: dict = {}

    def run():
        try:
            box["value"] = fn(*args, **kwargs)
        except BaseException as e:  # noqa: BLE001
            box["error"] = e

    t = threading.Thread(target=run, daemon=True)
    started = time.monotonic()
    t.start()
    t.join(seconds)
    assert not t.is_alive(), f"still running after {seconds} s"
    box["elapsed"] = time.monotonic() - started
    return box


def dashboard(sql: str, params: str = "", component: str = "      value: n") -> spec.Dashboard:
    text = (
        "spec: easy-ui/v1\ntitle: T\n" + (f"params:\n{params}" if params else "")
        + "queries:\n  a:\n    sql: |\n      " + sql.replace("\n", "\n      ")
        + "\nlayout:\n  - - type: stat\n      title: A\n      query: a\n" + component + "\n"
    )
    return spec.loads(text, slug="t")


@pytest.fixture
def pool():
    p = workers.WorkerPool(2, idle_s=5)
    yield p
    p.close()


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # a zombie still answers kill(0); reap it if it is ours
        done, _ = os.waitpid(pid, os.WNOHANG)
        return done == 0
    except ChildProcessError:
        return True


# ---------------------------------------------------------------- 1. one long step outlives the time limit


INSTR_ATTACK = "SELECT instr(printf('%.*c', 4900000, 'a'), printf('%.*c', 2400000, 'a') || 'b') AS n"


def test_the_auditors_instr_query_ends_at_once(bundle_path, pool):
    # Before: 5 MB strings were allowed and instr() ran for minutes inside one SQLite step.
    box = finishes_within(10, spec.run_queries, dashboard(INSTR_ATTACK), ["a"], bundle_path=bundle_path,
                          errors="inline", pool=pool)
    assert "error" not in box, box.get("error")
    # Text over the 200 KB limit is never made: printf() gives NULL, other functions refuse.
    assert box["value"]["results"]["a"] == {"columns": ["n"], "rows": [[None]], "truncated": False}
    assert box["elapsed"] < 3
    box = finishes_within(10, spec.run_queries, dashboard(INSTR_ATTACK.replace("printf('%.*c', 4900000, 'a')",
                          "CAST(zeroblob(4900000) AS TEXT)")), ["a"], bundle_path=bundle_path, errors="inline", pool=pool)
    assert box["value"]["results"]["a"]["error"]["code"] == "query_refused" and box["elapsed"] < 3


def test_a_stuck_worker_is_killed_at_its_deadline_and_replaced(bundle_path, pool):
    # A worker that does not answer (as if stuck in one long SQLite step) is killed, not waited for.
    with pool.session(bundle_path, FAST) as sb:
        assert sb.run("SELECT count(*) FROM days").rows == [[120]]
        (pid,) = pool.worker_pids()
        os.kill(pid, signal.SIGSTOP)
        started = time.monotonic()
        with pytest.raises(QueryTimeout) as e:
            sb.run("SELECT count(*) FROM days", name="a")
        took = time.monotonic() - started
        assert took < FAST.timeout_s + workers.KILL_GRACE_S + 1.0, took
        assert "longer than 0.3 seconds" in e.value.message
        assert not alive(pid)
        assert sb.run("SELECT count(*) FROM days").rows == [[120]]  # a fresh worker, same session
    assert pool.stats()["killed"] == 1


def test_composed_dashboards_and_app_sections_run_their_queries_in_workers(built_app, pool, monkeypatch):
    # An app's contributed section with the auditor's query: composing, validating and publishing check it in a
    # worker process (never in the command's own process), and it ends at once.
    monkeypatch.setattr(workers, "default_pool", lambda: pool)
    monkeypatch.setattr(sandbox, "Sandbox", None)  # nothing may run a query outside a worker
    f = built_app.app_dir / "apps" / "demo_moves" / "dashboards" / "overview-moves.yaml"
    f.write_text(f.read_text() + (
        "  - id: attack\n    position: end\n    queries:\n      moves_attack:\n        sql: " + INSTR_ATTACK + "\n"
        "      moves_missing:\n        sql: SELECT 1 AS n\n"
        "    layout:\n      - - type: stat\n          title: A\n          query: moves_attack\n          value: n\n"
        "        - type: stat\n          title: B\n          query: moves_missing\n          value: nope\n"))
    box = finishes_within(20, spec.validate_app, built_app.config())
    assert "error" not in box, box.get("error")
    res = box["value"]
    over = next(d for d in res["dashboards"] if d["slug"] == "overview")
    # the column check needs the query's real columns, so this problem is only found by running it in a worker
    assert any("nope" in e["problem"] and "overview-moves.yaml" in e["file"] for e in over["errors"]), over["errors"]
    assert pool.stats()["started"] >= 1 and box["elapsed"] < 10
    with pytest.raises(SpecError):
        publish.publish(built_app.config())


def test_a_worker_that_dies_gives_a_plain_error_and_is_replaced(bundle_path, pool):
    with pool.session(bundle_path, FAST) as sb:
        sb.run("SELECT 1")
        (pid,) = pool.worker_pids()
        os.kill(pid, signal.SIGKILL)
        with pytest.raises(QueryRefused) as e:
            sb.run("SELECT 1", name="a")
        assert "memory" in e.value.message
        assert sb.run("SELECT 2").rows == [[2]]


def test_text_controls_are_tried_with_long_text_at_publish(bundle_path):
    # Before: only choices and int bounds were tried, so a query that is slow for long text got published.
    dash = dashboard(
        "SELECT CASE WHEN length(:q) >= 100 THEN (WITH RECURSIVE r(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM r) "
        "SELECT count(*) FROM r) ELSE 1 END AS n",
        params="  q:\n    type: text\n    label: Search\n    default: ''\n",
    )
    errors, _ = spec.check_with_data(dash, bundle_path, limits=FAST)
    assert errors and "text of 200 characters" in errors[0].message


def test_a_busy_server_says_so_after_a_short_wait(env):
    c = env.make(slot_wait_s=0.2)
    assert login(c).status_code == 200
    import asyncio

    c.asgi.easyui.query_slots = asyncio.Semaphore(0)  # every slot taken
    started = time.monotonic()
    r = c.post("/api/query", json={"dashboard": "overview", "queries": ["totals"]}, headers={"origin": "http://localhost"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "busy"
    assert r.headers["retry-after"] and time.monotonic() - started < 2


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT length(zeroblob(300000)) AS n",  # one value over 200 KB
        "SELECT length(replace(printf('%.*c', 150000, 'a'), 'a', 'aa')) AS n",
        "SELECT 'a' LIKE printf('%.*c', 400, '%') AS n",  # a LIKE pattern over 300 characters
        "SELECT 1 AS n" + " UNION ALL SELECT 1" * 25,  # too many compound parts
        "SELECT " + ", ".join(f"{i} AS c{i}" for i in range(101)),  # more than 100 columns
    ],
)
def test_sqlite_limits_are_small(bundle_path, sql):
    with pytest.raises(QueryRefused):
        sandbox.run_query(bundle_path, sql)


# ---------------------------------------------------------------- 2. memory


def test_rows_are_counted_against_the_size_limit_one_at_a_time(bundle_path):
    # Before: fetchmany(500) held 500 rows (here 75 MB) before the size limit was looked at.
    sql = ("WITH RECURSIVE r(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM r WHERE i < 600) "
           "SELECT printf('%.*c', 150000, 'a') || i AS big FROM r")
    tracemalloc.start()
    try:
        res = sandbox.run_query(bundle_path, sql)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert res.truncated and res.truncated_reason == "size"
    assert peak < 25_000_000, peak


def test_every_sandbox_caps_sqlite_memory_well_below_the_service_limit(bundle_path):
    previous = sandbox.set_heap_limit(0)
    try:
        with Sandbox(bundle_path):
            pass
        assert sandbox.set_heap_limit(0) == sandbox.HEAP_LIMIT == 64_000_000
    finally:
        sandbox.set_heap_limit(previous)
    units_text = "\n".join(__import__("easyui.units", fromlist=["x"]).render_units(
        "fitness", title="F", exec_path="/usr/local/bin/easy-ui", origins=("https://f.example",)).values())
    assert "MemoryMax=256M" in units_text and "OOMPolicy=continue" in units_text
    assert workers.MEMORY_LIMIT < 256 * 1024 * 1024


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="RLIMIT_AS is enforced on Linux only")
def test_worker_memory_is_capped(bundle_path, pool):
    with pool.session(bundle_path) as sb:
        sb.run("SELECT 1")
        (pid,) = pool.worker_pids()
        limits = Path(f"/proc/{pid}/limits").read_text()
        line = next(x for x in limits.splitlines() if x.startswith("Max address space"))
        assert str(workers.MEMORY_LIMIT) in line
        assert "Max file size            0" in limits.replace("  ", " ") or re.search(r"Max file size\s+0\s", limits)


# ---------------------------------------------------------------- 3. temporary files


def test_sorting_stays_in_memory_never_in_temporary_files(bundle_path):
    with Sandbox(bundle_path) as sb:
        sb.conn.set_authorizer(None)
        assert sb.conn.execute("PRAGMA temp_store").fetchone()[0] == 2  # MEMORY
    with pytest.raises((QueryRefused, QueryTimeout)):
        sandbox.run_query(bundle_path, "SELECT a.day, b.day, c.day FROM days a, days b, days c "
                          "ORDER BY a.steps * b.steps * c.steps, a.day || b.day || c.day", limits=Limits(timeout_s=5))


# ---------------------------------------------------------------- 4. link mode and the single file


@pytest.fixture
def published(built_app):
    publish.publish(built_app.config())
    return built_app


def test_single_file_export_holds_everything_and_only_inline_parts_it_names(published, tmp_path):
    (tmp_path / "out").mkdir()
    out = tmp_path / "out" / "share.html"
    res = export.export(published.config(), out=out, single_file=True)
    assert res["single_file"] and res["files"] == ["share.html"]
    assert sorted(p.name for p in out.parent.iterdir()) == ["share.html"]  # no folder, no leftovers
    page = out.read_text()
    assert 'name="robots" content="noindex' in page
    markup = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", "", page, flags=re.S)
    assert not re.search(r"""\b(?:src|href)\s*=\s*["'](?!data:)""", markup)  # nothing loaded from anywhere
    # The two scripts, easy-ui's style sheet, the data and (the demo has an app's stylesheet) the look block.
    assert page.count("<script") == 4 and page.count("<style") == 1
    csp = re.search(r'http-equiv="Content-Security-Policy" content="([^"]+)"', page).group(1)
    assert "connect-src 'none'" in csp and "unsafe-inline" not in csp
    for kind, body in [("script", m) for m in re.findall(r"<script>(.*?)</script>", page, re.S)] + \
            [("style", m) for m in re.findall(r"<style>(.*?)</style>", page, re.S)]:
        digest = base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
        assert f"'sha256-{digest}'" in csp, kind
    # The look's stylesheets are in the page as text the page adds as <style> elements: each one allowed by its
    # hash, and exactly the ones the confirmation lists.
    look = json.loads(re.search(r'id="easyui-look">(.*?)</script>', page, re.S).group(1))
    assert look and sorted(look) == [f for f in res["look"] if not re.search(r"/assets/", f)]
    for path, text in look.items():
        digest = base64.b64encode(hashlib.sha256(text.encode()).digest()).decode()
        assert f"'sha256-{digest}'" in csp, path
    assert len(re.findall(r"'sha256-", csp)) == 3 + len(look)  # nothing else is allowed inline
    assert json.loads(re.search(r'id="easyui-export">(.*?)</script>', page, re.S).group(1))["specs"]
    with pytest.raises(Refused):
        export.export(published.config(), out=out, single_file=True)  # never overwritten without --force
    export.export(published.config(), out=out, single_file=True, force=True, encrypt=True)


def test_encrypt_says_what_the_lock_does_not_protect_against(published, tmp_path):
    code, out, _ = run_cli("export", "--out", str(tmp_path / "s"), "--encrypt")
    assert code == 0
    assert "does not protect them from whoever serves the files" in out and "history" in out
    readme = (ROOT / "README.md").read_text()
    assert "does **not** protect against whoever serves" in readme and "--single-file" in readme
    assert "uv sync --locked" in readme and "--require-hashes" in readme


# ---------------------------------------------------------------- 5. data build


def _sql(db: Path, *statements: str) -> None:
    conn = sqlite3.connect(db)
    try:
        for s in statements:
            conn.execute(s)
        conn.commit()
    finally:
        conn.close()


def _add_view(app, name: str, sql: str) -> None:
    """A view in the project's file that project.toml lists under [views], so dashboards may read it."""
    _sql(app.db_path, f"CREATE VIEW {name} AS {sql}")
    toml = app.app_dir / "project.toml"
    toml.write_text(toml.read_text() + f'\n[views.{name}]\nsql = """\n{sql}\n"""\n')


def test_indexes_are_made_again_from_their_columns_never_from_stored_sql(demo_app):
    # sleep_nights is an app's own table (a real table in the copy); days is a shared table's view.
    _sql(demo_app.db_path,
         "CREATE INDEX sleep_expr ON sleep_nights(abs(score) + 1)",
         "CREATE INDEX sleep_partial ON sleep_nights(score) WHERE score > 50",
         "CREATE INDEX sleep_score ON sleep_nights(score DESC, night COLLATE NOCASE)")
    res = bundle.build(demo_app.config())
    conn = sqlite3.connect(res["path"])
    try:
        rows = dict(conn.execute("SELECT name, sql FROM sqlite_schema WHERE type = 'index'").fetchall())
    finally:
        conn.close()
    assert "sleep_expr" not in rows and "sleep_partial" not in rows
    assert rows["sleep_score"] == ('CREATE INDEX "sleep_score" ON "sleep_nights" ("score" COLLATE BINARY DESC, "night" '
                                   'COLLATE NOCASE)')
    # A shared table's view gets an index on its key columns, made by easy-ui from the column names only (read
    # through the same authorizer that allows only reading).
    assert rows["days_key"] == 'CREATE INDEX "days_key" ON "days" ("day")'
    assert "activities_key" in rows


def test_a_view_that_never_ends_stops_the_build_in_time(demo_app):
    _add_view(demo_app, "forever", "WITH RECURSIVE r(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM r) SELECT i FROM r")
    cfg = demo_app.config()
    assert "forever" in cfg.tables
    box = finishes_within(20, bundle.build, cfg, time_limit_s=1.0)
    assert isinstance(box.get("error"), DataBuildError), box
    assert "longer than 1 seconds" in box["error"].message
    assert not [p for p in cfg.state.data.iterdir() if p.name.startswith(".tmp-")]


def test_the_copy_cannot_grow_without_bound(demo_app, monkeypatch):
    monkeypatch.setattr(bundle, "MIN_SIZE_LIMIT", 1_000_000)
    _add_view(demo_app, "huge", "SELECT a.day, printf('%.*c', 2000, 'x') AS pad FROM days a, days b")
    cfg = demo_app.config()
    assert "huge" in cfg.tables
    with pytest.raises(DataBuildError) as e:
        bundle.build(cfg)
    assert "larger than" in e.value.message


def test_reading_the_project_file_is_limited_to_reading(demo_app):
    guard = bundle._SourceGuard(["days"], 10)
    conn = bundle._connect_ro(demo_app.db_path, immutable=False)
    conn.set_authorizer(guard.authorize)
    try:
        assert conn.execute("SELECT count(*) FROM days").fetchone()[0] == 120
        for sql in ("SELECT * FROM private_notes", "SELECT * FROM main.private_notes", "ATTACH ':memory:' AS x",
                    "PRAGMA writable_schema = ON", "SELECT load_extension('x')"):
            with pytest.raises(sqlite3.DatabaseError):
                conn.execute(sql).fetchall()
        # the connection itself is defensive, with untrusted schema and no extensions
        conn.set_authorizer(None)
        assert conn.getconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE) and not conn.getconfig(sqlite3.SQLITE_DBCONFIG_TRUSTED_SCHEMA)
    finally:
        conn.close()


def test_stale_temporary_files_are_removed_when_a_build_starts(built_app):
    cfg = built_app.config()
    stale = cfg.state.data / ".tmp-0123456789ab.sqlite"
    stale.write_bytes(b"left by a build that was killed")
    bundle.build(cfg)
    assert not stale.exists()


def test_sigterm_during_a_build_removes_its_temporary_file(demo_app, tmp_path):
    _add_view(demo_app, "slow", "WITH RECURSIVE r(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM r) SELECT i FROM r")
    script = tmp_path / "build.py"
    script.write_text(
        "import sys\nfrom easyui import appconfig, bundle\n"
        f"cfg = appconfig.load({str(demo_app.app_dir)!r})\n"
        "assert 'slow' in cfg.tables\n"
        "print('go', flush=True)\nbundle.build(cfg, time_limit_s=60)\n"
    )
    proc = subprocess.Popen([sys.executable, str(script)], env={**os.environ, **demo_app.env()}, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    try:
        assert proc.stdout.readline().strip() == b"go"
        data = demo_app.config().state.data
        deadline = time.monotonic() + 10
        while not [p for p in data.iterdir() if p.name.startswith(".tmp-") and p.suffix == ".sqlite"]:
            assert time.monotonic() < deadline, "the build never started"
            time.sleep(0.05)
        proc.send_signal(signal.SIGTERM)
        proc.wait(10)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert not [p.name for p in data.iterdir() if p.name.startswith(".tmp-") and p.suffix == ".sqlite"]


def test_tables_shows_sample_values_only_for_listed_tables(built_app):
    code, data = run_cli_json("tables", "--source")
    assert code == 0
    by = {t["name"]: t for t in data["tables"]}
    assert by["private_notes"].get("samples_hidden") and all(not c["samples"] for c in by["private_notes"]["columns"])
    assert any(c["samples"] for c in by["days"]["columns"])
    code, out, _ = run_cli("tables", "--source")
    assert "synthetic secret" not in out and "not for dashboards" not in out and "--all-tables" in out
    code, out, _ = run_cli("tables", "--source", "--all-tables")
    assert code == 0 and ("synthetic secret" in out or "not for dashboards" in out)


def test_a_wider_table_list_is_shown_on_build_and_publish(built_app):
    publish.publish(built_app.config())
    # A new [views] entry widens what dashboards may read (here: a view of the table no dashboard may read).
    _add_view(built_app, "notes", "SELECT id, note FROM private_notes")
    assert "notes" in built_app.config().tables
    code, out, _ = run_cli("data", "build")
    assert code == 0 and "+ notes" in out
    code, out, _ = run_cli("publish")
    assert code == 0 and "+ notes" in out


# ---------------------------------------------------------------- 6. an export holds exactly what it lists


SHOWN_YAML = """\
spec: easy-ui/v1
title: Shown
params:
  kind: {type: choice, label: Kind, default: run, options: [run, ride, walk]}
  n: {type: int, label: N, default: 3, min: 1, max: 9}
queries:
  rows:
    sql: SELECT day, steps, distance_m, active_s FROM days ORDER BY day LIMIT :n * 10
  one:
    sql: SELECT count(*) AS n, sum(steps) AS steps, 'extra' AS unused FROM days WHERE :kind = :kind
layout:
  - - type: table
      title: Some
      query: rows
      columns: [day, steps]
      limit: 3
    - type: stat
      title: Days
      query: one
      value: n
      hint: How many days.
"""


def test_export_holds_only_the_columns_rows_and_options_shown(built_app, tmp_path):
    built_app.write_dashboard("shown", SHOWN_YAML)
    res = export.export(built_app.config(), out=tmp_path / "s", working=True, dashboards=["shown"])
    p = json.loads(re.search(r'id="easyui-export">(.*?)</script>', (tmp_path / "s" / "index.html").read_text(), re.S).group(1))
    results = p["data"]["shown"]["results"]
    assert results["rows"]["columns"] == ["day", "steps"] and len(results["rows"]["rows"]) == 3
    assert results["one"]["columns"] == ["n"] and len(results["one"]["rows"]) == 1
    params = {x["name"]: x for x in p["specs"]["shown"]["params"]}
    assert params["kind"]["options"] == [{"value": "run", "label": "run"}]
    assert "min" not in params["n"] and "max" not in params["n"]
    assert res["visible"][0]["queries"][0]["columns"] == ["day", "steps"]


def _leaves(node, path=()):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _leaves(v, (*path, k))
    elif isinstance(node, list):
        for v in node:
            yield from _leaves(v, path)
    elif isinstance(node, (str, int, float)) and not isinstance(node, bool):
        yield path, node


THEMED_YAML = SHOWN_YAML.replace("title: Shown\n", "title: Shown\ntheme:\n  colors:\n    accent: \"#a2451d\"\n").replace(
    "    - type: stat\n      title: Days\n", "    - type: stat\n      title: Days\n      id: day-count\n      class: wide highlight\n")


def _payload_of(page: str) -> dict:
    return json.loads(re.search(r'id="easyui-export">(.*?)</script>', page, re.S).group(1))


def _assert_every_piece_is_listed(p: dict, lines: list[str]) -> None:
    listed = "\n".join(lines)
    assert lines == export.describe_payload(p)
    missing = []
    for path, value in _leaves(p):
        if path == ("v",) or (len(path) >= 1 and path[-1] in export._STRUCTURE):
            continue
        if "results" in path and path[-1] == "rows":
            continue  # rows are counted below, not listed one by one
        if str(value) not in listed:
            missing.append((path, value))
    assert not missing, missing[:5]
    for slug, data in p["data"].items():
        for name, r in data["results"].items():
            assert f"numbers of {name}: {len(r['rows'])} row(s) of {', '.join(r['columns'])}" in listed, (slug, name)
    for f in p["styles"]["files"]:  # every file of the look, by name
        assert re.search(rf"(?:: |, ){re.escape(f)}(?:,|\.$)", listed, re.M), f


def test_export_confirmation_lists_every_piece_of_the_payload(published, tmp_path):
    from test_styles import make_look  # noqa: PLC0415 - a theme, a stylesheet, a font and an SVG picture

    make_look(published.app_dir / "ui")
    published.write_dashboard("shown", THEMED_YAML)
    res = export.export(published.config(), out=tmp_path / "s", working=True)
    p = _payload_of((tmp_path / "s" / "index.html").read_text())
    _assert_every_piece_is_listed(p, res["lines"])
    # The look, the dashboard's own theme, a component's id and classes and the apps' sections are all listed.
    look = p["styles"]["files"]
    assert {"ui/theme.css", "ui/styles/a.css", "ui/assets/fonts/notes.woff2", "ui/assets/logo.svg",
            "ui/apps/demo-sleep/styles/sleep.css"} <= set(look)
    assert look == res["look"] == sorted(f for f in res["files"] if f.startswith("ui/"))  # exactly what was written
    listed = "\n".join(res["lines"])
    assert "never locked" in listed and "#a2451d" in listed and "class wide, highlight" in listed
    assert "app demo-moves" in listed and "app demo-sleep" in listed
    # The single file lists what it holds: the stylesheets, and only the fonts and pictures they name.
    one = export.export(published.config(), out=tmp_path / "one.html", working=True, single_file=True, encrypt=True)
    page = (tmp_path / "one.html").read_text()
    box = _payload_of(page)
    assert box.get("enc") == "A256GCM"
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: PLC0415

    key = base64.urlsafe_b64decode(one["fragment"][3:] + "=")
    plain = AESGCM(key).decrypt(base64.urlsafe_b64decode(box["iv"] + "=="), base64.urlsafe_b64decode(box["ct"] + "=="),
                                export.AAD)
    q = json.loads(plain)
    q.pop("encrypted")
    _assert_every_piece_is_listed(q, one["lines"])
    inline = json.loads(re.search(r'id="easyui-look">(.*?)</script>', page, re.S).group(1))
    assert sorted(inline) == [f for f in q["styles"]["files"] if "/assets/" not in f] and q["styles"]["embedded"]
    assert "ui/assets/fonts/notes.woff2" in q["styles"]["files"] and "data:font/woff2;base64," in inline["ui/theme.css"]
    assert "data:image/svg+xml;base64," in inline["ui/styles/a.css"] and "../assets/" not in "".join(inline.values())
    code, out, _ = run_cli("export", "--out", str(tmp_path / "t"))
    assert code == 0 and "exactly this, and nothing else" in out and "Demo project" in out


def test_a_single_file_refuses_a_look_it_cannot_carry(published, tmp_path):
    from test_styles import make_look  # noqa: PLC0415

    make_look(published.app_dir / "ui")
    (published.app_dir / "ui" / "styles" / "b.css").write_text('@import "a.css";\n.eui-title { color: red; }\n')
    with pytest.raises(Refused) as e:
        export.export(published.config(), out=tmp_path / "one.html", working=True, single_file=True)
    assert e.value.code == "export_look_not_embeddable" and "ui/styles/b.css" in e.value.message
    assert not (tmp_path / "one.html").exists()
    export.export(published.config(), out=tmp_path / "folder", working=True)  # a folder carries it as files


# ---------------------------------------------------------------- 7. a WITH name hiding a real table


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT count(*) FROM sqlite_master WHERE 1 IN (WITH sqlite_master AS (SELECT 1) SELECT 1)",
        "SELECT count(*) FROM sqlite_schema WHERE 1 IN (WITH sqlite_schema AS (SELECT 1) SELECT 1)",
        "SELECT count(*) FROM pragma_database_list WHERE 1 IN (WITH pragma_database_list AS (SELECT 1) SELECT 1)",
        "SELECT count(*) FROM dbstat WHERE 1 IN (WITH dbstat AS (SELECT 1) SELECT 1)",
        "SELECT count(*) FROM sqlite_dbpage WHERE 1 IN (WITH sqlite_dbpage AS (SELECT 1) SELECT 1)",
        "WITH SQLite_Master AS (SELECT 1) SELECT count(*) FROM main.sqlite_master",
        "SELECT count(*) FROM days WHERE 1 IN (WITH days AS (SELECT 1) SELECT 1)",
        'WITH "json_each" AS (SELECT 1) SELECT 1',
    ],
)
def test_a_with_name_can_never_hide_a_real_table(bundle_path, sql):
    with pytest.raises(QueryRefused):
        sandbox.run_query(bundle_path, sql)
    assert sandbox.scan_sql(sql).problems or "days" in sql  # caught before SQLite sees it (or by the sandbox)


def test_with_parts_still_work(bundle_path):
    assert sandbox.run_query(bundle_path, "WITH x AS (SELECT * FROM days) SELECT count(*) FROM x").rows == [[120]]
    assert sandbox.run_query(
        bundle_path, "WITH x AS MATERIALIZED (SELECT * FROM days) SELECT count(*) FROM x").rows == [[120]]


# ---------------------------------------------------------------- 8. cookie flags


def test_cookie_flags_follow_the_configured_origin_not_a_header(env):
    https_site = env.make(client=("127.0.0.1", 40000), origins=("https://localhost",))
    r = login(https_site, origin="https://localhost")  # the proxy forgot X-Forwarded-Proto
    assert r.status_code == 200
    assert all(k.startswith("__Host-") for k in r.cookies) and "secure" in r.headers["set-cookie"].lower()
    plain = env.make(client=("127.0.0.1", 40000))  # http://localhost, the local origin
    r = login(plain, **{"x-forwarded-proto": "https"})
    assert r.status_code == 200
    assert not any(k.startswith("__Host-") for k in r.cookies) and "strict-transport-security" not in r.headers
    assert not security.is_https({"scheme": "http", "client": ("127.0.0.1", 1), "headers": [(b"x-forwarded-proto", b"https")]})


# ---------------------------------------------------------------- 9. blowing up while being prepared


def test_nested_with_parts_are_refused_before_sqlite_prepares_them(bundle_path):
    n = 40
    sql = "WITH " + ", ".join(
        f"c{i} AS (SELECT * FROM c{i - 1} UNION ALL SELECT * FROM c{i - 1})" if i else "c0 AS (SELECT 1 x)" for i in range(n)
    ) + f" SELECT count(*) FROM c{n - 1}"
    box = finishes_within(10, sandbox.run_query, bundle_path, sql)
    assert isinstance(box.get("error"), QueryRefused) and box["elapsed"] < 1


def test_running_out_of_memory_while_preparing_is_a_plain_refusal(bundle_path):
    sql = "WITH " + ", ".join(
        f"c{i} AS (" + " UNION ALL ".join([f"SELECT * FROM c{i - 1}"] * 4) + ")" if i else "c0 AS (SELECT 1 x)"
        for i in range(10)
    ) + " SELECT count(*) FROM c9"
    previous = sandbox.set_heap_limit(sandbox.HEAP_LIMIT)
    try:
        with pytest.raises(QueryRefused) as e:
            sandbox.run_query(bundle_path, sql)
    finally:
        sandbox.set_heap_limit(previous)
    assert "memory" in e.value.message or "complex" in e.value.message


def test_checks_prepare_queries_in_a_worker(bundle_path, pool):
    dash = dashboard("SELECT count(*) AS n FROM days")
    errors, _ = spec.check_with_data(dash, bundle_path, pool=pool)
    assert not errors and pool.stats()["started"] == 1


# ---------------------------------------------------------------- 10. rev and a linked specs folder


def test_rev_never_follows_a_linked_specs_folder(built_app, tmp_path):
    cfg = built_app.config()
    st = cfg.state
    shutil.rmtree(st.specs)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "a").write_text("x")
    st.specs.symlink_to(elsewhere, target_is_directory=True)
    before = bundle.rev(st)
    (elsewhere / "b").write_text("y")
    assert bundle.rev(st) == before


def test_rev_reads_a_bounded_number_of_entries(built_app, monkeypatch):
    st = built_app.config().state
    monkeypatch.setattr(bundle, "MAX_REV_ENTRIES", 5)
    for i in range(20):
        (st.specs / f"f{i:02d}").write_text("x")
    before = bundle.rev(st)
    (st.specs / "zz-after-the-cap").write_text("x")
    assert bundle.rev(st) == before


# ---------------------------------------------------------------- 11. validate reads like publish


def test_validate_refuses_links_and_fifos_without_hanging(demo_app, tmp_path):
    d = demo_app.app_dir / "ui" / "dashboards"
    secret = tmp_path / "secret.yaml"
    secret.write_text("spec: easy-ui/v1\ntitle: SECRET-CONTENT\nlayout: []\n")
    (d / "linked.yaml").symlink_to(secret)
    os.mkfifo(d / "pipe.yaml")
    big = d / "big.yaml"
    big.write_bytes(b"#" * (spec.LIMITS["file_bytes"] + 10))
    box = finishes_within(10, spec.validate_app, demo_app.config(), check_data=False)
    report = box["value"]
    by = {e["file"]: e for e in report["dashboards"]}
    assert "link" in by["ui/dashboards/linked.yaml"]["errors"][0]["problem"]
    assert "not a plain file" in by["ui/dashboards/pipe.yaml"]["errors"][0]["problem"]
    assert "bytes" in by["ui/dashboards/big.yaml"]["errors"][0]["problem"]
    assert "SECRET-CONTENT" not in json.dumps(report)
    for f in ("linked.yaml", "pipe.yaml"):
        box = finishes_within(10, spec.validate_file, d / f)
        assert box["value"]["errors"] and not box["value"]["ok"]
    assert isinstance(finishes_within(10, spec.load_file, d / "pipe.yaml").get("error"), SpecError)
    with pytest.raises(SpecError):
        spec.load_file(d / "linked.yaml")


# ---------------------------------------------------------------- 12. the smaller ones


def test_published_titles_come_from_the_checked_files_not_the_manifest(published):
    cfg = published.config()
    vdir = publish.published_dir(cfg)
    m = vdir / "manifest.json"
    os.chmod(vdir, 0o700)
    os.chmod(m, 0o600)
    data = json.loads(m.read_text())
    data["dashboards"][0]["title"] = "<img src=x onerror=alert(1)>"
    data["dashboards"][0]["description"] = "planted"
    m.write_text(json.dumps(data))
    slug = data["dashboards"][0]["slug"]
    real = publish.load_published_dashboard(cfg, slug)
    listed = {d["slug"]: d for d in publish.list_published(cfg)}
    assert listed[slug]["title"] == real.title != data["dashboards"][0]["title"]
    assert listed[slug]["description"] == real.description != "planted"


def test_published_files_are_0440_and_check_flags_unexpected_groups(built_app, tmp_path, monkeypatch):
    from test_styles import make_look  # noqa: PLC0415 - the look's files follow the same rule

    make_look(built_app.app_dir / "ui")
    res = publish.publish(built_app.config())
    assert {"theme.yaml", "styles/a.css", "assets/fonts/notes.woff2", "apps/demo-sleep/styles/sleep.css"} <= {
        s["path"] for s in res["styles"]}
    st = built_app.config().state
    assert (publish.published_dir(st) / "assets" / "fonts" / "notes.woff2").is_file()
    assert (publish.published_dir(st) / "project" / "ui" / "dashboards").is_dir()
    for p in [*st.data.iterdir(), *st.specs.rglob("*")]:
        if p.is_file() and not p.is_symlink() and not p.name.endswith(".lock"):
            assert stat.S_IMODE(p.stat().st_mode) == 0o440, p
    for p in [x for x in st.specs.rglob("*") if x.is_dir()]:
        assert stat.S_IMODE(p.stat().st_mode) in (0o700, 0o750), p
    # a shared ui folder whose group is not the web account's is flagged
    os.chmod(st.root, 0o750)
    report = server.run_checks(built_app.config())
    item = next(i for i in report["items"] if i["id"] == "ui_folder_group")
    assert item["status"] == "fail" and "mes-demo-web" in item["why"]


def test_preview_link_is_not_used_up_by_head(env):
    asgi = server.create_app(env.cfg, server.ServerSettings(web_dir=env.web_dir, local_port=8750),
                             source=DirSource(env.cfg), preview_token="tok-" + "c" * 30)
    from starlette.testclient import TestClient

    c = TestClient(asgi, base_url="http://127.0.0.1:8750", follow_redirects=False)
    r = c.head("/?token=tok-" + "c" * 30)
    assert "set-cookie" not in r.headers and r.status_code != 303
    assert c.get("/?token=tok-" + "c" * 30).status_code == 303  # still works for the real visit


def test_every_export_asks_not_to_be_indexed(published, tmp_path):
    export.export(published.config(), out=tmp_path / "plain")
    assert 'name="robots" content="noindex' in (tmp_path / "plain" / "index.html").read_text()


def test_link_key_is_never_stored_and_a_plain_copy_never_reloads_forever():
    js = (WEB / "assets" / "static-api.js").read_text()
    assert "Storage" not in js and "let currentKey = null" in js
    app = (WEB / "assets" / "app.js").read_text()
    assert "api.keyInAddress()" in app and "/^#k=/.test(window.location.hash)) {\n    window.location.reload" not in app


def _browser():
    from easyui import screenshot

    return screenshot.find_browser()


@pytest.mark.skipif(not _browser(), reason="needs a Chromium-based browser")
def test_a_plain_copy_opened_with_a_key_shows_its_dashboards(published, tmp_path):
    from easyui import screenshot

    export.export(published.config(), out=tmp_path / "plain")
    box = finishes_within(40, screenshot.capture, tmp_path / "plain" / "index.html", out=tmp_path / "x.png", width=800,
                          height=600, dark=False, browser=_browser(), fragment="#k=" + "A" * 43, timeout=25)
    assert "error" not in box, box.get("error")
    assert box["value"]["page_title"].startswith("Overview")


def test_echarts_file_is_checked_by_hash(published, tmp_path):
    manifest = json.loads((WEB / "assets" / "vendor" / "MANIFEST.json").read_text())
    info = manifest["files"]["echarts.min.js"]
    data = (WEB / "assets" / "vendor" / "echarts.min.js").read_bytes()
    integrity = "sha384-" + base64.b64encode(hashlib.sha384(data).digest()).decode()
    assert info["integrity"] == integrity
    assert f'ECHARTS_INTEGRITY = "{integrity}"' in (WEB / "assets" / "charts.js").read_text()
    security.verify_vendored(WEB)
    bad = tmp_path / "web"
    shutil.copytree(WEB, bad)
    (bad / "assets" / "vendor" / "echarts.min.js").write_bytes(data + b"\n;window.leak=1;")
    with pytest.raises(Refused) as e:
        security.verify_vendored(bad)
    assert e.value.code == "vendor_changed"
    with pytest.raises(Refused):
        export.export(published.config(), out=tmp_path / "x", web_dir=bad)


def test_signals_raise_inside_cleanup_on_signals_so_temporary_files_go():
    cleaned = []
    with pytest.raises(paths.StopRequested):
        with paths.cleanup_on_signals():
            try:
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(1)
            finally:
                cleaned.append(True)
    assert cleaned == [True]
    assert signal.getsignal(signal.SIGTERM) is not None
    import inspect

    from easyui import screenshot

    assert "cleanup_on_signals" in inspect.getsource(screenshot.screenshot)


def test_check_says_whether_query_workers_can_start(built_app):
    report = server.run_checks(built_app.config())
    item = next(i for i in report["items"] if i["id"] == "query_workers")
    assert item["status"] == "ok"

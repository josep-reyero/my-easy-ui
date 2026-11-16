"""The query sandbox against hostile SQL. Every layer is tested on its own where possible."""

from __future__ import annotations

import hashlib
import sqlite3
import time

import pytest

from easyui import sandbox
from easyui.errors import ExitCode, NoData, ParamError, QueryError, QueryRefused, QueryTimeout
from easyui.sandbox import Limits, Sandbox, run_query, scan_sql

FAST = Limits(timeout_s=0.3, max_rows=5000, max_bytes=5_000_000)


def sha(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------- layer 1: static scan


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "select day from days",
        "WITH x AS (SELECT 1 AS a) SELECT a FROM x",
        "VALUES (1), (2)",
        "SELECT 1;",
        "SELECT 1; -- trailing comment",
        "SELECT 1 /* ; DROP TABLE days */",
        "SELECT ';' AS semi, 'it''s' AS q",
        "SELECT \"day\" FROM days -- ; DELETE FROM days",
        "SELECT [day] FROM days",
        "SELECT x'00ff' AS b",
        "SELECT :since AS s, :since AS again",
    ],
)
def test_scan_accepts_single_selects(sql):
    assert scan_sql(sql).problems == []


@pytest.mark.parametrize(
    "sql, words",
    [
        ("SELECT 1; SELECT 2", "more than one statement"),
        ("SELECT 1; DROP TABLE days", "more than one statement"),
        ("SELECT 1;\nATTACH 'x.db' AS x", "more than one statement"),
        ("ATTACH DATABASE '/tmp/evil.db' AS evil", "starts with ATTACH"),
        ("DETACH evil", "starts with DETACH"),
        ("PRAGMA table_info(days)", "starts with PRAGMA"),
        ("PRAGMA writable_schema = 1", "starts with PRAGMA"),
        ("INSERT INTO days (day) VALUES ('x')", "starts with INSERT"),
        ("REPLACE INTO days (day) VALUES ('x')", "starts with REPLACE"),
        ("UPDATE days SET steps = 0", "starts with UPDATE"),
        ("DELETE FROM days", "starts with DELETE"),
        ("DROP TABLE days", "starts with DROP"),
        ("CREATE TABLE t (a)", "starts with CREATE"),
        ("CREATE VIRTUAL TABLE t USING fts5(x)", "starts with CREATE"),
        ("ALTER TABLE days ADD COLUMN x", "starts with ALTER"),
        ("VACUUM INTO '/tmp/copy.db'", "starts with VACUUM"),
        ("EXPLAIN SELECT 1", "starts with EXPLAIN"),
        ("BEGIN", "starts with BEGIN"),
        ("ANALYZE", "starts with ANALYZE"),
        ("SELECT ?", "'?'"),
        ("SELECT ?1", "'?1'"),
        ("SELECT @name", "'@name'"),
        ("SELECT $name", "'$name'"),
        ("SELECT :Name", "parameter :Name"),
        ("SELECT 'unclosed", "never closed"),
        ('SELECT "unclosed', "never closed"),
        ("SELECT 1 /* never ends", "never ends"),
        ("SELECT [x", "never closed"),
        ("", "empty"),
        ("   -- only a comment", "no statement"),
        ("SELECT 1\x00", "NUL"),
        ("SELECT " + "1," * 20_000 + "1", "longer than"),
    ],
)
def test_scan_refuses(sql, words):
    problems = scan_sql(sql).problems
    assert problems, sql
    assert words in problems[0]


def test_scan_lists_params_in_order_and_ctes():
    s = scan_sql("WITH a AS (SELECT :x), b(n) AS NOT MATERIALIZED (SELECT :y) SELECT :x, cast(1 AS integer)")
    assert s.params == ["x", "y"]
    assert s.ctes == ["a", "b"]


def test_scan_never_raises_on_non_text():
    assert scan_sql(None).problems
    assert scan_sql(42).problems


# ---------------------------------------------------------------- working queries


def test_plain_select(bundle_path):
    r = run_query(bundle_path, "SELECT day, steps FROM days ORDER BY day LIMIT 3")
    assert r.columns == ["day", "steps"]
    assert [row[0] for row in r.rows] == ["2026-01-01", "2026-01-02", "2026-01-03"]
    assert r.truncated is False
    assert r.to_dict() == {"columns": r.columns, "rows": r.rows, "truncated": False}


def test_bound_params_and_null(bundle_path):
    r = run_query(bundle_path, "SELECT count(*) FROM days WHERE day >= :since", {"since": "2026-04-01"})
    assert r.rows == [[30]]
    r = run_query(bundle_path, "SELECT count(*) FROM days WHERE day >= :since", {"since": None})
    assert r.rows == [[0]]


def test_ctes_windows_json_and_recursive_counts_are_allowed(bundle_path):
    ok = [
        "WITH w AS (SELECT day, steps FROM days) SELECT count(*) FROM w",
        "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 10) SELECT count(*), sum(n) FROM r",
        "SELECT day, sum(steps) OVER (ORDER BY day ROWS 6 PRECEDING) AS rolling FROM days LIMIT 5",
        "SELECT value FROM json_each('[1, 2, 3]')",
        "SELECT kind, count(*) FROM activities GROUP BY kind",
        "SELECT (SELECT count(*) FROM days) AS n",
        "SELECT week, distance_m FROM weekly",
        "SELECT strftime('%Y', day), round(avg(resting_hr), 1) FROM days GROUP BY 1",
    ]
    for sql in ok:
        run_query(bundle_path, sql)


def test_blobs_and_infinities_become_null(bundle_path):
    r = run_query(bundle_path, "SELECT x'00ff' AS b, 1e999 AS inf, -1e999 AS ninf, 1.5 AS f")
    assert r.rows == [[None, None, None, 1.5]]


# ---------------------------------------------------------------- layer 3: the authorizer


@pytest.mark.parametrize(
    "sql, words",
    [
        ("SELECT * FROM sqlite_master", "sqlite_master"),
        ("SELECT * FROM sqlite_schema", "sqlite_"),
        ("SELECT name FROM main.sqlite_master", "sqlite_master"),
        ("SELECT * FROM sqlite_temp_master", "sqlite_temp_master"),
        ("SELECT * FROM pragma_table_info('days')", "PRAGMA"),
        ("SELECT * FROM pragma_database_list", "PRAGMA"),
        ("SELECT * FROM pragma_compile_options", "PRAGMA"),
        ("SELECT * FROM dbstat", "dbstat"),
        ("SELECT load_extension('/tmp/evil.so')", "load_extension"),
        ("SELECT load_extension('/tmp/evil.so', 'entry')", "load_extension"),
        ("SELECT sqlite_compileoption_get(0)", "sqlite_compileoption_get"),
        ("WITH a AS (SELECT 1) DELETE FROM days", "change data"),
        ("WITH a AS (SELECT 1) INSERT INTO days (day) VALUES ('x')", "change data"),
        ("WITH a AS (SELECT 1) UPDATE days SET steps = 0", "change data"),
        ("WITH sqlite_master AS (SELECT 1) SELECT count(*) FROM main.sqlite_master", "sqlite_master"),
    ],
)
def test_authorizer_refuses(bundle_path, sql, words):
    before = sha(bundle_path)
    with pytest.raises(QueryRefused) as e:
        run_query(bundle_path, sql)
    assert words in e.value.message
    assert e.value.exit_code == ExitCode.REFUSED
    assert e.value.http_status == 400
    assert "SELECT" in e.value.hint  # says what to do instead
    assert sha(bundle_path) == before


@pytest.mark.parametrize("fn", sorted(sandbox.DENIED_FUNCTIONS))
def test_denied_functions_are_refused_even_when_present(bundle_path, fn):
    """Simulate a SQLite build that has readfile/writefile/edit/fts3_tokenizer/... compiled in."""
    with Sandbox(bundle_path, FAST) as sb:
        sb.conn.create_function(fn, -1, lambda *a: "leaked")
        with pytest.raises(QueryRefused) as e:
            sb.run(f"SELECT {fn}('/etc/hosts')")
        assert fn in e.value.message


@pytest.mark.parametrize("fn", ["readfile", "writefile", "edit", "fts3_tokenizer", "zipfile"])
def test_file_functions_never_work(bundle_path, fn):
    with pytest.raises((QueryRefused, QueryError)):
        run_query(bundle_path, f"SELECT {fn}('/etc/hosts')")


def test_authorizer_catches_what_the_scan_would_miss(bundle_path, monkeypatch):
    """Defence in depth: with the static scan switched off, SQLite itself still refuses."""
    monkeypatch.setattr(sandbox, "scan_sql", lambda sql: sandbox.SqlScan(keyword="SELECT"))
    before = sha(bundle_path)
    for sql in [
        "ATTACH DATABASE ':memory:' AS evil",
        "PRAGMA table_info(days)",
        "PRAGMA writable_schema = ON",
        "DELETE FROM days",
        "INSERT INTO days (day) VALUES ('x')",
        "CREATE TABLE evil (a)",
        "CREATE TEMP TABLE evil (a)",
        "DROP TABLE days",
        "CREATE VIRTUAL TABLE evil USING fts5(x)",
        "BEGIN",
        "SAVEPOINT a",
        "ANALYZE",
        "REINDEX",
    ]:
        with pytest.raises((QueryRefused, QueryError)) as e:
            run_query(bundle_path, sql)
        assert not isinstance(e.value, QueryError) or "not authorized" in (e.value.detail or "") or "readonly" in (
            e.value.detail or ""
        ), sql
    with pytest.raises((QueryRefused, QueryError)):
        run_query(bundle_path, "SELECT 1; DELETE FROM days")  # sqlite3 itself allows one statement only
    assert sha(bundle_path) == before


def test_attach_is_also_limited_to_zero_databases(bundle_path):
    with Sandbox(bundle_path) as sb:
        assert sb.conn.getlimit(sqlite3.SQLITE_LIMIT_ATTACHED) == 0


def test_extension_loading_is_switched_off(bundle_path):
    with Sandbox(bundle_path) as sb:
        sb.conn.set_authorizer(None)  # even without the authorizer
        with pytest.raises(sqlite3.OperationalError):
            sb.conn.execute("SELECT load_extension('/nonexistent/evil')").fetchall()


def test_connection_is_read_only_even_without_authorizer(bundle_path):
    before = sha(bundle_path)
    with Sandbox(bundle_path) as sb:
        sb.conn.set_authorizer(None)
        with pytest.raises(sqlite3.Error):
            sb.conn.execute("DELETE FROM days")
        with pytest.raises(sqlite3.Error):
            sb.conn.execute("CREATE TABLE evil (a)")
    assert sha(bundle_path) == before


def test_tables_outside_the_allowlist_do_not_exist(bundle_path):
    with pytest.raises(QueryError) as e:
        run_query(bundle_path, "SELECT * FROM private_notes")
    assert "not in the dashboard data" in e.value.message
    assert "activities, days, weekly" in e.value.hint


def test_readable_tables_are_exactly_the_bundle(bundle_path):
    with Sandbox(bundle_path) as sb:
        assert sb.tables == {"days", "activities", "weekly"}


# ---------------------------------------------------------------- layer 5: time, rows, size


def test_recursive_cte_runaway_is_stopped_by_time(bundle_path):
    started = time.monotonic()
    with pytest.raises(QueryTimeout) as e:
        run_query(
            bundle_path,
            "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT count(*) FROM r",
            limits=FAST,
        )
    assert time.monotonic() - started < 2.0
    assert e.value.exit_code == ExitCode.TEMPORARY
    assert e.value.http_status == 504
    assert "0.3 seconds" in e.value.message


def test_cross_join_runaway_is_stopped_by_time(bundle_path):
    with pytest.raises(QueryTimeout):
        run_query(bundle_path, "SELECT count(*) FROM days a, days b, days c, days d", limits=FAST)


def test_runaway_rows_are_truncated(bundle_path):
    r = run_query(
        bundle_path,
        "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT n FROM r",
        limits=Limits(timeout_s=2, max_rows=100),
    )
    assert len(r.rows) == 100 and r.truncated and r.truncated_reason == "rows"


def test_default_row_limit_is_5000(bundle_path):
    r = run_query(bundle_path, "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT n FROM r")
    assert len(r.rows) == 5000 and r.truncated


def test_result_size_limit(bundle_path):
    r = run_query(
        bundle_path,
        "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 1000) "
        "SELECT n, printf('%.2000c', 'x') AS big FROM r",
        limits=Limits(max_bytes=100_000),
    )
    assert r.truncated and r.truncated_reason == "size"
    assert 0 < len(r.rows) < 60


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT randomblob(100000000)",
        "SELECT zeroblob(100000000)",
        "SELECT hex(zeroblob(4000000))",
        "SELECT group_concat(printf('%.1000c', 'x'), '') FROM (WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL "
        "SELECT n + 1 FROM r WHERE n < 10000) SELECT n FROM r)",
    ],
)
def test_huge_values_are_refused(bundle_path, sql):
    with pytest.raises((QueryRefused, QueryError)) as e:
        run_query(bundle_path, sql, limits=FAST)
    assert "too" in e.value.message


def test_printf_cannot_build_huge_text(bundle_path):
    r = run_query(bundle_path, "SELECT printf('%.*c', 100000000, 'x') AS v", limits=FAST)
    assert len(r.rows[0][0] or "") <= 5_000_000


# ---------------------------------------------------------------- layer 4: params


@pytest.mark.parametrize(
    "value",
    [
        "'; DROP TABLE days; --",
        "x' OR '1'='1",
        "1 OR 1=1",
        "); ATTACH DATABASE '/tmp/x' AS x; --",
        ":since",
        "\\'",
    ],
)
def test_params_cannot_smuggle_sql(bundle_path, value):
    before = sha(bundle_path)
    r = run_query(bundle_path, "SELECT :v AS v, (SELECT count(*) FROM days WHERE day = :v) AS n", {"v": value})
    assert r.rows == [[value, 0]]
    assert sha(bundle_path) == before


@pytest.mark.parametrize("value", [[1, 2], {"a": 1}, b"bytes", True, float("nan"), 2**70, "x" * 20_000, "a\x00b"])
def test_unusable_param_values_are_refused(bundle_path, value):
    with pytest.raises(ParamError):
        run_query(bundle_path, "SELECT :v", {"v": value})


def test_missing_param_is_a_plain_error(bundle_path):
    with pytest.raises(QueryError) as e:
        run_query(bundle_path, "SELECT :v", {}, name="q")
    assert ":v" in e.value.message and 'query "q"' in e.value.message


def test_extra_param_names_are_ignored_not_executed(bundle_path):
    r = run_query(bundle_path, "SELECT :v", {"v": 1, "v); DROP TABLE days; --": 2})
    assert r.rows == [[1]]


def test_params_must_be_a_mapping(bundle_path):
    with pytest.raises(ParamError):
        run_query(bundle_path, "SELECT :v", ["x"])  # type: ignore[arg-type]


# ---------------------------------------------------------------- plain-language errors


def test_typo_in_table_suggests_closest(bundle_path):
    with pytest.raises(QueryError) as e:
        run_query(bundle_path, "SELECT * FROM dayz", name="totals")
    assert 'Did you mean "days"' in e.value.message
    assert e.value.detail == "no such table: dayz"


def test_double_quoted_text_is_explained(bundle_path):
    with pytest.raises(QueryError) as e:
        run_query(bundle_path, 'SELECT * FROM activities WHERE kind = "run"')
    assert "single quotes" in e.value.hint


def test_syntax_error_is_explained(bundle_path):
    with pytest.raises(QueryError) as e:
        run_query(bundle_path, "SELECT FROM WHERE")
    assert "mistake near" in e.value.message


def test_missing_bundle_is_no_data(tmp_path):
    with pytest.raises(NoData) as e:
        run_query(tmp_path / "nope.sqlite", "SELECT 1")
    assert "easy-ui data build" in e.value.hint


def test_query_columns(bundle_path):
    cols = sandbox.query_columns(bundle_path, "SELECT day, steps AS s FROM days WHERE day > :since")
    assert cols == ["day", "s"]


def test_heap_limit_can_be_set_and_restored(bundle_path):
    previous = sandbox.set_heap_limit(64_000_000)
    try:
        assert sandbox.set_heap_limit(64_000_000) == 64_000_000
        assert run_query(bundle_path, "SELECT count(*) FROM days").rows == [[120]]
        # a big sort spills to temporary files instead of memory, and the time limit still applies
        with pytest.raises(QueryTimeout):
            run_query(
                bundle_path,
                "SELECT a.day, b.day, c.day FROM days a, days b, days c ORDER BY a.steps * b.steps * c.steps",
                limits=FAST,
            )
    finally:
        sandbox.set_heap_limit(previous)

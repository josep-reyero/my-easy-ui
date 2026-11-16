"""Dashboard files: validation messages, the compiled form, params, queries, formats, markdown."""

from __future__ import annotations

import datetime as dt
import json
import textwrap

import pytest

from conftest import ACTIVITIES_YAML, OVERVIEW_YAML
from easyui import formats, sandbox, spec
from easyui.errors import NotFound, ParamError, QueryRefused, SpecError

TODAY = dt.date(2026, 3, 31)


def yaml_doc(body: str) -> str:
    return textwrap.dedent(body).lstrip("\n")


def errors_of(text: str, slug: str = "t"):
    dash, errors, warnings = spec.check(yaml_doc(text), slug=slug, file="t.yaml")
    return errors


def one_error(text: str):
    errs = errors_of(text)
    assert errs, "expected an error"
    return errs[0]


MINIMAL = """
spec: easy-ui/v1
title: T
queries:
  q:
    sql: SELECT day, steps FROM days
layout:
  - - type: line
      title: Steps
      query: q
      x: day
      y: steps
"""


# ---------------------------------------------------------------- the demo dashboards


@pytest.mark.parametrize("slug, text", [("overview", OVERVIEW_YAML), ("activities", ACTIVITIES_YAML)])
def test_demo_dashboards_are_valid(slug, text):
    dash, errors, warnings = spec.check(text, slug=slug)
    assert errors == [] and warnings == []
    assert dash.slug == slug


def test_demo_dashboards_are_valid_against_data(built_app, bundle_path):
    for slug in ("overview", "activities"):
        entry = spec.validate_file(built_app.app_dir / "ui" / "dashboards" / f"{slug}.yaml", bundle=bundle_path)
        assert entry["ok"], entry
        assert entry["warnings"] == []


def test_compiled_form():
    dash = spec.loads(OVERVIEW_YAML, slug="overview")
    c = spec.compile_dashboard(dash, today=TODAY)
    text = json.dumps(c)
    assert "SELECT" not in text and "sql" not in text  # SQL never reaches the browser
    assert c["spec"] == "easy-ui/v1" and c["slug"] == "overview" and c["title"] == "Overview"
    assert [p["name"] for p in c["params"]] == ["since", "period"]
    assert c["params"][1]["options"] == [{"value": "day", "label": "Day"}, {"value": "week", "label": "Week"}]
    assert c["queries"] == {"totals": {"params": ["since"]}, "daily": {"params": ["period", "since"]}, "heart": {"params": ["since"]}}
    controls, stats, charts, text_row = c["layout"]
    assert controls == [{"id": "r0c0", "type": "controls", "title": None, "params": ["since", "period"]}]
    assert stats[0] == {
        "id": "r1c0", "type": "stat", "title": "Distance", "query": "totals", "value": "distance",
        "format": "distance", "delta": None, "delta_format": None, "better": None, "hint": "Walking, running and riding together.",
    }
    assert charts[0]["y"] == ["Distance"] and charts[0]["stacked"] is False
    assert charts[1]["unit"] == "bpm" and charts[1]["format"] is None
    blocks = text_row[0]["blocks"]
    assert blocks[0] == {"t": "h", "level": 2, "c": [{"t": "text", "v": "About this page"}]}
    assert {"t": "a", "href": "https://example.org/easy-ui", "c": [{"t": "text", "v": "the project page"}]} in blocks[1]["c"]
    assert blocks[2]["t"] == "ul" and len(blocks[2]["items"]) == 2
    assert "body" not in text_row[0]


def test_compiled_table_columns_and_defaults():
    c = spec.compile_dashboard(spec.loads(ACTIVITIES_YAML, slug="activities"))
    table = c["layout"][1][0]
    assert table["limit"] == 50
    assert table["columns"][0] == {"column": "day", "label": "Day", "format": "date"}
    assert table["columns"][1] == {"column": "kind", "label": "kind", "format": None}
    ints = next(p for p in c["params"] if p["name"] == "min_km")
    assert (ints["min"], ints["max"], ints["default"]) == (0, 200, 0)
    text = next(p for p in c["params"] if p["name"] == "search")
    assert text["default"] == "" and text["max_length"] == 200


def test_compile_does_not_share_state_with_the_dashboard():
    dash = spec.loads(OVERVIEW_YAML, slug="overview")
    c = spec.compile_dashboard(dash)
    c["layout"][0][0]["params"].append("x")
    assert dash.layout[0][0]["params"] == ["since", "period"]


# ---------------------------------------------------------------- structural errors


def test_unknown_top_level_key_with_line_and_closest_match():
    e = one_error(MINIMAL + "titel: x\n")
    assert e.where == "titel" and e.line == 12 and e.did_you_mean == "title"
    assert "title" in e.allowed and e.file == "t.yaml"


def test_missing_and_wrong_spec():
    e = one_error(MINIMAL.replace("spec: easy-ui/v1\n", ""))
    assert 'no "spec"' in e.message and "spec: easy-ui/v1" in e.hint
    e = one_error(MINIMAL.replace("easy-ui/v1", "easy-ui/v2"))
    assert e.where == "spec" and e.allowed == ["easy-ui/v1"]


def test_unknown_component_type():
    e = one_error(MINIMAL.replace("type: line", "type: lines"))
    assert e.where == "layout[0][0].type" and e.did_you_mean == "line" and "stat" in e.allowed


def test_unknown_component_setting():
    e = one_error(MINIMAL.replace("query: q", "querry: q"))
    assert e.where == "layout[0][0].querry" and e.did_you_mean == "query"
    assert e.allowed == ["type", "title", "query", "x", "y", "format", "unit", "step", "readout", "partial"]


def test_missing_required_setting():
    e = one_error(MINIMAL.replace("      x: day\n", ""))
    assert e.where == "layout[0][0]" and 'no "x"' in e.message


def test_query_reference_must_exist():
    e = one_error(MINIMAL.replace("query: q", "query: qq"))
    assert e.where == "layout[0][0].query" and e.did_you_mean == "q" and e.allowed == ["q"]


def test_undeclared_param():
    e = one_error(MINIMAL.replace("FROM days", "FROM days WHERE day > :since"))
    assert e.where == "queries.q.sql" and ":since" in e.message and "params:" in e.hint


def test_unsafe_sql_is_a_validation_error():
    e = one_error(MINIMAL.replace("SELECT day, steps FROM days", "SELECT 1; DROP TABLE days"))
    assert e.where == "queries.q.sql" and "more than one statement" in e.message


def test_unknown_format():
    e = one_error(MINIMAL + "      format: km\n")
    assert e.where == "layout[0][0].format" and e.allowed == list(formats.FORMAT_NAMES)


def test_row_must_be_a_list():
    text = MINIMAL.replace("  - - type: line", "  - type: line").replace("\n      ", "\n    ")
    e = one_error(text)
    assert e.where == "layout[0]" and "- -" in e.hint


def test_too_many_components_in_a_row():
    comp = "    - {type: text, body: hi}\n"
    text = MINIMAL + "  -\n" + comp * 7
    e = one_error(text)
    assert e.where == "layout[1]" and "at most 6" in e.message


def test_duplicate_yaml_key():
    e = one_error(MINIMAL + "title: again\n")
    assert "appears twice" in e.message and e.line == 12


def test_yaml_aliases_and_merges_are_refused():
    e = one_error(
        """
        spec: easy-ui/v1
        title: &t T
        description: *t
        layout: [[{type: text, body: hi}]]
        """
    )
    assert "alias" in e.message and e.line == 3
    e = one_error(
        """
        spec: easy-ui/v1
        title: T
        layout: [[{type: text, <<: {body: hi}}]]
        """
    )
    assert "merge" in e.message


def test_billion_laughs_is_refused_quickly():
    lol = 'spec: easy-ui/v1\ntitle: T\nlayout: [[{type: text, body: hi}]]\na: &a ["x","x","x","x","x","x","x","x","x"]\n'
    for i in range(1, 9):
        lol += f"b{i}: &b{i} [*{'a' if i == 1 else 'b' + str(i - 1)}" + f", *{'a' if i == 1 else 'b' + str(i - 1)}" * 8 + "]\n"
    e = one_error(lol)
    assert "alias" in e.message


def test_invalid_yaml_has_a_line():
    e = one_error("spec: easy-ui/v1\ntitle: [unclosed\nlayout: x\n")
    assert "cannot be read" in e.message and e.line is not None and "YAML (" not in e.message.split("(")[0]


def test_python_tags_are_refused():
    e = one_error("spec: easy-ui/v1\ntitle: !!python/object/apply:os.system ['echo hi']\nlayout: []\n")
    assert "YAML tag" in e.message and e.line == 2


def test_not_a_mapping():
    assert "set of settings" in one_error("- a\n- b\n").message
    assert "set of settings" in one_error("").message


def test_file_size_limit():
    e = one_error(MINIMAL + "description: " + "x" * 300_000 + "\n")
    assert "larger than" in e.message


def test_yes_no_options_need_quotes():
    e = one_error(
        """
        spec: easy-ui/v1
        title: T
        params:
          flag: {type: choice, default: "yes", options: [yes, no]}
        layout: [[{type: controls}]]
        """
    )
    assert "true/false" in e.message and "quotes" in e.hint


def test_title_that_yaml_reads_as_bool():
    e = one_error(MINIMAL.replace("title: T", "title: yes"))
    assert e.where == "title" and "quotes" in e.hint


@pytest.mark.parametrize(
    "param, where, words",
    [
        ("{type: choice, default: walk, options: [run, ride]}", "params.p.default", "not one of its options"),
        ("{type: choice, default: run}", "params.p.options", "needs a list of options"),
        ("{type: choice, default: run, options: [run, run]}", "params.p.options[1]", "appears twice"),
        ("{type: int, default: 500, min: 0, max: 200}", "params.p.default", "outside"),
        ("{type: int, default: 1, min: 5, max: 2}", "params.p.min", "larger than its max"),
        ("{type: int, default: '1'}", "params.p.default", "whole number"),
        ("{type: date, default: last week}", "params.p.default", "not a date"),
        ("{type: date, default: '2026-02-30'}", "params.p.default", "not a date"),
        ("{type: date, default: 2026-02-30}", "", "date does not exist"),
        ("{type: text, default: " + "x" * 201 + "}", "params.p.default", "most allowed is 200"),
        ("{type: text}", "params.p", "no default"),
        ("{type: slider, default: 1}", "params.p.type", "not known"),
        ("{default: 1}", "params.p", "no type"),
        ("{type: int, default: 1, options: [1]}", "params.p.options", "not a setting of a int param"),
    ],
)
def test_param_errors(param, where, words):
    e = one_error(
        f"""
        spec: easy-ui/v1
        title: T
        params:
          p: {param}
        queries:
          q: {{sql: "SELECT :p AS v"}}
        layout: [[{{type: controls}}, {{type: stat, title: V, query: q, value: v}}]]
        """
    )
    assert e.where == where, e
    assert words in e.message, e


def test_bad_param_and_query_names():
    e = one_error(MINIMAL.replace("queries:\n  q:", "queries:\n  Bad-Name:"))
    assert e.where == "queries.Bad-Name"
    e = one_error(MINIMAL.replace("queries:", "params:\n  Since: {type: text, default: ''}\nqueries:"))
    assert e.where == "params.Since"


def test_param_failing_its_own_check_is_reported_once():
    errs = errors_of(
        """
        spec: easy-ui/v1
        title: T
        params:
          p: {type: date, default: nope}
        queries:
          q: {sql: "SELECT :p AS v"}
        layout: [[{type: controls, params: [p]}, {type: stat, title: V, query: q, value: v}]]
        """
    )
    assert len(errs) == 1


def test_choice_with_number_options_and_labels():
    dash = spec.loads(
        yaml_doc(
            """
            spec: easy-ui/v1
            title: T
            params:
              days:
                type: choice
                default: 30
                options: [{value: 7, label: A week}, {value: 30, label: A month}, 90]
            queries:
              q: {sql: "SELECT :days AS d"}
            layout: [[{type: controls}, {type: stat, title: D, query: q, value: d}]]
            """
        ),
        slug="t",
    )
    p = dash.params["days"]
    assert p.default == 30 and p.options[2] == {"value": 90, "label": "90"}
    assert spec.resolve_params(dash, {"days": "7"}) == {"days": 7}


def test_markdown_link_must_be_https():
    e = one_error(MINIMAL + "  - - type: text\n      body: '[x](javascript:alert(1))'\n")
    assert e.where == "layout[1][0].body" and "https://" in e.message


def test_table_columns_forms():
    e = one_error(MINIMAL + "  - - {type: table, title: T, query: q, columns: [{label: X}]}\n")
    assert "no column:" in e.message
    e = one_error(MINIMAL + "  - - {type: table, title: T, query: q, columns: [{column: day, colour: red}]}\n")
    assert e.where == "layout[1][0].columns[0].colour" and e.allowed == ["column", "label", "format"]
    e = one_error(MINIMAL + "  - - {type: table, title: T, query: q, limit: 0}\n")
    assert "from 1 to 5000" in e.message


def test_controls_unknown_param():
    e = one_error(MINIMAL + "  - - {type: controls, params: [sinse]}\n")
    assert e.where == "layout[1][0].params[0]"


def test_warnings_for_unused_and_hidden_things():
    dash, errors, warnings = spec.check(
        yaml_doc(
            """
            spec: easy-ui/v1
            title: T
            params:
              unused: {type: text, default: ""}
            queries:
              q: {sql: "SELECT 1 AS v"}
              spare: {sql: "SELECT 2 AS v"}
            layout: [[{type: stat, title: V, query: q, value: v}]]
            """
        ),
        slug="t",
    )
    assert errors == []
    msgs = " | ".join(w.message for w in warnings)
    assert '"spare" is not used' in msgs
    assert '"unused" is not used by any query' in msgs
    assert "no control on the page" in msgs
    assert all(w.level == "warning" for w in warnings)


def test_loads_raises_spec_error_with_problems():
    with pytest.raises(SpecError) as e:
        spec.loads(yaml_doc(MINIMAL.replace("type: line", "type: lines")), slug="t", file="t.yaml")
    assert e.value.problems[0]["where"] == "layout[0][0].type"
    assert e.value.problems[0]["did_you_mean"] == "line"
    assert e.value.http_status == 422 and "easy-ui validate" in e.value.hint


@pytest.mark.parametrize("name", ["Overview.yaml", "over_view.yaml", "-x.yaml", "x.yml", "x.json", "a" * 42 + ".yaml"])
def test_bad_file_names(name):
    with pytest.raises(SpecError):
        spec.slug_for(name)


def test_good_file_names():
    assert spec.slug_for("ui/dashboards/training-2026.yaml") == "training-2026"


def test_problem_text_is_one_readable_line():
    e = one_error(MINIMAL.replace("query: q", "querry: q"))
    t = e.text()
    assert t.startswith("t.yaml:9  row 1, first component (line), setting querry:") and 'Did you mean "query"?' in t
    assert e.where == "layout[0][0].querry" and e.to_dict()["place"] == "row 1, first component (line), setting querry"


# ---------------------------------------------------------------- against the data


def write(app, slug, body):
    return app.write_dashboard(slug, yaml_doc(body))


def test_columns_are_checked_against_data(built_app, bundle_path):
    p = write(
        built_app,
        "cols",
        """
        spec: easy-ui/v1
        title: T
        queries:
          q: {sql: "SELECT day, steps FROM days"}
        layout:
          - - {type: line, title: S, query: q, x: dayy, y: [steps, stepz]}
            - {type: table, title: T, query: q, columns: [day, {column: stps}]}
            - {type: stat, title: S, query: q, value: steps, delta: delta}
        """,
    )
    entry = spec.validate_file(p, bundle=bundle_path)
    got = {(e["where"], e.get("did_you_mean")) for e in entry["errors"]}
    assert got == {
        ("layout[0][0].x", "day"),
        ("layout[0][0].y[1]", "steps"),
        ("layout[0][1].columns[1]", "steps"),
        ("layout[0][2].delta", None),
    }
    assert all(e["allowed"] == ["day", "steps"] for e in entry["errors"])


def test_sql_errors_against_data_point_at_the_query(built_app, bundle_path):
    p = write(
        built_app,
        "sqlerr",
        """
        spec: easy-ui/v1
        title: T
        queries:
          a: {sql: "SELECT * FROM private_notes"}
          b: {sql: "SELECT * FROM sqlite_master"}
          c: {sql: "SELECT nope FROM days"}
        layout:
          - - {type: table, title: A, query: a}
            - {type: table, title: B, query: b}
            - {type: table, title: C, query: c}
        """,
    )
    entry = spec.validate_file(p, bundle=bundle_path)
    by_where = {e["where"]: e for e in entry["errors"]}
    assert set(by_where) == {"queries.a.sql", "queries.b.sql", "queries.c.sql"}
    assert "not in the dashboard data" in by_where["queries.a.sql"]["problem"]
    assert "refused for safety" in by_where["queries.b.sql"]["problem"]
    assert "nope" in by_where["queries.c.sql"]["problem"]
    assert by_where["queries.a.sql"]["line"] == 4


def test_duplicate_result_columns_warn(built_app, bundle_path):
    p = write(
        built_app,
        "dupes",
        """
        spec: easy-ui/v1
        title: T
        queries:
          q: {sql: "SELECT count(*), count(*) FROM days"}
        layout: [[{type: table, title: T, query: q}]]
        """,
    )
    entry = spec.validate_file(p, bundle=bundle_path)
    assert entry["ok"] and "more than one column" in entry["warnings"][0]["problem"]


def test_validate_app_reports_everything(built_app):
    (built_app.app_dir / "ui" / "dashboards" / "stray.yml").write_text("x")
    write(built_app, "bad", MINIMAL.replace("type: line", "type: lines"))
    report = spec.validate_app(built_app.config())
    assert report["ok"] is False and report["errors"] == 1
    assert report["data"]["checked"] is True and report["data"]["bundle"].startswith("bundle-")
    assert [d["slug"] for d in report["dashboards"]] == ["activities", "bad", "overview"]
    assert any(".yml" in n for n in report["notes"])
    assert report["dashboards"][1]["file"] == "ui/dashboards/bad.yaml"
    json.dumps(report)


def test_validate_app_without_data(demo_app):
    report = spec.validate_app(demo_app.config())
    assert report["ok"] and report["data"]["checked"] is False
    assert "easy-ui data build" in report["data"]["note"]


# ---------------------------------------------------------------- params at run time


@pytest.fixture
def activities():
    return spec.loads(ACTIVITIES_YAML, slug="activities")


def test_resolve_params_defaults_and_types(activities):
    assert spec.resolve_params(activities) == {"kind": "all", "min_km": 0, "search": ""}
    assert spec.resolve_params(activities, {"kind": "run", "min_km": "5", "search": "u"}) == {
        "kind": "run", "min_km": 5, "search": "u",
    }
    assert spec.resolve_params(activities, {"min_km": 5.0, "kind": None})["min_km"] == 5


@pytest.mark.parametrize(
    "given",
    [
        {"kind": "hike"},
        {"kind": ["run"]},
        {"kind": True},
        {"min_km": "5; DROP TABLE days"},
        {"min_km": 201},
        {"min_km": -1},
        {"min_km": 1.5},
        {"min_km": True},
        {"search": "x" * 201},
        {"search": 5},
        {"search": "a\x00b"},
        {"unknown": 1},
    ],
)
def test_resolve_params_refuses(activities, given):
    with pytest.raises(ParamError) as e:
        spec.resolve_params(activities, given)
    assert e.value.http_status == 400


def test_resolve_params_not_a_mapping(activities):
    with pytest.raises(ParamError):
        spec.resolve_params(activities, ["kind"])  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "rule, expected",
    [
        ("today", "2026-03-31"),
        ("today-90d", "2025-12-31"),
        ("today - 2w", "2026-03-17"),
        ("today-1m", "2026-02-28"),
        ("today+1m", "2026-04-30"),
        ("today-1y", "2025-03-31"),
    ],
)
def test_date_rules(rule, expected):
    dash = spec.loads(
        yaml_doc(
            f"""
            spec: easy-ui/v1
            title: T
            params:
              since: {{type: date, default: "{rule}"}}
            queries:
              q: {{sql: "SELECT :since AS s"}}
            layout: [[{{type: controls}}, {{type: stat, title: S, query: q, value: s}}]]
            """
        ),
        slug="t",
    )
    assert spec.resolve_params(dash, today=TODAY)["since"] == expected
    c = spec.compile_dashboard(dash, today=TODAY)
    assert c["params"][0]["default"] == expected and c["params"][0]["default_rule"] == rule
    assert spec.resolve_params(dash, {"since": "2026-01-05"}, today=TODAY)["since"] == "2026-01-05"
    for bad in ("2026-1-5", "yesterday", "2026-02-30", 20260105):
        with pytest.raises(ParamError):
            spec.resolve_params(dash, {"since": bad}, today=TODAY)


# ---------------------------------------------------------------- running queries


def test_run_queries(bundle_path, activities):
    out = spec.run_queries(activities, ["recent", "by_kind", "recent"], {"kind": "run"}, bundle_path=bundle_path)
    assert set(out["results"]) == {"recent", "by_kind"}
    recent = out["results"]["recent"]
    assert recent["columns"][:2] == ["day", "kind"] and recent["truncated"] is False
    assert {row[1] for row in recent["rows"]} == {"run"}
    assert out["results"]["by_kind"]["rows"][0][0] == "run"


def test_run_queries_text_param_is_data_not_sql(bundle_path, activities):
    out = spec.run_queries(activities, ["recent"], {"search": "' OR 1=1 --"}, bundle_path=bundle_path)
    assert out["results"]["recent"]["rows"] == []


def test_run_queries_unknown_query(bundle_path, activities):
    with pytest.raises(NotFound) as e:
        spec.run_queries(activities, ["recent", "secret"], bundle_path=bundle_path)
    assert "recent, by_kind, weekly" in e.value.hint


@pytest.mark.parametrize("names", [[], "recent", [1], ["recent"] * 51])
def test_run_queries_bad_names(bundle_path, activities, names):
    with pytest.raises(ParamError):
        spec.run_queries(activities, names, bundle_path=bundle_path)


def test_run_queries_inline_errors(bundle_path, activities, monkeypatch):
    activities.queries["weekly"].sql = "SELECT * FROM nothing_here"
    out = spec.run_queries(activities, ["weekly", "by_kind"], bundle_path=bundle_path, errors="inline")
    assert out["results"]["weekly"]["error"]["code"] == "query_failed"
    assert "columns" in out["results"]["by_kind"]
    with pytest.raises(Exception):
        spec.run_queries(activities, ["weekly"], bundle_path=bundle_path)


def test_run_queries_share_one_size_budget(bundle_path, activities):
    small = sandbox.Limits(max_bytes=3000)
    out = spec.run_queries(activities, ["recent", "weekly"], bundle_path=bundle_path, limits=small)
    assert out["results"]["recent"]["truncated"] is True
    assert out["results"]["weekly"]["truncated"] is True


def test_run_queries_refuses_tampered_sql(bundle_path, activities):
    activities.queries["weekly"].sql = "SELECT * FROM sqlite_master"
    with pytest.raises(QueryRefused):
        spec.run_queries(activities, ["weekly"], bundle_path=bundle_path)


# ---------------------------------------------------------------- vocabulary, formats, markdown


def test_catalog_is_complete_and_json():
    cat = spec.catalog()
    json.dumps(cat)
    assert set(cat["components"]) == {"stat", "line", "area", "bar", "table", "text", "controls"}
    for name, comp in spec.COMPONENTS.items():
        assert set(cat["components"][name]["settings"]) == set(comp["settings"])
    assert set(cat["formats"]["formats"]) == set(formats.FORMAT_NAMES)
    assert spec.check(cat["example"], slug="example")[1] == []


@pytest.mark.parametrize(
    "fmt, value, text",
    [(name, v, t) for name, info in formats.FORMATS.items() for v, t in info["examples"]],
)
def test_format_examples(fmt, value, text):
    assert formats.format_value(fmt, value) == text


@pytest.mark.parametrize(
    "fmt, value, text",
    [
        (None, 1234.567, "1,234.57"),
        (None, "text", "text"),
        (None, None, "—"),
        ("number", float("inf"), "—"),
        ("integer", -0.4, "0"),
        ("number", True, "True"),
        ("percent", "n/a", "n/a"),
        ("date", "2026-13-01", "2026-13-01"),
        ("date", 10**20, "100000000000000000000"),
        ("datetime", "2026-03-14T07:05", "14 Mar 2026 07:05"),
        ("duration", 0, "0 s"),
        ("duration", -0.4, "0 s"),
        ("duration", 3599.6, "1 h 00 min"),
        ("duration", 3629, "1 h 00 min"),
        ("duration", 3630, "1 h 01 min"),
        ("distance", -1500, "-1.50 km"),
        ("pace", None, "—"),
    ],
)
def test_format_edge_cases(fmt, value, text):
    assert formats.format_value(fmt, value) == text


def test_unknown_format_raises():
    with pytest.raises(ValueError):
        formats.format_value("km", 1)


def test_vocabulary_is_json():
    v = formats.vocabulary()
    assert json.loads(json.dumps(v))["rules"]["null_text"] == "—"


@pytest.mark.parametrize(
    "text, blocks",
    [
        ("Hello", [{"t": "p", "c": [{"t": "text", "v": "Hello"}]}]),
        ("one\ntwo", [{"t": "p", "c": [{"t": "text", "v": "one two"}]}]),
        ("a\n\nb", [{"t": "p", "c": [{"t": "text", "v": "a"}]}, {"t": "p", "c": [{"t": "text", "v": "b"}]}]),
        ("# H1\n#### H4", [{"t": "h", "level": 1, "c": [{"t": "text", "v": "H1"}]}, {"t": "h", "level": 4, "c": [{"t": "text", "v": "H4"}]}]),
        ("##### five", [{"t": "p", "c": [{"t": "text", "v": "##### five"}]}]),
        ("1. a\n2. b", [{"t": "ol", "items": [[{"t": "text", "v": "a"}], [{"t": "text", "v": "b"}]]}]),
        ("- a\n  more", [{"t": "ul", "items": [[{"t": "text", "v": "a more"}]]}]),
        ("**b** *i* _j_", [{"t": "p", "c": [{"t": "b", "c": [{"t": "text", "v": "b"}]}, {"t": "text", "v": " "}, {"t": "i", "c": [{"t": "text", "v": "i"}]}, {"t": "text", "v": " "}, {"t": "i", "c": [{"t": "text", "v": "j"}]}]}]),
        ("snake_case_word", [{"t": "p", "c": [{"t": "text", "v": "snake_case_word"}]}]),
        ("\\*literal\\*", [{"t": "p", "c": [{"t": "text", "v": "*literal*"}]}]),
        ("<b onclick=x>hi</b>", [{"t": "p", "c": [{"t": "text", "v": "<b onclick=x>hi</b>"}]}]),
        ("[w](https://e.org/a_(b))", [{"t": "p", "c": [{"t": "a", "href": "https://e.org/a_(b)", "c": [{"t": "text", "v": "w"}]}]}]),
        ("**unclosed", [{"t": "p", "c": [{"t": "text", "v": "**unclosed"}]}]),
    ],
)
def test_markdown(text, blocks):
    got, problems = spec.parse_markdown(text)
    assert problems == [] and got == blocks


@pytest.mark.parametrize(
    "href",
    ["javascript:alert(1)", "http://e.org", "data:text/html,x", "//e.org", "https://", "https://e.org/\"onmouseover", "HTTPS:e.org", "mailto:a@b.c", "https://e.org/a b"],
)
def test_markdown_refuses_unsafe_links(href):
    blocks, problems = spec.parse_markdown(f"[x]({href})")
    assert problems or all(n["t"] != "a" for b in blocks for n in b.get("c", []))
    assert "javascript" not in json.dumps([n for b in blocks for n in b.get("c", []) if n["t"] == "a"])


def test_markdown_deep_nesting_is_bounded():
    blocks, _ = spec.parse_markdown("*" * 5000 + "x" + "*" * 5000)
    json.dumps(blocks)


# ---------------------------------------------------------------- v1 visual settings


def test_chart_and_table_visual_settings_compile():
    dash, errors, _ = spec.check(yaml_doc("""
        spec: easy-ui/v1
        title: T
        queries:
          q: {sql: "SELECT day, steps, 0 AS open FROM days"}
        layout:
          - - {type: bar, title: B, query: q, x: day, y: steps, partial: last, step: 5000, readout: pointer}
            - {type: line, title: L, query: q, x: day, y: steps, partial: open, readout: none, step: 0.5}
            - {type: table, title: T, query: q, phone: scroll}
            - {type: table, title: U, query: q}
        """), slug="t", file="t.yaml")
    assert errors == []
    row = spec.compile_dashboard(dash)["layout"][0]
    assert (row[0]["partial"], row[0]["step"], row[0]["readout"]) == ("last", 5000, "pointer")
    assert (row[1]["partial"], row[1]["readout"], row[1]["step"]) == ("open", "none", 0.5)
    assert row[2]["phone"] == "scroll" and row[3]["phone"] == "cards"
    assert spec.compile_dashboard(dash)["layout"][0][0]["type"] == "bar"


@pytest.mark.parametrize("setting, where", [
    ("readout: always", "readout"),
    ("step: 0", "step"),
    ("step: -5", "step"),
    ("step: '5'", "step"),
    ("step: true", "step"),
    ("step: .nan", "step"),
    ("step: .inf", "step"),
    ("partial: 5", "partial"),
])
def test_bad_chart_visual_settings(setting, where):
    e = one_error(MINIMAL + f"      {setting}\n")
    assert e.where == f"layout[0][0].{where}"


def test_bad_table_phone_setting():
    e = one_error(MINIMAL.replace("type: line", "type: table").replace("      x: day\n      y: steps\n", "      phone: tiny\n"))
    assert e.where == "layout[0][0].phone" and e.allowed == ["cards", "scroll"]


def test_partial_column_is_checked_against_the_data(built_app, bundle_path):
    p = write(built_app, "partial", """
        spec: easy-ui/v1
        title: T
        queries:
          q: {sql: "SELECT day, steps FROM days"}
        layout:
          - - {type: bar, title: S, query: q, x: day, y: steps, partial: in_progress}
        """)
    entry = spec.validate_file(p, bundle=bundle_path)
    assert [e["where"] for e in entry["errors"]] == ["layout[0][0].partial"]

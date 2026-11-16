"""The easy-ui command line: components, tables, validate, data build, --json, exit codes, registration."""

from __future__ import annotations

import json
import sys
import types

import pytest

from conftest import run_cli, run_cli_json
from easyui import cli

MINIMAL_BROKEN = """\
spec: easy-ui/v1
title: Broken
queries:
  q:
    sql: SELECT day, steps FROM days
layout:
  - - type: stat
      title: Steps
      querry: q
      value: steps
"""


@pytest.fixture(autouse=True)
def only_core_commands(monkeypatch):
    """Keep these tests independent of the server and publish parts."""
    monkeypatch.setattr(cli, "COMMAND_MODULES", ())


def test_components_json():
    code, data = run_cli_json("components")
    assert code == 0
    assert data["spec"] == "easy-ui/v1"
    assert "stat" in data["components"] and "pace" in data["formats"]["formats"]
    assert data["components"]["stat"]["settings"]["value"]["required"] is True


def test_components_text():
    code, out, err = run_cli("components")
    assert code == 0 and "stat:" in out and "Formats:" in out and err == ""


@pytest.mark.parametrize("argv", [["--json", "validate"], ["validate", "--json"]])
def test_json_anywhere_and_stdout_is_only_json(built_app, argv):
    code, out, err = run_cli(*argv)
    assert code == 0
    data = json.loads(out)
    assert data["ok"] is True and data["data"]["checked"] is True
    assert [d["slug"] for d in data["dashboards"]] == ["activities", "overview"]


def test_validate_failure_exit_code_and_report(built_app):
    built_app.write_dashboard("broken", MINIMAL_BROKEN)
    code, data = run_cli_json("validate")
    assert code == 1 and data["ok"] is False
    broken = next(d for d in data["dashboards"] if d["slug"] == "broken")
    err = broken["errors"][0]
    assert err["where"] == "layout[0][0].querry" and err["did_you_mean"] == "query" and err["line"] == 9
    code, out, _ = run_cli("validate", "ui/dashboards/broken.yaml")
    assert code == 1
    assert "ui/dashboards/broken.yaml:9  row 1, first component (stat), setting querry" in out
    # validate never publishes, so it does not talk about publishing
    assert "Fix them and run easy-ui validate again." in out and "published" not in out


def test_validate_named_files_and_no_data(demo_app):
    code, data = run_cli_json("validate", "ui/dashboards/overview.yaml", "--no-data")
    assert code == 0 and [d["slug"] for d in data["dashboards"]] == ["overview"]
    assert data["data"]["checked"] is False


def test_validate_outside_an_app(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from conftest import OVERVIEW_YAML

    (tmp_path / "draft.yaml").write_text(OVERVIEW_YAML)
    code, data = run_cli_json("validate", "draft.yaml")
    assert code == 0 and data["app"] is None and data["dashboards"][0]["ok"]
    code, data = run_cli_json("validate")
    assert code == 3 and data["error"]["code"] == "not_found"


def test_data_build_and_list(demo_app):
    code, data = run_cli_json("data", "build")
    assert code == 0 and data["unchanged"] is False
    assert data["tables"]["days"]["rows"] == 120
    code, out, _ = run_cli("data", "build")
    assert code == 0 and "has not changed" in out
    code, data = run_cli_json("data", "list")
    assert code == 0 and len(data["bundles"]) == 1 and data["bundles"][0]["current"]


def test_data_without_subcommand_is_usage(demo_app):
    code, data = run_cli_json("data")
    assert code == 2 and data["error"]["code"] == "usage"


def test_data_build_keep_must_be_positive(demo_app):
    code, data = run_cli_json("data", "build", "--keep", "0")
    assert code == 2


def test_tables_needs_data(demo_app):
    code, data = run_cli_json("tables")
    assert code == 3 and data["error"]["code"] == "no_data"
    assert "easy-ui data build" in data["error"]["hint"]


def test_tables_from_bundle_and_source(built_app):
    code, data = run_cli_json("tables", "--samples", "1")
    assert code == 0 and data["from"] == "dashboard data"
    assert [t["name"] for t in data["tables"]] == ["activities", "days", "weekly"]
    code, data = run_cli_json("tables", "--source")
    flags = {t["name"]: t["in_dashboards"] for t in data["tables"]}
    assert flags == {"activities": True, "days": True, "private_notes": False, "weekly": True}
    code, out, _ = run_cli("tables", "--source")
    assert "* days" in out and "  private_notes" in out


def test_tables_samples_range(built_app):
    assert run_cli_json("tables", "--samples", "99")[0] == 2


@pytest.mark.parametrize("form", ["sep", "eq", "after"])
def test_app_dir_option(built_app, tmp_path, monkeypatch, form):
    monkeypatch.chdir(tmp_path)
    app = str(built_app.app_dir)
    argv = {
        "sep": ["--app-dir", app, "tables"],
        "eq": [f"--app-dir={app}", "tables"],
        "after": ["tables", "--app-dir", app],
    }[form]
    code, data = run_cli_json(*argv)
    assert code == 0 and data["app"] == "demo"


def test_app_dir_needs_a_value():
    code, data = run_cli_json("tables", "--app-dir")
    assert code == 2 and "--app-dir" in data["error"]["message"]


def test_app_dir_without_app_toml(tmp_path):
    code, data = run_cli_json("--app-dir", str(tmp_path), "validate")
    assert code == 3 and "project.toml" in data["error"]["message"]


def test_unknown_command_is_usage_error_as_json():
    code, data = run_cli_json("frobnicate")
    assert code == 2 and data["error"]["code"] == "usage" and "--help" in data["error"]["hint"]


def test_unknown_command_text_goes_to_stderr():
    code, out, err = run_cli("frobnicate")
    assert code == 2 and out == "" and "What to do:" in err


def test_no_command():
    code, out, err = run_cli()
    assert code == 2 and "components" in err
    code, data = run_cli_json()
    assert code == 2 and data["error"]["code"] == "usage"


def test_version():
    code, out, _ = run_cli("--version")
    assert code == 0 and out.startswith("easy-ui ")


def test_broken_app_toml_is_reported(demo_app):
    (demo_app.app_dir / "project.toml").write_text("[app\n")
    code, data = run_cli_json("validate")
    assert code == 1 and data["error"]["code"] == "config_invalid"


def test_missing_modules_give_placeholder_commands(monkeypatch):
    monkeypatch.setattr(cli, "COMMAND_MODULES", ("easyui._not_installed_for_test",))
    monkeypatch.setattr(cli, "PLANNED_COMMANDS", {"easyui._not_installed_for_test": {"serve": "Run the server."}})
    code, data = run_cli_json("serve", "--port", "1")
    assert code == 1 and data["error"]["code"] == "not_available"
    assert "serve" in data["error"]["message"] and "uv sync" in data["error"]["hint"]


def test_feature_modules_register_commands(monkeypatch, demo_app):
    mod = types.ModuleType("easyui_fake_feature")

    def register(sub):
        p = sub.add_parser("hello", help="Say hello.")
        p.add_argument("--who", default="world")
        p.set_defaults(func=lambda args: cli.Result({"hello": args.who, "json": args.json, "app": cli.load_config(args).name}, "hi", 0))

    mod.register = register
    monkeypatch.setitem(sys.modules, "easyui_fake_feature", mod)
    monkeypatch.setattr(cli, "COMMAND_MODULES", ("easyui_fake_feature",))
    code, data = run_cli_json("hello", "--who", "you")
    assert code == 0 and data == {"hello": "you", "json": True, "app": "demo"}
    code, out, _ = run_cli("hello")
    assert code == 0 and out.strip() == "hi"


def test_feature_module_errors_use_exit_codes(monkeypatch):
    from easyui.errors import NeedsHuman

    mod = types.ModuleType("easyui_fake_feature2")

    def register(sub):
        def handler(args):
            raise NeedsHuman("Only you can type the password.", hint="Run easy-ui set-password yourself.")

        sub.add_parser("pw").set_defaults(func=handler)

    mod.register = register
    monkeypatch.setitem(sys.modules, "easyui_fake_feature2", mod)
    monkeypatch.setattr(cli, "COMMAND_MODULES", ("easyui_fake_feature2",))
    code, data = run_cli_json("pw")
    assert code == 6 and data == {
        "error": {"code": "needs_human", "message": "Only you can type the password.", "hint": "Run easy-ui set-password yourself."}
    }

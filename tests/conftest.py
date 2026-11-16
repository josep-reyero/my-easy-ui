"""Shared test fixtures and importable helpers: a synthetic demo app.

All data here is made up (seeded random numbers). Other test modules can use
the fixtures, or import the helpers directly::

    from conftest import make_demo_app, run_cli, OVERVIEW_YAML

Helpers
-------
``DEMO_APP_TOML``, ``OVERVIEW_YAML``, ``ACTIVITIES_YAML``  file contents.
``DEMO_TABLES``  the allowlisted tables (``days``, ``activities``, ``weekly`` view).
``make_demo_db(path) -> Path``
    A SQLite file with 120 days of made-up numbers (2026-01-01 .. 2026-04-30),
    about 70 activities, a ``weekly`` view and a ``private_notes`` table that is
    NOT allowlisted (so tests can prove it never reaches dashboards).
``make_demo_app(base, *, name="demo", dashboards=True) -> DemoApp``
    Creates ``base/app`` (project.toml + ui/dashboards/*.yaml), ``base/data``
    (MES_DATA_HOME) and ``base/state`` (EASYUI_STATE_HOME). Does not touch
    the environment; ``DemoApp.env()`` gives the two variables.
``DemoApp``  ``base``, ``app_dir``, ``data_home``, ``state_home``, ``db_path``,
    ``name``; ``env()``, ``config()``, ``write_dashboard(slug, text)``, ``build()``.
``run_cli(*argv) -> (exit_code, stdout, stderr)``   runs ``easyui.cli.run``.
``run_cli_json(*argv) -> (exit_code, data)``         same with ``--json``, parsed.

Fixtures
--------
``demo_app``     a fresh DemoApp with MES_DATA_HOME / EASYUI_STATE_HOME set and
                 the current directory set to the app folder.
``built_app``    ``demo_app`` after ``easy-ui data build``.
``bundle_path``  the current bundle file of ``built_app``.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import random
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from easyui.demo import (  # noqa: E402,F401 - the demo app is part of easy-ui (easy-ui demo)
    ACTIVITIES_YAML,
    DEMO_APP_TOML,
    DEMO_TABLES,
    OVERVIEW_YAML,
    make_demo_db,
)


@dataclass
class DemoApp:
    base: Path
    app_dir: Path
    data_home: Path
    state_home: Path
    db_path: Path
    name: str = "demo"

    def env(self) -> dict[str, str]:
        return {"MES_DATA_HOME": str(self.data_home), "EASYUI_STATE_HOME": str(self.state_home)}

    def config(self):
        from easyui import appconfig

        return appconfig.load(self.app_dir)

    def write_dashboard(self, slug: str, text: str) -> Path:
        p = self.app_dir / "ui" / "dashboards" / f"{slug}.yaml"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def build(self) -> dict[str, Any]:
        from easyui import bundle

        return bundle.build(self.config())


def make_demo_app(base: Path, *, name: str = "demo", dashboards: bool = True) -> DemoApp:
    from easyui.demo import write_demo

    made = write_demo(base, name=name, dashboards=dashboards)
    return DemoApp(base, made["app_dir"], made["data_home"], made["state_home"], made["db"], name)


def run_cli(*argv: str) -> tuple[int, str, str]:
    from easyui import cli

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.run(list(argv))
    return code, out.getvalue(), err.getvalue()


def run_cli_json(*argv: str) -> tuple[int, Any]:
    code, out, _err = run_cli("--json", *argv)
    return code, json.loads(out)


@pytest.fixture(autouse=True)
def _isolated_machine(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """No test reads this computer's own mes settings or server layout."""
    monkeypatch.setenv("MES_CONFIG", str(tmp_path_factory.mktemp("cfg") / "config.toml"))
    for k in ("MES_ROOT", "MES_STATE_HOME", "EASYUI_ALLOW_HOSTS"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def demo_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DemoApp:
    app = make_demo_app(tmp_path)
    for k, v in app.env().items():
        monkeypatch.setenv(k, v)
    monkeypatch.chdir(app.app_dir)
    return app


@pytest.fixture
def built_app(demo_app: DemoApp) -> DemoApp:
    demo_app.build()
    return demo_app


@pytest.fixture
def bundle_path(built_app: DemoApp) -> Path:
    from easyui import bundle

    return bundle.require_current(built_app.config())

"""The ``easy-ui`` command line (agent-first).

``easy-ui`` implements ``components``, ``tables``, ``validate``,
``data build`` / ``data list`` and ``demo`` (``easyui.demo``) itself. The other commands come from feature
modules, each imported inside ``try/except ImportError``; when a module cannot
be loaded its commands still exist and explain that they are not available:

- ``easyui.server``:  ``serve``, ``set-password``, ``check``
- ``easyui.publish``: ``publish``, ``versions``, ``rollback``, ``preview``, ``units``
- ``easyui.export``: ``export``
- ``easyui.screenshot``: ``screenshot``

Contract for a feature module
-----------------------------
- Define ``register(subparsers)``. ``subparsers`` is the top-level
  ``add_subparsers(dest="command")`` action; call ``subparsers.add_parser(name,
  help=..., description=...)``. Parsers are ``easyui.cli.ArgumentParser``, so
  a usage error raises ``UsageError`` (exit 2) and respects ``--json``.
- Nested subcommands use their own ``dest`` (e.g. ``dest="publish_command"``),
  never ``"command"``.
- Every leaf parser sets ``set_defaults(func=handler)``. ``handler(args)``
  gets the ``argparse.Namespace`` with ``args.json`` (bool) and
  ``args.project_dir`` (also as ``args.app_dir``) and ``args.ui_dir`` (``Path | None``) already set. Use
  ``easyui.cli.load_config(args)`` to get the ``AppConfig``.
- Return the data (dict, list, str or None) or a ``Result(data, text=None,
  exit_code=0, json_line=False)`` to also give the plain-language text shown
  without ``--json`` and/or a non-zero exit code. Raise an ``easyui.errors``
  subclass for errors. Never print the result yourself and never prompt.
  Two exceptions: ``set-password`` prompts on the terminal (``/dev/tty``, never
  stdout), because only the person may type the password; and a command that
  runs until stopped (``preview``) prints its address as soon as it is ready,
  with ``print_now(data, text, json_mode)``, and returns its final ``Result``
  with ``json_line=True`` so that ``--json`` output stays one JSON object per
  line.
- Text shown without ``--json`` goes through ``safe_text``: control
  characters (which could clear or retitle the terminal, or fake a line) from
  dashboard files are shown as visible escapes such as ``\x1b``.
- ``--json``, ``--project-dir PATH`` (old name ``--app-dir``) and ``--ui-dir PATH`` are taken off the command line before
  parsing, so they work in any position; do not add them yourself.

Exit codes: 0 ok, 1 error, 2 usage, 3 not found, 4 not allowed, 5 refused
for safety, 6 needs the human, 75 temporary.

Public API
----------
``COMMAND_MODULES``, ``PLANNED_COMMANDS``
``ArgumentParser``                     argparse parser raising ``UsageError``.
``Result(data, text=None, exit_code=0, json_line=False)``
``safe_text(text) -> str``             control characters shown as visible escapes.
``print_now(data, text, json_mode)``   print a line before a long-running command blocks.
``load_config(args) -> AppConfig``
``build_parser() -> (parser, load_problems)``
``run(argv=None) -> int``               run one command line, return the exit code.
``main(argv=None)``                     console entry point.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from easyui import __version__, appconfig, bundle, formats, paths, spec
from easyui.appconfig import AppConfig
from easyui.errors import EasyUIError, NotAvailable, NotFound, UsageError

COMMAND_MODULES = ("easyui.server", "easyui.publish", "easyui.export", "easyui.screenshot")
PLANNED_COMMANDS: dict[str, dict[str, str]] = {
    "easyui.server": {
        "serve": "Run the dashboard server for this project.",
        "set-password": "Set the sign-in password (typed by the person).",
        "check": "Check that this project's dashboards can be served.",
        "passkeys": "List or remove the passkeys that can sign in.",
        "sign-in": "How signing in is set up; turn password sign-in on or off.",
        "recovery-codes": "Make new one-time recovery codes.",
    },
    "easyui.publish": {
        "publish": "Publish the dashboards so the server shows them.",
        "versions": "List the published versions.",
        "rollback": "Go back to an earlier published version.",
        "preview": "Preview the dashboards on this computer before publishing.",
        "units": "Show the service files that run the server.",
    },
    "easyui.export": {
        "export": "Make a static copy of chosen dashboards (no server needed).",
    },
    "easyui.screenshot": {
        "screenshot": "Take a picture of a dashboard with a locally installed Chromium.",
    },
}


class _Exit(Exception):
    def __init__(self, code: int) -> None:
        self.code = code


_INVALID_CHOICE = re.compile(r"argument (?:<[^>]*>|[\w-]+): invalid choice: '([^']*)' \(choose from (.*)\)", re.S)
_INVALID_VALUE = re.compile(r"argument (--[\w-]+)(?:/[^:]*)?: invalid (\w+) value: '([^']*)'")
_MISSING = re.compile(r"the following arguments are required: (.*)")
_UNKNOWN = re.compile(r"unrecognized arguments: (.*)")
_NEEDS_VALUE = re.compile(r"argument (--[\w-]+)(?:/[^:]*)?: expected one argument")


def _plain_usage(message: str, prog: str) -> tuple[str, str]:
    """argparse's message in plain words, and what to do."""
    see = f"See: {prog} --help"
    m = _INVALID_CHOICE.search(message)
    if m:
        choices = ", ".join(c.strip().strip("'") for c in m.group(2).split(","))
        if prog == "easy-ui":
            return f"There is no command called '{m.group(1)}'. The commands are: {choices}.", see
        return f"'{m.group(1)}' is not one of the choices here. The choices are: {choices}.", see
    m = _INVALID_VALUE.search(message)
    if m:
        kind = {"int": "a whole number", "float": "a number"}.get(m.group(2), "a different value")
        return f"{m.group(1)} needs {kind}, but it was given '{m.group(3)}'.", see
    m = _NEEDS_VALUE.search(message)
    if m:
        return f"{m.group(1)} needs a value after it.", see
    m = _MISSING.search(message)
    if m:
        return f"Something this command needs is missing: {m.group(1)}.", see
    m = _UNKNOWN.search(message)
    if m:
        return f"This command does not take: {m.group(1)}.", see
    return f"The command line was not understood: {message}.", see


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        text, hint = _plain_usage(message, self.prog)
        raise UsageError(text, hint=hint)

    def exit(self, status: int = 0, message: str | None = None) -> NoReturn:
        if message:
            sys.stderr.write(message)
        raise _Exit(status)


@dataclass
class Result:
    data: Any
    text: str | None = None
    exit_code: int = 0
    json_line: bool = False


# C0 controls except newline and tab, DEL, C1 controls, and the Unicode bidirectional overrides.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


def safe_text(text: Any) -> str:
    """Text for the terminal, with control characters shown as visible escapes (\\x1b)."""
    s = text if isinstance(text, str) else str(text)
    return _CONTROL.sub(lambda m: f"\\x{ord(m.group()):02x}" if ord(m.group()) < 256 else f"\\u{ord(m.group()):04x}", s)


def print_now(data: Any, text: str, json_mode: bool) -> None:
    """For a command that runs until stopped: print its first result line now."""
    if json_mode:
        sys.stdout.write(json.dumps(data, ensure_ascii=False, default=str) + "\n")
    else:
        sys.stdout.write(safe_text(text) + "\n")
    sys.stdout.flush()


def load_config(args: argparse.Namespace) -> AppConfig:
    return appconfig.load(getattr(args, "app_dir", None), ui_dir=getattr(args, "ui_dir", None))


# ---------------------------------------------------------------- commands


def _cmd_components(args: argparse.Namespace) -> Result:
    cat = spec.catalog()
    lines = [f"Dashboard files use spec: {cat['spec']}. Components (put them in layout: rows):", ""]
    for name, c in cat["components"].items():
        lines.append(f"  {name}: {c['description']}")
        for key, s in c["settings"].items():
            req = "required" if s["required"] else "optional"
            lines.append(f"      {key} ({req}): {s['description']}")
    lines += ["", "Formats:"]
    for name, f in formats.FORMATS.items():
        lines.append(f"  {name}: {f['description']}")
    lines += ["", "Param types: " + ", ".join(cat["params"]), "", "Example:", "", cat["example"]]
    lines.append("Use --json for the complete vocabulary.")
    return Result(cat, "\n".join(lines))


def _cmd_tables(args: argparse.Namespace) -> Result:
    cfg = load_config(args)
    samples = args.samples
    if not 0 <= samples <= 10:
        raise UsageError("--samples must be a number from 0 to 10.", hint="For example: easy-ui tables --samples 3")
    if args.source:
        db = cfg.source_db()
        if not db.is_file():
            raise NotFound(
                f"The project's data file {paths.display_path(db)} does not exist yet.",
                hint="Run the project's job or command that creates its data first, or check [ui] data in project.toml.",
            )
        tables = bundle.describe_tables(db, samples=samples, immutable=False)
        allowed = {t.lower() for t in cfg.tables}
        for t in tables:
            t["in_dashboards"] = t["name"].lower() in allowed
        data = {"app": cfg.name, "from": "app data file", "bundle": None, "tables": tables}
        intro = (
            "Tables and views in the project's data file. Only those marked * are copied for dashboards "
            "([ui] tables in project.toml):"
        )
    else:
        current = bundle.require_current(cfg)
        tables = bundle.describe_tables(current, samples=samples)
        data = {"app": cfg.name, "from": "dashboard data", "bundle": current.stem, "tables": tables}
        intro = "Tables dashboards can read (the current dashboard data):"
    lines = [intro, ""]
    for t in tables:
        star = "* " if t.get("in_dashboards") else "  "
        lines.append(f"{star}{t['name']} ({t['kind']}, {t['rows']} rows)")
        for c in t["columns"]:
            sample = ", ".join(json.dumps(v, ensure_ascii=False) for v in c["samples"])
            lines.append(f"      {c['name']}  {c['type'] or '-'}" + (f"   e.g. {sample}" if sample else ""))
    if not tables:
        lines.append("  (none)")
    return Result(data, "\n".join(lines))


def _validate_without_app(files: list[str]) -> dict[str, Any]:
    entries = [spec.validate_file(f) for f in files]
    n_err = sum(len(e["errors"]) for e in entries)
    return {
        "ok": n_err == 0,
        "app": None,
        "data": {"checked": False, "bundle": None, "note": "No project.toml was found, so queries were not checked against data."},
        "dashboards": entries,
        "errors": n_err,
        "warnings": sum(len(e["warnings"]) for e in entries),
        "notes": [],
    }


def _cmd_validate(args: argparse.Namespace) -> Result:
    try:
        cfg = load_config(args)
    except NotFound:
        if not args.files or args.app_dir is not None:
            raise
        report = _validate_without_app(args.files)
    else:
        report = spec.validate_app(cfg, args.files or None, check_data=not args.no_data)
    lines = []
    for d in report["dashboards"]:
        state = "ok" if d["ok"] else f"{len(d['errors'])} problem(s)"
        lines.append(f"{d['file']}: {state}" + (f", {len(d['warnings'])} warning(s)" if d["warnings"] else ""))
        for p in d["errors"] + d["warnings"]:
            prob = spec.Problem(
                file=p["file"], where=p["where"], message=p["problem"], line=p.get("line"), hint=p.get("hint"),
                allowed=p.get("allowed"), did_you_mean=p.get("did_you_mean"), level=p["level"], place=p.get("place"),
            )
            lines.append(("  error:   " if p["level"] == "error" else "  warning: ") + prob.text())
    lines += report["notes"]
    if report["data"]["note"]:
        lines.append(report["data"]["note"])
    if report["dashboards"]:
        lines.append(
            "All dashboards are valid." if report["ok"] else
            f"Found {report['errors']} problem(s). Fix them and run easy-ui validate again."
        )
    return Result(report, "\n".join(lines), 0 if report["ok"] else 1)


def _cmd_data_build(args: argparse.Namespace) -> Result:
    cfg = load_config(args)
    if args.keep < 1:
        raise UsageError("--keep must be 1 or more.", hint="For example: easy-ui data build --keep 3")
    res = bundle.build(cfg, keep=args.keep)
    parts = ", ".join(f"{name} ({t['rows']} rows)" for name, t in res["tables"].items())
    if res["unchanged"]:
        text = f"The project's data has not changed since the last build, so the dashboards keep using it: {parts}."
    else:
        text = f"Built new dashboard data from the project's data file: {parts}. Open dashboards will show it on their next refresh."
    return Result(res, text)


def _cmd_data_list(args: argparse.Namespace) -> Result:
    cfg = load_config(args)
    items = bundle.list_bundles(cfg)
    lines = [f"{'* ' if b['current'] else '  '}{b['bundle']}  built {b['built_at']}  {b['size']} bytes" for b in items]
    text = "\n".join(lines) if lines else "No dashboard data has been built yet. Build it with: easy-ui data build"
    return Result({"app": cfg.name, "bundles": items}, text)


def _add_placeholder(sub: argparse._SubParsersAction, name: str, about: str, module: str, err: BaseException) -> None:
    p = sub.add_parser(name, help=about + " (not available)", description=about)
    p.add_argument("rest", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)

    def handler(args: argparse.Namespace) -> NoReturn:
        raise NotAvailable(
            f'The "{name}" command is not part of this installation of easy-ui, because its part ({module}) '
            "could not be loaded.",
            hint="Reinstall my-easy-ui (for example: uv sync), then try again.",
            detail=f"{type(err).__name__}: {err}",
        )

    handler.accepts_any_arguments = True  # type: ignore[attr-defined]
    p.set_defaults(func=handler)


def build_parser() -> tuple[ArgumentParser, list[str]]:
    parser = ArgumentParser(
        prog="easy-ui",
        description="Dashboards for one project: declarative views over a read-only copy of its data.",
        epilog="Every command accepts --json (only JSON on stdout), --project-dir PATH (default: the nearest folder "
        "with project.toml) and --ui-dir PATH (the project's dashboard folder, when it is not the usual one), "
        "before or after the command.",
    )
    parser.add_argument("--version", action="version", version=f"easy-ui {__version__}")
    parser.add_argument("--json", action="store_true", help="print only JSON on stdout")
    parser.add_argument("--project-dir", metavar="PATH", help="the project's folder (the one holding project.toml)")
    parser.add_argument("--ui-dir", metavar="PATH", help="the project's dashboard folder (published dashboards, data, sign-in)")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p = sub.add_parser("components", help="The dashboard vocabulary: components, settings, formats (for agents).")
    p.set_defaults(func=_cmd_components)

    p = sub.add_parser("tables", help="Tables and columns dashboards can read, with sample values.")
    p.add_argument("--source", action="store_true", help="list the project's own data file instead (marks what is copied)")
    p.add_argument("--samples", type=int, default=3, help="sample values per column (0-10, default 3)")
    p.set_defaults(func=_cmd_tables)

    p = sub.add_parser("validate", help="Check dashboard files (default: every file in ui/dashboards/).")
    p.add_argument("files", nargs="*", help="dashboard files to check")
    p.add_argument("--no-data", action="store_true", help="do not check queries against the current data")
    p.set_defaults(func=_cmd_validate)

    p = sub.add_parser("data", help="Build or list the dashboard data (a read-only copy of the allowed tables).")
    data_sub = p.add_subparsers(dest="data_command", metavar="<build|list>")
    b = data_sub.add_parser("build", help="Copy the tables listed in project.toml into new dashboard data.")
    b.add_argument("--keep", type=int, default=bundle.DEFAULT_KEEP, help="how many builds to keep (default 3)")
    b.set_defaults(func=_cmd_data_build)
    lst = data_sub.add_parser("list", help="List the builds of the dashboard data.")
    lst.set_defaults(func=_cmd_data_list)

    from easyui import demo  # noqa: PLC0415

    demo.register(sub)

    problems: list[str] = []
    for module in COMMAND_MODULES:
        try:
            mod = importlib.import_module(module)
            mod.register(sub)
        except ImportError as e:
            if os.environ.get("EASYUI_DEBUG"):
                raise
            problems.append(f"{module}: {e}")
            for name, about in PLANNED_COMMANDS[module].items():
                if name not in sub.choices:
                    _add_placeholder(sub, name, about, module, e)
    return parser, problems


# Taken off the command line before parsing, so they work before or after the command.
# --app-dir is the v0.1 name of --project-dir and still works.
_FOLDER_OPTIONS = {"--project-dir": "project_dir", "--app-dir": "project_dir", "--ui-dir": "ui_dir"}


def _extract(argv: list[str]) -> tuple[bool, dict[str, Path | None], list[str]]:
    json_mode = False
    folders: dict[str, Path | None] = {"project_dir": None, "ui_dir": None}
    rest: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--":
            rest.extend(argv[i:])
            break
        opt = a.split("=", 1)[0]
        if a == "--json":
            json_mode = True
        elif opt in _FOLDER_OPTIONS:
            if "=" not in a:
                if i + 1 >= len(argv) or not argv[i + 1]:
                    raise UsageError(f"{opt} needs a folder after it.", hint=f"For example: {opt} ./myproject")
                value = argv[i + 1]
                i += 1
            else:
                value = a.split("=", 1)[1]
            if not value:
                raise UsageError(f"{opt} needs a folder after it.", hint=f"For example: {opt} ./myproject")
            folders[_FOLDER_OPTIONS[opt]] = Path(value).expanduser()
        else:
            rest.append(a)
        i += 1
    return json_mode, folders, rest


def _emit_error(e: EasyUIError, json_mode: bool) -> None:
    if json_mode:
        print(json.dumps(e.to_dict(), ensure_ascii=False, indent=2, default=str))
        return
    sys.stderr.write(safe_text(e.message) + "\n")
    if e.hint:
        sys.stderr.write(safe_text(f"What to do: {e.hint}") + "\n")
    if e.problems and len(e.problems) > 1:
        for p in e.problems[1:]:
            sys.stderr.write(safe_text(f"  also: {p.get('place') or p.get('where', '')}: {p.get('problem', '')}") + "\n")
    if e.detail and os.environ.get("EASYUI_DEBUG"):
        sys.stderr.write(safe_text(f"Details: {e.detail}") + "\n")


def run(argv: Sequence[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    json_mode = "--json" in raw
    try:
        json_mode, folders, rest = _extract(raw)
        parser, _problems = build_parser()
        args, extra = parser.parse_known_args(rest)
        if extra and not getattr(getattr(args, "func", None), "accepts_any_arguments", False):
            parser.error(f"unrecognized arguments: {' '.join(extra)}")
        args.json = json_mode
        args.app_dir = folders["project_dir"]
        args.project_dir = folders["project_dir"]
        args.ui_dir = folders["ui_dir"]
        func = getattr(args, "func", None)
        if func is None:
            if args.command == "data":
                raise UsageError("Say what to do with the data: build or list.", hint="For example: easy-ui data build")
            if json_mode:
                raise UsageError("No command was given.", hint="See the commands with: easy-ui --help")
            parser.print_help(sys.stderr)
            return 2
        out = func(args)
    except _Exit as e:
        return e.code
    except EasyUIError as e:
        _emit_error(e, json_mode)
        return int(e.exit_code)
    except KeyboardInterrupt:
        return 130
    result = out if isinstance(out, Result) else Result(out)
    if json_mode:
        print(json.dumps(result.data, ensure_ascii=False, indent=None if result.json_line else 2, default=str))
    elif result.text is not None:
        print(safe_text(result.text))
    elif isinstance(result.data, str):
        print(safe_text(result.data))
    elif result.data is not None:
        print(json.dumps(result.data, ensure_ascii=False, indent=2, default=str))
    return int(result.exit_code)


def main(argv: Sequence[str] | None = None) -> NoReturn:
    sys.exit(run(argv))

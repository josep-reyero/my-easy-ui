"""The look of a project: its theme, stylesheets and assets, gathered, checked and described.

A project restyles its dashboards with three kinds of files in ``ui/``::

    ui/theme.yaml          named settings: colours, fonts, sizes (see easyui.theme)
    ui/styles/*.css        ordinary CSS, applied after easy-ui's own styles, in file-name order
    ui/assets/**           woff2 fonts and png, jpg, webp or svg pictures (one folder level deep at most twice)

They are read without following links (the same way dashboards are), checked
together (``check``), and published, versioned, rolled back, previewed,
exported and photographed together with the dashboards. What reaches the
browser is a ``StyleSet``: the theme turned into a generated stylesheet
(``theme.css``), the stylesheets exactly as checked, and the assets with a
content type found from their first bytes, never from their name alone.

Where custom styles apply (and where never)
--------------------------------------------
Project stylesheets apply only inside the dashboard area, which the page
draws in its own document tree (a shadow root) with easy-ui's stylesheet and
the project's ones. The top bar, the sign-in screen, the sign-in settings, the
preview badge and easy-ui's own problem pages are outside it and are never
reached by them. The generated theme stylesheet (only values easy-ui checked
itself) also colours the top bar; it is taken away on the sign-in settings and
problem pages, and the sign-in screen never loads it.

Limits
------
``MAX_STYLESHEETS`` (20) files of at most ``css.MAX_STYLESHEET_BYTES`` (200 KB),
``MAX_STYLES_BYTES`` (1 MB) together; ``MAX_ASSETS`` (60) files of at most
``MAX_ASSET_BYTES`` (2 MB), ``MAX_ASSETS_BYTES`` (8 MB) together; the theme at most
``theme.MAX_THEME_BYTES`` (64 KB).

Public API
----------
``STABLE_CLASSES``, ``DATA_ATTRIBUTES``, ``COMPONENT_SETTINGS``, ``CUSTOM_PROPERTIES`` (the documented names;
    changing one is a breaking change), ``ASSET_TYPES`` (suffix -> content type).
``Blob`` (dataclass): ``path`` (``"theme.yaml"``, ``"styles/a.css"``, ``"assets/fonts/x.woff2"``), ``label``
    (as shown, ``"ui/styles/a.css"``), ``data``, ``sha256``.
``StyleFiles`` (dataclass): ``theme`` (Blob | None), ``stylesheets``, ``assets`` (lists of Blob), ``problems``, ``notes``;
    ``blobs()``, ``empty``.
``read_style_dir(ui_dir, *, label_prefix="ui/") -> StyleFiles``   hardened reading of a project's ui folder.
``asset_type(name, data) -> (content_type | None, problem | None)``   by suffix AND first bytes; SVG content checked.
``check(files) -> StyleCheck``  ``ok``, ``errors``, ``warnings``, ``styles`` (StyleSet | None), ``summary()``.
``StyleSet``: ``theme``, ``stylesheets``, ``assets`` (dict path -> Blob), ``theme_css`` (bytes), ``rev``,
    ``public()`` -> ``{"rev", "theme_css", "stylesheets", "assets"}``, ``get(path)`` -> ``(bytes, content_type)`` | None.
``EMPTY``  the StyleSet of a project with no style files.
``style_guide() -> dict``   every stable name, setting and custom property with a description (``easy-ui style-guide``).
``validate_project(config) -> dict``   for ``easy-ui validate``: ``{"ok", "errors", "warnings", "files"}``.
``use_theme(config, name, *, force=False) -> dict``  copy a ready-made theme to ui/theme.yaml.
``restore(ui_dir, wanted, *, backup) -> (restored, set_aside)``  put a version's style files back (rollback --restore-files).
``register(subparsers)``  ``style-guide``, ``themes``, ``theme use``.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import secrets
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from easyui import css, paths, spec
from easyui import theme as themes
from easyui.errors import Refused

MAX_STYLESHEETS = 20
MAX_STYLES_BYTES = 1_000_000
MAX_ASSETS = 60
MAX_ASSET_BYTES = 2_000_000
MAX_ASSETS_BYTES = 8_000_000
MAX_ASSET_DEPTH = 3  # assets/<folder>/<folder>/<file>
ASSET_TYPES = {
    ".woff2": "font/woff2",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
}
STYLESHEET_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,60}\.css\Z")
ASSET_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}\Z")
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)

# ---------------------------------------------------------------- the documented names

STABLE_CLASSES: dict[str, str] = {
    "eui-app": "The dashboard area: the root of everything a stylesheet can reach. Carries data-project and data-theme.",
    "eui-dashboard": "One dashboard. Carries data-dashboard.",
    "eui-dashboard-title": "The dashboard's title.",
    "eui-dashboard-description": "The dashboard's description under its title.",
    "eui-status": "The small \"Checked 16:03\" line beside the title.",
    "eui-row": "One row of the layout. Carries data-row (1 for the first row).",
    "eui-component": "Every component. Carries data-type and data-id, plus the classes of its class: setting.",
    "eui-title": "A component's heading.",
    "eui-value": "The big figure of a stat (and the value in a chart's readout).",
    "eui-unit": "The unit set smaller beside a figure (km, min).",
    "eui-delta": "The change under a stat. Also has is-up, is-down or is-flat, and is-good or is-bad with better:.",
    "eui-hint": "The explanation under a stat (hint:).",
    "eui-chart": "The box a chart is drawn in. Set chart custom properties here or on the component.",
    "eui-readout": "The value shown in a chart's heading.",
    "eui-legend": "The legend of a chart with several series.",
    "eui-table": "A table (also the one behind View as table).",
    "eui-th": "A table heading cell.",
    "eui-td": "A table value cell.",
    "eui-text": "The text of a text component (paragraphs, headings, lists, links).",
    "eui-control": "One input of a controls component (a date, a choice, a number or a search box).",
    "eui-empty": "A component that has no data to show (\"No data yet\").",
    "eui-error": "A component whose data could not be loaded.",
}
DATA_ATTRIBUTES: dict[str, str] = {
    "data-project": "On .eui-app: the project's name.",
    "data-theme": "On .eui-app (and the page): light or dark, as the device shows it now.",
    "data-dashboard": "On .eui-dashboard (and the page): the dashboard's name (its file name without .yaml).",
    "data-row": "On .eui-row: the row's number, 1 for the first.",
    "data-type": "On .eui-component: stat, line, area, bar, table, text or controls.",
    "data-id": "On .eui-component: the component's id: setting, or r<row>c<column> counted from 0 when it has none.",
}
COMPONENT_SETTINGS: dict[str, str] = {
    "id": "A stable name for one component (lowercase letters, digits and -), shown as data-id. Unique in the "
          "dashboard. Without it the id is r<row>c<column>, which changes when the layout changes.",
    "class": "Space-separated names of your own (lowercase letters, digits and -, not starting with eui-), added "
             "to the component's classes for your stylesheets.",
}
_CHART_PROPS = {
    f"--eui-chart-{i}": (f"Colour of chart series {i}.", "derived from the accent") for i in range(1, 9)
}
CUSTOM_PROPERTIES: dict[str, tuple[str, str]] = {
    "--eui-accent": ("The project's colour (theme colors.accent, else project.toml [ui] accent).", "#3d6fb6"),
    "--eui-accent-text": ("The accent made dark or light enough to read as text (links, current page).", "derived"),
    "--eui-on-accent": ("Text on an accent background (buttons).", "derived"),
    "--eui-accent-soft": ("A faint accent background (highlights, the bar under the pointer).", "derived"),
    "--eui-background": ("The page behind everything.", "#f3f5f7 / dark #161a20, tinted by the accent"),
    "--eui-surface": ("Panels.", "#ffffff / dark #1d222a, tinted by the accent"),
    "--eui-text": ("Main text and figures.", "#1a212b / dark #e8ebef"),
    "--eui-muted": ("Secondary text.", "#5a6472 / dark #a0a9b5"),
    "--eui-border": ("Hairlines and edges.", "#dde2e8 / dark #2e353f"),
    "--eui-grid": ("Chart gridlines.", "#e9ecf0 / dark #272d36"),
    "--eui-good": ("A change for the better.", "the accent text colour"),
    "--eui-bad": ("A change for the worse; error marks.", "#a8321f / dark #f19a86"),
    **_CHART_PROPS,
    "--eui-font-body": ("Font of everything that is not a heading or a figure.", "system-sans"),
    "--eui-font-heading": ("Font of headings.", "book"),
    "--eui-font-numbers": ("Font of stat figures and readouts.", "system-sans"),
    "--eui-size-xs": ("Smallest text (notes, axis labels).", "0.8125rem"),
    "--eui-size-s": ("Small text (hints).", "0.875rem"),
    "--eui-size-m": ("Body text.", "1rem"),
    "--eui-size-l": ("Component headings.", "1.25rem"),
    "--eui-size-xl": ("Large headings.", "1.75rem"),
    "--eui-size-display": ("The dashboard title.", "clamp(2rem, 1.35rem + 2.3vw, 2.875rem)"),
    "--eui-size-figure": ("Stat figures.", "clamp(2.375rem, 1.7rem + 2.4vw, 3.375rem)"),
    "--eui-radius": ("Corner radius of panels.", "14px"),
    "--eui-radius-small": ("Corner radius of small parts (inputs, buttons).", "9px"),
    "--eui-gap": ("Space between components in a row.", "clamp(16px, 2.2vw, 28px)"),
    "--eui-row-gap": ("Space between rows.", "clamp(28px, 3.6vw, 44px)"),
    "--eui-pad": ("Space inside panels.", "clamp(16px, 1.8vw, 22px)"),
    "--eui-card-background": ("Background of panels (charts, tables).", "var(--eui-surface)"),
    "--eui-card-shadow": ("Edge or shadow of panels (a box-shadow value).", "a faint hairline"),
    "--eui-card-padding": ("Space inside panels.", "var(--eui-pad)"),
    "--eui-chart-height": ("Height of a chart.", "clamp(196px, 22vw, 264px)"),
    "--eui-chart-line-width": ("Width of chart lines, a number of px.", "2"),
    "--eui-chart-grid-style": ("Gridlines: solid, dashed, dotted or none.", "solid"),
    "--eui-chart-area-opacity": ("How strongly area charts are filled, 0 to 1.", "0.16"),
    "--eui-chart-bar-radius": ("Corner radius of bars, a number of px.", "3"),
    "--eui-chart-font": ("Font of axis labels.", "var(--eui-font-body)"),
    "--eui-chart-font-size": ("Size of axis labels, a number of px.", "11"),
    "--eui-chart-label": ("Colour of axis labels.", "var(--eui-muted)"),
    "--eui-chart-axis": ("Colour of the baseline.", "var(--eui-border)"),
    "--eui-chart-grid": ("Colour of gridlines.", "var(--eui-grid)"),
    "--eui-theme": ("Set to 1 by a theme file (read by easy-ui).", "unset"),
}


# ---------------------------------------------------------------- reading the files


@dataclass(frozen=True)
class Blob:
    path: str
    label: str
    data: bytes
    sha256: str


@dataclass
class StyleFiles:
    theme: Blob | None = None
    stylesheets: list[Blob] = field(default_factory=list)
    assets: list[Blob] = field(default_factory=list)
    problems: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    app_stylesheets: list[Blob] = field(default_factory=list)  # paths apps/<app name>/styles/<name>.css
    app_assets: list[Blob] = field(default_factory=list)  # paths apps/<app name>/assets/<path>

    def blobs(self) -> list[Blob]:
        return ([self.theme] if self.theme else []) + list(self.stylesheets) + list(self.assets) + \
            list(self.app_stylesheets) + list(self.app_assets)

    def add(self, b: Blob) -> None:
        """Sort a published or archived file into its place by its path."""
        if b.path == themes.THEME_FILE:
            self.theme = b
        elif b.path.startswith("styles/"):
            self.stylesheets.append(b)
        elif b.path.startswith("assets/"):
            self.assets.append(b)
        elif b.path.startswith("apps/") and b.path.split("/")[2] == "styles":
            self.app_stylesheets.append(b)
        else:
            self.app_assets.append(b)

    def sort(self) -> None:
        for items in (self.stylesheets, self.assets, self.app_stylesheets, self.app_assets):
            items.sort(key=lambda b: b.path)

    @property
    def empty(self) -> bool:
        return not self.blobs() and not self.problems


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _problem(label: str, message: str, hint: str | None = None, line: int | None = None) -> dict[str, Any]:
    d: dict[str, Any] = {"file": label, "line": line, "where": "", "problem": message}
    if hint:
        d["hint"] = hint
    d["level"] = "error"
    return d


def read_at(dir_fd: int, name: str, label: str, limit: int, what: str) -> bytes | dict[str, Any]:
    """One plain file below an open folder, without following links; a problem dict when refused."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | _O_NOFOLLOW | _O_CLOEXEC, dir_fd=dir_fd)
    except OSError as e:
        try:
            is_link = stat.S_ISLNK(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode)
        except OSError:
            is_link = False
        if is_link:
            return _problem(label, f"{label} is a link to another file, which easy-ui does not follow.",
                            "Replace the link with the real file.")
        return _problem(label, f"{label} could not be opened ({e.strerror}).", "Check that the file is readable.")
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return _problem(label, f"{label} is not a plain file, so easy-ui does not read it.",
                            f"Keep only ordinary files in {what}.")
        if st.st_size > limit:
            return _problem(label, f"{label} is {st.st_size:,} bytes, more than the {limit:,} bytes easy-ui accepts "
                            f"for {what}.", "Make it smaller.")
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                return _problem(label, f"{label} is larger than the {limit:,} bytes easy-ui accepts for {what}.",
                                "Make it smaller.")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _open_dir(parent_fd: int | None, path: str | Path, label: str) -> int | dict[str, Any] | None:
    """An open folder (no links); None when it does not exist; a problem when it is a link or a file."""
    try:
        st = os.stat(path, dir_fd=parent_fd, follow_symlinks=False) if parent_fd is not None else os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as e:
        return _problem(label, f"{label} could not be read ({e.strerror}).", "Check that the folder is readable.")
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        return _problem(label, f"{label} is a link or not a folder, so easy-ui does not read it.",
                        "Make it a real folder inside the project.")
    try:
        return os.open(path, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC, dir_fd=parent_fd)
    except OSError as e:
        return _problem(label, f"{label} could not be opened ({e.strerror}).", "Check that the folder is readable.")


def read_style_dir(ui_dir: str | Path, *, label_prefix: str = "ui/") -> StyleFiles:
    """The theme, stylesheets and assets of a project's ``ui/`` folder. Never follows links."""
    out = StyleFiles()
    ui = _open_dir(None, ui_dir, label_prefix.rstrip("/") or "ui")
    if ui is None:
        return out
    if isinstance(ui, dict):
        out.problems.append(ui)
        return out
    try:
        _read_theme(ui, out, label_prefix)
        _read_stylesheets(ui, out, label_prefix)
        _read_assets(ui, out, label_prefix)
    finally:
        os.close(ui)
    return out


APP_PATH_RE = re.compile(r"^apps/(?P<app>[a-z][a-z0-9-]{1,30})/(?P<rest>(?:styles|assets)/.+)\Z")


def read_app_styles(app: Any, out: StyleFiles) -> None:
    """An app's ``styles/*.css`` and ``assets/**`` (in its own folder), added to ``out`` under ``apps/<app>/``.

    Read the same way as the project's (no links followed, the same limits per app), from the app folder opened by
    ``easyui.project.AppFolder``, so nothing outside the app's folder is ever read.
    """
    sub = StyleFiles()
    with app.open() as folder:
        _read_stylesheets(folder.fd, sub, f"{app.label}/")
        _read_assets(folder.fd, sub, f"{app.label}/")
    for b in sub.stylesheets:
        out.app_stylesheets.append(Blob(f"apps/{app.name}/{b.path}", b.label, b.data, b.sha256))
    for b in sub.assets:
        out.app_assets.append(Blob(f"apps/{app.name}/{b.path}", b.label, b.data, b.sha256))
    out.problems.extend(sub.problems)
    out.notes.extend(sub.notes)


def read_project_look(config: Any, apps: Any = None) -> StyleFiles:
    """The project's look (``ui/``) and every app's styles and assets, in ``[project] apps`` order."""
    out = read_style_dir(Path(config.app_dir) / "ui")
    for app in (config.apps if apps is None else apps):
        read_app_styles(app, out)
    return out


def _read_theme(ui: int, out: StyleFiles, prefix: str) -> None:
    label = f"{prefix}{themes.THEME_FILE}"
    try:
        os.stat(themes.THEME_FILE, dir_fd=ui, follow_symlinks=False)
    except FileNotFoundError:
        if _exists(ui, "theme.yml"):
            out.notes.append(f"{prefix}theme.yml is ignored; rename it to theme.yaml.")
        return
    got = read_at(ui, themes.THEME_FILE, label, themes.MAX_THEME_BYTES, "a theme file")
    if isinstance(got, dict):
        out.problems.append(got)
    else:
        out.theme = Blob(themes.THEME_FILE, label, got, _sha(got))


def _exists(dir_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return False
    return True


def _read_stylesheets(ui: int, out: StyleFiles, prefix: str) -> None:
    label_dir = f"{prefix}styles"
    d = _open_dir(ui, "styles", label_dir)
    if d is None:
        return
    if isinstance(d, dict):
        out.problems.append(d)
        return
    try:
        with os.scandir(d) as it:
            names = sorted(e.name for e in it)
        candidates, other = [], []
        for name in names:
            if name.startswith("."):
                continue
            if not name.endswith(".css"):
                other.append(name)
                continue
            candidates.append(name)
        if other:
            out.notes.append(f"Only .css files in {label_dir}/ are used; these were left out: {', '.join(other[:5])}"
                             + (" and others." if len(other) > 5 else "."))
        if len(candidates) > MAX_STYLESHEETS:
            out.problems.append(_problem(label_dir, f"There are {len(candidates)} stylesheets in {label_dir}/, more than "
                                         f"the {MAX_STYLESHEETS} easy-ui accepts.", "Join some of them into one file."))
            return
        total = 0
        for name in candidates:
            label = f"{label_dir}/{name}"
            if not STYLESHEET_RE.match(name):
                out.problems.append(_problem(label, f'The stylesheet name "{name}" cannot be used.',
                                             "Use lowercase letters, digits, '.', '-' and '_', ending in .css, for "
                                             "example 10-layout.css (files are applied in name order)."))
                continue
            got = read_at(d, name, label, css.MAX_STYLESHEET_BYTES, "a stylesheet")
            if isinstance(got, dict):
                out.problems.append(got)
                continue
            total += len(got)
            out.stylesheets.append(Blob(f"styles/{name}", label, got, _sha(got)))
        if total > MAX_STYLES_BYTES:
            out.problems.append(_problem(label_dir, f"The stylesheets are {total:,} bytes together, more than the "
                                         f"{MAX_STYLES_BYTES:,} easy-ui accepts.", "Remove rules that are not used."))
    finally:
        os.close(d)


def _read_assets(ui: int, out: StyleFiles, prefix: str) -> None:
    root_label = f"{prefix}assets"
    d = _open_dir(ui, "assets", root_label)
    if d is None:
        return
    if isinstance(d, dict):
        out.problems.append(d)
        return
    total = 0
    count = 0

    def walk(fd: int, rel: str, depth: int) -> None:
        nonlocal total, count
        with os.scandir(fd) as it:
            entries = sorted((e.name, e.is_dir(follow_symlinks=False)) for e in it)
        for name, is_dir in entries:
            if name.startswith("."):
                continue
            path = f"{rel}{name}"
            label = f"{root_label}/{path}"
            if not ASSET_SEGMENT_RE.match(name):
                out.problems.append(_problem(label, f'The name "{name}" cannot be used in ui/assets/.',
                                             "Use letters, digits, '.', '-' and '_', starting with a letter or digit."))
                continue
            if is_dir:
                if depth + 1 >= MAX_ASSET_DEPTH:
                    out.problems.append(_problem(label, f"{label} is nested too deep for ui/assets/.",
                                                 f"Keep files at most {MAX_ASSET_DEPTH - 1} folders deep."))
                    continue
                sub = _open_dir(fd, name, label)
                if isinstance(sub, dict):
                    out.problems.append(sub)
                    continue
                if sub is None:
                    continue
                try:
                    walk(sub, f"{path}/", depth + 1)
                finally:
                    os.close(sub)
                continue
            count += 1
            if count > MAX_ASSETS:
                if count == MAX_ASSETS + 1:
                    out.problems.append(_problem(root_label, f"There are more than {MAX_ASSETS} files in ui/assets/.",
                                                 "Keep only the fonts and pictures the stylesheets use."))
                continue
            got = read_at(fd, name, label, MAX_ASSET_BYTES, "a font or picture")
            if isinstance(got, dict):
                out.problems.append(got)
                continue
            total += len(got)
            out.assets.append(Blob(f"assets/{path}", label, got, _sha(got)))

    try:
        walk(d, "", 0)
    finally:
        os.close(d)
    if total > MAX_ASSETS_BYTES:
        out.problems.append(_problem(root_label, f"The files in ui/assets/ are {total:,} bytes together, more than the "
                                     f"{MAX_ASSETS_BYTES:,} easy-ui accepts.", "Make the pictures smaller (webp is small)."))


# ---------------------------------------------------------------- assets


SVG_NS = "http://www.w3.org/2000/svg"
_XLINK_NS = "http://www.w3.org/1999/xlink"
_XML_NS = "http://www.w3.org/XML/1998/namespace"

# Pictures are checked against an allowlist, not a blocklist: the file is parsed as XML
# (no DOCTYPE, no entities, no processing instructions), every element must be one of
# these, in the SVG namespace (or in none), whatever prefix it is written with, and every
# attribute must be one of _SVG_ATTRIBUTES. Scripts, foreignObject, <style>, <image>,
# links (<a>) and every animation element (set, animate*, which can rewrite an href) are
# not on the list, so they are refused however they are spelled.
_SVG_ELEMENTS = frozenset("""
svg g defs symbol use title desc path rect circle ellipse line polyline polygon text tspan textPath
linearGradient radialGradient stop clipPath mask pattern marker filter
feBlend feColorMatrix feComponentTransfer feComposite feConvolveMatrix feDiffuseLighting feDisplacementMap
feDistantLight feDropShadow feFlood feFuncA feFuncB feFuncG feFuncR feGaussianBlur feMerge feMergeNode
feMorphology feOffset fePointLight feSpecularLighting feSpotLight feTile feTurbulence
""".split())
_SVG_ATTRIBUTES = frozenset("""
id class style lang tabindex role aria-label aria-hidden aria-labelledby aria-describedby focusable version
x y x1 y1 x2 y2 cx cy r rx ry fx fy fr width height viewBox preserveAspectRatio d points pathLength transform
fill fill-opacity fill-rule stroke stroke-width stroke-linecap stroke-linejoin stroke-miterlimit
stroke-dasharray stroke-dashoffset stroke-opacity opacity color display visibility overflow
clip-path clip-rule clipPathUnits mask maskUnits maskContentUnits mask-type
filter filterUnits primitiveUnits gradientUnits gradientTransform spreadMethod offset stop-color stop-opacity
patternUnits patternContentUnits patternTransform markerWidth markerHeight markerUnits refX refY orient
marker-start marker-mid marker-end font-family font-size font-weight font-style font-variant font-stretch
text-anchor dominant-baseline alignment-baseline baseline-shift letter-spacing word-spacing text-decoration
writing-mode direction unicode-bidi dx dy rotate textLength lengthAdjust startOffset method spacing side
vector-effect shape-rendering text-rendering image-rendering color-rendering paint-order mix-blend-mode isolation
in in2 result stdDeviation flood-color flood-opacity lighting-color mode operator k1 k2 k3 k4 type values
tableValues slope intercept amplitude exponent order kernelMatrix divisor bias targetX targetY edgeMode
kernelUnitLength preserveAlpha surfaceScale diffuseConstant specularConstant specularExponent
azimuth elevation pointsAtX pointsAtY pointsAtZ limitingConeAngle z scale xChannelSelector yChannelSelector
radius baseFrequency numOctaves seed stitchTiles color-interpolation color-interpolation-filters href
""".split())
_URL_FRAGMENT = re.compile(r"url\(\s*(['\"]?)#[A-Za-z_][A-Za-z0-9_.:-]*\1\s*\)", re.I)


class _SvgRefused(Exception):
    pass


def _svg_value_problem(local: str, value: str) -> str | None:
    """Why an attribute value is refused, or None. ``value`` is already decoded (entities resolved)."""
    if local == "href":
        if not re.fullmatch(r"#[A-Za-z_][A-Za-z0-9_.:-]*", value.strip()):
            return "links to another file (only #fragment links are allowed)"
        return None
    rest = _URL_FRAGMENT.sub("", value)
    low = rest.lower()
    if "url(" in low or "image(" in low or "image-set(" in low:
        return "refers to another file with url() (only url(#...) is allowed)"
    if "\\" in rest:
        return "has an escaped character (\\) in a value, which can hide a link"
    if "@import" in low or "expression(" in low or "javascript:" in low:
        return "has a value that loads or runs something"
    return None


def svg_problem(data: bytes) -> str | None:
    """Why an SVG picture is refused, or None when it holds only plain drawing.

    The file is parsed as XML with a parser that refuses a DOCTYPE, entities and processing
    instructions; elements and attributes are matched by local name and namespace against an
    allowlist, so a prefix (``<x:script>``) never changes the outcome.
    """
    from xml.parsers import expat

    parser = expat.ParserCreate(encoding="utf-8", namespace_separator=" ")
    seen_root: list[str] = []

    def split(name: str) -> tuple[str, str]:
        ns, _sp, local = name.rpartition(" ")
        return ns, local

    def doctype(*_a):
        raise _SvgRefused("declares a DOCTYPE or entities")

    def entity(*_a):
        raise _SvgRefused("declares a DOCTYPE or entities")

    def pi(target, _data):
        raise _SvgRefused("has a processing instruction (for example a linked stylesheet)")

    def start(name, attrs):
        ns, local = split(name)
        if ns not in ("", SVG_NS):
            raise _SvgRefused(f"has an element from another vocabulary ({local})")
        if local not in _SVG_ELEMENTS:
            raise _SvgRefused(f"has a <{local}> element (only drawing elements are allowed: no script, "
                              "style, image, link or animation)")
        if not seen_root:
            if local != "svg":
                raise _SvgRefused("does not start with an <svg> element")
            seen_root.append(local)
        for key, value in attrs.items():
            ans, alocal = split(key)
            if ans == _XLINK_NS and alocal == "href":
                pass
            elif ans == _XML_NS and alocal in ("space", "lang"):
                continue
            elif ans:
                raise _SvgRefused(f"has an attribute from another vocabulary ({alocal})")
            elif alocal not in _SVG_ATTRIBUTES:
                raise _SvgRefused(f"has the attribute {alocal}, which is not a drawing attribute")
            why = _svg_value_problem(alocal, value)
            if why:
                raise _SvgRefused(why)

    parser.StartDoctypeDeclHandler = doctype
    parser.EntityDeclHandler = entity
    parser.UnparsedEntityDeclHandler = entity
    parser.NotationDeclHandler = entity
    parser.ExternalEntityRefHandler = lambda *_a: (_ for _ in ()).throw(_SvgRefused("declares a DOCTYPE or entities"))
    parser.ProcessingInstructionHandler = pi
    parser.StartElementHandler = start
    try:
        parser.Parse(data, True)
    except _SvgRefused as e:
        return str(e)
    except expat.ExpatError as e:
        return f"is not well-formed XML ({expat.ErrorString(e.code)}, line {e.lineno})"
    if not seen_root:
        return "does not hold an <svg> element"
    return None


def asset_type(name: str, data: bytes) -> tuple[str | None, str | None]:
    """The content type of an asset, from its suffix and its first bytes; or why it is refused."""
    suffix = Path(name).suffix.lower()
    ctype = ASSET_TYPES.get(suffix)
    if ctype is None:
        return None, (f"{name} is not a kind of file easy-ui serves from ui/assets/ (it serves .woff2 fonts and .png, "
                      ".jpg, .webp and .svg pictures).")
    head = data[:16]
    ok = {
        ".woff2": head.startswith(b"wOF2"),
        ".png": head.startswith(b"\x89PNG\r\n\x1a\n"),
        ".jpg": head.startswith(b"\xff\xd8\xff"),
        ".jpeg": head.startswith(b"\xff\xd8\xff"),
        ".webp": head[:4] == b"RIFF" and head[8:12] == b"WEBP",
    }
    if suffix == ".svg":
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            return None, f"{name} is not a text SVG file (it is not UTF-8)."
        body = data.lstrip(b"\xef\xbb\xbf").lstrip()
        if not (body.startswith(b"<svg") or body.startswith(b"<?xml") or body.startswith(b"<!--")) \
                or b"<svg" not in body:
            return None, f"{name} does not look like an SVG picture (it should start with <svg or <?xml)."
        why = svg_problem(data)
        if why:
            return None, (f"{name} was refused because it {why}. A picture may hold only drawing, never anything "
                          "that runs or loads. Save it again as a plain SVG (shapes, paths, text and gradients only), "
                          "or use a .png or .webp picture instead.")
        return ctype, None
    if not ok[suffix]:
        kind = {".woff2": "a WOFF2 font", ".png": "a PNG picture", ".jpg": "a JPEG picture", ".jpeg": "a JPEG picture",
                ".webp": "a WebP picture"}[suffix]
        return None, f"{name} is named like {kind}, but its content is not {kind}."
    return ctype, None


# ---------------------------------------------------------------- the checked set


@dataclass
class StyleSet:
    theme: themes.Theme | None = None
    stylesheets: list[Blob] = field(default_factory=list)
    assets: dict[str, Blob] = field(default_factory=dict)
    types: dict[str, str] = field(default_factory=dict)
    theme_css: bytes = b""
    has_theme_css: bool = False
    rev: str = "none"
    theme_source: Blob | None = None
    app_stylesheets: list[Blob] = field(default_factory=list)  # as served: scoped to the app's own content
    app_assets: dict[str, Blob] = field(default_factory=dict)  # apps/<app>/assets/<path> -> Blob

    def public(self) -> dict[str, Any]:
        return {
            "rev": self.rev,
            "theme_css": self.has_theme_css,
            "stylesheets": [b.path[len("styles/"):] for b in self.stylesheets],
            "app_stylesheets": [b.path for b in self.app_stylesheets],
            "assets": len(self.assets) + len(self.app_assets),
        }

    def get(self, path: str) -> tuple[bytes, str] | None:
        """A served file: ``theme.css``, ``styles/<name>`` or ``assets/<path>``."""
        if path == "theme.css":
            return (self.theme_css, "text/css; charset=utf-8") if self.has_theme_css else None
        if path.startswith("styles/"):
            for b in self.stylesheets:
                if b.path == path:
                    return b.data, "text/css; charset=utf-8"
            return None
        if path.startswith("assets/"):
            rel = path[len("assets/"):]
            b = self.assets.get(rel)
            return (b.data, self.types[rel]) if b is not None else None
        if path.startswith("apps/"):
            for b in self.app_stylesheets:
                if b.path == path:
                    return b.data, "text/css; charset=utf-8"
            b = self.app_assets.get(path)
            return (b.data, self.types[path]) if b is not None else None
        return None

    def files(self) -> dict[str, bytes]:
        """Every file as it is served, for an export: ``theme.css``, ``styles/...``, ``assets/...``."""
        out: dict[str, bytes] = {}
        if self.has_theme_css:
            out["theme.css"] = self.theme_css
        for b in self.stylesheets:
            out[b.path] = b.data
        for rel, b in self.assets.items():
            out[f"assets/{rel}"] = b.data
        for b in self.app_stylesheets:
            out[b.path] = b.data
        for path, b in self.app_assets.items():
            out[path] = b.data
        return out


EMPTY = StyleSet()


@dataclass
class StyleCheck:
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    styles: StyleSet | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def check(files: StyleFiles) -> StyleCheck:
    """Check the theme, every stylesheet and every asset together; with no errors, the StyleSet to serve."""
    res = StyleCheck(errors=list(files.problems), notes=list(files.notes))
    assets: dict[str, Blob] = {}
    types: dict[str, str] = {}
    for b in files.assets:
        rel = b.path[len("assets/"):]
        ctype, why = asset_type(rel, b.data)
        if why:
            res.errors.append(_problem(b.label, why, "Use .woff2 fonts and .png, .jpg, .webp or plain .svg pictures."))
            continue
        assets[rel] = b
        types[rel] = ctype  # type: ignore[assignment]
    asset_names = set(assets) | {b.path[len("assets/"):] for b in files.assets}
    theme_obj = None
    if files.theme is not None:
        try:
            text = files.theme.data.decode("utf-8")
        except UnicodeDecodeError:
            res.errors.append(_problem(files.theme.label, f"{files.theme.label} is not UTF-8 text.", "Save it as UTF-8 text."))
        else:
            theme_obj, errs, warns = themes.check_theme(text, file=files.theme.label, assets=asset_names)
            res.errors.extend(errs)
            res.warnings.extend(warns)
    names = [b.path[len("styles/"):] for b in files.stylesheets]
    font_faces: list[str] = []
    for b in files.stylesheets:
        try:
            text = b.data.decode("utf-8")
        except UnicodeDecodeError:
            res.errors.append(_problem(b.label, f"{b.label} is not UTF-8 text.", "Save it as UTF-8 text."))
            continue
        c = css.check_stylesheet(
            text, file=b.label, name=b.path[len("styles/"):], stylesheets=names, assets=asset_names,
            classes=STABLE_CLASSES, properties=CUSTOM_PROPERTIES, data_types=spec.COMPONENT_TYPES,
        )
        res.errors.extend(c.errors)
        res.warnings.extend(c.warnings)
        font_faces.extend(c.font_faces)
    app_sheets, app_assets, app_faces = _check_apps(files, res, types)
    if res.errors:
        return res
    h = hashlib.sha256()
    for b in files.blobs():
        h.update(f"{b.path}\0{b.sha256}\n".encode())
    faces = app_faces + font_faces  # the project's last, so its own fonts win
    generated = themes.theme_css(theme_obj, faces)
    res.styles = StyleSet(
        theme=theme_obj, stylesheets=list(files.stylesheets), assets=assets, types=types,
        theme_css=generated.encode("utf-8"), has_theme_css=theme_obj is not None or bool(faces),
        rev=h.hexdigest()[:16] if files.blobs() else "none", theme_source=files.theme,
        app_stylesheets=app_sheets, app_assets=app_assets,
    )
    return res


def _app_words(p: dict[str, Any]) -> dict[str, Any]:
    """Problems of an app's stylesheet speak of the app's folders, not the project's ui/."""
    for k in ("problem", "hint"):
        if isinstance(p.get(k), str):
            p[k] = (p[k].replace("ui/assets/", "the app's assets/ folder").replace("ui/styles/", "the app's styles/ "
                    "folder"))
    return p


def _check_apps(files: StyleFiles, res: StyleCheck, types: dict[str, str]) -> tuple[list[Blob], dict[str, Blob], list[str]]:
    """Each app's assets and stylesheets: checked against the app's own files, then scoped to the app's content."""
    by_app: dict[str, tuple[list[Blob], list[Blob]]] = {}
    for b in files.app_stylesheets + files.app_assets:
        m = APP_PATH_RE.match(b.path)
        if m is None:
            res.errors.append(_problem(b.label, f"{b.label} is not a file easy-ui serves for an app.",
                                       "Keep an app's look in its styles/ and assets/ folders."))
            continue
        sheets, assets = by_app.setdefault(m.group("app"), ([], []))
        (sheets if m.group("rest").startswith("styles/") else assets).append(b)
    served: list[Blob] = []
    served_assets: dict[str, Blob] = {}
    faces: list[str] = []
    for app, (sheets, assets) in by_app.items():
        base = f"apps/{app}/"
        names: set[str] = set()
        for b in assets:
            rel = b.path[len(base) + len("assets/"):]
            ctype, why = asset_type(rel, b.data)
            names.add(rel)
            if why:
                res.errors.append(_problem(b.label, why, "Use .woff2 fonts and .png, .jpg, .webp or plain .svg pictures."))
                continue
            served_assets[b.path] = b
            types[b.path] = ctype  # type: ignore[assignment]
        sheet_names = [b.path[len(base) + len("styles/"):] for b in sheets]
        for b in sorted(sheets, key=lambda x: x.path):
            try:
                text = b.data.decode("utf-8")
            except UnicodeDecodeError:
                res.errors.append(_problem(b.label, f"{b.label} is not UTF-8 text.", "Save it as UTF-8 text."))
                continue
            c = css.check_stylesheet(
                text, file=b.label, name=b.path[len(base) + len("styles/"):], stylesheets=sheet_names, assets=names,
                classes=STABLE_CLASSES, properties=CUSTOM_PROPERTIES, data_types=spec.COMPONENT_TYPES, url_prefix=base,
            )
            errs = c.errors + css.app_at_rule_problems(text, file=b.label)
            res.errors.extend(_app_words(e) for e in errs)
            res.warnings.extend(_app_words(w) for w in c.warnings)
            faces.extend(c.font_faces)
            if not errs:
                scoped = css.scope_stylesheet(text, app).encode("utf-8")
                served.append(Blob(b.path, b.label, scoped, _sha(scoped)))
    return served, served_assets, faces


def summary(files: StyleFiles) -> dict[str, Any]:
    return {
        "theme": files.theme.label if files.theme else None,
        "stylesheets": [b.label for b in files.stylesheets],
        "assets": [b.label for b in files.assets],
        "app_stylesheets": [b.label for b in files.app_stylesheets],
        "app_assets": [b.label for b in files.app_assets],
    }


def validate_project(config: Any) -> dict[str, Any]:
    files = read_project_look(config)
    res = check(files)
    return {"ok": res.ok, "errors": res.errors, "warnings": res.warnings, "files": summary(files),
            "notes": res.notes}


# ---------------------------------------------------------------- the style guide


def style_guide() -> dict[str, Any]:
    return {
        "about": "Stable names easy-ui puts on the page for stylesheets in ui/styles/*.css, the custom properties "
                 "they and ui/theme.yaml can set, and the theme settings. Changing a name is a breaking change.",
        "where": "Stylesheets apply inside the dashboard area (.eui-app) only. The top bar, sign-in, sign-in settings, "
                 "the preview badge and easy-ui's problem pages always keep easy-ui's own look.",
        "files": {
            "ui/theme.yaml": "Named settings, no CSS (see theme).",
            "ui/styles/*.css": f"Ordinary CSS, applied after easy-ui's styles in file-name order (at most "
                               f"{MAX_STYLESHEETS} files).",
            "ui/assets/": "woff2 fonts and png, jpg, webp and svg pictures; from a stylesheet: url(\"../assets/NAME\").",
        },
        "classes": {f".{k}": v for k, v in STABLE_CLASSES.items()},
        "data_attributes": dict(DATA_ATTRIBUTES),
        "component_settings": dict(COMPONENT_SETTINGS),
        "component_types": list(spec.COMPONENT_TYPES),
        "custom_properties": {k: {"about": v[0], "default": v[1]} for k, v in CUSTOM_PROPERTIES.items()},
        "theme": themes.catalog(),
        "rules": [
            "url() and @import only with relative paths to files in ui/assets/ and ui/styles/",
            "no expression(), behavior:, -moz-binding, element(), src(), @namespace or @document",
            "@font-face names may not be the names of fonts easy-ui's own screens use (Arial, Georgia, system-ui, ...)",
            "charts read their colours, fonts, line width and gridlines from the --eui-chart-* custom properties of "
            "their own element, so [data-id=\"weekly\"] { --eui-chart-1: #b0383a; } restyles one chart",
        ],
        "example": (
            "/* ui/styles/10-look.css */\n"
            "@font-face { font-family: \"Field Notes\"; src: url(\"../assets/fonts/field-notes.woff2\") format(\"woff2\"); }\n"
            ".eui-app { --eui-font-heading: \"Field Notes\", serif; }\n"
            ".eui-dashboard-title { letter-spacing: -0.02em; }\n"
            "[data-id=\"weekly\"] { --eui-chart-1: #b0383a; --eui-chart-grid-style: dashed; }\n"
            "@media (max-width: 560px) { .eui-row { gap: 12px; } }\n"
        ),
    }


# ---------------------------------------------------------------- ready-made themes


APP_THEME_RE = re.compile(r"^(?P<app>[a-z][a-z0-9-]{1,30})/(?P<name>[a-z][a-z0-9-]{0,30})\Z")


def app_themes(config: Any, apps: Any = None) -> tuple[list[dict[str, Any]], list[str]]:
    """Ready-made themes the project's apps ship in ``<app folder>/themes/<name>.yaml``, named ``<app>/<name>``.

    Each is read like every app file (no links, at most ``theme.MAX_THEME_BYTES``) and listed only when it passes
    the theme checks without font files (an app's theme uses the built-in font stacks). Returns (themes, notes).
    """
    out: list[dict[str, Any]] = []
    notes: list[str] = []
    for app in (config.apps if apps is None else apps):
        with app.open() as folder:
            dfd = folder.open_dir("themes")
            if dfd is None:
                continue
            try:
                with os.scandir(dfd) as it:
                    names = sorted(e.name for e in it if e.name.endswith(".yaml") and not e.name.startswith("."))
            finally:
                os.close(dfd)
            for fname in names[:40]:
                stem = fname[: -len(".yaml")]
                if not re.match(r"^[a-z][a-z0-9-]{0,30}\Z", stem):
                    continue
                got = folder.read(f"themes/{fname}", themes.MAX_THEME_BYTES, "a theme file")
                label = app.file_label(f"themes/{fname}")
                if isinstance(got, dict):
                    notes.append(f"{label} was left out: {got['problem']}")
                    continue
                try:
                    text = got.decode("utf-8")
                except UnicodeDecodeError:
                    notes.append(f"{label} was left out: it is not UTF-8 text.")
                    continue
                theme, errors, _w = themes.check_theme(text, file=label)
                if theme is None or errors:
                    notes.append(f"{label} was left out: {errors[0]['problem'] if errors else 'it is not a theme'}")
                    continue
                out.append({"name": f"{app.name}/{stem}", "title": theme.data.get("name", stem),
                            "description": theme.data.get("description"), "app": app.name, "file": text})
    return out, notes


def use_theme(config: Any, name: str, *, force: bool = False) -> dict[str, Any]:
    if isinstance(name, str) and APP_THEME_RE.match(name):
        found, _notes = app_themes(config)
        hit = next((t for t in found if t["name"] == name), None)
        if hit is None:
            from easyui.errors import NotFound  # noqa: PLC0415

            raise NotFound(f"None of the project's apps has a theme called {name[:70]!r}.",
                           hint="See the themes with: easy-ui themes")
        text = hit["file"]
    else:
        text = themes.ready_made_text(name)
    ui = Path(config.app_dir) / "ui"
    for p in (ui,):
        if p.is_symlink():
            raise Refused(f"{paths.display_path(p, config.app_dir)} is a link, so easy-ui does not write through it.",
                          hint="Make it a real folder inside the project.", code="link_refused")
    ui.mkdir(parents=True, exist_ok=True)
    target = ui / themes.THEME_FILE
    existed = os.path.lexists(target)
    if target.is_symlink():
        raise Refused("ui/theme.yaml is a link, so easy-ui does not write through it.",
                      hint="Remove the link, then try again.", code="link_refused")
    if existed and not force:
        raise Refused(
            "ui/theme.yaml already exists, so it was not replaced (your changes in it would be lost).",
            hint=f"To replace it anyway: easy-ui theme use {name} --force. Or copy the settings you want by hand "
                 f"(see the theme with: easy-ui themes --json).",
            code="theme_exists",
        )
    tmp = ui / f".theme.yaml.{secrets.token_hex(4)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC, 0o644)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, target)
    return {"app": config.name, "theme": name, "file": "ui/theme.yaml", "replaced": existed}


# ---------------------------------------------------------------- restoring a version's files


def _no_links_on_the_way(ui_dir: Path, rel: str) -> None:
    """Refuse when a folder between ui/ and ``rel`` (or ``rel`` itself) is a link: writing would leave the project."""
    parts = Path(rel).parts
    if not parts or any(x in ("", ".", "..") for x in parts) or Path(rel).is_absolute():
        raise Refused(f"ui/{rel} is not a file name easy-ui writes.", hint="This version's files look damaged.",
                      code="link_refused")
    here = ui_dir
    for part in parts:
        here = here / part
        if here.is_symlink():
            raise Refused(f"{'ui/' + str(here.relative_to(ui_dir))} is a link, so easy-ui does not write through it.",
                          hint="Replace the link with a real folder or file, then try again.", code="link_refused")


def restore(ui_dir: Path, wanted: dict[str, bytes], *, backup: Path) -> tuple[list[str], list[str]]:
    """Make ui/theme.yaml, ui/styles/ and ui/assets/ hold exactly ``wanted`` (path -> data).

    Files that would be replaced or are not in ``wanted`` are moved under ``backup`` first. Never follows links.
    """
    current = read_style_dir(ui_dir)
    for p in (ui_dir / "styles", ui_dir / "assets", ui_dir / themes.THEME_FILE):
        if p.is_symlink():
            raise Refused(f"ui/{p.name} is a link, so easy-ui does not write through it.",
                          hint="Replace the link with a real folder or file.", code="link_refused")
    set_aside: list[str] = []
    for b in current.blobs():
        if wanted.get(b.path) == b.data:
            continue
        dest = backup / b.path
        dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.replace(ui_dir / b.path, dest)
        set_aside.append(f"ui/{b.path}")
    restored = []
    for rel, data in sorted(wanted.items()):
        target = ui_dir / rel
        _no_links_on_the_way(ui_dir, rel)
        if target.exists() and not target.is_symlink() and target.read_bytes() == data:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        _no_links_on_the_way(ui_dir, rel)  # nothing was swapped for a link while the folders were made
        tmp = target.parent / f".{target.name}.restore-{secrets.token_hex(4)}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC, 0o644)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        os.replace(tmp, target)
        restored.append(f"ui/{rel}")
    return restored, set_aside


# ---------------------------------------------------------------- command line


def _cmd_style_guide(args: argparse.Namespace) -> Any:
    from easyui.cli import Result  # noqa: PLC0415

    g = style_guide()
    lines = [g["about"], g["where"], "", "Files:"]
    lines += [f"  {k}  {v}" for k, v in g["files"].items()]
    lines += ["", "Classes:"] + [f"  {k}  {v}" for k, v in g["classes"].items()]
    lines += ["", "Data attributes:"] + [f"  [{k}]  {v}" for k, v in g["data_attributes"].items()]
    lines += ["", "Component settings (in dashboard files):"] + [f"  {k}:  {v}" for k, v in g["component_settings"].items()]
    lines += ["", "Custom properties (default in brackets):"]
    lines += [f"  {k}  {v['about']} [{v['default']}]" for k, v in g["custom_properties"].items()]
    lines += ["", "Theme settings (ui/theme.yaml; a dashboard's theme: takes "
              + ", ".join(g["theme"]["dashboard_overrides"]) + "):"]
    lines += [f"  {k}:  {v}" for k, v in g["theme"]["settings"].items()]
    lines += ["", "Font stacks: " + ", ".join(g["theme"]["fonts"]), "", "Rules:"] + [f"  - {r}" for r in g["rules"]]
    lines += ["", "Example:", "", g["example"], "Use --json for all of this as data."]
    return Result(g, "\n".join(lines))


def _cmd_themes(args: argparse.Namespace) -> Any:
    from easyui.cli import Result  # noqa: PLC0415

    from easyui import appconfig  # noqa: PLC0415
    from easyui.errors import EasyUIError  # noqa: PLC0415

    items = themes.ready_made()
    for t in items:
        t["file"] = themes.ready_made_text(t["name"])
        t["app"] = None
    notes: list[str] = []
    try:  # inside a project, its apps' themes too
        cfg = appconfig.load(getattr(args, "app_dir", None))
        extra, notes = app_themes(cfg)
    except EasyUIError:
        extra = []
    items += extra
    lines = ["Ready-made themes (copy one into ui/theme.yaml with: easy-ui theme use NAME):", ""]
    lines += [f"  {t['name']:<12} {t['description']}" + (f"  (from the app {t['app']})" if t["app"] else "")
              for t in items]
    lines += notes
    return Result({"themes": items, "notes": notes}, "\n".join(lines))


def _cmd_theme_use(args: argparse.Namespace) -> Any:
    from easyui.cli import Result, load_config  # noqa: PLC0415

    cfg = load_config(args)
    res = use_theme(cfg, args.name, force=args.force)
    verb = "Replaced" if res["replaced"] else "Wrote"
    text = (f"{verb} ui/theme.yaml with the {args.name} theme. Change any setting in it (see: easy-ui style-guide); "
            "easy-ui preview shows each saved change, and easy-ui publish makes it the look people see.")
    return Result(res, text)


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "style-guide", help="Every stable name and custom property stylesheets can use.",
        description="List the class names, data attributes, component settings, custom properties and theme "
        "settings that ui/styles/*.css and ui/theme.yaml can use, with a description of each (--json for agents).")
    p.set_defaults(func=_cmd_style_guide)
    p = subparsers.add_parser("themes", help="List the ready-made themes.",
                              description="List the themes that ship with easy-ui (copy one with: easy-ui theme use NAME).")
    p.set_defaults(func=_cmd_themes)
    p = subparsers.add_parser("theme", help="Copy a ready-made theme into the project.",
                              description="Work with the project's theme file ui/theme.yaml.")
    sub = p.add_subparsers(dest="theme_command", metavar="use")
    sub.required = True
    u = sub.add_parser("use", help="Copy a ready-made theme into ui/theme.yaml.",
                       description="Copy a ready-made theme (see: easy-ui themes) into ui/theme.yaml as a starting "
                       "point. An existing ui/theme.yaml is never replaced without --force.")
    u.add_argument("name", help="the theme's name, for example dusk (or <app>/<name> for a theme an app ships)")
    u.add_argument("--force", action="store_true", help="replace an existing ui/theme.yaml")
    u.set_defaults(func=_cmd_theme_use)


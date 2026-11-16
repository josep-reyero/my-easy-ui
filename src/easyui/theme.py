"""The theme file ``ui/theme.yaml``: named look settings (no CSS), checked and turned into CSS custom properties.

A theme is the quick way to change how the dashboards look: colours (with
separate values for dark mode), fonts (easy-ui's built-in font stacks or font
files the project ships in ``ui/assets/``), a type scale, corner radius,
density, card style and chart details. Every value becomes a ``--eui-…``
custom property in a stylesheet easy-ui generates (``ui/theme.css`` on the
server), so project stylesheets and charts read the same names.

Everything is checked before it is used: unknown settings say what is
allowed and the closest match, colours must be ``#rgb`` or ``#rrggbb``, font
names are a fixed list or a family the theme itself declares, and low contrast
between text and background colours is a warning. A dashboard file may carry a
``theme:`` with the same settings (except ``name``, ``description`` and
``font_files``) to restyle that one dashboard.

Ready-made themes ship with easy-ui (``easyui/themes/*.yaml``):
``easy-ui themes`` lists them, ``easy-ui theme use NAME`` copies one into the
project as a starting point.

Public API
----------
``THEME_FILE`` (``"theme.yaml"``), ``MAX_THEME_BYTES``, ``FONT_STACKS`` (name -> (stack, description)),
``COLOR_KEYS`` (name -> description), ``SETTINGS`` (top-level key -> description), ``OVERRIDE_KEYS``,
``DENSITIES``, ``CARD_STYLES``, ``GRID_STYLES``.
``Theme`` (dataclass): ``data`` (the checked settings), ``tokens()`` -> ``{"both", "light", "dark"}`` (property -> value).
``check_theme(text, *, file="ui/theme.yaml", assets=()) -> (Theme | None, errors, warnings)``  never raises.
``check_overrides(ctx, path, raw) -> dict | None``   a dashboard's ``theme:`` (``ctx`` is ``spec._Ctx``);
    the compiled form ``{"both", "light", "dark"}`` for the browser.
``theme_css(theme, font_faces=()) -> str``   the generated stylesheet (custom properties, light and dark, @font-face).
``contrast(a, b) -> float``                  WCAG contrast ratio of two ``#rrggbb`` colours.
``ready_made() -> list[{"name", "title", "description"}]``,  ``ready_made_text(name) -> str`` (raises ``NotFound``).
``catalog() -> dict``  the theme settings for ``easy-ui style-guide``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from easyui import css
from easyui.errors import NotFound

THEME_FILE = "theme.yaml"
MAX_THEME_BYTES = 64_000
THEMES_DIR = Path(__file__).resolve().parent / "themes"
_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})\Z")
_THEME_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}\Z")

FONT_STACKS: dict[str, tuple[str, str]] = {
    "system-sans": ('system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", "Noto Sans", '
                    '"Liberation Sans", Arial, sans-serif', "The computer's own interface font (easy-ui's default)."),
    "book": ('"Iowan Old Style", "Palatino Linotype", Palatino, "URW Palladio L", P052, "Book Antiqua", Georgia, serif',
             "A calm book serif (easy-ui's default for headings)."),
    "transitional": ('Charter, "Bitstream Charter", "Sitka Text", Cambria, "Noto Serif", serif',
                     "A sturdy reading serif."),
    "didone": ('Didot, "Bodoni MT", "Noto Serif Display", "URW Palladio L", P052, Sylfaen, serif',
               "A high-contrast display serif, for large headings."),
    "slab": ('Rockwell, "Rockwell Nova", "Roboto Slab", "DejaVu Serif", "Sitka Small", serif',
             "A slab serif with square feet: sturdy, a little technical."),
    "humanist": ('Seravek, "Gill Sans Nova", Ubuntu, Calibri, "DejaVu Sans", source-sans-pro, sans-serif',
                 "A warm, handwritten-feeling sans."),
    "geometric": ('Avenir, Montserrat, Corbel, "URW Gothic", source-sans-pro, sans-serif',
                  "A round, geometric sans."),
    "grotesque": ('"Helvetica Neue", "Arial Nova", "Nimbus Sans", Inter, Roboto, Arial, sans-serif',
                  "A plain, neutral sans in the Swiss tradition."),
    "rounded": ('ui-rounded, "Hiragino Maru Gothic ProN", Quicksand, Comfortaa, Manjari, "Arial Rounded MT", '
                '"Arial Rounded MT Bold", Calibri, source-sans-pro, sans-serif', "A sans with rounded ends: soft, friendly."),
    "mono": ('ui-monospace, "Cascadia Code", "Source Code Pro", Menlo, Consolas, "DejaVu Sans Mono", monospace',
             "A monospaced font: every character the same width."),
}
FONT_ROLES = {"body": ("--eui-font-body", "system-sans", "Everything that is not a heading or a figure."),
              "headings": ("--eui-font-heading", "book", "Page and section headings."),
              "numbers": ("--eui-font-numbers", "system-sans", "The big figures of stats and readouts.")}

COLOR_KEYS: dict[str, str] = {
    "accent": "The project's colour: the current page, links, focus rings and the first chart series.",
    "background": "The page behind everything.",
    "surface": "Panels: charts, tables and other raised parts.",
    "text": "Main text and figures.",
    "muted": "Secondary text: hints, labels, axis labels.",
    "border": "Hairlines and panel edges.",
    "grid": "The faint gridlines across charts.",
    "good": "A change for the better (with better: on a stat).",
    "bad": "A change for the worse, and error marks.",
    "chart": "The series colours of charts, in order (1 to 8 colours).",
}
DENSITIES = {"compact": (0.7, 0.62, 0.75), "normal": (1.0, 1.0, 1.0), "roomy": (1.3, 1.35, 1.2)}
CARD_STYLES = ("flat", "outline", "raised", "plain")
GRID_STYLES = ("solid", "dashed", "dotted", "none")
SETTINGS: dict[str, str] = {
    "name": "A short name for the theme.",
    "description": "One sentence about it.",
    "colors": "Colours for light mode: " + ", ".join(COLOR_KEYS) + ". Write them in quotes: \"#2f6fdf\".",
    "dark": "The same colours for dark mode. Colours not given here use easy-ui's dark values (the accent the "
            "light one).",
    "fonts": "Which font each part uses: body, headings, numbers. A built-in stack (" + ", ".join(FONT_STACKS) +
             ") or a family declared under font_files.",
    "font_files": "Font families the project ships as .woff2 files in ui/assets/: Family: [{file, weight, style}].",
    "type": "size: the base text size in px (12 to 22, default 16); scale: how much larger each heading step is "
            "(1.1 to 1.6, default 1.25).",
    "radius": "Corner radius of panels in px (0 to 32, default 14).",
    "density": "Space between and inside parts: compact, normal or roomy.",
    "cards": "How panels look: flat (a filled panel, the default), outline (a line around it), raised (a soft "
             "shadow) or plain (no panel at all).",
    "chart": "line_width (0.5 to 6 px, default 2), grid (solid, dashed, dotted or none), area_opacity (0 to 1, "
             "default 0.16) and bar_radius (0 to 12 px, default 3).",
}
TOP_KEYS = tuple(SETTINGS)
OVERRIDE_KEYS = ("colors", "dark", "fonts", "type", "radius", "density", "cards", "chart")
CHART_KEYS = ("line_width", "grid", "area_opacity", "bar_radius")
TYPE_KEYS = ("size", "scale")
FONT_FILE_KEYS = ("file", "weight", "style")
MAX_FONT_FILES = 16

_BAD = object()


# ---------------------------------------------------------------- colour maths


def _rgb(hex_: str) -> tuple[float, float, float]:
    h = hex_.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def _lum(hex_: str) -> float:
    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (lin(c) for c in _rgb(hex_))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    x, y = sorted((_lum(a), _lum(b)), reverse=True)
    return (x + 0.05) / (y + 0.05)


def _norm_hex(v: str) -> str:
    h = v.lstrip("#").lower()
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return "#" + h


# Easy-ui's own values, for contrast checks of a theme that sets only some colours.
BUILTIN_COLORS = {
    "light": {"background": "#f3f5f7", "surface": "#ffffff", "text": "#1a212b", "muted": "#5a6472",
              "border": "#dde2e8", "grid": "#e9ecf0", "good": "#2f6f4f", "bad": "#a8321f", "accent": "#3d6fb6"},
    "dark": {"background": "#161a20", "surface": "#1d222a", "text": "#e8ebef", "muted": "#a0a9b5",
             "border": "#2e353f", "grid": "#272d36", "good": "#7fc8a0", "bad": "#f19a86", "accent": "#3d6fb6"},
}


# ---------------------------------------------------------------- the checked theme


@dataclass
class Theme:
    data: dict[str, Any] = field(default_factory=dict)

    def tokens(self) -> dict[str, dict[str, str]]:
        return _tokens(self.data)


def _tokens(d: dict[str, Any]) -> dict[str, dict[str, str]]:
    both: dict[str, str] = {}
    light: dict[str, str] = {}
    dark: dict[str, str] = {}
    for scheme, out in (("colors", light), ("dark", dark)):
        for k, v in (d.get(scheme) or {}).items():
            if k == "chart":
                for i, c in enumerate(v, 1):
                    out[f"--eui-chart-{i}"] = c
            else:
                out[f"--eui-{k}"] = v
    fonts = d.get("fonts") or {}
    files = d.get("font_families") or {}
    for role, (prop, _default, _about) in FONT_ROLES.items():
        if role in fonts:
            both[prop] = _font_value(fonts[role], role, files)
    t = d.get("type") or {}
    if t:
        size = float(t.get("size", 16))
        scale = float(t.get("scale", 1.25))
        b = size / 16

        def r(x: float) -> str:
            return f"{round(x, 4):g}rem"

        both["--eui-size-xs"] = r(b * scale ** -0.93)
        both["--eui-size-s"] = r(b * scale ** -0.6)
        both["--eui-size-m"] = r(b)
        both["--eui-size-l"] = r(b * scale)
        both["--eui-size-xl"] = r(b * scale ** 2.5)
        lo, hi = b * scale ** 3.1, b * scale ** 4.7
        both["--eui-size-display"] = f"clamp({r(lo)}, calc({r(lo * 0.675)} + 2.3vw), {r(hi)})"
        lo, hi = b * scale ** 3.9, b * scale ** 5.5
        both["--eui-size-figure"] = f"clamp({r(lo)}, calc({r(lo * 0.716)} + 2.4vw), {r(hi)})"
    if "radius" in d:
        rad = float(d["radius"])
        both["--eui-radius"] = f"{rad:g}px"
        both["--eui-radius-small"] = f"{round(rad * 0.64):g}px"
    if "density" in d:
        g, rg, p = DENSITIES[d["density"]]
        both["--eui-gap"] = f"clamp({round(16 * g)}px, {round(2.2 * g, 2):g}vw, {round(28 * g)}px)"
        both["--eui-row-gap"] = f"clamp({round(28 * rg)}px, {round(3.6 * rg, 2):g}vw, {round(44 * rg)}px)"
        both["--eui-pad"] = f"clamp({round(16 * p)}px, {round(1.8 * p, 2):g}vw, {round(22 * p)}px)"
    if "cards" in d:
        style = d["cards"]
        if style == "flat":
            both["--eui-card-background"] = "var(--eui-surface)"
            both["--eui-card-shadow"] = "inset 0 0 0 1px color-mix(in srgb, var(--eui-border) 55%, transparent)"
            both["--eui-card-padding"] = "var(--eui-pad)"
        elif style == "outline":
            both["--eui-card-background"] = "transparent"
            both["--eui-card-shadow"] = "inset 0 0 0 1px var(--eui-border)"
            both["--eui-card-padding"] = "var(--eui-pad)"
        elif style == "raised":
            both["--eui-card-background"] = "var(--eui-surface)"
            light["--eui-card-shadow"] = "0 1px 2px rgb(20 28 40 / 0.06), 0 14px 34px -18px rgb(20 28 40 / 0.28)"
            dark["--eui-card-shadow"] = "0 1px 2px rgb(0 0 0 / 0.5), 0 14px 34px -16px rgb(0 0 0 / 0.7)"
            both["--eui-card-padding"] = "var(--eui-pad)"
        else:  # plain
            both["--eui-card-background"] = "transparent"
            both["--eui-card-shadow"] = "none"
            both["--eui-card-padding"] = "0px"
    ch = d.get("chart") or {}
    if "line_width" in ch:
        both["--eui-chart-line-width"] = f"{float(ch['line_width']):g}"
    if "grid" in ch:
        both["--eui-chart-grid-style"] = ch["grid"]
    if "area_opacity" in ch:
        both["--eui-chart-area-opacity"] = f"{float(ch['area_opacity']):g}"
    if "bar_radius" in ch:
        both["--eui-chart-bar-radius"] = f"{float(ch['bar_radius']):g}"
    return {"both": both, "light": light, "dark": dark}


def _font_value(name: str, role: str, families: dict[str, Any]) -> str:
    if name in FONT_STACKS:
        return FONT_STACKS[name][0]
    fallback = FONT_STACKS["book" if role == "headings" else "system-sans"][0]
    return f'"{name}", {fallback}'


# ---------------------------------------------------------------- checking


def _ctx(file: str, lines: dict, data: Any):  # noqa: ANN202 - spec._Ctx
    from easyui import spec  # noqa: PLC0415 - spec imports this module for dashboards' theme:

    return spec._Ctx(file, lines, data)


def _number(ctx: Any, path: tuple, v: Any, lo: float, hi: float, what: str, example: str) -> Any:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or not lo <= v <= hi:
        ctx.err(path, f"{what} must be a number from {lo:g} to {hi:g}.", hint=f"For example: {example}")
        return _BAD
    return v


def _choice(ctx: Any, path: tuple, v: Any, allowed: tuple[str, ...], what: str) -> Any:
    if v not in allowed:
        ctx.err(path, f"{what} is {v!r}, but it can only be one of: {', '.join(allowed)}.", allowed=list(allowed),
                got=str(v))
        return _BAD
    return v


def _color(ctx: Any, path: tuple, key: str, v: Any) -> Any:
    if v is None:
        ctx.err(path, f'"{key}" has no value. In YAML a colour starting with # must be in quotes, or it is read as a '
                "comment.", hint=f'Write it in quotes, for example {key}: "#2f6fdf"')
        return _BAD
    if not isinstance(v, str) or not _HEX_RE.match(v.strip()):
        ctx.err(path, f'"{key}" is {str(v)[:40]!r}, which is not a colour easy-ui reads.',
                hint=f'Write it as "#rrggbb" in quotes, for example {key}: "#2f6fdf"')
        return _BAD
    return _norm_hex(v.strip())


def _colors(ctx: Any, path: tuple, raw: Any) -> Any:
    if not isinstance(raw, dict):
        ctx.err(path, f"{path[-1]} must be a set of colours (name: \"#rrggbb\" lines).",
                hint='For example:\n  accent: "#2f6fdf"\n  background: "#f4f6f8"')
        return _BAD
    out: dict[str, Any] = {}
    for k, v in raw.items():
        p = path + (k,)
        if k not in COLOR_KEYS:
            ctx.err(p, f'"{k}" is not a colour of a theme.', allowed=list(COLOR_KEYS), got=str(k),
                    hint="Remove it or use one of the allowed names.")
            continue
        if k == "chart":
            items = v if isinstance(v, list) else None
            if not items or len(items) > 8:
                ctx.err(p, "chart must be a list of 1 to 8 colours.", hint='For example: chart: ["#2f6fdf", "#d9822b"]')
                continue
            got = [_color(ctx, p + (i,), f"chart colour {i + 1}", c) for i, c in enumerate(items)]
            if all(g is not _BAD for g in got):
                out[k] = got
            continue
        c = _color(ctx, p, k, v)
        if c is not _BAD:
            out[k] = c
    return out


def _fonts(ctx: Any, path: tuple, raw: Any, families: list[str] | None) -> Any:
    if not isinstance(raw, dict):
        ctx.err(path, "fonts must say which font each part uses (body, headings, numbers).",
                hint="For example:\n  body: humanist\n  headings: book")
        return _BAD
    out: dict[str, str] = {}
    names = list(FONT_STACKS) + list(families or [])
    for k, v in raw.items():
        p = path + (k,)
        if k not in FONT_ROLES:
            ctx.err(p, f'"{k}" is not a part a font can be chosen for.', allowed=list(FONT_ROLES), got=str(k))
            continue
        if not isinstance(v, str) or not v.strip():
            ctx.err(p, f"The font of {k} must be a name.", allowed=list(FONT_STACKS))
            continue
        v = v.strip()
        if v in FONT_STACKS or (families is not None and v in families):
            out[k] = v
            continue
        if families is None and css.FONT_FAMILY_RE.match(v) and v.lower() not in css.RESERVED_FONT_NAMES \
                and not css._closest(v, list(FONT_STACKS)):
            out[k] = v  # a dashboard naming a family the project theme declares (checked with the theme)
            continue
        ctx.err(p, f'The font "{v[:40]}" is not one of easy-ui\'s font stacks'
                + (" nor a family declared under font_files." if families is not None else "."),
                allowed=names, got=v, hint="Use a built-in stack, or ship the font as a .woff2 file in ui/assets/ "
                "and declare it under font_files in ui/theme.yaml.")
    return out


def _font_files(ctx: Any, path: tuple, raw: Any, assets: set[str]) -> Any:
    if not isinstance(raw, dict) or not raw:
        ctx.err(path, "font_files must name font families, each with its .woff2 files.",
                hint="For example:\n  font_files:\n    Field Notes:\n      - {file: fonts/field-notes.woff2, weight: 400}")
        return _BAD
    out: dict[str, list[dict[str, Any]]] = {}
    count = 0
    for fam, files in raw.items():
        p = path + (fam,)
        if not isinstance(fam, str) or not css.FONT_FAMILY_RE.match(fam):
            ctx.err(p, f"The font family name {str(fam)[:40]!r} cannot be used.",
                    hint="Use letters, digits, spaces, - and _ (up to 48 characters), starting with a letter.")
            continue
        if fam.lower() in css.RESERVED_FONT_NAMES or fam in FONT_STACKS:
            ctx.err(p, f'The family name "{fam}" is taken by easy-ui\'s own screens or font stacks.',
                    hint=f'Give it a name of its own, for example "{fam} Project".')
            continue
        items = files if isinstance(files, list) else [files]
        got = []
        for i, item in enumerate(items):
            ip = p + (i,) if isinstance(files, list) else p
            if isinstance(item, str):
                item = {"file": item}
            if not isinstance(item, dict) or "file" not in item:
                ctx.err(ip, "Each font file is {file, weight, style} (weight and style optional).",
                        hint="For example: {file: fonts/field-notes-bold.woff2, weight: 700}")
                continue
            for k in item:
                if k not in FONT_FILE_KEYS:
                    ctx.err(ip + (k,), f'"{k}" is not a setting of a font file.', allowed=list(FONT_FILE_KEYS), got=str(k))
            f = item["file"]
            target, why = css.resolve_ref(f, "assets") if isinstance(f, str) else (None, "It is not text.")
            if why or target is None:
                ctx.err(ip + ("file",), f"The font file is refused: {why}",
                        hint="Name a .woff2 file inside ui/assets/, relative to that folder, for example fonts/a.woff2.")
                continue
            rel = target[len("assets/"):]
            if not rel.endswith(".woff2"):
                ctx.err(ip + ("file",), f"{rel} is not a .woff2 font file.", hint="Fonts must be .woff2 files.")
                continue
            if rel not in assets:
                ctx.err(ip + ("file",), f"There is no font file ui/assets/{rel}.", hint="Add it, or fix the name.",
                        allowed=sorted(a for a in assets if a.endswith(".woff2")) or None, got=rel)
                continue
            weight = item.get("weight", 400)
            if isinstance(weight, bool) or not isinstance(weight, int) or not 1 <= weight <= 1000:
                ctx.err(ip + ("weight",), "weight must be a whole number from 1 to 1000 (400 regular, 700 bold).")
                continue
            style = item.get("style", "normal")
            if style not in ("normal", "italic"):
                ctx.err(ip + ("style",), "style must be normal or italic.", allowed=["normal", "italic"], got=str(style))
                continue
            count += 1
            got.append({"file": rel, "weight": weight, "style": style})
        if got:
            out[fam] = got
    if count > MAX_FONT_FILES:
        ctx.err(path, f"The theme declares {count} font files; at most {MAX_FONT_FILES} are allowed.",
                hint="Ship fewer weights (regular and bold are usually enough).")
        return _BAD
    return out


def _settings(ctx: Any, data: dict[str, Any], path: tuple, keys: tuple[str, ...], assets: set[str] | None,
              families: list[str] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in data:
        if k not in keys:
            ctx.err(path + (k,), f'"{k}" is not a setting of a theme.', allowed=list(keys), got=str(k),
                    hint="Remove it or use one of the allowed settings (see: easy-ui style-guide).")
    if "font_files" in keys and "font_files" in data:
        ff = _font_files(ctx, path + ("font_files",), data["font_files"], assets or set())
        if ff is not _BAD:
            out["font_families"] = ff
            families = list(ff)
        else:
            families = list(data["font_files"]) if isinstance(data["font_files"], dict) else []
    for k in ("name", "description"):
        if k in keys and k in data:
            v = data[k]
            limit = 60 if k == "name" else 300
            if not isinstance(v, str) or not v.strip() or len(v) > limit:
                ctx.err(path + (k,), f"{k} must be text of at most {limit} characters.")
            else:
                out[k] = v.strip()
    for k in ("colors", "dark"):
        if k in data:
            got = _colors(ctx, path + (k,), data[k])
            if got is not _BAD:
                out[k] = got
    if "fonts" in data:
        got = _fonts(ctx, path + ("fonts",), data["fonts"], families)
        if got is not _BAD:
            out["fonts"] = got
    if "type" in data:
        t = data["type"]
        if not isinstance(t, dict):
            ctx.err(path + ("type",), "type must be settings: size and scale.", hint="For example:\n  size: 16\n  scale: 1.25")
        else:
            tout = {}
            for k in t:
                if k not in TYPE_KEYS:
                    ctx.err(path + ("type", k), f'"{k}" is not a setting of type.', allowed=list(TYPE_KEYS), got=str(k))
            if "size" in t:
                v = _number(ctx, path + ("type", "size"), t["size"], 12, 22, "type size", "size: 16")
                if v is not _BAD:
                    tout["size"] = v
            if "scale" in t:
                v = _number(ctx, path + ("type", "scale"), t["scale"], 1.1, 1.6, "type scale", "scale: 1.25")
                if v is not _BAD:
                    tout["scale"] = v
            if tout:
                out["type"] = tout
    if "radius" in data:
        v = _number(ctx, path + ("radius",), data["radius"], 0, 32, "radius", "radius: 14")
        if v is not _BAD:
            out["radius"] = v
    if "density" in data:
        v = _choice(ctx, path + ("density",), data["density"], tuple(DENSITIES), "density")
        if v is not _BAD:
            out["density"] = v
    if "cards" in data:
        v = _choice(ctx, path + ("cards",), data["cards"], CARD_STYLES, "cards")
        if v is not _BAD:
            out["cards"] = v
    if "chart" in data:
        ch = data["chart"]
        if not isinstance(ch, dict):
            ctx.err(path + ("chart",), "chart must be settings: " + ", ".join(CHART_KEYS) + ".")
        else:
            cout: dict[str, Any] = {}
            for k in ch:
                if k not in CHART_KEYS:
                    ctx.err(path + ("chart", k), f'"{k}" is not a chart setting of a theme.', allowed=list(CHART_KEYS),
                            got=str(k))
            limits = {"line_width": (0.5, 6, "line_width: 2"), "area_opacity": (0, 1, "area_opacity: 0.16"),
                      "bar_radius": (0, 12, "bar_radius: 3")}
            for k, (lo, hi, ex) in limits.items():
                if k in ch:
                    v = _number(ctx, path + ("chart", k), ch[k], lo, hi, k, ex)
                    if v is not _BAD:
                        cout[k] = v
            if "grid" in ch:
                v = _choice(ctx, path + ("chart", "grid"), ch["grid"], GRID_STYLES, "grid")
                if v is not _BAD:
                    cout["grid"] = v
            if cout:
                out["chart"] = cout
    return out


_PAIRS = (
    ("text", "background", 4.5, "text on the page"),
    ("text", "surface", 4.5, "text on panels"),
    ("muted", "background", 3.0, "secondary text on the page"),
    ("muted", "surface", 3.0, "secondary text on panels"),
    ("accent", "background", 3.0, "the accent on the page"),
    ("good", "surface", 3.0, "a good change"),
    ("bad", "surface", 3.0, "a bad change"),
)


def _contrast_warnings(ctx: Any, path: tuple, d: dict[str, Any]) -> None:
    for scheme, key in (("light", "colors"), ("dark", "dark")):
        given = d.get(key) or {}
        if not given:
            continue
        vals = {**BUILTIN_COLORS[scheme], **{k: v for k, v in given.items() if k != "chart"}}
        if scheme == "dark" and "accent" not in given and "accent" in (d.get("colors") or {}):
            vals["accent"] = d["colors"]["accent"]
        for fg, bg, need, what in _PAIRS:
            if fg not in given and bg not in given:
                continue
            ratio = contrast(vals[fg], vals[bg])
            if ratio < need:
                ctx.warn(path + (key, fg if fg in given else bg),
                         f"In {scheme} mode, {what} has low contrast ({fg} {vals[fg]} on {bg} {vals[bg]}: "
                         f"{ratio:.1f}:1, at least {need:g}:1 is easy to read), so some people will struggle to read it.",
                         hint=f"Make {fg} darker or lighter, or change {bg}.")
        for i, c in enumerate(given.get("chart") or []):
            ratio = contrast(c, vals["surface"])
            if ratio < 2.0:
                ctx.warn(path + (key, "chart", i), f"In {scheme} mode, chart colour {i + 1} ({c}) barely stands out "
                         f"from the panel ({ratio:.1f}:1).", hint="Pick a colour further from surface.")


def check_theme(text: str, *, file: str = "ui/theme.yaml", assets: Any = ()) -> tuple[Theme | None, list[dict], list[dict]]:
    from easyui import spec  # noqa: PLC0415

    ctx = _ctx(file, {}, None)
    if len(text.encode("utf-8", "surrogatepass")) > MAX_THEME_BYTES:
        ctx.err((), f"The theme file is larger than {MAX_THEME_BYTES // 1000} KB, more than a theme needs.",
                hint="Keep only the settings (see: easy-ui style-guide).")
        return None, [p.to_dict() for p in ctx.errors], []
    try:
        data, lines = spec._parse_yaml(text)
    except spec._YamlProblem as e:
        msg = e.message.replace("dashboard files", "theme files").replace("no dashboard needs", "no theme needs")
        return None, [spec.Problem(file, "", msg, e.line, e.hint).to_dict()], []
    ctx.lines, ctx.data = lines, data
    if data is None:
        data = {}
    if not isinstance(data, dict):
        ctx.err((), "The theme file must be a set of settings (colors:, fonts:, ...).",
                hint="Start from a ready-made one: easy-ui themes, then easy-ui theme use NAME")
        return None, [p.to_dict() for p in ctx.errors], []
    out = _settings(ctx, data, (), TOP_KEYS, set(assets), [])
    _contrast_warnings(ctx, (), out)
    errors = [p.to_dict() for p in ctx.errors]
    warnings = [p.to_dict() for p in ctx.warnings]
    return (None if errors else Theme(out)), errors, warnings


def check_overrides(ctx: Any, path: tuple, raw: Any) -> dict[str, dict[str, str]] | None:
    """A dashboard's ``theme:``; reports into ``ctx`` (``spec._Ctx``); the tokens for the browser, or None."""
    if not isinstance(raw, dict) or not raw:
        ctx.err(path, "theme must be a set of theme settings for this dashboard.",
                hint="For example:\ntheme:\n  colors:\n    accent: \"#7a3e8e\"\n  cards: outline")
        return None
    before = len(ctx.errors)
    out = _settings(ctx, raw, path, OVERRIDE_KEYS, None, None)
    _contrast_warnings(ctx, path, out)
    if len(ctx.errors) > before:
        return None
    return _tokens(out)


# ---------------------------------------------------------------- the generated stylesheet


def _block(selector: str, props: dict[str, str], indent: str = "") -> str:
    if not props:
        return ""
    inner = "".join(f"{indent}  {k}: {v};\n" for k, v in props.items())
    return f"{indent}{selector} {{\n{inner}{indent}}}\n"


def theme_css(theme: Theme | None, font_faces: list[str] | tuple[str, ...] = ()) -> str:
    """The stylesheet easy-ui serves as ``ui/theme.css``: only values it checked itself, and font files."""
    parts = ["/* Made by easy-ui from ui/theme.yaml and the @font-face rules of ui/styles/. Do not edit. */\n"]
    if theme is not None:
        tok = theme.tokens()
        parts.append(_block(":root", {"--eui-theme": "1", **tok["both"], **tok["light"]}))
        if tok["dark"]:
            parts.append("@media (prefers-color-scheme: dark) {\n" + _block(":root", tok["dark"], "  ") + "}\n")
        for fam, files in (theme.data.get("font_families") or {}).items():
            for f in files:
                parts.append(
                    "@font-face {\n"
                    f'  font-family: "{fam}";\n'
                    f'  src: url("assets/{f["file"]}") format("woff2");\n'
                    f"  font-weight: {f['weight']};\n"
                    f"  font-style: {f['style']};\n"
                    "  font-display: swap;\n"
                    "}\n"
                )
    parts.extend(f"{ff}\n" for ff in font_faces)
    return "".join(parts)


# ---------------------------------------------------------------- ready-made themes


def ready_made() -> list[dict[str, Any]]:
    out = []
    for p in sorted(THEMES_DIR.glob("*.yaml")):
        theme, errors, _w = check_theme(p.read_text(encoding="utf-8"), file=p.name)
        if theme is None:  # pragma: no cover - the shipped themes are tested
            continue
        out.append({"name": p.stem, "title": theme.data.get("name", p.stem),
                    "description": theme.data.get("description")})
    return out


def ready_made_text(name: str) -> str:
    names = [t["name"] for t in ready_made()]
    if not isinstance(name, str) or not _THEME_NAME_RE.match(name) or name not in names:
        raise NotFound(f"There is no ready-made theme called {str(name)[:40]!r}.",
                       hint=f"The ready-made themes are: {', '.join(names)}. See them with: easy-ui themes")
    return (THEMES_DIR / f"{name}.yaml").read_text(encoding="utf-8")


def catalog() -> dict[str, Any]:
    return {
        "file": "ui/theme.yaml",
        "settings": dict(SETTINGS),
        "colors": dict(COLOR_KEYS),
        "fonts": {k: {"stack": v[0], "about": v[1]} for k, v in FONT_STACKS.items()},
        "font_roles": {k: {"property": v[0], "default": v[1], "about": v[2]} for k, v in FONT_ROLES.items()},
        "density": list(DENSITIES),
        "cards": list(CARD_STYLES),
        "chart_grid": list(GRID_STYLES),
        "dashboard_overrides": list(OVERRIDE_KEYS),
    }

"""Project stylesheets (``ui/styles/*.css``): a CSS tokenizer and the checks that keep them safe.

A project may restyle its dashboards with real CSS. The browser's own rules
already stop a stylesheet from running code or reaching another site (the
page's Content-Security-Policy allows styles, fonts and images only from its
own origin). These checks add a second, explainable layer, so a mistake or a
tricked agent is caught before anything is published:

- every ``url()`` (also ``image-set()``, ``image()`` and ``cross-fade()``
  strings) must be a relative path to a file in ``ui/assets/``, and every
  ``@import`` a relative path to another file in ``ui/styles/``. Schemes
  (``https:``, ``data:``, ``javascript:``), absolute paths, ``//host``,
  ``?``, ``#``, ``%`` and ``\\`` are refused, and so is a path that leaves
  those folders or names a file that does not exist; ``var()``, ``env()``, ``attr()``,
  ``if()`` and ``inherit()`` inside those functions, a quoted ``@font-face`` ``src`` and a custom
  property whose text is a web address are refused too (an address put together at run time);
- ``expression()``, ``behavior:``, ``-moz-binding``, ``element()`` /
  ``-moz-element()``, ``src()``, ``@namespace`` and ``@document`` are refused;
- unknown at-rules, unclosed blocks, strings and ``url(`` are errors;
- ``@font-face`` may not reuse the name of a font easy-ui's own screens use
  (so a project font can never change how the sign-in or the preview badge
  looks), and its rules are collected: fonts declared inside the dashboard
  area would not load there (browsers ignore ``@font-face`` inside a shadow
  root), so easy-ui serves them in its generated theme stylesheet;
- class names starting with ``eui-`` and custom properties starting with
  ``--eui-`` that easy-ui does not document are reported (as warnings) with
  the closest documented name, and so are unknown ``data-type`` values.

The tokenizer follows CSS Syntax Level 3 (escapes, ``url(`` tokens, comments,
strings), so ``u\\72l(x)`` is seen as the ``url(x)`` it is, the same way the
browser reads it.

Public API
----------
``MAX_STYLESHEET_BYTES``, ``ALLOWED_AT_RULES``, ``RESERVED_FONT_NAMES``
``Token`` (dataclass): ``kind``, ``value``, ``line``, ``start``, ``end``, ``extra``.
``tokenize(text) -> list[Token]``   never raises; bad strings and urls become ``bad-string`` / ``bad-url``.
``resolve_ref(ref, base) -> (target | None, problem | None)``
    ``base`` is ``"styles"`` (a stylesheet) or ``"assets"`` (the theme's font files);
    ``target`` is ``"assets/<path>"`` or ``"styles/<name>"``.
``check_stylesheet(text, *, file, name, stylesheets, assets, classes, properties, data_types) -> CssCheck``
``scope_stylesheet(text, app) -> str``   an app's stylesheet limited to elements marked ``data-app="<app>"``.
``inline_refs(text, target_of, data_url) -> (text | None, why | None)``   every file a stylesheet names put inside
    it as a ``data:`` address (a single-file export); refuses ``@import``.
``app_at_rule_problems(text, *, file) -> list``   at-rules an app's stylesheet may not use (``@scope``, ``@page``).
``CssCheck`` (dataclass): ``errors``, ``warnings`` (problem dicts like ``spec.Problem.to_dict()``),
    ``font_faces`` (each ``@font-face`` rule as CSS text, its urls rewritten relative to ``ui/``),
    ``refs`` (every file it refers to), ``ok``.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

MAX_STYLESHEET_BYTES = 200_000
MAX_DEPTH = 24
MAX_PROBLEMS = 50  # problems (and, apart, warnings) listed per stylesheet
ALLOWED_AT_RULES = (
    "media", "supports", "layer", "container", "keyframes", "-webkit-keyframes", "font-face", "import",
    "page", "property", "counter-style", "font-feature-values", "font-palette-values", "scope",
    "starting-style", "position-try", "view-transition", "charset",
    # descriptors' blocks inside @font-feature-values and @page
    "swash", "annotation", "ornaments", "stylistic", "styleset", "character-variant",
    "top-left-corner", "top-left", "top-center", "top-right", "top-right-corner", "bottom-left-corner",
    "bottom-left", "bottom-center", "bottom-right", "bottom-right-corner", "left-top", "left-middle",
    "left-bottom", "right-top", "right-middle", "right-bottom",
)
REFUSED_AT_RULES = {
    "namespace": "@namespace changes how every selector matches, which easy-ui does not allow.",
    "document": "@document applies styles by web address, which easy-ui does not allow.",
    "-moz-document": "@-moz-document applies styles by web address, which easy-ui does not allow.",
}
REFUSED_FUNCTIONS = {
    "expression": "expression() runs code in old browsers, so easy-ui refuses it.",
    "element": "element() paints another part of the page, so easy-ui refuses it.",
    "-moz-element": "-moz-element() paints another part of the page, so easy-ui refuses it.",
    "src": "src() loads a file by an address built at run time, so easy-ui refuses it.",
    "-webkit-image-set": None,  # allowed: its strings are checked like url()
}
REFUSED_PROPERTIES = {
    "behavior": "behavior: runs code in old browsers, so easy-ui refuses it.",
    "-moz-binding": "-moz-binding runs code in old browsers, so easy-ui refuses it.",
}
URL_STRING_FUNCTIONS = ("image-set", "-webkit-image-set", "image", "cross-fade", "-webkit-cross-fade")
_LOADING_FUNCTIONS = frozenset(("url", *URL_STRING_FUNCTIONS))
# Functions whose result is only known when the page is drawn: inside a function that loads a file they would let
# an address be put together where the checks cannot see it.
_SUBSTITUTING_FUNCTIONS = frozenset(("var", "env", "attr", "if", "inherit"))
# A quoted text that is a web address (another site, a protocol-relative address, or a data:/javascript:/blob: link).
_ADDRESS_RE = re.compile(r"\s*(?://|[A-Za-z][A-Za-z0-9+.-]*://|(?:data|javascript|blob|file|filesystem):)", re.I)
# Fonts easy-ui's own screens (sign-in, settings, the preview badge) are drawn with; a project's
# @font-face may not take these names, so it can never change how those screens look.
# Every font family easy-ui's own screens name (web/assets/style.css; a test keeps the two in step), the generic
# families and the CSS-wide keywords: a project @font-face may not take any of these names.
RESERVED_FONT_NAMES = frozenset(n.lower() for n in (
    "system-ui", "-apple-system", "blinkmacsystemfont", "segoe ui", "roboto", "helvetica neue", "helvetica",
    "noto sans", "liberation sans", "arial", "iowan old style", "palatino linotype", "palatino",
    "urw palladio l", "p052", "book antiqua", "georgia", "sans-serif", "serif", "monospace", "cursive",
    "fantasy", "ui-sans-serif", "ui-serif", "ui-monospace", "ui-rounded", "menlo", "consolas",
    "courier new", "courier", "times new roman", "times", "sf pro", "sf pro text", "sf pro display",
    "sf mono", "liberation mono",
    "inherit", "initial", "unset", "default",
))
FONT_FAMILY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 _-]{0,47}\Z")
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}\Z")


# ---------------------------------------------------------------- tokenizer


@dataclass
class Token:
    kind: str  # ident function at-keyword hash string bad-string url bad-url number percentage dimension
    # whitespace delim colon semicolon comma [ ] ( ) { } cdo cdc comment-unclosed
    value: str = ""
    line: int = 1
    start: int = 0
    end: int = 0
    extra: str = ""  # hash: "id" when it would be a valid id; dimension: the unit


def _is_name_start(c: str) -> bool:
    return c.isalpha() and c.isascii() or c == "_" or (c != "" and ord(c) >= 0x80)


def _is_name(c: str) -> bool:
    return _is_name_start(c) or c.isdigit() and c.isascii() or c == "-"


def _valid_escape(s: str, i: int) -> bool:
    return i < len(s) and s[i] == "\\" and (i + 1 >= len(s) or s[i + 1] != "\n")


def _starts_ident(s: str, i: int) -> bool:
    c = s[i] if i < len(s) else ""
    if c == "-":
        n = s[i + 1] if i + 1 < len(s) else ""
        return _is_name_start(n) or n == "-" or _valid_escape(s, i + 1)
    if _is_name_start(c):
        return True
    return _valid_escape(s, i)


_HEX = frozenset("0123456789abcdefABCDEF")


def _escape(s: str, i: int) -> tuple[str, int]:
    """Consume an escape; ``s[i]`` is the character after the backslash."""
    if i >= len(s):
        return "�", i
    if s[i] in _HEX:
        j = i
        while j < len(s) and j - i < 6 and s[j] in _HEX:
            j += 1
        cp = int(s[i:j], 16)
        if j < len(s) and s[j] in " \t\n":
            j += 1
        if cp == 0 or 0xD800 <= cp <= 0xDFFF or cp > 0x10FFFF:
            return "�", j
        return chr(cp), j
    return s[i], i + 1


def _name(s: str, i: int) -> tuple[str, int]:
    out = []
    while i < len(s):
        c = s[i]
        if _is_name(c):
            out.append(c)
            i += 1
        elif _valid_escape(s, i):
            ch, i = _escape(s, i + 1)
            out.append(ch)
        else:
            break
    return "".join(out), i


def _starts_number(s: str, i: int) -> bool:
    c = s[i] if i < len(s) else ""
    n = s[i + 1] if i + 1 < len(s) else ""
    if c in "+-":
        return n.isdigit() and n.isascii() or n == "." and i + 2 < len(s) and s[i + 2].isdigit()
    if c == ".":
        return n.isdigit() and n.isascii()
    return c.isdigit() and c.isascii()


_NUM_RE = re.compile(r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?", re.ASCII)


def tokenize(text: str) -> list[Token]:
    s = text.replace("\r\n", "\n").replace("\r", "\n").replace("\f", "\n")
    toks: list[Token] = []
    i, n, line = 0, len(s), 1

    def add(kind: str, start: int, end: int, value: str = "", extra: str = "", at_line: int | None = None) -> None:
        toks.append(Token(kind, value, at_line or line, start, end, extra))

    while i < n:
        c = s[i]
        start, start_line = i, line
        if c == "/" and s.startswith("/*", i):
            j = s.find("*/", i + 2)
            if j < 0:
                add("comment-unclosed", i, n)
                line += s.count("\n", i, n)
                break
            line += s.count("\n", i, j + 2)
            i = j + 2
            continue
        if c in " \t\n":
            j = i
            while j < n and s[j] in " \t\n":
                j += 1
            line += s.count("\n", i, j)
            add("whitespace", i, j, " ", at_line=start_line)
            i = j
            continue
        if c in "\"'":
            out, j, bad = [], i + 1, False
            while j < n:
                d = s[j]
                if d == c:
                    j += 1
                    break
                if d == "\n":
                    bad = True
                    break
                if d == "\\":
                    if j + 1 >= n:
                        j += 1
                        continue
                    if s[j + 1] == "\n":
                        line += 1
                        j += 2
                        continue
                    ch, j = _escape(s, j + 1)
                    out.append(ch)
                    continue
                out.append(d)
                j += 1
            else:
                bad = False  # end of file closes the string (the browser does the same)
            add("bad-string" if bad else "string", start, j, "".join(out), at_line=start_line)
            i = j
            continue
        if c == "#":
            if i + 1 < n and (_is_name(s[i + 1]) or _valid_escape(s, i + 1)):
                kind = "id" if _starts_ident(s, i + 1) else "unrestricted"
                value, j = _name(s, i + 1)
                add("hash", start, j, value, kind)
                i = j
                continue
            add("delim", i, i + 1, "#")
            i += 1
            continue
        if _starts_number(s, i):
            m = _NUM_RE.match(s, i)
            assert m is not None
            j = m.end()
            if _starts_ident(s, j):
                unit, k = _name(s, j)
                add("dimension", start, k, m.group(0), unit)
                i = k
            elif j < n and s[j] == "%":
                add("percentage", start, j + 1, m.group(0))
                i = j + 1
            else:
                add("number", start, j, m.group(0))
                i = j
            continue
        if c == "<" and s.startswith("<!--", i):
            add("cdo", i, i + 4)
            i += 4
            continue
        if c == "-" and s.startswith("-->", i):
            add("cdc", i, i + 3)
            i += 3
            continue
        if _starts_ident(s, i):
            value, j = _name(s, i)
            if j < n and s[j] == "(":
                if value.lower() == "url":
                    k = j + 1
                    while k < n and s[k] in " \t\n":
                        k += 1
                    if k < n and s[k] in "\"'":
                        add("function", start, j + 1, value)
                        i = j + 1
                        continue
                    # an unquoted url( token
                    out, k, bad = [], k, False
                    while k < n:
                        d = s[k]
                        if d == ")":
                            k += 1
                            break
                        if d in " \t\n":
                            m2 = k
                            while m2 < n and s[m2] in " \t\n":
                                m2 += 1
                            if m2 < n and s[m2] == ")":
                                k = m2 + 1
                                break
                            bad = True
                        elif d in "\"'(" or ord(d) < 0x20 and d not in "\t\n" or ord(d) == 0x7F:
                            bad = True
                        elif d == "\\":
                            if _valid_escape(s, k):
                                ch, k = _escape(s, k + 1)
                                out.append(ch)
                                continue
                            bad = True
                        if bad:
                            # consume the rest of a bad url, as the browser does
                            while k < n and s[k] != ")":
                                k += 2 if _valid_escape(s, k) else 1
                            k = min(n, k + 1)
                            break
                        out.append(d)
                        k += 1
                    line += s.count("\n", start, k)
                    add("bad-url" if bad else "url", start, k, "".join(out), at_line=start_line)
                    i = k
                    continue
                add("function", start, j + 1, value)
                i = j + 1
                continue
            add("ident", start, j, value)
            i = j
            continue
        if c == "@":
            if _starts_ident(s, i + 1):
                value, j = _name(s, i + 1)
                add("at-keyword", start, j, value)
                i = j
                continue
            add("delim", i, i + 1, "@")
            i += 1
            continue
        if c == "\\":
            if _valid_escape(s, i):  # pragma: no cover - handled by _starts_ident
                value, j = _name(s, i)
                add("ident", start, j, value)
                i = j
                continue
            add("delim", i, i + 1, "\\")
            i += 1
            continue
        kinds = {":": "colon", ";": "semicolon", ",": "comma", "[": "[", "]": "]", "(": "(", ")": ")",
                 "{": "{", "}": "}"}
        add(kinds.get(c, "delim"), i, i + 1, c)
        i += 1
    return toks


# ---------------------------------------------------------------- references


def resolve_ref(ref: str, base: str) -> tuple[str | None, str | None]:
    """Where a relative address in a stylesheet (``base="styles"``) or the theme (``"assets"``) points.

    Returns ``("assets/fonts/a.woff2", None)`` or ``(None, "why it is refused")``.
    """
    shown = ref[:80]
    if not ref.strip():
        return None, "The address is empty."
    if any(ord(ch) < 0x21 or ord(ch) == 0x7F for ch in ref):
        return None, f'The address "{shown}" contains spaces or control characters.'
    if ":" in ref:
        return None, (f'The address "{shown}" names another site or a kind of link (like https:, data: or '
                      "javascript:); only files of this project can be used.")
    if ref.startswith(("/", "\\")) or "\\" in ref:
        return None, f'The address "{shown}" is not a relative path inside the project.'
    for ch, what in (("?", "a ?"), ("#", "a #"), ("%", "a %")):
        if ch in ref:
            return None, f'The address "{shown}" contains {what}, which easy-ui does not allow in file addresses.'
    parts = [base]
    for seg in ref.split("/"):
        if seg in ("", "."):
            if seg == "":
                return None, f'The address "{shown}" has an empty part (//).'
            continue
        if seg == "..":
            if not parts:
                return None, f'The address "{shown}" leaves the project\'s ui folder.'
            parts.pop()
            continue
        if not _SEGMENT_RE.match(seg):
            return None, (f'The address "{shown}" has a part ("{seg[:40]}") easy-ui does not accept: use letters, '
                          "digits, '.', '-' and '_', starting with a letter or digit.")
        parts.append(seg)
    if len(parts) < 2 or parts[0] not in ("assets", "styles"):
        return None, f'The address "{shown}" points outside ui/assets/ and ui/styles/.'
    return "/".join(parts), None


# ---------------------------------------------------------------- checking


@dataclass
class CssCheck:
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    font_faces: list[str] = field(default_factory=list)
    refs: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _closest(word: str, options: Iterable[str]) -> str | None:
    m = difflib.get_close_matches(word, list(options), n=1, cutoff=0.6)
    return m[0] if m else None


class _Checker:
    def __init__(self, text: str, file: str, name: str, stylesheets: Iterable[str], assets: Iterable[str],
                 classes: Iterable[str], properties: Iterable[str], data_types: Iterable[str],
                 url_prefix: str = "") -> None:
        self.url_prefix = url_prefix
        self.text = text
        self.file = file
        self.name = name
        self.stylesheets = set(stylesheets)
        self.assets = set(assets)
        self.classes = set(classes)
        self.properties = set(properties)
        self.data_types = list(data_types)
        self.res = CssCheck()
        self._seen_warn: set[tuple[str, str]] = set()
        self._more_errors = 0
        self._more_warnings = 0

    def err(self, line: int | None, message: str, hint: str | None = None, *, did_you_mean: str | None = None) -> None:
        if len(self.res.errors) >= MAX_PROBLEMS:
            self._more_errors += 1
            return
        self.res.errors.append(_problem(self.file, line, message, hint, did_you_mean, "error"))

    def warn(self, line: int | None, message: str, hint: str | None = None, *, did_you_mean: str | None = None,
             key: tuple[str, str] | None = None) -> None:
        if key is not None:
            if key in self._seen_warn:
                return
            self._seen_warn.add(key)
        if len(self.res.warnings) >= MAX_PROBLEMS:
            self._more_warnings += 1
            return
        self.res.warnings.append(_problem(self.file, line, message, hint, did_you_mean, "warning"))

    def closest(self, word: str, options: Iterable[str], *, warning: bool = False) -> str | None:
        """The closest name, while problems are still being listed (a suggestion takes time)."""
        if len(self.res.warnings if warning else self.res.errors) >= MAX_PROBLEMS:
            return None
        return _closest(word, options)

    # -- references
    def check_url(self, tok: Token, value: str, *, kind: str) -> str | None:
        target, why = resolve_ref(value, "styles")
        if why:
            self.err(tok.line, f"This {kind} is refused. {why}",
                     "Put the file in ui/assets/ (pictures and fonts) and refer to it relatively, for example "
                     'url("../assets/logo.svg").' if kind != "@import" else
                     'Import another stylesheet of ui/styles/ by its name, for example @import "base.css";')
            return None
        assert target is not None
        if kind == "@import":
            if not target.startswith("styles/") or target.count("/") != 1 or not target.endswith(".css"):
                self.err(tok.line, f'@import "{value[:80]}" does not name a stylesheet in ui/styles/.',
                         'Import another file of ui/styles/ by its name, for example @import "base.css";')
                return None
            if target[len("styles/"):] not in self.stylesheets:
                self.err(tok.line, f'@import "{value[:80]}" names a stylesheet that does not exist in ui/styles/.',
                         "Check the file name.",
                         did_you_mean=self.closest(target[len("styles/"):], self.stylesheets))
                return None
            if target == f"styles/{self.name}":
                self.err(tok.line, "This stylesheet imports itself.", "Remove the @import.")
                return None
        else:
            if not target.startswith("assets/"):
                self.err(tok.line, f'url("{value[:80]}") does not point at a file in ui/assets/.',
                         "Pictures and fonts live in ui/assets/; from a stylesheet write url(\"../assets/NAME\").")
                return None
            rel = target[len("assets/"):]
            if rel not in self.assets:
                self.err(tok.line, f'url("{value[:80]}") names a file that is not in ui/assets/ ({rel}).',
                         "Add the file to ui/assets/ or fix the name.", did_you_mean=self.closest(rel, self.assets))
                return None
        self.res.refs.append(target)
        return target

    # -- the walk
    def run(self) -> CssCheck:
        if "\x00" in self.text:
            self.err(None, "The stylesheet contains a NUL character, so it is not plain text.",
                     "Save it as UTF-8 text.")
            return self.res
        bad = re.search(r"[\x01-\x08\x0b\x0e-\x1f\x7f]", self.text)
        if bad:
            line = self.text.count("\n", 0, bad.start()) + 1
            self.err(line, "The stylesheet contains a control character, which CSS never needs.",
                     "Remove it (it may come from copying text from another program).")
        all_toks = tokenize(self.text)
        toks = [t for t in all_toks if t.kind != "comment-unclosed"]
        if len(toks) != len(all_toks):
            self.err(None, "A comment starting with /* is never closed with */, so the rest of the file is ignored.",
                     "Close the comment.")
        self.toks = toks
        self.walk(0, len(toks), depth=0, context="top")
        for more, out, level in ((self._more_errors, self.res.errors, "error"),
                                 (self._more_warnings, self.res.warnings, "warning")):
            if more:
                out.append(_problem(self.file, None, f"... and {more} more {'problems' if level == 'error' else 'warnings'}"
                                    " in this stylesheet (only the first ones are listed).",
                                    "Fix these first, then check again.", None, level))
        return self.res

    def _skip_ws(self, i: int, end: int) -> int:
        while i < end and self.toks[i].kind == "whitespace":
            i += 1
        return i

    def _match(self, i: int, end: int) -> int:
        """The index after the block or function opened at ``i``."""
        pairs = {"{": "}", "[": "]", "(": ")", "function": ")"}
        stack = [pairs[self.toks[i].kind]]
        braces = 1 if stack[0] == "}" else 0  # how many "}" the stack holds: a stray } closes the nearest {
        j = i + 1
        while j < end and stack:
            k = self.toks[j].kind
            if k in pairs:
                stack.append(pairs[k])
                braces += k == "{"
            elif k in ("}", "]", ")"):
                if k == stack[-1]:
                    stack.pop()
                    braces -= k == "}"
                elif k == "}" and braces:
                    while stack[-1] != "}":
                        stack.pop()
                    stack.pop()
                    braces -= 1
            j += 1
        if stack:
            what = {"}": "{", "]": "[", ")": "("}[stack[0]]
            self.err(self.toks[i].line, f"A {what} opened here is never closed, so the browser may ignore the rest "
                     "of the file.", "Close it, or remove it.")
            return end
        return j

    def walk(self, i: int, end: int, *, depth: int, context: str) -> None:
        if depth > MAX_DEPTH:
            self.err(self.toks[i].line if i < end else None,
                     f"The rules are nested more than {MAX_DEPTH} levels deep, which no stylesheet needs.",
                     "Write the rules out more plainly.")
            return
        while i < end:
            t = self.toks[i]
            if t.kind in ("whitespace", "semicolon", "cdo", "cdc"):
                i += 1
                continue
            if t.kind == "}":
                self.err(t.line, "There is a } here without a { before it.", "Remove it, or add the missing {.")
                i += 1
                continue
            if t.kind == "at-keyword":
                i = self.at_rule(i, end, depth=depth, context=context)
                continue
            # a declaration (inside a style rule) or a qualified rule (selector { ... })
            j = i
            while j < end and self.toks[j].kind not in ("{", "semicolon", "}"):
                if self.toks[j].kind in ("function", "(", "["):
                    j = self._match(j, end)
                    continue
                j += 1
            if j < end and self.toks[j].kind == "{" and not self._is_declaration(i, j, context):
                self.selector(i, j)
                close = self._match(j, end)
                inner_ctx = "keyframe" if context == "keyframes" else "style"
                self.walk(j + 1, self._inner_end(j, close), depth=depth + 1, context=inner_ctx)
                i = close
                continue
            # declaration up to ; or end of block
            k = i
            while k < end and self.toks[k].kind not in ("semicolon", "}"):
                if self.toks[k].kind in ("function", "(", "[", "{"):
                    k = self._match(k, end)
                    continue
                k += 1
            if context == "top":
                if self.toks[i].kind not in ("whitespace",):
                    self.err(t.line, "Something here is neither a rule (selector { ... }) nor an at-rule, so the "
                             "browser ignores it.", "Check for a missing { or a stray character.")
            self.declaration(i, k, context)
            i = k

    def _inner_end(self, open_i: int, close: int) -> int:
        if close > open_i + 1 and self.toks[close - 1].kind == "}":
            return close - 1
        return close

    def _is_declaration(self, i: int, j: int, context: str) -> bool:
        """In a style rule, "name: value {" is a nested rule only when it is not a declaration like ``a:hover {``."""
        if context not in ("style", "font-face", "page", "descriptors"):
            return False
        t = self.toks[i]
        if t.kind != "ident":
            return False
        k = self._skip_ws(i + 1, j)
        if k >= j or self.toks[k].kind != "colon":
            return False
        return t.value.startswith("--")  # custom property values may hold {} blocks

    def at_rule(self, i: int, end: int, *, depth: int, context: str) -> int:
        t = self.toks[i]
        name = t.value.lower()
        j = i + 1
        while j < end and self.toks[j].kind not in ("{", "semicolon", "}"):
            if self.toks[j].kind in ("function", "(", "["):
                j = self._match(j, end)
                continue
            j += 1
        if name in REFUSED_AT_RULES:
            self.err(t.line, REFUSED_AT_RULES[name], "Remove it.")
        elif name not in ALLOWED_AT_RULES:
            self.err(t.line, f"@{t.value[:40]} is not an at-rule easy-ui knows.",
                     "Use @media, @supports, @container, @layer, @keyframes, @font-face, @import, @page or @property.",
                     did_you_mean=self.closest(name, ALLOWED_AT_RULES))
        if name == "import":
            k = self._skip_ws(i + 1, j)
            rest = k + 1
            if k < j and self.toks[k].kind in ("string", "url"):
                self.check_url(self.toks[k], self.toks[k].value, kind="@import")
            elif k < j and self.toks[k].kind == "function" and self.toks[k].value.lower() == "url":
                s = self._skip_ws(k + 1, j)
                rest = self._match(k, j)
                if s < j and self.toks[s].kind == "string":
                    self.check_url(self.toks[s], self.toks[s].value, kind="@import")
                else:
                    self.err(t.line, "@import must name a stylesheet in ui/styles/.", 'For example @import "base.css";')
            else:
                self.err(t.line, "@import must name a stylesheet in ui/styles/.", 'For example @import "base.css";')
            self.values(rest, j)
        else:
            self.values(i + 1, j)
        if j < end and self.toks[j].kind == "{":
            close = self._match(j, end)
            inner = {"keyframes": "keyframes", "-webkit-keyframes": "keyframes", "font-face": "font-face",
                     "page": "page", "property": "descriptors", "counter-style": "descriptors",
                     "font-feature-values": "descriptors", "font-palette-values": "descriptors",
                     "position-try": "descriptors", "view-transition": "descriptors"}.get(name)
            if name == "font-face":
                self.font_face(t, j, close)
            if inner in ("font-face", "page", "descriptors"):
                self.walk(j + 1, self._inner_end(j, close), depth=depth + 1, context=inner)
            elif inner == "keyframes":
                self.walk(j + 1, self._inner_end(j, close), depth=depth + 1, context="keyframes")
            else:
                # @media, @supports, @layer, @container, @scope, @starting-style: rules (or, nested, declarations)
                self.walk(j + 1, self._inner_end(j, close), depth=depth + 1,
                          context="style" if context == "style" else "top")
            return close
        return j + 1 if j < end and self.toks[j].kind == "semicolon" else j

    def font_face(self, at: Token, open_i: int, close: int) -> None:
        family = None
        k = open_i + 1
        while k < close:
            tok = self.toks[k]
            if tok.kind == "ident" and tok.value.lower() == "font-family":
                c = self._skip_ws(k + 1, close)
                if c < close and self.toks[c].kind == "colon":
                    v = self._skip_ws(c + 1, close)
                    words = []
                    while v < close and self.toks[v].kind not in ("semicolon", "}"):
                        if self.toks[v].kind in ("string", "ident"):
                            words.append(self.toks[v].value)
                        elif self.toks[v].kind != "whitespace":
                            words.append("\x00")
                        v += 1
                    family = " ".join(words).strip() if words else ""
                    k = v
                    continue
            k += 1
        if family is None:
            self.err(at.line, "This @font-face has no font-family, so no text can use it.",
                     'Add font-family: "My font"; and use that name in font-family elsewhere.')
            return
        if "\x00" in family or not FONT_FAMILY_RE.match(family):
            self.err(at.line, "The font-family of this @font-face is not a plain name.",
                     'Use letters, digits, spaces, - and _, for example font-family: "Field Notes";')
            return
        if family.lower() in RESERVED_FONT_NAMES:
            self.err(at.line, f'The @font-face takes the name "{family}", which easy-ui\'s own screens use, so it is '
                     "refused (a project font must never change how the sign-in or the preview badge look).",
                     f'Give it a name of its own, for example font-family: "{family} Project";')
            return
        # The rule as text, with its urls rewritten to be relative to ui/ (where the theme stylesheet lives).
        out = []
        pos = at.start
        end_pos = self.toks[close - 1].end if close - 1 < len(self.toks) else len(self.text)
        k = open_i
        while k < close:
            tok = self.toks[k]
            if tok.kind == "url" or (tok.kind == "function" and tok.value.lower() == "url"):
                target = None
                if tok.kind == "url":
                    target, _ = resolve_ref(tok.value, "styles")
                    stop = tok.end
                else:
                    s = self._skip_ws(k + 1, close)
                    if s < close and self.toks[s].kind == "string":
                        target, _ = resolve_ref(self.toks[s].value, "styles")
                    stop = self.toks[self._match(k, close) - 1].end
                if target is None:
                    return  # already reported by values()
                if not (target.startswith("assets/") and target.lower().endswith(".woff2")):
                    self.err(tok.line, f'This @font-face uses "{target}", which is not a .woff2 font in ui/assets/.',
                             'Put the font in ui/assets/ as .woff2 and use url("../assets/<name>.woff2").')
                    return
                out.append(self.text[pos:tok.start])
                out.append(f'url("{self.url_prefix}{target}")')
                pos = stop
                k = self._advance_to(stop, k)
                continue
            k += 1
        out.append(self.text[pos:end_pos])
        self.res.font_faces.append("".join(out))

    def _advance_to(self, pos: int, k: int) -> int:
        while k < len(self.toks) and self.toks[k].start < pos:
            k += 1
        return k

    def selector(self, i: int, j: int) -> None:
        k = i
        while k < j:
            t = self.toks[k]
            if t.kind == "delim" and t.value == "." and k + 1 < j and self.toks[k + 1].kind == "ident":
                cls = self.toks[k + 1].value
                if cls.lower().startswith("eui-") and cls not in self.classes:
                    self.warn(t.line, f'The class ".{cls[:60]}" is not one of easy-ui\'s names, so this rule matches '
                              "nothing easy-ui draws.", "See the names with: easy-ui style-guide",
                              did_you_mean=self.closest(cls, self.classes, warning=True), key=("class", cls))
            elif t.kind == "[":
                close = self._match(k, j)
                inner = [x for x in self.toks[k + 1:close - 1] if x.kind != "whitespace"]
                if len(inner) >= 3 and inner[0].kind == "ident" and inner[0].value.lower() == "data-type" \
                        and inner[-1].kind in ("string", "ident"):
                    v = inner[-1].value
                    if v not in self.data_types:
                        self.warn(t.line, f'[data-type="{v[:40]}"] is not a kind of component, so this rule matches '
                                  "nothing.", f"The kinds are: {', '.join(self.data_types)}.",
                                  did_you_mean=self.closest(v, self.data_types, warning=True), key=("type", v))
                k = close
                continue
            elif t.kind == "function":
                self.function(k)
            k += 1
        self.values(i, j, selectors=True)

    def declaration(self, i: int, k: int, context: str = "style") -> None:
        i = self._skip_ws(i, k)
        if i >= k:
            return
        t = self.toks[i]
        if t.kind == "ident":
            name = t.value
            low = name.lower()
            c = self._skip_ws(i + 1, k)
            if c < k and self.toks[c].kind == "colon":
                if low in REFUSED_PROPERTIES:
                    self.err(t.line, REFUSED_PROPERTIES[low], "Remove it.")
                elif low.startswith("--eui-") and name not in self.properties:
                    self.warn(t.line, f"{name[:60]} is not one of easy-ui's custom properties, so setting it changes "
                              "nothing easy-ui draws.", "See them with: easy-ui style-guide",
                              did_you_mean=self.closest(name, self.properties, warning=True), key=("prop", name))
                if name.startswith("--"):
                    self.custom_property(c + 1, k)
                elif context == "font-face" and low == "src":
                    self.font_src(c + 1, k)
        self.values(i, k)

    def custom_property(self, i: int, k: int) -> None:
        """A custom property may not hold a text that is a web address (it could only be meant to be fetched)."""
        for x in range(i, k):
            tk = self.toks[x]
            if tk.kind == "string" and _ADDRESS_RE.match(tk.value):
                self.err(tk.line, f'A custom property here holds the address "{tk.value[:80]}"; easy-ui does not '
                         "allow addresses of other sites or kinds of links in custom properties.",
                         "Put the file in ui/assets/ and use url(\"../assets/NAME\") where it is shown.")

    def font_src(self, i: int, k: int) -> None:
        """``src`` of an ``@font-face``: only ``url()`` (and ``local()``, ``format()``, ``tech()``)."""
        depth = 0
        for x in range(i, k):
            tk = self.toks[x]
            if tk.kind in ("function", "(", "["):
                depth += 1
                if tk.kind == "function" and tk.value.lower() in _SUBSTITUTING_FUNCTIONS:
                    self.err(tk.line, f"The src of this @font-face uses {tk.value.lower()}(), so the font's address "
                             "could not be checked.", 'Write it as src: url("../assets/NAME.woff2");')
            elif tk.kind in (")", "]"):
                depth = max(0, depth - 1)
            elif tk.kind == "string" and depth == 0:
                self.err(tk.line, "The src of this @font-face is a plain quoted text; write the font's address as "
                         "url(...).", 'Write it as src: url("../assets/NAME.woff2");')

    def function(self, k: int) -> None:
        t = self.toks[k]
        low = t.value.lower()
        if low in REFUSED_FUNCTIONS and REFUSED_FUNCTIONS[low]:
            self.err(t.line, REFUSED_FUNCTIONS[low], "Remove it.")

    def values(self, i: int, k: int, *, selectors: bool = False) -> None:
        """Check every url, function and custom property use between i and k (one pass: nesting is linear)."""
        # The functions and brackets open at each token: (closing token, function name or "" for a bracket).
        stack: list[tuple[str, str]] = []
        loading = 0  # how many of them load what their strings name: url(), image-set(), image(), cross-fade()
        closers = {"(": ")", "[": "]", "{": "}"}
        for j in range(i, k):
            t = self.toks[j]
            if t.kind == "url":
                self.check_url(t, t.value, kind="url()")
            elif t.kind == "bad-url":
                self.err(t.line, "This url( is not written correctly (it has a quote, a space or a bracket inside "
                         "it, or it is never closed).", 'Write it as url("../assets/NAME").')
            elif t.kind == "bad-string":
                self.err(t.line, "A quoted text here is not closed before the end of the line.",
                         "Close the quote on the same line.")
            elif t.kind == "string":
                if stack and stack[-1][1] in _LOADING_FUNCTIONS:
                    self.check_url(t, t.value, kind="url()")
            elif t.kind == "function":
                low = t.value.lower()
                if not selectors:
                    self.function(j)
                if loading and low in _SUBSTITUTING_FUNCTIONS:
                    outer = next(name for _, name in reversed(stack) if name in _LOADING_FUNCTIONS)
                    self.err(t.line, f"{outer}() here uses {low}(), so the address it loads could not be checked.",
                             f'Name the file itself, for example {outer}("../assets/NAME" ...).' if outer != "url"
                             else 'Write it as url("../assets/NAME").')
                if low == "url":
                    s = self._skip_ws(j + 1, k)
                    if not (s < k and self.toks[s].kind == "string"):
                        self.err(t.line, "This url( does not hold an address.", 'Write it as url("../assets/NAME").')
                elif low == "var":
                    s = self._skip_ws(j + 1, k)
                    if s < k and self.toks[s].kind == "ident":
                        name = self.toks[s].value
                        if name.lower().startswith("--eui-") and name not in self.properties:
                            self.warn(t.line, f"var({name[:60]}) uses a custom property easy-ui does not set.",
                                      "See them with: easy-ui style-guide",
                                      did_you_mean=self.closest(name, self.properties, warning=True), key=("prop", name))
                stack.append((")", low))
                loading += low in _LOADING_FUNCTIONS
            elif t.kind in closers:
                stack.append((closers[t.kind], ""))
            elif t.kind in (")", "]", "}"):
                if stack and stack[-1][0] == t.kind:
                    loading -= stack.pop()[1] in _LOADING_FUNCTIONS
            elif t.kind == "ident" and t.value.lower() == "-moz-binding":
                self.err(t.line, REFUSED_PROPERTIES["-moz-binding"], "Remove it.")


def _problem(file: str, line: int | None, message: str, hint: str | None, did_you_mean: str | None,
             level: str) -> dict[str, Any]:
    d: dict[str, Any] = {"file": file, "line": line, "where": "", "problem": message}
    if did_you_mean is not None:
        d["did_you_mean"] = did_you_mean
    if hint:
        d["hint"] = hint
    d["level"] = level
    return d


def check_stylesheet(
    text: str,
    *,
    file: str,
    name: str,
    stylesheets: Iterable[str] = (),
    assets: Iterable[str] = (),
    classes: Iterable[str] = (),
    properties: Iterable[str] = (),
    data_types: Iterable[str] = (),
    url_prefix: str = "",
) -> CssCheck:
    if len(text.encode("utf-8", "surrogatepass")) > MAX_STYLESHEET_BYTES:
        res = CssCheck()
        res.errors.append(_problem(file, None, f"The stylesheet is larger than {MAX_STYLESHEET_BYTES // 1000} KB.",
                                   "Split it into several files, or remove what is not used.", None, "error"))
        return res
    return _Checker(text, file, name, stylesheets, assets, classes, properties, data_types, url_prefix).run()


# ---------------------------------------------------------------- an app's stylesheet, scoped to its own content

APP_REFUSED_AT_RULES = {
    "scope": "@scope is not needed in an app's stylesheet: easy-ui already limits it to the app's own dashboards and "
    "sections.",
    "page": "@page styles the printed page as a whole, which an app's stylesheet may not do.",
    "view-transition": "@view-transition changes the whole page, which an app's stylesheet may not do.",
}
# Rules whose blocks hold more rules (their selectors are scoped too); other at-rules are left as they are.
_RULE_BLOCKS = ("media", "supports", "layer", "container", "starting-style")
_LEGACY_PSEUDO_ELEMENTS = ("before", "after", "first-line", "first-letter")


def app_at_rule_problems(text: str, *, file: str) -> list[dict[str, Any]]:
    """The at-rules an app's stylesheet may not use (on top of ``check_stylesheet``)."""
    out = []
    for t in tokenize(text):
        if t.kind == "at-keyword" and t.value.lower() in APP_REFUSED_AT_RULES:
            out.append(_problem(file, t.line, APP_REFUSED_AT_RULES[t.value.lower()], "Remove it.", None, "error"))
    return out


def _block_end(toks: list[Token], i: int, end: int) -> int:
    """The index after the block or function opened at ``i`` (the stylesheet is already checked)."""
    pairs = {"{": "}", "[": "]", "(": ")", "function": ")"}
    stack = [pairs[toks[i].kind]]
    j = i + 1
    while j < end and stack:
        k = toks[j].kind
        if k in pairs:
            stack.append(pairs[k])
        elif k in ("}", "]", ")") and stack and k == stack[-1]:
            stack.pop()
        j += 1
    return j


def _scoped_selector(s: str, toks: list[Token], a: int, b: int, attr: str) -> str:
    """One selector (tokens a..b-1) as two: inside an element of the app, and an element of the app itself."""
    sel = s[toks[a].start:toks[b - 1].end]
    last = a  # where the last compound selector starts
    insert = toks[b - 1].end
    depth = 0
    k = a
    while k < b:
        t = toks[k]
        if t.kind in ("function", "(", "["):
            k = _block_end(toks, k, b)
            continue
        if t.kind == "whitespace" or (t.kind == "delim" and t.value in (">", "+", "~", "|")):
            last = k + 1
        k += 1
    k = last
    while k < b:
        t = toks[k]
        if t.kind in ("function", "(", "["):
            k = _block_end(toks, k, b)
            continue
        if t.kind == "colon" and depth == 0:
            nxt = toks[k + 1] if k + 1 < b else None
            if nxt is not None and nxt.kind == "colon":
                insert = t.start
                break
            if nxt is not None and nxt.kind == "ident" and nxt.value.lower() in _LEGACY_PSEUDO_ELEMENTS:
                insert = t.start
                break
        k += 1
    own = s[toks[a].start:insert] + f":where({attr})" + s[insert:toks[b - 1].end]
    return f":where({attr}) {sel}, {own}"


def _scope_prelude(s: str, toks: list[Token], i: int, j: int, attr: str, edits: list[tuple[int, int, str]]) -> None:
    parts: list[tuple[int, int]] = []
    start = i
    k = i
    while k < j:
        t = toks[k]
        if t.kind in ("function", "(", "["):
            k = _block_end(toks, k, j)
            continue
        if t.kind == "comma":
            parts.append((start, k))
            start = k + 1
        k += 1
    parts.append((start, j))
    pieces = []
    for a, b in parts:
        while a < b and toks[a].kind == "whitespace":
            a += 1
        while b > a and toks[b - 1].kind == "whitespace":
            b -= 1
        if a < b:
            pieces.append(_scoped_selector(s, toks, a, b, attr))
    if pieces:
        first = next(t for t in toks[i:j] if t.kind != "whitespace")
        last = next(t for t in reversed(toks[i:j]) if t.kind != "whitespace")
        edits.append((first.start, last.end, ", ".join(pieces)))


def scope_stylesheet(text: str, app: str) -> str:
    """An app's (already checked) stylesheet with every rule limited to that app's own content.

    Each selector ``S`` becomes ``:where([data-app="<app>"]) S, S:where([data-app="<app>"])`` (the second form
    put before a pseudo-element): it matches only elements inside, or themselves, marked as the app's. ``:where()``
    adds no specificity, and the project's own stylesheets apply after the apps', so with equal weight and
    importance the project's rule wins (docs/STYLES.md says exactly who wins). Rules
    inside ``@media``, ``@supports``, ``@layer``, ``@container`` and ``@starting-style`` are scoped the same way;
    rules nested inside a rule follow their parent.
    """
    s = text.replace("\r\n", "\n").replace("\r", "\n").replace("\f", "\n")
    toks = [t for t in tokenize(s) if t.kind != "comment-unclosed"]
    attr = f'[data-app="{app}"]'
    edits: list[tuple[int, int, str]] = []

    def walk(i: int, end: int) -> None:
        while i < end:
            t = toks[i]
            if t.kind in ("whitespace", "semicolon", "cdo", "cdc", "}"):
                i += 1
                continue
            j = i
            while j < end and toks[j].kind not in ("{", "semicolon"):
                if toks[j].kind in ("function", "(", "["):
                    j = _block_end(toks, j, end)
                    continue
                j += 1
            if j >= end or toks[j].kind == "semicolon":
                i = j + 1
                continue
            close = _block_end(toks, j, end)
            if t.kind == "at-keyword":
                if t.value.lower() in _RULE_BLOCKS:
                    walk(j + 1, close - 1)
            else:
                _scope_prelude(s, toks, i, j, attr, edits)
            i = close

    walk(0, len(toks))
    out = []
    pos = 0
    for a, b, new in sorted(edits):
        out.append(s[pos:a])
        out.append(new)
        pos = b
    out.append(s[pos:])
    return "".join(out)


def inline_refs(text: str, target_of: Any, data_url: Any) -> tuple[str | None, str | None]:
    """A checked stylesheet with every file it names put inside it, for a single-file export.

    Every ``url()`` (quoted or not) and every string of ``image-set()``, ``image()`` and ``cross-fade()`` becomes a
    ``data:`` address of the file it names. ``target_of(ref)`` says which served file a reference names (``None``
    when it names none); ``data_url(target)`` gives that file as a ``data:`` address (``None`` when the look has no
    such file). Returns ``(text, None)``, or ``(None, why)`` when the stylesheet cannot be carried in one file:
    an ``@import`` (a single file's policy allows only the stylesheets it holds), or a reference to no file.
    """
    s = text.replace("\r\n", "\n").replace("\r", "\n").replace("\f", "\n")
    toks = tokenize(s)
    edits: list[tuple[int, int, str]] = []
    stack: list[str] = []  # the functions (and plain brackets, as "") open at each token

    def address(ref: str, line: int) -> tuple[str | None, str | None]:
        target = target_of(ref)
        got = data_url(target) if target is not None else None
        if got is None:
            return None, f'line {line} names "{ref[:80]}", which is not a file of the look'
        return got, None

    for t in toks:
        if t.kind == "at-keyword" and t.value.lower() == "import":
            return None, f"line {t.line} uses @import"
        if t.kind in ("bad-url", "comment-unclosed", "bad-string"):
            return None, f"line {t.line} is not written correctly"
        if t.kind == "function":
            stack.append(t.value.lower())
        elif t.kind == "(":
            stack.append("")
        elif t.kind == ")":
            if stack:
                stack.pop()
        elif t.kind == "url":
            got, why = address(t.value, t.line)
            if why:
                return None, why
            edits.append((t.start, t.end, f'url("{got}")'))
        elif t.kind == "string" and stack and (stack[-1] == "url" or stack[-1] in URL_STRING_FUNCTIONS):
            got, why = address(t.value, t.line)
            if why:
                return None, why
            edits.append((t.start, t.end, f'"{got}"'))
    out = []
    pos = 0
    for a, b, new in edits:
        out.append(s[pos:a])
        out.append(new)
        pos = b
    out.append(s[pos:])
    return "".join(out), None

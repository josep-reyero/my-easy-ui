"""The value-format vocabulary, shared as data so the browser renderer can mirror it.

Dashboards choose how a value looks with ``format: <name>``. The set is small
and fixed. Formatting is locale-independent: ``,`` groups thousands,
``.`` is the decimal point, months are English three-letter abbreviations.
Rounding is half away from zero on the exact value (JavaScript's
``Number.prototype.toFixed`` behaves the same), and a missing value (NULL)
always shows as ``NULL_TEXT`` (an em dash).

A text value given to a numeric format is shown unchanged, unless it is plain
numeric text such as ``"12.5"``, which is formatted as the number.

When no format is set (``null`` in the compiled spec) the renderer uses
``auto``: numbers as ``number``, everything else unchanged.

Public API
----------
``NULL_TEXT``   ``"—"``.
``MONTHS``      ``("Jan", ..., "Dec")``.
``FORMATS``     dict name -> {"description", "input", "examples": [[value, text], ...]}.
                The examples are the contract: the renderer's implementation must
                produce exactly these texts.
``FORMAT_NAMES`` tuple of the names, in order.
``vocabulary() -> dict``
    JSON-ready description of every format plus the shared rules, used by
    ``easy-ui components`` and available to the renderer.
``format_value(fmt, value) -> str``
    Reference implementation. ``fmt`` is a format name or ``None`` (auto).
    Raises ``ValueError`` for an unknown format name.
"""

from __future__ import annotations

import datetime as _dt
import math
import re
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from typing import Any

NULL_TEXT = "—"
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

FORMATS: dict[str, dict[str, Any]] = {
    "number": {
        "description": "A number with thousands separators and at most 2 decimals (trailing zeros dropped).",
        "input": "number",
        "examples": [[1234.5, "1,234.5"], [0.333, "0.33"], [-1234, "-1,234"], [2.005, "2"], [None, NULL_TEXT]],
    },
    "integer": {
        "description": "A whole number with thousands separators (rounded).",
        "input": "number",
        "examples": [[1234.5, "1,235"], [-2.5, "-3"], [999999, "999,999"], ["42", "42"]],
    },
    "decimal1": {
        "description": "A number with exactly one decimal.",
        "input": "number",
        "examples": [[12, "12.0"], [1234.56, "1,234.6"], [-0.04, "0.0"]],
    },
    "percent": {
        "description": "A fraction shown as a percentage (0.256 is 25.6%), at most 1 decimal.",
        "input": "number (a fraction: 1 means 100%)",
        "examples": [[0.256, "25.6%"], [0.5, "50%"], [1.234, "123.4%"], [-0.1, "-10%"]],
    },
    "duration": {
        "description": "A number of seconds shown in words: 7 h 07 min from one hour on (to the minute), "
        "25 min 31 s under an hour, 45 s under a minute. Never looks like a time of day.",
        "input": "number of seconds",
        "examples": [[25620, "7 h 07 min"], [3725, "1 h 02 min"], [720, "12 min"], [65, "1 min 05 s"], [5, "5 s"],
                     [59.6, "1 min"], [-65, "-1 min 05 s"], [-21603, "-6 h 00 min"], [360000, "100 h 00 min"],
                     [0, "0 s"]],
    },
    "distance": {
        "description": "A number of metres: under 1 km as whole metres, otherwise kilometres with 2 decimals.",
        "input": "number of metres",
        "examples": [[850, "850 m"], [12345, "12.35 km"], [1000, "1.00 km"], [1234567, "1,234.57 km"], [999.6, "1.00 km"]],
    },
    "date": {
        "description": "A calendar date such as 14 Mar 2026.",
        "input": "text starting with YYYY-MM-DD, or a number of seconds since 1970-01-01 UTC",
        "examples": [["2026-03-14", "14 Mar 2026"], ["2026-03-14T07:05:00", "14 Mar 2026"], [0, "1 Jan 1970"], ["soon", "soon"]],
    },
    "datetime": {
        "description": "A date and time such as 14 Mar 2026 07:05 (the time as written; no time-zone conversion).",
        "input": "text YYYY-MM-DD HH:MM[:SS] (or with T), or a number of seconds since 1970-01-01 UTC",
        "examples": [["2026-03-14T07:05:09Z", "14 Mar 2026 07:05"], ["2026-03-14 18:30", "14 Mar 2026 18:30"], ["2026-03-14", "14 Mar 2026"], [86400, "2 Jan 1970 00:00"]],
    },
    "pace": {
        "description": "Seconds per kilometre shown as m:ss /km.",
        "input": "number of seconds per kilometre",
        "examples": [[312, "5:12 /km"], [299.6, "5:00 /km"], [0, NULL_TEXT]],
    },
    "clock": {
        "description": "A time of day on a 24-hour clock, such as 23:10 (to the minute). A number counts seconds "
        "after midnight and wraps around the day, so a bedtime after midnight can be written as 90000 (01:00) to "
        "average it with ones before; text with a time in it shows that time.",
        "input": "number of seconds after midnight, or text HH:MM[:SS] or YYYY-MM-DD HH:MM[:SS]",
        "examples": [[83400, "23:10"], [90000, "01:00"], [-1800, "23:30"], [25229.5, "07:00"], [0, "00:00"],
                     ["2026-03-14T07:05:09Z", "07:05"], ["6:30", "06:30"], ["bedtime", "bedtime"]],
    },
}
FORMAT_NAMES = tuple(FORMATS)

_NUMERIC_TEXT = re.compile(r"^-?\d+(\.\d+)?\Z")
_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_ISO_DATETIME = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})")
_TIME_TEXT = re.compile(r"^(\d{1,2}):(\d{2})(?::\d{2}(?:\.\d+)?)?\Z")


def vocabulary() -> dict[str, Any]:
    return {
        "formats": {name: dict(info) for name, info in FORMATS.items()},
        "rules": {
            "null_text": NULL_TEXT,
            "group_separator": ",",
            "decimal_separator": ".",
            "rounding": "half away from zero on the exact value (like JavaScript toFixed)",
            "months": list(MONTHS),
            "auto": "when no format is set: numbers use 'number', anything else is shown unchanged",
            "text_values": "text given to a numeric format is shown unchanged unless it is plain numeric text",
            "negative_zero": "a value that rounds to zero is shown without a minus sign",
        },
    }


def _round(value: float | int, places: int) -> Decimal:
    q = Decimal(1).scaleb(-places)
    return Decimal(value).quantize(q, rounding=ROUND_HALF_UP)


def _group(d: Decimal, places: int, trim: bool = False) -> str:
    sign = "-" if d < 0 else ""
    text = f"{abs(d):.{places}f}"
    whole, _, frac = text.partition(".")
    if trim:
        frac = frac.rstrip("0")
    groups = []
    while len(whole) > 3:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    groups.insert(0, whole)
    out = ",".join(groups) + (f".{frac}" if frac else "")
    if out.strip("0.,") == "":
        sign = ""  # a value that rounds to zero never shows a minus sign
    return sign + out


def _as_number(value: Any) -> float | int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str) and _NUMERIC_TEXT.match(value.strip()):
        s = value.strip()
        return int(s) if "." not in s else float(s)
    return None


def _clock(total: int) -> str:
    sign = "-" if total < 0 else ""
    total = abs(total)
    h, rest = divmod(total, 3600)
    m, s = divmod(rest, 60)
    return f"{sign}{h}:{m:02d}:{s:02d}" if h else f"{sign}{m}:{s:02d}"


def _words(total: int) -> str:
    """Seconds as '7 h 07 min' (to the minute, from one hour on), '25 min 31 s' or '45 s'."""
    sign = "-" if total < 0 else ""
    total = abs(total)
    if total >= 3600:
        minutes = (total + 30) // 60  # half a minute rounds up, like every other format
        return f"{sign}{minutes // 60} h {minutes % 60:02d} min"
    if total >= 60:
        m, s = divmod(total, 60)
        return f"{sign}{m} min {s:02d} s" if s else f"{sign}{m} min"
    return f"{sign}{total} s" if total else "0 s"


def _date_text(y: int, m: int, d: int) -> str | None:
    try:
        _dt.date(y, m, d)
    except ValueError:
        return None
    return f"{d} {MONTHS[m - 1]} {y}"


def format_value(fmt: str | None, value: Any) -> str:
    if fmt is not None and fmt not in FORMATS:
        raise ValueError(f"unknown format {fmt!r}")
    if value is None:
        return NULL_TEXT
    if fmt in ("date", "datetime"):
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            try:
                t = _dt.datetime.fromtimestamp(int(value), tz=_dt.UTC)
            except (OverflowError, OSError, ValueError):
                return str(value)
            text = _date_text(t.year, t.month, t.day) or str(value)
            return text if fmt == "date" else f"{text} {t.hour:02d}:{t.minute:02d}"
        if isinstance(value, str):
            m = _ISO_DATE.match(value)
            if not m:
                return value
            text = _date_text(int(m[1]), int(m[2]), int(m[3]))
            if text is None:
                return value
            if fmt == "datetime":
                mt = _ISO_DATETIME.match(value)
                if mt:
                    text += f" {mt[4]}:{mt[5]}"
            return text
        return str(value)

    if fmt == "clock" and isinstance(value, str):
        mt = _ISO_DATETIME.match(value)
        if mt:
            return f"{mt[4]}:{mt[5]}"
        mt = _TIME_TEXT.match(value.strip())
        if mt and int(mt[1]) < 24 and int(mt[2]) < 60:
            return f"{int(mt[1]):02d}:{mt[2]}"
    n = _as_number(value)
    if n is None:
        if isinstance(value, float):  # inf / nan
            return NULL_TEXT
        return value if isinstance(value, str) else str(value)

    if fmt is None or fmt == "number":
        return _group(_round(n, 2), 2, trim=True)
    if fmt == "integer":
        return _group(_round(n, 0), 0)
    if fmt == "decimal1":
        return _group(_round(n, 1), 1)
    if fmt == "percent":
        return _group(_round(Decimal(n) * 100, 1), 1, trim=True) + "%"
    if fmt == "duration":
        return _words(int(_round(n, 0)))
    if fmt == "distance":
        metres = _round(n, 0)
        if abs(metres) < 1000:
            return f"{_group(metres, 0)} m"
        return f"{_group(_round(n / 1000, 2), 2)} km"
    if fmt == "pace":
        secs = int(_round(n, 0))
        if secs <= 0:
            return NULL_TEXT
        return f"{_clock(secs)} /km"
    if fmt == "clock":
        exact = Fraction(n) / 60  # minutes, exactly; half a minute rounds away from zero
        whole = int(abs(exact) + Fraction(1, 2))
        minutes = (-whole if exact < 0 else whole) % 1440
        return f"{minutes // 60:02d}:{minutes % 60:02d}"
    raise AssertionError(fmt)  # pragma: no cover

// The value formats, mirroring easyui/formats.py exactly.
//
// The Python module is the reference: every example in easyui.formats.FORMATS
// must come out of formatValue() unchanged (tests/test_web_static.py runs them
// through Node when it is installed). Rounding is half away from zero on the
// exact binary value of the number, done with BigInt so it matches Python's
// Decimal(value).quantize(..., ROUND_HALF_UP) digit for digit.

export const NULL_TEXT = "—";
export const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
export const FORMAT_NAMES = ["number", "integer", "decimal1", "percent", "duration", "distance", "date", "datetime", "pace"];
const NUMERIC_FORMATS = new Set(["number", "integer", "decimal1", "percent", "duration", "distance", "pace"]);

const NUMERIC_TEXT = /^-?\d+(\.\d+)?$/;
const ISO_DATE = /^(\d{4})-(\d{2})-(\d{2})/;
const ISO_DATETIME = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/;

// ---- exact arithmetic ---------------------------------------------------

const view = new DataView(new ArrayBuffer(8));

// |x| = mant * 2^exp, exactly.
function decompose(x) {
  if (typeof x === "bigint") return { neg: x < 0n, mant: x < 0n ? -x : x, exp: 0 };
  view.setFloat64(0, x);
  const hi = view.getUint32(0);
  const lo = view.getUint32(4);
  const neg = hi >>> 31 === 1 && x !== 0;
  const e = (hi >>> 20) & 0x7ff;
  let mant = (BigInt(hi & 0xfffff) << 32n) | BigInt(lo);
  let exp;
  if (e === 0) {
    exp = -1074;
  } else {
    mant |= 1n << 52n;
    exp = e - 1075;
  }
  return { neg, mant, exp };
}

// Round x * mul to `places` decimals, half away from zero.
// Returns {neg, q}: the result is (neg ? -1 : 1) * q / 10^places.
function roundExact(x, places, mul = 1n) {
  const { neg, mant, exp } = decompose(x);
  const num = mant * mul * 10n ** BigInt(places);
  let q;
  if (exp >= 0) {
    q = num << BigInt(exp);
  } else {
    const d = 1n << BigInt(-exp);
    q = num / d;
    if (2n * (num % d) >= d) q += 1n;
  }
  return { neg, q };
}

function group({ neg, q }, places, trim = false) {
  const digits = q.toString().padStart(places + 1, "0");
  let whole = places ? digits.slice(0, -places) : digits;
  let frac = places ? digits.slice(-places) : "";
  if (trim) frac = frac.replace(/0+$/, "");
  const parts = [];
  while (whole.length > 3) {
    parts.unshift(whole.slice(-3));
    whole = whole.slice(0, -3);
  }
  parts.unshift(whole);
  const out = parts.join(",") + (frac ? `.${frac}` : "");
  return (neg && q !== 0n ? "-" : "") + out; // a value that rounds to zero has no minus sign
}

function signedInt(r) {
  return r.neg ? -r.q : r.q;
}

function clock(total) {
  // total: BigInt seconds
  const sign = total < 0n ? "-" : "";
  const t = total < 0n ? -total : total;
  const hours = t / 3600n;
  const rest = t % 3600n;
  const m = String(rest / 60n).padStart(2, "0");
  const s = String(rest % 60n).padStart(2, "0");
  return hours ? `${sign}${hours}:${m}:${s}` : `${sign}${rest / 60n}:${s}`;
}

// Seconds in words: "7 h 07 min" (to the minute, from one hour on), "25 min 31 s", "45 s".
function words(total) {
  // total: BigInt seconds
  const sign = total < 0n ? "-" : "";
  const t = total < 0n ? -total : total;
  if (t >= 3600n) {
    const minutes = (t + 30n) / 60n;
    return `${sign}${minutes / 60n} h ${String(minutes % 60n).padStart(2, "0")} min`;
  }
  if (t >= 60n) {
    const s = t % 60n;
    return s ? `${sign}${t / 60n} min ${String(s).padStart(2, "0")} s` : `${sign}${t / 60n} min`;
  }
  return t ? `${sign}${t} s` : "0 s";
}

// A number for formatting, or null. Plain numeric text counts as a number
// (whole numbers exactly, like Python's int()).
function asNumber(value) {
  if (typeof value === "boolean") return null;
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value === "bigint") return value;
  if (typeof value === "string") {
    const s = value.trim();
    if (NUMERIC_TEXT.test(s)) return s.includes(".") ? Number(s) : BigInt(s);
  }
  return null;
}

function toFloat(n) {
  return typeof n === "bigint" ? Number(n) : n;
}

function pyStr(value) {
  if (value === true) return "True";
  if (value === false) return "False";
  return String(value);
}

function daysIn(y, m) {
  if (m === 2) return (y % 4 === 0 && y % 100 !== 0) || y % 400 === 0 ? 29 : 28;
  return [4, 6, 9, 11].includes(m) ? 30 : 31;
}

function dateText(y, m, d) {
  if (y < 1 || y > 9999 || m < 1 || m > 12 || d < 1 || d > daysIn(y, m)) return null;
  return `${d} ${MONTHS[m - 1]} ${y}`;
}

function formatDate(fmt, value) {
  if (typeof value === "number" || typeof value === "bigint") {
    const n = typeof value === "bigint" ? Number(value) : value;
    if (!Number.isFinite(n)) return pyStr(value);
    const secs = Math.trunc(n);
    const t = new Date(secs * 1000);
    const y = t.getUTCFullYear();
    if (Number.isNaN(t.getTime()) || y < 1 || y > 9999) return pyStr(value);
    const text = dateText(y, t.getUTCMonth() + 1, t.getUTCDate()) || pyStr(value);
    if (fmt === "date") return text;
    const hh = String(t.getUTCHours()).padStart(2, "0");
    const mm = String(t.getUTCMinutes()).padStart(2, "0");
    return `${text} ${hh}:${mm}`;
  }
  if (typeof value === "string") {
    const m = ISO_DATE.exec(value);
    if (!m) return value;
    let text = dateText(Number(m[1]), Number(m[2]), Number(m[3]));
    if (text === null) return value;
    if (fmt === "datetime") {
      const mt = ISO_DATETIME.exec(value);
      if (mt) text += ` ${mt[4]}:${mt[5]}`;
    }
    return text;
  }
  return pyStr(value);
}

/**
 * Format a value. fmt is a format name or null (auto). Throws for an unknown
 * name, like the Python reference.
 */
export function formatValue(fmt, value) {
  if (fmt !== null && fmt !== undefined && !FORMAT_NAMES.includes(fmt)) {
    throw new Error(`unknown format ${fmt}`);
  }
  if (value === null || value === undefined) return NULL_TEXT;
  if (fmt === "date" || fmt === "datetime") return formatDate(fmt, value);

  const n = asNumber(value);
  if (n === null) {
    if (typeof value === "number") return NULL_TEXT; // inf / nan
    return typeof value === "string" ? value : pyStr(value);
  }
  if (fmt === null || fmt === undefined || fmt === "number") return group(roundExact(n, 2), 2, true);
  if (fmt === "integer") return group(roundExact(n, 0), 0);
  if (fmt === "decimal1") return group(roundExact(n, 1), 1);
  if (fmt === "percent") return `${group(roundExact(n, 1, 100n), 1, true)}%`;
  if (fmt === "duration") return words(signedInt(roundExact(n, 0)));
  if (fmt === "distance") {
    const metres = roundExact(n, 0);
    if (metres.q < 1000n) return `${group(metres, 0)} m`;
    return `${group(roundExact(toFloat(n) / 1000, 2), 2)} km`;
  }
  if (fmt === "pace") {
    const secs = signedInt(roundExact(n, 0));
    if (secs <= 0n) return NULL_TEXT;
    return `${clock(secs)} /km`;
  }
  throw new Error(`unhandled format ${fmt}`);
}

/** Like formatValue, but an unknown format name falls back to auto. */
export function display(fmt, value) {
  return formatValue(FORMAT_NAMES.includes(fmt) ? fmt : null, value);
}

export function isNumericFormat(fmt) {
  return NUMERIC_FORMATS.has(fmt);
}

/** A number from a data value, or null (for charts and sorting). */
export function numeric(value) {
  const n = asNumber(value);
  return n === null ? null : toFloat(n);
}

/**
 * Split formatted text into the figure and its unit, so the unit can be set
 * smaller: "12.35 km" -> ["12.35", "km"], "25.6%" -> ["25.6", "%"].
 */
export function splitUnit(text) {
  const m = /^(-?[\d,.:]+)\s?(km|m|\/km|%)$/.exec(text);
  return m ? [m[1], m[2]] : [text, ""];
}

// ---- axis labels (compact, our own; not part of the format contract) ----

function trimmed(x, places) {
  const r = roundExact(x, places);
  return group(r, places, true);
}

// One short form for a whole axis, chosen from its largest value, so the
// labels read 15k, 10k, 5k, 0 rather than 15k, 10k, 5,000, 0.
function compact(v, max) {
  const a = Math.abs(max ?? v);
  if (a >= 1e9) return `${trimmed(v / 1e9, 1)}B`;
  if (a >= 1e6) return `${trimmed(v / 1e6, 1)}M`;
  if (a >= 1e4) return `${trimmed(v / 1e3, 1)}k`;
  return trimmed(v, 2);
}

/** A function for axis tick labels. `max` is the largest absolute value shown. */
export function axisFormatter(fmt, max) {
  switch (fmt) {
    case "percent":
      return (v) => `${trimmed(v * 100, 1)}%`;
    case "distance":
      return max >= 1000 ? (v) => `${trimmed(v / 1000, 1)} km` : (v) => `${trimmed(v, 0)} m`;
    case "duration":
      return max >= 3600 ? (v) => `${trimmed(v / 3600, 1)} h` : (v) => `${trimmed(v / 60, 0)} min`;
    case "pace":
      return (v) => (v > 0 ? clock(signedInt(roundExact(v, 0))) : "");
    default:
      return (v) => compact(v, max);
  }
}

/**
 * A round tick step for time-based formats (seconds), so axes read 0, 30 min,
 * 1 h rather than 0, 33 min, 67 min. null lets the chart choose.
 */
export function axisStep(fmt, max) {
  if (fmt !== "duration" && fmt !== "pace") return null;
  if (!(max > 0)) return null;
  const steps = fmt === "pace"
    ? [5, 10, 15, 30, 60, 120, 300, 600]
    : [60, 120, 300, 600, 900, 1200, 1800, 3600, 7200, 10800, 21600, 43200, 86400];
  for (const s of steps) if (max / s <= 4) return s;
  return null;
}

/** True when every value looks like an ISO date (for date-aware axis labels). */
export function looksLikeDates(values) {
  let seen = 0;
  for (const v of values) {
    if (v === null || v === undefined) continue;
    if (typeof v !== "string" || !ISO_DATE.test(v)) return false;
    seen += 1;
  }
  return seen > 0;
}

/** Short date label for an axis: "14 Mar", or "Mar 2026" when the range spans years. */
export function shortDate(value, spansYears) {
  const m = typeof value === "string" ? ISO_DATE.exec(value) : null;
  if (!m) return pyStr(value);
  const mon = MONTHS[Number(m[2]) - 1];
  if (!mon) return value;
  return spansYears ? `${mon} ${m[1]}` : `${Number(m[3])} ${mon}`;
}

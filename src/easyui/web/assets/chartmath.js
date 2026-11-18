// The arithmetic behind the charts, kept apart from drawing so it can be tested on its own (no DOM here):
// filling missing days so a line breaks instead of joining across them, round axis ranges with headroom,
// and date labels that never crowd (days, weeks, months, quarters or years, by the room there is).

import { MONTHS } from "./format.js";

const PLAIN_DAY = /^(\d{4})-(\d{2})-(\d{2})$/;
const DAY_MS = 86400000;

/** Days since 1970 for "YYYY-MM-DD" (a plain date, nothing after it), or null. */
export function dayNumber(text) {
  const m = typeof text === "string" ? PLAIN_DAY.exec(text) : null;
  if (!m) return null;
  const t = Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  const d = new Date(t);
  if (d.getUTCDate() !== Number(m[3]) || d.getUTCMonth() !== Number(m[2]) - 1) return null;
  return Math.round(t / DAY_MS);
}

function dayText(n) {
  return new Date(n * DAY_MS).toISOString().slice(0, 10);
}

/**
 * Where a daily (or weekly) date series has holes, the missing dates as empty rows, so a line stops at a
 * missing day instead of drawing straight across it. Returns {xs, rowOf} (rowOf[i]: the row of the query
 * behind point i, or -1 for a filled-in date), or null when nothing is filled in: when the x values are not
 * plain dates in rising order, when the dates are not a regular daily or weekly series, or when filling
 * would more than double the points.
 */
export function fillDateGaps(xs, limit = 6000) {
  if (xs.length < 3) return null;
  const days = [];
  for (const x of xs) {
    const n = dayNumber(x);
    if (n === null) return null;
    if (days.length && n <= days[days.length - 1]) return null;
    days.push(n);
  }
  const counts = new Map();
  for (let i = 1; i < days.length; i += 1) {
    const d = days[i] - days[i - 1];
    counts.set(d, (counts.get(d) || 0) + 1);
  }
  let step = 0;
  let best = 0;
  for (const [d, c] of counts) if (c > best || (c === best && d < step)) [step, best] = [d, c];
  if (step !== 1 && step !== 7) return null;
  if ([...counts.keys()].some((d) => d % step !== 0)) return null;
  const total = (days[days.length - 1] - days[0]) / step + 1;
  if (total === xs.length) return null;
  if (total > Math.min(limit, xs.length * 2 + 60)) return null;
  const out = [];
  const rowOf = [];
  let r = 0;
  for (let n = days[0]; n <= days[days.length - 1]; n += step) {
    if (days[r] === n) {
      out.push(xs[r]);
      rowOf.push(r);
      r += 1;
    } else {
      out.push(dayText(n));
      rowOf.push(-1);
    }
  }
  return { xs: out, rowOf };
}

// ---- the value axis ---------------------------------------------------------

// Round steps for the formats whose numbers are seconds or metres: 0, 30 min, 1 h; 0, 20 km, 40 km.
const ROUND_STEPS = {
  pace: [5, 10, 15, 30, 60, 120, 300, 600],
  clock: [300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200],
  duration: [60, 120, 300, 600, 900, 1200, 1800, 3600, 7200, 10800, 21600, 43200, 86400],
  distance: [100, 200, 250, 500, 1000, 2000, 5000, 10000, 20000, 25000, 50000, 100000, 200000, 250000, 500000,
    1000000, 2000000, 5000000, 10000000],
};

/** The smallest round step (1, 2, 2.5 or 5 times a power of ten, or a round time or distance) of at least raw. */
export function niceStep(raw, format) {
  if (!(raw > 0) || !Number.isFinite(raw)) return 1;
  const list = ROUND_STEPS[format];
  if (list && !(format === "distance" && raw < 100)) {
    for (const s of list) if (s >= raw) return s;
  }
  const p = 10 ** Math.floor(Math.log10(raw));
  for (const m of [1, 2, 2.5, 5, 10]) {
    if (m * p >= raw * (1 - 1e-9)) return format === "integer" ? Math.max(1, m * p) : m * p;
  }
  return 10 * p;
}

/**
 * The ends and step of a value axis: round numbers, a little room above (and below) the values so a line
 * never runs along the edge, and zero included for bars and filled areas (their length is their value).
 * {lo, hi}: the values shown; ticks: about how many gaps; zero: start from zero; step: a chosen step or null.
 * Returns {min, max, interval} or null when there is nothing to show.
 */
export function valueRange({ lo, hi, ticks = 4, zero = false, step = null, format = null }) {
  if (lo === null || hi === null || !Number.isFinite(lo) || !Number.isFinite(hi)) return null;
  let a = lo;
  let b = hi;
  if (zero) {
    a = Math.min(0, a);
    b = Math.max(0, b);
  }
  if (a === b) {
    const pad = Math.abs(a) > 0 ? Math.abs(a) * 0.1 : 1;
    a -= zero && a >= 0 ? 0 : pad;
    b += pad;
  }
  const span = b - a;
  // Headroom: lines get room on both sides, bars and areas above (and below when there are negatives).
  const room = span * (zero ? 0.04 : 0.08);
  const top = b > 0 || !zero ? b + room : b;
  const bottom = zero && a >= 0 ? a : a - room;
  let interval = step && step > 0 ? step : niceStep((top - bottom) / Math.max(2, ticks), format);
  let min = Math.floor(bottom / interval + 1e-9) * interval;
  let max = Math.ceil(top / interval - 1e-9) * interval;
  if (!zero && lo >= 0 && min < 0) min = 0;
  // A chosen step far too small for these values: fall back to a round one.
  if ((max - min) / interval > 40) {
    interval = niceStep((top - bottom) / Math.max(2, ticks), format);
    min = Math.floor(bottom / interval + 1e-9) * interval;
    max = Math.ceil(top / interval - 1e-9) * interval;
    if (!zero && lo >= 0 && min < 0) min = 0;
  }
  if (max <= min) max = min + interval;
  return { min: clean(min), max: clean(max), interval };
}

function clean(x) {
  return Number.parseFloat(x.toPrecision(12));
}

// ---- date labels ------------------------------------------------------------

function parts(n) {
  const d = new Date(n * DAY_MS);
  return { y: d.getUTCFullYear(), m: d.getUTCMonth(), d: d.getUTCDate(), wd: (d.getUTCDay() + 6) % 7 };
}

/**
 * Which points of a date axis get a label, and what each says, so that labels never overlap and fall on
 * natural boundaries: every day, every other day, Mondays, every other Monday, the first of each month,
 * every quarter, or each year, whichever is the finest that fits. `width` is the room for the axis in CSS
 * pixels and `charWidth` about how wide one character of the labels is.
 * Returns {labels: Map(index -> text), unit} or null when the values are not dates.
 */
export function dateTicks(xs, width, charWidth = 6.6) {
  const days = xs.map((x) => (typeof x === "string" ? dayNumber(x.slice(0, 10)) : null));
  const known = days.filter((d) => d !== null);
  if (!known.length) return null;
  const n = xs.length;
  const per = n > 1 ? width / (n - 1) : width;
  const first = parts(known[0]);
  const last = parts(known[known.length - 1]);
  const spansYears = first.y !== last.y;
  // Labels keep well apart: at least about four characters of space between two of them.
  const gap = Math.max(charWidth * 4.2, 24);
  const fits = (idx, chars) => {
    for (let i = 1; i < idx.length; i += 1) if ((idx[i] - idx[i - 1]) * per < chars * charWidth + gap) return false;
    return true;
  };
  const dayLabel = (p) => `${p.d} ${MONTHS[p.m]}`;
  const monthLabel = (p, prev) => (spansYears && (p.m === 0 || !prev) ? `${MONTHS[p.m]} ${p.y}` : MONTHS[p.m]);
  const pick = (test) => {
    const idx = [];
    for (let i = 0; i < n; i += 1) if (days[i] !== null && test(i, parts(days[i]))) idx.push(i);
    return idx;
  };
  const firstOfMonth = (i, p) => {
    if (p.d === 1) return true;
    // A month whose first day is not on the axis is labelled at its first point, unless that point is late
    // in the month (the label would then say "March" under the 20th).
    if (i === 0) return p.d <= 7 && n > 1;
    const q = days[i - 1] === null ? null : parts(days[i - 1]);
    return q !== null && q.m !== p.m && p.d <= 7;
  };
  const candidates = [
    { unit: "day", test: () => true, chars: 6 },
    { unit: "2 days", test: (i) => (days[i] - known[0]) % 2 === 0, chars: 6 },
    { unit: "week", test: (i, p) => p.wd === 0, chars: 6 },
    { unit: "2 weeks", test: (i, p) => p.wd === 0 && Math.floor((days[i] - known[0]) / 7) % 2 === 0, chars: 6 },
    { unit: "month", test: firstOfMonth, chars: spansYears ? 8 : 3 },
    { unit: "quarter", test: (i, p) => firstOfMonth(i, p) && p.m % 3 === 0, chars: spansYears ? 8 : 3 },
    { unit: "half", test: (i, p) => firstOfMonth(i, p) && p.m % 6 === 0, chars: 8 },
    { unit: "year", test: (i, p) => firstOfMonth(i, p) && p.m === 0, chars: 4 },
  ];
  // Weekly data (every point a week apart) is labelled like days: each point is a week.
  for (const c of candidates) {
    let idx = pick(c.test);
    if (c.unit === "week" && !idx.length) continue;
    if (!idx.length) continue;
    if (idx.length > 1 && !fits(idx, c.chars)) continue;
    const labels = new Map();
    let prev = null;
    for (const i of idx) {
      const p = parts(days[i]);
      let text;
      if (c.unit === "year") text = String(p.y);
      else if (c.unit === "month" || c.unit === "quarter" || c.unit === "half") text = monthLabel(p, prev);
      else text = dayLabel(p);
      labels.set(i, text);
      prev = p;
    }
    // A long axis of days with room for only a few labels still says which year it is.
    if (spansYears && c.chars === 6 && idx.length) {
      const i0 = idx[0];
      const p0 = parts(days[i0]);
      if (fits(idx, 11)) labels.set(i0, `${p0.d} ${MONTHS[p0.m]} ${p0.y}`);
    }
    return { labels, unit: c.unit };
  }
  // Nothing fits (a very narrow chart): the first and the last date.
  const labels = new Map();
  const i0 = days.findIndex((d) => d !== null);
  labels.set(i0, spansYears ? `${MONTHS[first.m]} ${first.y}` : dayLabel(first));
  return { labels, unit: "ends" };
}

// Colour: the project's accent, the light/dark scheme, and the values derived from them.
//
// Every colour on the page is a documented custom property (--eui-*, see
// style.css and docs/STYLES.md). A project's theme stylesheet (ui/theme.css)
// may set them; where it does not, style.css falls back to --eui-auto-*
// values that this module writes onto the root element: the neutrals tinted
// with a trace of the accent, the accent made readable as text, and the chart
// series (the accent first, then companions around the hue circle at a calm
// lightness). All maths is in OKLab so tints stay even across hues. Nothing
// here sets an inline style attribute: values go in through element.style.

const DEFAULT_ACCENT = "#3d6fb6";
const HEX = /^#[0-9a-fA-F]{6}$/;

let accentHex = DEFAULT_ACCENT;
const listeners = new Set();
const darkQuery = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

// ---- colour maths ---------------------------------------------------------

function hexToRgb(hex) {
  const n = parseInt(hex.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255].map((v) => v / 255);
}

function rgbToHex(rgb) {
  return `#${rgb
    .map((v) => Math.round(Math.min(1, Math.max(0, v)) * 255).toString(16).padStart(2, "0"))
    .join("")}`;
}

const toLinear = (c) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
const fromLinear = (c) => (c <= 0.0031308 ? 12.92 * c : 1.055 * c ** (1 / 2.4) - 0.055);

function rgbToOklab([r, g, b]) {
  const [lr, lg, lb] = [r, g, b].map(toLinear);
  const l = Math.cbrt(0.4122214708 * lr + 0.5363325363 * lg + 0.0514459929 * lb);
  const m = Math.cbrt(0.2119034982 * lr + 0.6806995451 * lg + 0.1073969566 * lb);
  const s = Math.cbrt(0.0883024619 * lr + 0.2817188376 * lg + 0.6299787005 * lb);
  return [
    0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s,
    1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s,
    0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s,
  ];
}

function oklabToRgb([L, a, b]) {
  const l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3;
  const m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3;
  const s = (L - 0.0894841775 * a - 1.291485548 * b) ** 3;
  return [
    4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
    -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
    -0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s,
  ].map(fromLinear);
}

function toLch(hex) {
  const [L, a, b] = rgbToOklab(hexToRgb(hex));
  return { L, C: Math.hypot(a, b), H: (Math.atan2(b, a) * 180) / Math.PI };
}

const inGamut = (rgb) => rgb.every((v) => v >= -0.0005 && v <= 1.0005);

function fromLch({ L, C, H }) {
  const rad = (H * Math.PI) / 180;
  let lo = 0;
  let hi = C;
  let rgb = oklabToRgb([L, C * Math.cos(rad), C * Math.sin(rad)]);
  if (inGamut(rgb)) return rgbToHex(rgb);
  for (let i = 0; i < 20; i += 1) {
    const mid = (lo + hi) / 2;
    const test = oklabToRgb([L, mid * Math.cos(rad), mid * Math.sin(rad)]);
    if (inGamut(test)) {
      lo = mid;
      rgb = test;
    } else {
      hi = mid;
    }
  }
  return rgbToHex(oklabToRgb([L, lo * Math.cos(rad), lo * Math.sin(rad)]));
}

function mix(hexA, hexB, t) {
  const a = rgbToOklab(hexToRgb(hexA));
  const b = rgbToOklab(hexToRgb(hexB));
  return rgbToHex(oklabToRgb(a.map((v, i) => v + (b[i] - v) * t)));
}

function luminance(hex) {
  const [r, g, b] = hexToRgb(hex).map(toLinear);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

export function contrast(a, b) {
  const [x, y] = [luminance(a), luminance(b)].sort((p, q) => q - p);
  return (x + 0.05) / (y + 0.05);
}

// Move a colour's lightness until it reaches `ratio` against `bg`.
function withContrast(hex, bg, ratio) {
  if (contrast(hex, bg) >= ratio) return hex;
  const lch = toLch(hex);
  const darker = luminance(bg) > 0.4;
  let best = hex;
  for (let i = 1; i <= 60; i += 1) {
    const L = darker ? lch.L - i * 0.01 : lch.L + i * 0.01;
    if (L <= 0 || L >= 1) break;
    best = fromLch({ ...lch, L });
    if (contrast(best, bg) >= ratio) return best;
  }
  return best;
}

// ---- any CSS colour as #rrggbb -------------------------------------------

let probe = null;
const resolved = new Map();

/**
 * A CSS colour of any form (#abc, rgb(), oklch(), a name...) as "#rrggbb" or "rgba(r, g, b, a)", by painting
 * one pixel with it; null when it is not a colour. Charts are drawn on a canvas, which needs plain colours.
 */
export function resolveColor(value) {
  const v = String(value || "").trim();
  if (!v) return null;
  if (HEX.test(v)) return v.toLowerCase();
  if (resolved.has(v)) return resolved.get(v);
  if (!probe) {
    const c = document.createElement("canvas");
    c.width = 1;
    c.height = 1;
    probe = c.getContext("2d", { willReadFrequently: true });
  }
  let out = null;
  if (probe) {
    probe.clearRect(0, 0, 1, 1);
    probe.fillStyle = "#010203";
    probe.fillStyle = v;
    const unchanged = probe.fillStyle === "#010203";
    if (!unchanged || /^#010203$/i.test(v)) {
      probe.fillRect(0, 0, 1, 1);
      const [r, g, b, a] = probe.getImageData(0, 0, 1, 1).data;
      out = a === 255 ? rgbToHex([r / 255, g / 255, b / 255]) : `rgba(${r}, ${g}, ${b}, ${(a / 255).toFixed(3)})`;
    }
  }
  if (resolved.size > 256) resolved.clear();
  resolved.set(v, out);
  return out;
}

function hexOf(value, fallback) {
  const c = resolveColor(value);
  return c && HEX.test(c) ? c : fallback;
}

// ---- tokens ---------------------------------------------------------------

function cssToken(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return HEX.test(v) ? v.toLowerCase() : fallback;
}

function prop(el, name) {
  return getComputedStyle(el).getPropertyValue(name).trim();
}

export function isDark() {
  return Boolean(darkQuery && darkQuery.matches);
}

let current = null;

/** The values derived from an accent, a page colour and a panel colour. */
function derive(accent, paper, surface, ink, dark) {
  const accentInk = withContrast(accent, paper, 4.5);
  const onAccent = contrast("#ffffff", accentInk) >= contrast(ink, accentInk) ? "#ffffff" : "#10151c";
  const accentSoft = mix(surface, accent, dark ? 0.2 : 0.11);
  const a = toLch(accent);
  const C = Math.max(0.06, Math.min(a.C, 0.13));
  const L = dark ? 0.74 : 0.6;
  const offsets = [0, 160, 75, 250, 35, 200, 115, 290];
  // On a dark panel thin lines need more contrast than text-sized marks: the accent is lightened for them.
  const series = offsets.map((off, i) => withContrast(i === 0 ? accent : fromLch({ L, C, H: a.H + off }), surface, dark ? 4.5 : 3));
  return { accentInk, onAccent, accentSoft, series };
}

function compute() {
  const dark = isDark();
  const root = document.documentElement;
  const base = {
    paper: cssToken("--base-paper", dark ? "#161a20" : "#f3f5f7"),
    surface: cssToken("--base-surface", dark ? "#1d222a" : "#ffffff"),
    ink: cssToken("--base-ink", dark ? "#e8ebef" : "#1a212b"),
  };
  // Without a theme, the page and panels carry a faint trace of the project's colour.
  const s = root.style;
  s.setProperty("--eui-auto-accent", accentHex);
  s.setProperty("--eui-auto-background", mix(base.paper, accentHex, dark ? 0.04 : 0.022));
  s.setProperty("--eui-auto-surface", mix(base.surface, accentHex, dark ? 0.035 : 0.012));
  // What the page uses now (a theme's colours, or the ones just written).
  const accent = hexOf(prop(root, "--eui-accent"), accentHex);
  const paper = hexOf(prop(root, "--eui-background"), base.paper);
  const surface = hexOf(prop(root, "--eui-surface"), base.surface);
  const ink = hexOf(prop(root, "--eui-text"), base.ink);
  const d = derive(accent, paper, surface, ink, dark);
  return { dark, paper, surface, ink, accent, ...d };
}

function apply() {
  current = compute();
  const s = document.documentElement.style;
  s.setProperty("--eui-auto-accent-text", current.accentInk);
  s.setProperty("--eui-auto-on-accent", current.onAccent);
  s.setProperty("--eui-auto-accent-soft", current.accentSoft);
  current.series.forEach((c, i) => s.setProperty(`--eui-auto-chart-${i + 1}`, c));
  document.documentElement.dataset.theme = current.dark ? "dark" : "light";
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", current.paper);
  for (const fn of listeners) fn(current);
}

/** Set the project's accent ("#RRGGBB"; anything else keeps the default). */
export function setAccent(hex) {
  accentHex = typeof hex === "string" && HEX.test(hex) ? hex.toLowerCase() : DEFAULT_ACCENT;
  apply();
}

/** Work the derived values out again (after the theme stylesheet was added or taken away). */
export function refreshTheme() {
  apply();
}

export function palette() {
  if (!current) apply();
  return current;
}

/** Call fn(palette) whenever the scheme, the accent or the theme changes. Returns an unsubscribe function. */
export function onThemeChange(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

// ---- one dashboard's own theme: (compiled into {both, light, dark} custom properties) ----

const SAFE_PROP = /^--eui-[a-z0-9-]{1,40}$/;
const SAFE_VALUE = /^[#\w\s.,%()"'/+-]{0,400}$/;

/**
 * Put a dashboard's theme: onto its element (through element.style), for the current scheme, and work out the
 * values that follow from its colours (readable accent, chart series) unless it sets them itself.
 */
export function applyOverrides(el, tokens) {
  for (const name of Array.from(el.style)) if (name.startsWith("--eui-")) el.style.removeProperty(name);
  if (!tokens || typeof tokens !== "object") return;
  const dark = isDark();
  const set = { ...(tokens.both || {}), ...((dark ? tokens.dark : tokens.light) || {}) };
  for (const [k, v] of Object.entries(set)) {
    if (SAFE_PROP.test(k) && typeof v === "string" && SAFE_VALUE.test(v)) el.style.setProperty(k, v);
  }
  const colours = ["--eui-accent", "--eui-background", "--eui-surface", "--eui-text"];
  if (!colours.some((k) => k in set)) return;
  const p = palette();
  const accent = hexOf(prop(el, "--eui-accent"), p.accent);
  const paper = hexOf(prop(el, "--eui-background"), p.paper);
  const surface = hexOf(prop(el, "--eui-surface"), p.surface);
  const ink = hexOf(prop(el, "--eui-text"), p.ink);
  const d = derive(accent, paper, surface, ink, dark);
  el.style.setProperty("--eui-accent-text", d.accentInk);
  el.style.setProperty("--eui-on-accent", d.onAccent);
  el.style.setProperty("--eui-accent-soft", d.accentSoft);
  if (!("--eui-good" in set)) el.style.setProperty("--eui-good", d.accentInk);
  d.series.forEach((c, i) => {
    if (!(`--eui-chart-${i + 1}` in set)) el.style.setProperty(`--eui-chart-${i + 1}`, c);
  });
}

if (darkQuery) darkQuery.addEventListener("change", apply);

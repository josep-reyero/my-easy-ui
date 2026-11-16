// Colour: the app's accent, the light/dark scheme, and the chart palette.
//
// The neutrals live in style.css as plain hex tokens (--base-*), one set per
// scheme. This module reads them, mixes in the app's accent and writes the
// derived tokens (--accent, --accent-ink, --on-accent, --accent-soft,
// --paper, --surface) onto the root element. Charts get concrete colours from
// here as well. All maths is in OKLab so tints stay even across hues.

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

// ---- tokens ---------------------------------------------------------------

function cssToken(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return HEX.test(v) ? v.toLowerCase() : fallback;
}

export function isDark() {
  return Boolean(darkQuery && darkQuery.matches);
}

let current = null;

function compute() {
  const dark = isDark();
  const base = {
    paper: cssToken("--base-paper", dark ? "#161a20" : "#f3f5f7"),
    surface: cssToken("--base-surface", dark ? "#1d222a" : "#ffffff"),
    ink: cssToken("--base-ink", dark ? "#e8ebef" : "#1a212b"),
    ink2: cssToken("--base-ink-2", dark ? "#a0a9b5" : "#5a6472"),
    line: cssToken("--base-line", dark ? "#2e353f" : "#dde2e8"),
    grid: cssToken("--base-grid", dark ? "#272d36" : "#e9ecf0"),
  };
  // The page and panels carry a faint trace of the app's colour.
  const paper = mix(base.paper, accentHex, dark ? 0.04 : 0.022);
  const surface = mix(base.surface, accentHex, dark ? 0.035 : 0.012);
  const accentInk = withContrast(accentHex, paper, 4.5);
  const onAccent = contrast("#ffffff", accentInk) >= contrast(base.ink, accentInk) ? "#ffffff" : "#10151c";
  const accentSoft = mix(surface, accentHex, dark ? 0.2 : 0.11);

  // Series colours: the accent first, then companions spaced around the hue
  // circle at a matching calm lightness and chroma.
  const a = toLch(accentHex);
  const C = Math.max(0.06, Math.min(a.C, 0.13));
  const L = dark ? 0.74 : 0.6;
  const offsets = [0, 160, 75, 250, 35, 200, 115, 290];
  const series = offsets.map((off, i) => {
    const hex = i === 0 ? accentHex : fromLch({ L, C, H: a.H + off });
    return withContrast(hex, surface, 3);
  });

  return { dark, ...base, paper, surface, accent: accentHex, accentInk, onAccent, accentSoft, series };
}

function apply() {
  current = compute();
  const s = document.documentElement.style;
  s.setProperty("--accent", current.accent);
  s.setProperty("--accent-ink", current.accentInk);
  s.setProperty("--on-accent", current.onAccent);
  s.setProperty("--accent-soft", current.accentSoft);
  s.setProperty("--paper", current.paper);
  s.setProperty("--surface", current.surface);
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", current.paper);
  for (const fn of listeners) fn(current);
}

/** Set the app's accent ("#RRGGBB"; anything else keeps the default). */
export function setAccent(hex) {
  accentHex = typeof hex === "string" && HEX.test(hex) ? hex.toLowerCase() : DEFAULT_ACCENT;
  apply();
}

export function palette() {
  if (!current) apply();
  return current;
}

/** Call fn(palette) whenever the scheme or accent changes. Returns an unsubscribe function. */
export function onThemeChange(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

if (darkQuery) darkQuery.addEventListener("change", apply);

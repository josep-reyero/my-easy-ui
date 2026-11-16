// Small DOM helpers. Every piece of text goes in through text nodes, never
// through HTML parsing, so nothing from a dashboard or its data can become
// markup or script.

const SVG_NS = "http://www.w3.org/2000/svg";

/**
 * h("div", {class: "x", attrs: {"aria-label": "..."}, on: {click: fn}}, child, ...)
 * Children may be strings (become text nodes), nodes, arrays, or null/false (skipped).
 * Props: class, text, attrs, on, dataset, hidden, plus a few element
 * properties (type, value, name, checked, disabled, id, htmlFor, min, max, step,
 * placeholder, autocomplete, maxLength, tabIndex, required).
 */
export function h(tag, props, ...children) {
  const el = document.createElement(tag);
  applyProps(el, props || {});
  append(el, children);
  return el;
}

const PROPERTY_KEYS = new Set([
  "type", "value", "name", "checked", "disabled", "id", "htmlFor", "min", "max",
  "step", "placeholder", "autocomplete", "maxLength", "tabIndex", "required",
  "inputMode", "spellcheck", "selected",
]);

function applyProps(el, props) {
  for (const [key, val] of Object.entries(props)) {
    if (val === undefined || val === null || val === false) continue;
    if (key === "class") {
      el.className = Array.isArray(val) ? val.filter(Boolean).join(" ") : val;
    } else if (key === "text") {
      el.textContent = String(val);
    } else if (key === "attrs") {
      for (const [name, v] of Object.entries(val)) {
        if (v === undefined || v === null || v === false) continue;
        if (name === "style" || name.startsWith("on") || name === "href" || name === "src") {
          throw new Error(`attribute ${name} must not be set through h()`);
        }
        el.setAttribute(name, v === true ? "" : String(v));
      }
    } else if (key === "on") {
      for (const [type, fn] of Object.entries(val)) el.addEventListener(type, fn);
    } else if (key === "dataset") {
      for (const [k, v] of Object.entries(val)) el.dataset[k] = String(v);
    } else if (key === "hidden") {
      el.hidden = true;
    } else if (PROPERTY_KEYS.has(key)) {
      el[key] = val;
    } else {
      throw new Error(`unknown prop ${key}`);
    }
  }
}

export function append(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

/** Replace all children of el. */
export function fill(el, ...children) {
  el.replaceChildren();
  return append(el, children);
}

/** An https link built from a checked URL; anything else becomes plain text. */
export function safeLink(href, children, className) {
  const url = checkedHttps(href);
  if (!url) return h("span", { class: className }, children);
  const a = h("a", { class: className }, children);
  a.href = url;
  a.rel = "noopener noreferrer";
  a.target = "_blank";
  a.append(h("span", { class: "sr-only", text: " (opens in a new tab)" }));
  return a;
}

export function checkedHttps(href) {
  if (typeof href !== "string" || !/^https:\/\//i.test(href)) return null;
  try {
    const url = new URL(href);
    return url.protocol === "https:" ? url.href : null;
  } catch {
    return null;
  }
}

/** An in-app link (hash route). */
export function routeLink(hash, children, props = {}) {
  if (typeof hash !== "string" || !hash.startsWith("#/")) throw new Error("bad route");
  const a = h("a", props, children);
  a.href = hash;
  return a;
}

/** A small inline SVG icon from a fixed set of path data (no markup parsing). */
const ICONS = {
  refresh: "M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7",
  table: "M4 5h16v14H4zM4 10h16M4 15h16M10 5v14",
  chart: "M4 19h16M7 16V10M12 16V6M17 16v-4",
  eye: "M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12zM12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6z",
  eyeOff: "M3 3l18 18M10.6 5.1A10.6 10.6 0 0 1 12 5c6.4 0 10 7 10 7a17 17 0 0 1-3.2 4.1M6.6 6.6C3.8 8.4 2 12 2 12s3.6 7 10 7a9.8 9.8 0 0 0 5.4-1.6M9.9 9.9a3 3 0 0 0 4.2 4.2",
  back: "M15 18l-6-6 6-6",
};

export function icon(name) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  svg.setAttribute("class", "icon");
  const path = document.createElementNS(SVG_NS, "path");
  path.setAttribute("d", ICONS[name]);
  svg.append(path);
  return svg;
}

let uid = 0;
export function nextId(prefix = "e") {
  uid += 1;
  return `${prefix}-${uid}`;
}

export const prefersReducedMotion = () =>
  window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

// The project's look: its generated theme stylesheet (ui/theme.css) and its
// stylesheets (ui/styles/*.css), and when the page may use them.
//
// The theme stylesheet holds only values easy-ui checked itself (colours,
// font stacks, sizes) and the project's @font-face rules; it is added to the
// page for the dashboards and the list of dashboards, and taken away for
// easy-ui's own screens (sign-in settings, pages that list problems, "not
// found"). The sign-in screen never has it: it is only asked for after
// signing in. The project's stylesheets are never added to the page itself:
// each dashboard loads them into its own document tree (dashboard.js).
//
// A single-file copy (easy-ui export --single-file) has no files beside it:
// its stylesheets are inside the page (easy-ui's own in the page's style element,
// the look in the "easyui-look" block, with every font and picture as a data:
// address) and are added as style elements, which the page's policy allows
// only by their hashes.

import { api } from "./api.js";
import { refreshTheme } from "./theme.js";

const EMPTY = { rev: "none", theme_css: false, stylesheets: [], app_stylesheets: [], static: false };
const NAME = /^[a-z0-9][a-z0-9._-]{0,60}\.css$/;
// An app's stylesheet (scoped by easy-ui to that app's dashboards and sections): apps/<app>/styles/<name>.css
const APP_SHEET = /^apps\/[a-z][a-z0-9-]{1,30}\/styles\/[a-z0-9][a-z0-9._-]{0,60}\.css$/;
let info = EMPTY;
let allowed = false;
let themeLink = null;
let themeReady = Promise.resolve();
let inline = null; // address -> stylesheet text, in a single-file copy only

function inlineSheets() {
  if (inline) return inline;
  inline = new Map();
  const block = document.getElementById("easyui-look");
  if (!block) return inline; // the server and a folder copy: stylesheets are files
  const own = document.head.querySelector("style");
  if (own) inline.set(builtinStylesheet(), own.textContent);
  let sheets = {};
  try {
    sheets = JSON.parse(block.textContent) || {};
  } catch {
    sheets = {};
  }
  for (const [path, text] of Object.entries(sheets)) {
    if (typeof text === "string" && /^ui\/[A-Za-z0-9._/-]{1,320}$/.test(path)) inline.set(new URL(path, document.baseURI).href, text);
  }
  return inline;
}

/**
 * A stylesheet element for an address: a link element, or in a single-file copy a style element with the text the page
 * carries (resolved at once; the page's policy allows it by its hash). `ready` resolves once it is in use.
 */
export function makeStylesheet(url) {
  const text = inlineSheets().get(url);
  if (typeof text === "string") {
    const el = document.createElement("style");
    el.textContent = text;
    el.dataset.url = url;
    return { el, ready: Promise.resolve() };
  }
  const sheet = document.createElement("link");
  sheet.rel = "stylesheet";
  sheet.dataset.url = url;
  const ready = new Promise((resolve) => {
    sheet.addEventListener("load", resolve, { once: true });
    sheet.addEventListener("error", resolve, { once: true });
  });
  sheet.href = url;
  return { el: sheet, ready };
}

function address(rel) {
  const url = new URL(rel, document.baseURI);
  // A new version of the look is fetched again; a shared copy (files on a disk) has only one.
  if (!info.static && info.rev) url.searchParams.set("v", info.rev);
  return url.href;
}

/** easy-ui's own stylesheet, the first one of every dashboard's tree. */
export function builtinStylesheet() {
  return new URL("assets/style.css", document.baseURI).href;
}

/** Ask which look the dashboards use. True when it changed. */
export async function loadLook() {
  let got = null;
  try {
    got = await api.styles();
  } catch {
    got = null;
  }
  const next = got && typeof got === "object" ? got : EMPTY;
  const sheets = Array.isArray(next.stylesheets) ? next.stylesheets.filter((n) => typeof n === "string" && NAME.test(n)) : [];
  const appSheets = Array.isArray(next.app_stylesheets)
    ? next.app_stylesheets.filter((n) => typeof n === "string" && APP_SHEET.test(n)) : [];
  const fresh = {
    rev: typeof next.rev === "string" ? next.rev.slice(0, 64) : "none",
    theme_css: Boolean(next.theme_css),
    stylesheets: sheets,
    app_stylesheets: appSheets,
    static: Boolean(next.static),
  };
  const changed = fresh.rev !== info.rev || fresh.theme_css !== info.theme_css
    || fresh.stylesheets.join("\n") !== info.stylesheets.join("\n")
    || fresh.app_stylesheets.join("\n") !== info.app_stylesheets.join("\n");
  info = fresh;
  sync();
  return changed;
}

/** The addresses of the apps' stylesheets and then the project's, in the order they apply (the project's last). */
export function projectStylesheets() {
  return [
    ...info.app_stylesheets.map((path) => address(`ui/${path}`)),
    ...info.stylesheets.map((name) => address(`ui/styles/${name}`)),
  ];
}

/** Whether the screen shown now may use the project's theme (never easy-ui's own screens). */
export function useLook(on) {
  allowed = Boolean(on);
  sync();
}

/** Signed out: forget the look until the next sign-in. */
export function forgetLook() {
  info = EMPTY;
  allowed = false;
  sync();
}

/** Resolves when the theme stylesheet the page wants has loaded (pictures wait for it). */
export function lookReady() {
  return themeReady;
}

function sync() {
  const want = allowed && info.theme_css ? address("ui/theme.css") : null;
  if (themeLink && themeLink.dataset.url !== want) {
    themeLink.remove();
    themeLink = null;
    themeReady = Promise.resolve();
    refreshTheme();
  }
  if (want && !themeLink) {
    const sheet = makeStylesheet(want);
    themeReady = new Promise((resolve) => {
      sheet.ready.then(() => {
        refreshTheme();
        resolve();
      });
      setTimeout(resolve, 4000);
    });
    document.head.append(sheet.el);
    themeLink = sheet.el;
  }
}

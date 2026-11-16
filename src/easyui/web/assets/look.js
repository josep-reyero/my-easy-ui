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

import { api } from "./api.js";
import { refreshTheme } from "./theme.js";

const EMPTY = { rev: "none", theme_css: false, stylesheets: [], static: false };
const NAME = /^[a-z0-9][a-z0-9._-]{0,60}\.css$/;
let info = EMPTY;
let allowed = false;
let themeLink = null;
let themeReady = Promise.resolve();

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
  const fresh = {
    rev: typeof next.rev === "string" ? next.rev.slice(0, 64) : "none",
    theme_css: Boolean(next.theme_css),
    stylesheets: sheets,
    static: Boolean(next.static),
  };
  const changed = fresh.rev !== info.rev || fresh.theme_css !== info.theme_css
    || fresh.stylesheets.join("\n") !== info.stylesheets.join("\n");
  info = fresh;
  sync();
  return changed;
}

/** The addresses of the project's stylesheets, in the order they apply. */
export function projectStylesheets() {
  return info.stylesheets.map((name) => address(`ui/styles/${name}`));
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
    const sheet = document.createElement("link");
    sheet.rel = "stylesheet";
    sheet.dataset.url = want;
    themeReady = new Promise((resolve) => {
      const done = () => {
        refreshTheme();
        resolve();
      };
      sheet.addEventListener("load", done, { once: true });
      sheet.addEventListener("error", done, { once: true });
      setTimeout(resolve, 4000);
    });
    sheet.href = want;
    document.head.append(sheet);
    themeLink = sheet;
  }
}

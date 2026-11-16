// Entry point: session check, sign-in, the app shell (title, list of
// dashboards) and a small hash router.
//
//   #/            the first dashboard (in the project's [ui] dashboards order): the landing page
//   #/all         the list of dashboards, with their descriptions
//   #/d/<slug>    one dashboard; non-default control values follow as ?name=value
//   #/sign-in     sign-in settings: passkeys, the password switch, recovery codes

import { h, fill, routeLink, icon } from "./dom.js";
import { api, describe } from "./api.js";
import { setAccent } from "./theme.js";
import { renderSignIn } from "./signin.js";
import { DashboardView } from "./dashboard.js";
import { startRefresh } from "./refresh.js";
import { SignInSettings } from "./settings.js";
import { passkeysSupported } from "./passkey.js";
import { loadLook, projectStylesheets, useLook, forgetLook, lookReady } from "./look.js";

const SLUG = /^[a-z0-9][a-z0-9-]{0,40}$/;
const root = document.getElementById("app");

const state = {
  app: null,
  dashboards: [],
  view: null,
  slug: null,
  stopRefresh: null,
  stopSignIn: null,
  refreshState: "live",
  updatedAt: null,
  shell: null,
  preview: false,
  ways: null,
  offerPasskey: false,
  exported: null, // {made, encrypted} in a static export (easy-ui export)
};

// ---- small views ----------------------------------------------------------

function message(title, hint, actions = []) {
  return h("div", { class: "page-message" }, [
    h("h1", { class: "page-message-title", text: title }),
    hint ? h("p", { class: "page-message-hint", text: hint }) : null,
    actions.length ? h("div", { class: "page-message-actions" }, actions) : null,
  ]);
}

function fatal(err, retry) {
  useLook(false);
  const info = describe(err);
  fill(root, h("main", { class: "standalone", id: "main" }, message(info.message, info.hint, [
    h("button", { class: "btn-primary", type: "button", text: "Try again", on: { click: retry } }),
  ])));
}

// ---- shell ----------------------------------------------------------------

function buildShell() {
  const nav = h("nav", { class: "nav", attrs: { "aria-label": "Dashboards" } });
  const main = h("main", { class: "main", id: "main", tabIndex: -1 });
  const signOut = h("button", {
    class: "btn-quiet signout",
    type: "button",
    text: "Sign out",
    on: { click: signOutNow },
  });
  // A preview signs in with a one-time link, so signing out would lock this browser out of it.
  const madeText = state.exported && state.exported.made ? `Shared copy · ${state.exported.made}` : "Shared copy";
  const end = state.exported && state.exported.picture
    ? null // a picture for an agent (easy-ui screenshot) looks like the live page
    : state.exported
    ? h("span", { class: "preview-badge export-badge", attrs: { title: "A copy of the dashboards as they were then; it does not change" }, text: madeText })
    : state.preview
    ? h("span", { class: "preview-badge", attrs: { title: "What you see comes from ui/dashboards/ as it is now" }, text: "Preview – not published" })
    : h("div", { class: "top-end" }, [
      routeLink("#/sign-in", "Sign-in", { class: "btn-quiet settings-link", attrs: { "aria-label": "Sign-in settings: passkeys, password, recovery codes" } }),
      signOut,
    ]);
  const header = h("header", { class: "top" }, h("div", { class: "top-inner" }, [
    routeLink("#/", state.app.title || state.app.name || "Dashboards", { class: "brand" }),
    nav,
    end,
  ]));
  const skip = h("a", { class: "skip", text: "Skip to content" });
  skip.href = "#main";
  skip.addEventListener("click", (e) => {
    e.preventDefault();
    main.focus();
  });
  fill(root, skip, header, main);
  state.shell = { nav, main };
  renderNav();
}

function renderNav() {
  const { nav } = state.shell;
  if (state.dashboards.length < 2) {
    fill(nav);
    nav.hidden = true;
    return;
  }
  nav.hidden = false;
  const items = state.dashboards.map((d) => {
    const current = d.slug === state.slug;
    const link = routeLink(`#/d/${d.slug}`, d.title || d.slug, {
      class: "nav-link",
      attrs: { "aria-current": current ? "page" : null },
    });
    return h("li", null, link);
  });
  items.push(h("li", null, routeLink("#/all", "All", {
    class: "nav-link nav-all",
    attrs: { "aria-current": parseHash().name === "list" ? "page" : null, "aria-label": "All dashboards, with what each shows" },
  })));
  fill(nav, h("ul", { class: "nav-list" }, items));
}

function renderHome() {
  const title = state.app.title || state.app.name;
  document.title = title;
  if (!state.dashboards.length) {
    fill(state.shell.main, message(
      "No dashboards yet",
      "Dashboards appear here once they are published. Your agent can make one and publish it with easy-ui publish.",
    ));
    return;
  }
  fill(state.shell.main, h("div", { class: "home" }, [
    h("h1", { class: "home-title", text: "Dashboards" }),
    h("ul", { class: "home-list" }, state.dashboards.map((d) => h("li", null,
      routeLink(`#/d/${d.slug}`, [
        h("span", { class: "home-item-title", text: d.title || d.slug }),
        d.description ? h("span", { class: "home-item-desc", text: d.description }) : null,
      ], { class: "home-item" })))),
  ]));
}

function statusText() {
  // A shared copy never changes: the badge in the header already says when it was made.
  if (state.exported) return ["", true];
  // 24-hour time, like the English dates on the page ("Checked 16:03"): when the page last
  // checked for new data, not how old the data is.
  const time = state.updatedAt
    ? `${String(state.updatedAt.getHours()).padStart(2, "0")}:${String(state.updatedAt.getMinutes()).padStart(2, "0")}`
    : null;
  switch (state.refreshState) {
    case "paused":
      return ["Paused while you are away. Updates resume when you come back.", true];
    case "offline":
      return ["The dashboard server is not answering. Trying again in a minute.", true];
    case "updating":
      return ["Updating…", false];
    default:
      return [time ? `Checked ${time}` : "", false];
  }
}

function showStatus() {
  if (!state.view) return;
  const [text, quiet] = statusText();
  state.view.setStatus(text, quiet);
}

// ---- routing --------------------------------------------------------------

function parseHash() {
  const raw = window.location.hash || "#/";
  const [path, query = ""] = raw.slice(1).split("?");
  const m = /^\/d\/([^/]+)\/?$/.exec(path);
  if (m) {
    let slug = "";
    try {
      slug = decodeURIComponent(m[1]);
    } catch {
      slug = "";
    }
    return { name: "dashboard", slug, query: new URLSearchParams(query) };
  }
  if (path === "/" || path === "") return { name: "home" };
  if (path === "/all" || path === "/all/") return { name: "list" };
  if (path === "/sign-in" || path === "/sign-in/") return { name: "settings" };
  return { name: "unknown" };
}

function leaveView() {
  if (state.view) state.view.destroy();
  state.view = null;
  state.slug = null;
}

async function route() {
  if (!state.shell) return;
  // A shared copy opened again with a (new) link key while already open: start over so the key is read.
  // (Only a new key of a locked copy does that; anything else after #k= is just taken out of the address.)
  if (state.exported && typeof api.keyInAddress === "function" && api.keyInAddress()) {
    window.location.reload();
    return;
  }
  const r = parseHash();
  if (r.name === "dashboard" && r.slug === state.slug && state.view) {
    return; // our own address update (control values)
  }
  leaveView();
  delete document.body.dataset.loaded;
  if (r.name === "home" && state.dashboards.length) {
    // Start on the first dashboard (the app's chosen order); the list is one click away in the menu.
    window.location.replace(`#/d/${state.dashboards[0].slug}`);
    return;
  }
  if (r.name === "home" || r.name === "list") {
    useLook(true);
    renderNav();
    renderHome();
    return;
  }
  if (r.name === "settings" && !state.preview && !state.exported) {
    // easy-ui's own screens never take the project's look.
    useLook(false);
    renderNav();
    const view = new SignInSettings({
      app: state.app,
      offer: state.offerPasskey,
      onSignInAgain: () => signOutNow("Sign in again to change how you sign in."),
    });
    state.offerPasskey = false;
    state.view = { el: view.el, destroy: () => view.destroy(), setStatus: () => {}, refresh: async () => "kept" };
    fill(state.shell.main, view.el);
    document.title = `Sign-in – ${state.app.title || state.app.name}`;
    return;
  }
  if (r.name === "unknown" || !SLUG.test(r.slug)) {
    renderNav();
    showMissing();
    return;
  }
  state.slug = r.slug;
  renderNav();
  const main = state.shell.main;
  fill(main, h("p", { class: "page-loading", attrs: { role: "status" }, text: "Opening the dashboard…" }));
  let spec;
  try {
    spec = await api.dashboard(r.slug);
  } catch (err) {
    if (state.slug !== r.slug) return;
    if (err && err.status === 401) return;
    if (err && err.status === 404) showMissing();
    else {
      useLook(false);
      fill(main, message(describe(err).message, describe(err).hint, [
        h("button", { class: "btn-primary", type: "button", text: "Try again", on: { click: () => { state.slug = null; route(); } } }),
      ]));
    }
    return;
  }
  if (state.slug !== r.slug) return;
  // A page that lists problems is easy-ui's own: drawn without the project's look.
  useLook(!(spec && spec.problems));
  const view = new DashboardView({
    spec,
    initial: r.query,
    project: state.app.name,
    stylesheets: projectStylesheets(),
    onLoaded: async (outcome) => {
      // Only a load that worked moves "Checked"; a failed one says so instead of looking fresh.
      if (outcome === "ok") state.updatedAt = new Date();
      await lookReady();
      if (state.view !== view) return;
      // For tools that take pictures of the page (easy-ui screenshot): every part has loaded.
      document.body.dataset.loaded = outcome;
      if (outcome === "offline") state.refreshState = "offline";
      else if (state.refreshState === "updating" || state.refreshState === "offline") state.refreshState = "live";
      showStatus();
    },
  });
  state.view = view;
  fill(main, view.el);
  document.title = `${spec.title} – ${state.app.title || state.app.name}`;
  showStatus();
}

function showMissing() {
  useLook(false);
  document.title = `Not found – ${state.app.title || state.app.name}`;
  fill(state.shell.main, message(
    "This dashboard does not exist",
    "It may have been renamed or removed. Pick one from the list instead.",
    [routeLink("#/all", [icon("back"), "All dashboards"], { class: "btn-quiet" })],
  ));
}

// ---- refresh --------------------------------------------------------------

async function onRevChange() {
  let lookChanged = false;
  try {
    [state.dashboards, lookChanged] = await Promise.all([api.dashboards(), loadLook()]);
  } catch {
    return;
  }
  renderNav();
  if (lookChanged && state.view && typeof state.view.setLook === "function") state.view.setLook(projectStylesheets());
  if (state.view) {
    state.refreshState = "updating";
    showStatus();
    const outcome = await state.view.refresh();
    if (outcome === "gone") {
      leaveView();
      showMissing();
    } else if (outcome === "kept" && state.refreshState === "updating") {
      state.refreshState = "live";
      showStatus();
    }
  } else if (["home", "list"].includes(parseHash().name)) {
    route();
  }
}

// ---- session --------------------------------------------------------------

function teardown() {
  forgetLook();
  if (state.stopRefresh) state.stopRefresh();
  state.stopRefresh = null;
  leaveView();
  state.shell = null;
  window.removeEventListener("hashchange", route);
}

async function refreshWays() {
  try {
    const session = await api.session();
    if (session && session.sign_in) state.ways = session.sign_in;
  } catch {
    // Keep what we knew.
  }
}

function showSignIn(notice) {
  teardown();
  if (state.stopSignIn) state.stopSignIn();
  state.stopSignIn = renderSignIn(root, {
    app: state.app,
    ways: state.ways,
    notice,
    preview: state.preview,
    onSignedIn: (how) => {
      // After the first sign-in with the password, offer a passkey once (it is the better way in).
      const w = state.ways || {};
      state.offerPasskey = how === "password" && !w.passkeys && w.passkeys_here && passkeysSupported();
      if (state.offerPasskey) window.history.replaceState(null, "", "#/sign-in");
      startApp();
    },
  });
}

async function signOutNow(notice) {
  // Signing out from a page means starting again at the dashboards next time; "sign in again" to change
  // sign-in settings (a notice is given) comes back to the settings page.
  if (typeof notice !== "string") window.history.replaceState(null, "", "#/");
  try {
    await api.logout();
  } catch {
    // Signed out on this page either way.
  }
  await refreshWays();
  showSignIn(typeof notice === "string" ? notice : "You are signed out. Sign in again to see your dashboards.");
}

async function startApp() {
  if (state.stopSignIn) state.stopSignIn();
  state.stopSignIn = null;
  let rev = null;
  try {
    [state.dashboards, rev] = await Promise.all([
      api.dashboards(),
      api.rev().then((r) => r.rev).catch(() => null),
      loadLook(),
    ]);
  } catch (err) {
    if (err && err.status === 401) return;
    fatal(err, startApp);
    return;
  }
  if (!Array.isArray(state.dashboards)) state.dashboards = [];
  buildShell();
  window.addEventListener("hashchange", route);
  // A static export never changes, so it has nothing to check for.
  if (!state.exported) state.stopRefresh = startRefresh({
    rev,
    // A preview shows edits as they are saved; a published dashboard checks once a minute.
    interval: state.preview ? 1500 : 60_000,
    onChange: onRevChange,
    onState: (s) => {
      state.refreshState = s;
      showStatus();
    },
  });
  route();
}

async function boot() {
  setAccent(null);
  let session;
  try {
    session = await api.session();
  } catch (err) {
    fatal(err, boot);
    return;
  }
  state.app = (session && session.app) || { name: "easy-ui", title: "Dashboards", accent: null };
  state.preview = Boolean(session && session.preview);
  state.ways = (session && session.sign_in) || null;
  state.exported = (session && session.export) || null;
  setAccent(state.app.accent);
  if (session && session.signed_in) startApp();
  else showSignIn();
}

window.addEventListener("easyui:signedout", async () => {
  if (!state.shell) return;
  await refreshWays();
  showSignIn("Your session ended. Sign in again to continue.");
});

boot();

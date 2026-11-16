// The only module that talks to the server. Every call returns parsed JSON
// or throws an ApiError whose message and hint are written for people.
// (A static export replaces this module with static-api.js, which has the same api.)

import { ApiError, describe } from "./errors.js";

export { ApiError, describe };

const BASE = new URL("api/", document.baseURI);
// A wrong password or code on these is an answer, not a sign that the session ended.
const SIGN_IN_PATHS = new Set(["login", "recovery", "passkeys/login"]);

// Plain fallbacks for when the server's answer carries no message of its own.
function fallback(status) {
  if (status === 401) {
    return ["You are signed out.", "Sign in again to see your dashboards."];
  }
  if (status === 403) return ["This is not allowed.", "If you think it should be, ask the person who set up this app."];
  if (status === 404) return ["This could not be found.", "It may have been renamed or removed. Go back to the list of dashboards."];
  if (status === 429) return ["Too many tries in a short time.", "Wait a moment, then try again."];
  if (status === 502 || status === 503 || status === 504) {
    return ["The dashboard server is not answering right now.", "It may be starting up or busy. Try again in a minute."];
  }
  return [
    "Something went wrong on the dashboard server.",
    "Try again in a minute. If it keeps happening, ask your agent to run easy-ui check.",
  ];
}

function retryAfterOf(res, err) {
  for (const v of [err && err.retry_after, err && err.retry_after_s, err && err.wait_s, res.headers.get("Retry-After")]) {
    const n = Number(v);
    if (v !== null && v !== undefined && v !== "" && Number.isFinite(n) && n >= 0) return Math.ceil(n);
  }
  return null;
}

async function call(method, path, body) {
  const headers = { Accept: "application/json", "X-Requested-With": "easy-ui" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let res;
  try {
    res = await fetch(new URL(path, BASE), {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: "same-origin",
      cache: "no-store",
    });
  } catch {
    throw new ApiError({
      code: "offline",
      message: "The dashboard server is not answering.",
      hint: "It may have stopped (started by hand, it runs until it is stopped; as a service it starts again on "
        + "the next visit), or this device may be offline. Start it again with easy-ui serve if needed, check the "
        + "network, then try again.",
    });
  }
  let data = null;
  try {
    data = await res.json();
  } catch {
    data = null;
  }
  if (res.ok) {
    if (data === null) {
      throw new ApiError({
        status: res.status,
        code: "bad_response",
        message: "The dashboard server sent an answer this page cannot read.",
        hint: "Reload the page. If it keeps happening, ask your agent to run easy-ui check.",
      });
    }
    return data;
  }
  const err = data && typeof data.error === "object" && data.error ? data.error : null;
  const [message, hint] = fallback(res.status);
  const error = new ApiError({
    status: res.status,
    code: (err && typeof err.code === "string" && err.code) || (res.status === 401 ? "sign_in_required" : "error"),
    message: (err && typeof err.message === "string" && err.message) || message,
    hint: err && typeof err.hint === "string" ? err.hint : err ? null : hint,
    retryAfter: retryAfterOf(res, err),
    problems: err && Array.isArray(err.problems) ? err.problems : null,
  });
  if (res.status === 401 && !SIGN_IN_PATHS.has(path)) {
    window.dispatchEvent(new CustomEvent("easyui:signedout"));
  }
  throw error;
}

export const api = {
  session: () => call("GET", "session"),
  login: (password) => call("POST", "login", { password }),
  logout: () => call("POST", "logout", {}),
  dashboards: () => call("GET", "dashboards"),
  dashboard: (slug) => call("GET", `dashboards/${encodeURIComponent(slug)}`),
  query: (dashboard, queries, params) => call("POST", "query", { dashboard, queries, params }),
  rev: () => call("GET", "rev"),
  passkeyOptions: (purpose) => call("POST", "passkeys/options", { purpose }),
  passkeyLogin: (credential) => call("POST", "passkeys/login", { credential }),
  passkeyRegister: (credential, name) => call("POST", "passkeys/register", { credential, name }),
  passkeyRemove: (id, confirm) => call("POST", "passkeys/remove", confirm ? { id, confirm } : { id }),
  signInSettings: () => call("GET", "signin"),
  passwordSignIn: (on, confirm) => call("POST", "signin/password", confirm ? { on, confirm } : { on }),
  recoveryCodes: (confirm) => call("POST", "recovery-codes", confirm ? { confirm } : {}),
  recoveryLogin: (code) => call("POST", "recovery", { code }),
};

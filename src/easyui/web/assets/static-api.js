// The data source of a static export (easy-ui export): the same api as api.js,
// answered from the data written into the page itself, with no server and no
// network request at all. In a link-mode export the data is encrypted
// (AES-256-GCM); the key is only in the part of the link after "#k=", which
// browsers never send anywhere, and is decrypted here with WebCrypto.
// The key is kept in this script only (never in the browser's storage) and is
// taken out of the address bar at once; a reload needs the whole link again.

import { ApiError, describe } from "./errors.js";

export { ApiError, describe };

const KEY_RE = /^#k=([A-Za-z0-9_-]{43})(?:&|$)/;
const AAD = new TextEncoder().encode("easy-ui export v1");
let loading = null;
let currentKey = null; // the key of this page, for as long as it is open
let isLocked = false;

function locked(message, hint) {
  return new ApiError({ status: 403, code: "export_locked", message, hint });
}

function fromB64url(text) {
  const b64 = text.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((text.length + 3) % 4);
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i += 1) out[i] = bin.charCodeAt(i);
  return out;
}

/** The key from the address (then kept in this script only, and taken out of the address bar). */
function takeKey() {
  const m = KEY_RE.exec(window.location.hash || "");
  if (/^#k=/.test(window.location.hash || "")) window.history.replaceState(null, "", "#/");
  if (m) currentKey = m[1];
  return currentKey;
}

async function decrypt(box, keyText) {
  if (!window.crypto || !window.crypto.subtle) {
    throw locked("This browser cannot open locked pages here.", "Open the link in an up-to-date browser, over https.");
  }
  let key;
  try {
    key = await window.crypto.subtle.importKey("raw", fromB64url(keyText), { name: "AES-GCM" }, false, ["decrypt"]);
  } catch {
    throw locked("The key in this link is not valid.", "Open the page with the whole link you were given, including the part after #.");
  }
  try {
    const plain = await window.crypto.subtle.decrypt(
      { name: "AES-GCM", iv: fromB64url(box.iv), additionalData: AAD, tagLength: 128 },
      key,
      fromB64url(box.ct),
    );
    return JSON.parse(new TextDecoder().decode(plain));
  } catch {
    currentKey = null;
    throw locked(
      "This link's key does not open this page.",
      "The link may be cut short, or the page was shared again with a new key. Ask for the current link.",
    );
  }
}

function load() {
  if (!loading) {
    loading = (async () => {
      const el = document.getElementById("easyui-export");
      if (!el) throw locked("This page has no data.", "Make the copy again with easy-ui export.");
      const raw = JSON.parse(el.textContent);
      isLocked = Boolean(raw && raw.enc === "A256GCM");
      if (isLocked) {
        const keyText = takeKey();
        if (!keyText) {
          throw locked(
            "This page is locked. It opens only with the whole link you were given.",
            "Ask the person who shared it for the link, and open it including the part after #.",
          );
        }
        return decrypt(raw, keyText);
      }
      return raw;
    })();
    loading.catch(() => {
      loading = null;
    });
  }
  return loading;
}

function notHere() {
  return new ApiError({
    status: 404,
    code: "not_in_export",
    message: "This is a shared copy of the dashboards, so this part is not in it.",
    hint: null,
  });
}

function sameValue(a, b) {
  return a === b || String(a) === String(b);
}

export const api = {
  async session() {
    const p = await load();
    return { signed_in: true, app: p.app, export: { made: p.made, encrypted: Boolean(p.encrypted), picture: Boolean(p.picture) } };
  },
  async dashboards() {
    return (await load()).dashboards;
  },
  async dashboard(slug) {
    const p = await load();
    const spec = Object.prototype.hasOwnProperty.call(p.specs, slug) ? p.specs[slug] : null;
    if (!spec) throw new ApiError({ status: 404, code: "not_found", message: "This dashboard is not in this copy.", hint: "Pick one from the list." });
    return spec;
  },
  async query(slug, names, params) {
    const p = await load();
    const d = Object.prototype.hasOwnProperty.call(p.data, slug) ? p.data[slug] : null;
    if (!d) throw notHere();
    for (const [name, value] of Object.entries(params || {})) {
      if (Object.prototype.hasOwnProperty.call(d.values, name) && !sameValue(d.values[name], value)) {
        throw new ApiError({
          status: 400,
          code: "bad_param",
          message: "This shared copy only has the numbers for the settings it was made with.",
          hint: null,
        });
      }
    }
    const results = {};
    for (const n of names) {
      results[n] = Object.prototype.hasOwnProperty.call(d.results, n)
        ? d.results[n]
        : { error: { code: "not_in_export", message: "This part is not in the shared copy.", hint: null } };
    }
    return { results };
  },
  async rev() {
    return { rev: "export" };
  },
  /** True when the address now holds a new, well-formed key for this locked page (then the page starts
   * over to read it). Anything else after "#k=" (a plain copy, a broken or the same key) is only taken
   * out of the address bar, so the page never reloads again and again. */
  keyInAddress() {
    const hash = window.location.hash || "";
    if (!/^#k=/.test(hash)) return false;
    const m = KEY_RE.exec(hash);
    const fresh = Boolean(isLocked && m && m[1] !== currentKey);
    if (!fresh) window.history.replaceState(null, "", "#/");
    return fresh;
  },
  async logout() {
    return { signed_in: false };
  },
  login: async () => { throw notHere(); },
  passkeyOptions: async () => { throw notHere(); },
  passkeyLogin: async () => { throw notHere(); },
  passkeyRegister: async () => { throw notHere(); },
  passkeyRemove: async () => { throw notHere(); },
  signInSettings: async () => { throw notHere(); },
  passwordSignIn: async () => { throw notHere(); },
  recoveryCodes: async () => { throw notHere(); },
  recoveryLogin: async () => { throw notHere(); },
};

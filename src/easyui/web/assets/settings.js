// The sign-in settings page (#/sign-in): the passkeys that can sign in (add,
// remove), whether the password can still be used, and the recovery codes.
// Changes are allowed only shortly after signing in; the server enforces that
// and this page says so. When this page signed in with the password or a
// recovery code and a passkey exists, adding a passkey, removing one,
// switching the password and making new recovery codes are confirmed with a
// saved passkey first (the server asks for it: confirm_needed). A passkey
// added in that same sign-in cannot confirm it (confirm_possible is false
// when no other passkey exists): the page then says to sign in with it.

import { h, fill, icon, nextId } from "./dom.js";
import { api, describe, ApiError } from "./api.js";
import { passkeysSupported, createPasskey, getPasskey, passkeyProblem } from "./passkey.js";
import { MONTHS } from "./format.js";

function day(seconds) {
  if (typeof seconds !== "number") return null;
  const d = new Date(seconds * 1000);
  return `${d.getDate()} ${MONTHS[d.getMonth()]} ${d.getFullYear()}`;
}

function note(kind, text, hint) {
  return h("div", { class: `note note-${kind}`, attrs: { role: kind === "error" ? "alert" : "status" } }, [
    h("p", { class: "note-title", text }),
    hint ? h("p", { class: "note-hint", text: hint }) : null,
  ]);
}

export class SignInSettings {
  /** {app, offer: bool (just signed in with the password), onSignInAgain()} */
  constructor({ app, offer = false, onSignInAgain }) {
    this.app = app;
    this.offer = offer;
    this.onSignInAgain = onSignInAgain;
    this.data = null;
    this.codes = null;
    this.flash = null;
    this.confirming = null;
    this.busy = false;
    this.el = h("article", { class: "settings" });
    this.load();
  }

  async load() {
    fill(this.el, h("p", { class: "page-loading", attrs: { role: "status" }, text: "Opening the sign-in settings…" }));
    try {
      this.data = await api.signInSettings();
    } catch (err) {
      if (err && err.status === 401) return;
      const info = describe(err);
      fill(this.el, note("error", info.message, info.hint));
      return;
    }
    this.render();
  }

  failed(err) {
    if (err && err.status === 401) return;
    const p = passkeyProblem(err);
    const info = p || describe(err);
    this.flash = { kind: "error", text: info.message, hint: info.hint, code: info.code || (err && err.code) };
    this.render();
  }

  async act(fn, ok) {
    if (this.busy) return;
    this.busy = true;
    this.render();
    try {
      const out = await fn();
      if (out) this.data = { ...this.data, ...out };
      this.flash = ok ? { kind: "ok", text: typeof ok === "function" ? ok(out) : ok } : null;
    } catch (err) {
      this.busy = false;
      this.failed(err);
      return;
    }
    this.busy = false;
    this.render();
  }

  // ---- actions ----------------------------------------------------------

  /** A fresh passkey answer when the server needs one for this change, else undefined. */
  async confirmation() {
    if (!this.data || !this.data.confirm_needed) return undefined;
    if (this.data.confirm_possible === false) {
      throw new ApiError({
        status: 403,
        code: "sign_in_with_new_passkey",
        message: "The passkey you just added cannot confirm changes for this sign-in, because this page signed in with the password or a recovery code.",
        hint: "Sign out, sign in with your new passkey, and make the change then.",
      });
    }
    const options = await api.passkeyOptions("confirm");
    return getPasskey(options);
  }

  addPasskey(name) {
    return this.act(async () => {
      // Once a passkey exists, a page signed in with the password or a recovery code confirms with it first.
      const confirm = await this.confirmation();
      const options = await api.passkeyOptions("register");
      const credential = await createPasskey(options);
      const out = await api.passkeyRegister(credential, name, confirm);
      this.offer = false;
      return out;
    }, (out) => `Added the passkey “${out.added.name}”. Next time, sign in with it.`);
  }

  removePasskey(p) {
    this.confirming = null;
    return this.act(async () => api.passkeyRemove(p.id, await this.confirmation()), (out) => (out.removed.password_sign_in_turned_on
      ? `Removed “${p.name}”. It was the last passkey, so signing in with the password is on again.`
      : `Removed “${p.name}”. It can no longer sign in.`));
  }

  switchPassword(on) {
    return this.act(async () => api.passwordSignIn(on, await this.confirmation()), on
      ? "Signing in with the password is on again."
      : "Signing in with the password is off. Passkeys and recovery codes still sign in.");
  }

  makeCodes() {
    return this.act(async () => {
      const out = await api.recoveryCodes(await this.confirmation());
      this.codes = out.codes;
      return out;
    }, null);
  }

  // ---- rendering --------------------------------------------------------

  render() {
    const d = this.data;
    if (!d) return;
    const locked = d.can_change_for <= 0;
    const parts = [
      h("header", { class: "dash-head" }, h("div", { class: "dash-heading" }, [
        h("h1", { class: "dash-title", text: "Sign-in" }),
        h("p", { class: "dash-desc", text: "How you sign in to these dashboards. Passkeys are the safest way: they use this device's fingerprint, face or PIN, and nobody can guess them." }),
      ])),
    ];
    if (this.flash) {
      const f = this.flash;
      const again = f.code === "sign_in_again"
        ? h("button", { class: "btn-secondary", type: "button", text: "Sign in again", on: { click: () => this.onSignInAgain() } })
        : null;
      parts.push(h("div", { class: `note note-${f.kind}`, attrs: { role: f.kind === "error" ? "alert" : "status" } }, [
        h("p", { class: "note-title", text: f.text }),
        f.hint ? h("p", { class: "note-hint", text: f.hint }) : null,
        again,
      ]));
    } else if (locked) {
      parts.push(h("div", { class: "note note-info", attrs: { role: "status" } }, [
        h("p", { class: "note-title", text: "To change how you sign in, sign in again first." }),
        h("p", { class: "note-hint", text: "For your safety, these settings can only be changed in the 10 minutes after signing in." }),
        h("button", { class: "btn-secondary", type: "button", text: "Sign in again", on: { click: () => this.onSignInAgain() } }),
      ]));
    }
    if (d.confirm_needed && !locked && !this.flash) {
      parts.push(h("p", { class: "settings-p is-quiet", text: d.confirm_possible === false
        ? "This page signed in with the password or a recovery code, and the passkey you added since cannot confirm changes for this sign-in. To make more changes, sign out and sign in with that passkey."
        : "This page signed in with the password or a recovery code, so changes here, adding a passkey too, are confirmed with one of your saved passkeys first. If you have lost all of them, they are removed on the server with easy-ui passkeys remove." }));
    }
    if (this.codes) parts.push(this.codesPanel());
    if (this.offer && !d.passkeys.length && d.passkeys_here && passkeysSupported()) parts.push(this.offerPanel());
    parts.push(this.passkeysPanel(locked), this.passwordPanel(locked), this.recoveryPanel(locked));
    fill(this.el, h("div", { class: "settings-stack" }, parts));
  }

  offerPanel() {
    return h("section", { class: "panel settings-offer" }, [
      h("h2", { class: "settings-h", text: "Sign in faster next time" }),
      h("p", { class: "settings-p", text: "Add a passkey on this device. Then signing in is one touch, and the password is no longer needed." }),
      h("div", { class: "settings-actions" }, [
        h("button", { class: "btn-primary", type: "button", disabled: this.busy, on: { click: () => this.addPasskey("") } }, [icon("key"), h("span", { text: "Add a passkey" })]),
        h("button", { class: "btn-quiet", type: "button", text: "Not now", on: { click: () => { this.offer = false; this.render(); } } }),
      ]),
    ]);
  }

  passkeysPanel(locked) {
    const d = this.data;
    const usable = d.passkeys_here && passkeysSupported();
    const items = d.passkeys.map((p) => {
      const meta = [`Added ${day(p.created)}`, p.last_used ? `last used ${day(p.last_used)}` : "not used yet"];
      if (p.synced) meta.push("synced across your devices");
      if (!p.here) meta.push("made for another address of these dashboards");
      const confirming = this.confirming === p.id;
      const actions = confirming
        ? [
          h("span", { class: "confirm-text", text: "Remove it?" }),
          h("button", { class: "btn-danger", type: "button", text: "Remove", disabled: this.busy, on: { click: () => this.removePasskey(p) } }),
          h("button", { class: "btn-quiet", type: "button", text: "Keep", on: { click: () => { this.confirming = null; this.render(); } } }),
        ]
        : [h("button", {
          class: "btn-quiet",
          type: "button",
          text: "Remove",
          disabled: locked || this.busy,
          attrs: { "aria-label": `Remove the passkey ${p.name}` },
          on: { click: () => { this.confirming = p.id; this.render(); } },
        })];
      return h("li", { class: "pk-item" }, [
        h("span", { class: "pk-icon" }, icon("key")),
        h("div", { class: "pk-text" }, [
          h("span", { class: "pk-name", text: p.name }),
          h("span", { class: "pk-meta", text: meta.join(" · ") }),
        ]),
        h("div", { class: "pk-actions" }, actions),
      ]);
    });
    const nameId = nextId("pkname");
    const name = h("input", { class: "input input-text", type: "text", id: nameId, maxLength: 60, placeholder: "For example: Phone", autocomplete: "off" });
    const form = h("form", { class: "pk-add", attrs: { novalidate: true } }, [
      h("div", { class: "pk-add-field" }, [
        h("label", { class: "field-label", htmlFor: nameId, text: "Name (optional)" }),
        name,
      ]),
      h("button", { class: "btn-primary", type: "submit", disabled: locked || this.busy || !usable || d.passkeys.length >= d.max_passkeys }, [icon("key"), h("span", { text: "Add a passkey" })]),
    ]);
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      this.addPasskey(name.value.trim());
    });
    let why = null;
    if (!d.passkeys_here) why = "Passkeys can only be added when the dashboards are opened with their https address (or with http://localhost on the computer that runs them).";
    else if (!passkeysSupported()) why = "This browser cannot make passkeys. Try another browser on this device.";
    return h("section", { class: "panel settings-section" }, [
      h("h2", { class: "settings-h", text: "Passkeys" }),
      d.passkeys.length
        ? h("ul", { class: "pk-list" }, items)
        : h("p", { class: "settings-p", text: "No passkeys yet. Add one on each device you use, for example your phone and your computer." }),
      why ? h("p", { class: "settings-p is-quiet", text: why }) : form,
    ]);
  }

  passwordPanel(locked) {
    const d = this.data;
    let text;
    let button = null;
    if (!d.password_set) {
      text = "No password is set. The owner sets one by typing easy-ui set-password in a terminal on the computer that runs the dashboards.";
    } else if (d.password_sign_in) {
      text = d.passkeys.length
        ? "The password can still be used to sign in. Once your passkeys work on every device you use, you can turn it off, so nobody can sign in by guessing it."
        : "The password is how you sign in now. Add a passkey first; then you can turn the password off.";
      button = h("button", { class: "btn-secondary", type: "button", text: "Turn off password sign-in", disabled: locked || this.busy || !d.passkeys.length, on: { click: () => this.switchPassword(false) } });
    } else {
      text = "The password is turned off: only passkeys and recovery codes sign in.";
      button = h("button", { class: "btn-secondary", type: "button", text: "Turn password sign-in back on", disabled: locked || this.busy, on: { click: () => this.switchPassword(true) } });
    }
    return h("section", { class: "panel settings-section" }, [
      h("h2", { class: "settings-h", text: "Password" }),
      h("p", { class: "settings-p", text }),
      button ? h("div", { class: "settings-actions" }, button) : null,
    ]);
  }

  recoveryPanel(locked) {
    const r = this.data.recovery;
    const text = r.total
      ? `${r.left} of ${r.total} recovery codes left${r.made ? `, made ${day(r.made)}` : ""}. Each one signs in once, when no passkey is at hand.`
      : "No recovery codes yet. They sign you in when no passkey is at hand, for example after losing your phone. Make them once and keep them somewhere safe.";
    return h("section", { class: "panel settings-section" }, [
      h("h2", { class: "settings-h", text: "Recovery codes" }),
      h("p", { class: "settings-p", text }),
      h("div", { class: "settings-actions" }, h("button", {
        class: "btn-secondary",
        type: "button",
        text: r.total ? "Make new recovery codes" : "Make recovery codes",
        disabled: locked || this.busy,
        on: { click: () => this.makeCodes() },
      })),
      r.total ? h("p", { class: "settings-p is-quiet", text: "Making new codes replaces the old ones." }) : null,
    ]);
  }

  codesPanel() {
    const list = h("ol", { class: "codes" }, this.codes.map((c) => h("li", null, h("code", { class: "code", text: c }))));
    const copy = h("button", { class: "btn-secondary", type: "button", text: "Copy them" });
    copy.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(this.codes.join("\n"));
        copy.textContent = "Copied";
      } catch {
        copy.textContent = "Copying did not work: write them down";
      }
    });
    const done = h("button", { class: "btn-primary", type: "button", text: "I have saved them", on: { click: () => { this.codes = null; this.render(); } } });
    return h("section", { class: "panel settings-codes", attrs: { "aria-labelledby": "codes-h" } }, [
      h("h2", { class: "settings-h", id: "codes-h", text: "Your new recovery codes" }),
      h("p", { class: "settings-p", text: "Save them now, in your password manager or on paper: they are shown only this once. Each code signs in once. The old codes no longer work." }),
      list,
      h("div", { class: "settings-actions" }, [done, copy]),
    ]);
  }

  destroy() {
    this.codes = null;
  }
}

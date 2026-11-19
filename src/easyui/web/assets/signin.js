// The sign-in page. A passkey is the main way in; the password is the
// fallback (and the way in the first time); a recovery code is the way back
// when no passkey is at hand. Plain errors, and a visible wait when the server
// has paused password or code sign-in after too many tries (passkeys are never
// paused).

import { h, fill, icon, nextId } from "./dom.js";
import { api, describe } from "./api.js";
import { passkeysSupported, getPasskey, passkeyProblem } from "./passkey.js";

function waitText(seconds) {
  if (seconds >= 90) return `${Math.ceil(seconds / 60)} minutes`;
  if (seconds >= 60) return "about a minute";
  return seconds === 1 ? "1 second" : `${seconds} seconds`;
}

function previewView(root, title) {
  // A preview has no password: it is opened with the one-time link the preview command prints.
  fill(root, h("main", { class: "signin", id: "main" }, [
    h("div", { class: "signin-box" }, [
      h("h1", { class: "signin-title", text: title }),
      h("p", { class: "signin-lead", text: "This preview opens with the link that easy-ui preview printed in its terminal." }),
      h("p", { class: "signin-foot" }, [
        "Each link works once. After a link has been used, the terminal shows a new one: open that here. Or stop the "
          + "preview with Ctrl-C and start it again with ",
        h("code", { class: "cmd", text: "easy-ui preview" }),
        ".",
      ]),
    ]),
  ]));
  document.title = `Preview – ${title}`;
  return () => {};
}

/**
 * renderSignIn(root, {app, ways, notice, preview, onSignedIn(how)}) -> stop()
 * ways: {password, password_set, passkeys, passkeys_here, recovery} from GET /api/session.
 */
export function renderSignIn(root, { app, ways, notice, preview = false, onSignedIn }) {
  const title = (app && app.title) || "Dashboards";
  if (preview) return previewView(root, title);

  const w = ways || { password: true, passkeys: false, passkeys_here: false, recovery: false };
  const canPasskey = Boolean(w.passkeys && w.passkeys_here && passkeysSupported());
  const showPassword = Boolean(w.password);
  const errorId = nextId("err");
  const message = h("div", { class: "signin-message", id: errorId, attrs: { "aria-live": "polite" } });
  let countdown = null;
  let busy = false;
  const buttons = [];

  function say(kind, text, hint, extra) {
    fill(message,
      text ? h("p", { class: `msg msg-${kind}`, text }) : null,
      hint ? h("p", { class: "msg-hint", text: hint }) : null,
      extra || null);
  }

  function setBusy(on) {
    busy = on;
    for (const b of buttons) b.disabled = on || b.dataset.paused === "1";
  }

  function pause(button, err, seconds, label) {
    clearInterval(countdown);
    let left = seconds;
    const clock = h("span", { class: "msg-wait", attrs: { "aria-hidden": "true" } });
    const tick = () => {
      clock.textContent = `You can try again in ${waitText(left)}.`;
    };
    button.dataset.paused = "1";
    button.disabled = true;
    say("error", err.message || "Too many tries in a short time.",
      canPasskey ? `${label} is paused for ${waitText(seconds)}. Signing in with a passkey still works.`
        : `For your safety, ${label.toLowerCase()} is paused for ${waitText(seconds)}.`, clock);
    tick();
    countdown = setInterval(() => {
      left -= 1;
      if (left <= 0) {
        clearInterval(countdown);
        countdown = null;
        button.dataset.paused = "0";
        button.disabled = busy;
        say("info", "You can try again now.");
      } else {
        tick();
      }
    }, 1000);
  }

  function failed(err, button, label, field) {
    const info = describe(err);
    if (err && err.status === 429) {
      if (err.retryAfter) pause(button, info, err.retryAfter, label);
      else say("error", info.message, info.hint || "Wait a minute, then try again.");
    } else {
      say("error", info.message, info.hint);
    }
    if (field) {
      field.setAttribute("aria-invalid", "true");
      field.select();
    }
  }

  // ---- passkey ----------------------------------------------------------

  let passkeyButton = null;
  if (canPasskey) {
    passkeyButton = h("button", { class: "btn-primary btn-passkey", type: "button" }, [icon("key"), h("span", { text: "Sign in with a passkey" })]);
    buttons.push(passkeyButton);
    passkeyButton.addEventListener("click", async () => {
      if (busy) return;
      setBusy(true);
      say("info", "Waiting for your device…", "Confirm with your fingerprint, face or device PIN.");
      try {
        const options = await api.passkeyOptions("login");
        const credential = await getPasskey(options);
        say("info", "Checking…");
        await api.passkeyLogin(credential);
        onSignedIn("passkey");
        return;
      } catch (err) {
        const p = passkeyProblem(err);
        if (p) say("error", p.message, p.hint);
        else failed(err, passkeyButton, "Passkey sign-in");
      } finally {
        setBusy(false);
      }
    });
  }

  // ---- password ---------------------------------------------------------

  let passwordForm = null;
  let passwordInput = null;
  if (showPassword) {
    const inputId = nextId("pw");
    passwordInput = h("input", {
      class: "input input-password",
      type: "password",
      id: inputId,
      name: "password",
      autocomplete: "current-password",
      required: true,
      spellcheck: false,
      attrs: { "aria-describedby": errorId },
    });
    const reveal = h("button", {
      class: "reveal",
      type: "button",
      attrs: { "aria-label": "Show password", "aria-pressed": "false", "aria-controls": inputId },
    }, icon("eye"));
    reveal.addEventListener("click", () => {
      const show = passwordInput.type === "password";
      passwordInput.type = show ? "text" : "password";
      reveal.setAttribute("aria-pressed", String(show));
      reveal.setAttribute("aria-label", show ? "Hide password" : "Show password");
      fill(reveal, icon(show ? "eyeOff" : "eye"));
      passwordInput.focus();
    });
    const label = canPasskey ? "Sign in with the password" : "Sign in";
    const submit = h("button", { class: canPasskey ? "btn-secondary" : "btn-primary", type: "submit", text: label });
    buttons.push(submit);
    // Lets password managers file the password under this project.
    const username = h("input", {
      class: "sr-only",
      type: "text",
      name: "username",
      autocomplete: "username",
      value: (app && app.name) || "easy-ui",
      tabIndex: -1,
      attrs: { "aria-hidden": "true" },
    });
    passwordForm = h("form", { class: "signin-form", attrs: { novalidate: true } }, [
      username,
      h("label", { class: "field-label", htmlFor: inputId, text: "Password" }),
      h("div", { class: "password-wrap" }, [passwordInput, reveal]),
      submit,
    ]);
    passwordForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (busy || submit.dataset.paused === "1") return;
      if (!passwordInput.value) {
        say("error", "Type your password first.", null);
        passwordInput.focus();
        return;
      }
      setBusy(true);
      submit.textContent = "Signing in…";
      try {
        await api.login(passwordInput.value);
        passwordInput.value = "";
        onSignedIn("password");
        return;
      } catch (err) {
        if (err && err.status === 401) {
          const info = describe(err);
          say("error", info.message === "You are signed out." ? "That password did not work." : info.message,
            info.hint || "Check it and try again. Passwords are case-sensitive.");
          passwordInput.setAttribute("aria-invalid", "true");
          passwordInput.select();
        } else {
          failed(err, submit, "Password sign-in");
        }
      } finally {
        submit.textContent = label;
        setBusy(false);
      }
    });
  }

  // ---- recovery code ----------------------------------------------------

  let recovery = null;
  if (w.recovery) {
    const codeId = nextId("code");
    const codeInput = h("input", {
      class: "input input-code",
      type: "text",
      id: codeId,
      name: "recovery-code",
      autocomplete: "one-time-code",
      spellcheck: false,
      placeholder: "ABCD-EFGH-JKMN-PQRS",
      attrs: { "aria-describedby": errorId, autocapitalize: "characters" },
    });
    const submit = h("button", { class: "btn-secondary", type: "submit", text: "Sign in with the code" });
    buttons.push(submit);
    const form = h("form", { class: "signin-form signin-recovery", attrs: { novalidate: true }, hidden: true }, [
      h("label", { class: "field-label", htmlFor: codeId, text: "Recovery code" }),
      codeInput,
      h("p", { class: "field-help", text: "Each code works once. Dashes, spaces and capitals do not matter." }),
      submit,
    ]);
    const open = h("button", { class: "btn-quiet signin-alt", type: "button", text: "Use a recovery code", attrs: { "aria-expanded": "false" } });
    open.addEventListener("click", () => {
      const show = form.hidden;
      form.hidden = !show;
      open.setAttribute("aria-expanded", String(show));
      if (show) codeInput.focus();
    });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (busy || submit.dataset.paused === "1") return;
      if (!codeInput.value.trim()) {
        say("error", "Type one of your recovery codes first.", null);
        codeInput.focus();
        return;
      }
      setBusy(true);
      try {
        await api.recoveryLogin(codeInput.value);
        codeInput.value = "";
        onSignedIn("recovery");
        return;
      } catch (err) {
        failed(err, submit, "Recovery-code sign-in", err && err.status === 401 ? codeInput : null);
      } finally {
        setBusy(false);
      }
    });
    recovery = [open, form];
  }

  // ---- layout -----------------------------------------------------------

  // Nothing set up yet: no password and no passkey, so there is no way in until the owner sets a password.
  const notSetUp = w.password_set === false && !w.passkeys && !w.recovery;
  const lead = notice || (notSetUp ? "Nobody can sign in yet: no sign-in password has been set for these dashboards."
    : canPasskey ? "Sign in to see your dashboards." : "Enter your password to see your dashboards.");
  const notes = [];
  if (w.passkeys && !canPasskey) {
    notes.push(w.passkeys_here
      ? "This browser cannot use passkeys. Use another browser, or another way to sign in here."
      : "Passkeys work when the dashboards are opened with their https address (or with http://localhost on the computer that runs them).");
  }
  const setUp = notSetUp ? h("p", { class: "signin-foot" }, [
    "The owner sets one by typing ",
    h("code", { class: "cmd", text: "easy-ui set-password" }),
    " in a terminal on the computer that runs the dashboards (on a server, as the account that runs them, with the "
      + "same --ui-dir and --auth-dir), and types the password there. Then reload this page.",
  ]) : null;
  if (!notSetUp && !showPassword && !canPasskey && !w.recovery) {
    notes.push("Signing in with the password is turned off. The owner can turn it back on by typing easy-ui sign-in password on in a terminal on the computer that runs the dashboards.");
  }

  const view = h("main", { class: "signin", id: "main" }, [
    h("div", { class: "signin-box" }, [
      h("h1", { class: "signin-title", text: title }),
      h("p", { class: "signin-lead", text: lead }),
      passkeyButton ? h("div", { class: "signin-passkey" }, passkeyButton) : null,
      passkeyButton && passwordForm ? h("p", { class: "signin-or" }, h("span", { text: "or" })) : null,
      passwordForm,
      message,
      recovery ? h("div", { class: "signin-recovery-wrap" }, recovery) : null,
      ...notes.map((n) => h("p", { class: "signin-note", text: n })),
      setUp,
      showPassword ? h("p", { class: "signin-foot" }, [
        "Forgotten the password? Set a new one by typing ",
        h("code", { class: "cmd", text: "easy-ui set-password" }),
        " in a terminal on the computer that runs the dashboards.",
      ]) : null,
    ]),
  ]);
  fill(root, view);
  document.title = `Sign in – ${title}`;
  if (passkeyButton) passkeyButton.focus();
  else if (passwordInput) passwordInput.focus();
  return () => clearInterval(countdown);
}

// The sign-in page: one password field, plain errors, and a visible wait
// when the server has paused sign-in after too many tries.

import { h, fill, icon, nextId } from "./dom.js";
import { api, describe } from "./api.js";

function waitText(seconds) {
  if (seconds >= 90) return `${Math.ceil(seconds / 60)} minutes`;
  if (seconds >= 60) return "about a minute";
  return seconds === 1 ? "1 second" : `${seconds} seconds`;
}

export function renderSignIn(root, { app, notice, preview = false, onSignedIn }) {
  const inputId = nextId("pw");
  const errorId = nextId("err");
  const title = (app && app.title) || "Dashboards";

  const input = h("input", {
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
    const show = input.type === "password";
    input.type = show ? "text" : "password";
    reveal.setAttribute("aria-pressed", String(show));
    reveal.setAttribute("aria-label", show ? "Hide password" : "Show password");
    fill(reveal, icon(show ? "eyeOff" : "eye"));
    input.focus();
  });

  const message = h("div", { class: "signin-message", id: errorId, attrs: { "aria-live": "polite" } });
  const button = h("button", { class: "btn-primary", type: "submit", text: "Sign in" });
  // Lets password managers file the password under this app.
  const username = h("input", {
    class: "sr-only",
    type: "text",
    name: "username",
    autocomplete: "username",
    value: (app && app.name) || "easy-ui",
    tabIndex: -1,
    attrs: { "aria-hidden": "true" },
  });

  let countdown = null;
  let busy = false;

  function say(kind, text, hint, extra) {
    fill(message,
      text ? h("p", { class: `msg msg-${kind}`, text }) : null,
      hint ? h("p", { class: "msg-hint", text: hint }) : null,
      extra || null);
    input.setAttribute("aria-invalid", kind === "error" ? "true" : "false");
  }

  function throttle(err, seconds) {
    clearInterval(countdown);
    let left = seconds;
    const clock = h("span", { class: "msg-wait", attrs: { "aria-hidden": "true" } });
    const tick = () => {
      clock.textContent = `You can try again in ${waitText(left)}.`;
    };
    button.disabled = true;
    say("error", err.message || "Too many tries in a short time.",
      `For your safety, signing in is paused for ${waitText(seconds)}.`, clock);
    tick();
    countdown = setInterval(() => {
      left -= 1;
      if (left <= 0) {
        clearInterval(countdown);
        countdown = null;
        button.disabled = false;
        say("info", "You can try again now.");
        input.focus();
      } else {
        tick();
      }
    }, 1000);
  }

  const form = h("form", { class: "signin-form", attrs: { novalidate: true } }, [
    username,
    h("label", { class: "field-label", htmlFor: inputId, text: "Password" }),
    h("div", { class: "password-wrap" }, [input, reveal]),
    message,
    button,
  ]);

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy || countdown) return;
    if (!input.value) {
      say("error", "Type your password first.", null);
      input.focus();
      return;
    }
    busy = true;
    button.disabled = true;
    button.textContent = "Signing in…";
    try {
      await api.login(input.value);
      input.value = "";
      onSignedIn();
      return;
    } catch (err) {
      const info = describe(err);
      if (err && err.status === 429) {
        if (err.retryAfter) throttle(info, err.retryAfter);
        else say("error", info.message, info.hint || "Wait a minute, then try again.");
      } else if (err && err.status === 401) {
        say("error", info.message === "You are signed out." ? "That password did not work." : info.message,
          info.hint || "Check it and try again. Passwords are case-sensitive.");
        input.select();
      } else {
        say("error", info.message, info.hint);
      }
    } finally {
      busy = false;
      button.textContent = "Sign in";
      if (!countdown) button.disabled = false;
    }
  });

  if (preview) {
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

  const view = h("main", { class: "signin", id: "main" }, [
    h("div", { class: "signin-box" }, [
      h("h1", { class: "signin-title", text: title }),
      h("p", { class: "signin-lead", text: notice || "Enter your password to see your dashboards." }),
      form,
      h("p", { class: "signin-foot" }, [
        "Forgotten it? Set a new one by typing ",
        h("code", { class: "cmd", text: "easy-ui set-password" }),
        " in a terminal on the computer that runs the dashboards.",
      ]),
    ]),
  ]);
  fill(root, view);
  document.title = `Sign in – ${title}`;
  input.focus();
  return () => clearInterval(countdown);
}

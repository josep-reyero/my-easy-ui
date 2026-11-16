// Passkeys in the browser: turn the server's options (base64url text) into what
// navigator.credentials wants, and the browser's answer back into JSON for the
// server. The server checks everything; this only translates.

export function passkeysSupported() {
  return Boolean(window.PublicKeyCredential && navigator.credentials && navigator.credentials.create && window.isSecureContext);
}

function fromB64url(text) {
  const b64 = text.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((text.length + 3) % 4);
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i += 1) out[i] = bin.charCodeAt(i);
  return out.buffer;
}

function toB64url(buf) {
  const bytes = new Uint8Array(buf);
  let bin = "";
  for (let i = 0; i < bytes.length; i += 1) bin += String.fromCharCode(bytes[i]);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** A plain message for what the browser said when a passkey request did not finish. */
export function passkeyProblem(err) {
  const name = err && err.name;
  if (name === "NotAllowedError" || name === "AbortError") {
    return {
      message: "The passkey request was cancelled or took too long.",
      hint: "Try again, and confirm with your fingerprint, face or device PIN when your device asks.",
      code: "passkey_cancelled",
    };
  }
  if (name === "InvalidStateError") {
    return {
      message: "This device already has a passkey for these dashboards.",
      hint: "You can sign in with it. To replace it, remove the old one from the list first.",
      code: "passkey_exists",
    };
  }
  if (name === "SecurityError") {
    return {
      message: "This browser does not allow passkeys on this address.",
      hint: "Open the dashboards with their https address, or with http://localhost on this computer.",
      code: "passkeys_unavailable",
    };
  }
  if (name === "NotSupportedError") {
    return {
      message: "This device has no passkey that fits.",
      hint: "Use a device or browser with passkeys (most phones and computers have them), or a security key.",
      code: "passkey_unsupported",
    };
  }
  return null;
}

export async function createPasskey(options) {
  const pk = options.publicKey;
  const publicKey = {
    ...pk,
    challenge: fromB64url(pk.challenge),
    user: { ...pk.user, id: fromB64url(pk.user.id) },
    excludeCredentials: (pk.excludeCredentials || []).map((c) => ({ ...c, id: fromB64url(c.id) })),
  };
  const cred = await navigator.credentials.create({ publicKey });
  const r = cred.response;
  return {
    id: cred.id,
    rawId: toB64url(cred.rawId),
    type: cred.type,
    response: {
      clientDataJSON: toB64url(r.clientDataJSON),
      attestationObject: toB64url(r.attestationObject),
      transports: typeof r.getTransports === "function" ? r.getTransports() : [],
    },
  };
}

export async function getPasskey(options) {
  const pk = options.publicKey;
  const publicKey = {
    ...pk,
    challenge: fromB64url(pk.challenge),
    allowCredentials: (pk.allowCredentials || []).map((c) => ({ ...c, id: fromB64url(c.id) })),
  };
  const cred = await navigator.credentials.get({ publicKey });
  const r = cred.response;
  return {
    id: cred.id,
    rawId: toB64url(cred.rawId),
    type: cred.type,
    response: {
      clientDataJSON: toB64url(r.clientDataJSON),
      authenticatorData: toB64url(r.authenticatorData),
      signature: toB64url(r.signature),
      userHandle: r.userHandle ? toB64url(r.userHandle) : null,
    },
  };
}

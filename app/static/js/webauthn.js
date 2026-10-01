// Passkeys in the browser: turn the server's JSON options into what navigator.credentials wants, and the
// result back into JSON (base64url) for py_webauthn. Passkeys need a real host name: browsers refuse
// WebAuthn on an IP address, so callers hide passkey buttons unless passkeysUsable(rpId).

const b64 = {
  toBytes(s) {
    const pad = "=".repeat((4 - (s.length % 4)) % 4);
    const bin = atob((s + pad).replace(/-/g, "+").replace(/_/g, "/"));
    return Uint8Array.from(bin, (c) => c.charCodeAt(0));
  },
  fromBytes(buf) {
    let bin = "";
    new Uint8Array(buf).forEach((b) => { bin += String.fromCharCode(b); });
    return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  },
};

export function passkeysUsable(rpId) {
  const host = location.hostname;
  return Boolean(window.PublicKeyCredential) && window.isSecureContext !== false &&
    (host === rpId || host.endsWith(`.${rpId}`));
}

const creds = (list) => (list || []).map((c) => ({ ...c, id: b64.toBytes(c.id) }));

export async function createPasskey(options) {
  const publicKey = { ...options, challenge: b64.toBytes(options.challenge),
    user: { ...options.user, id: b64.toBytes(options.user.id) }, excludeCredentials: creds(options.excludeCredentials) };
  const cred = await navigator.credentials.create({ publicKey });
  const r = cred.response;
  return { id: cred.id, rawId: b64.fromBytes(cred.rawId), type: cred.type,
    response: { clientDataJSON: b64.fromBytes(r.clientDataJSON), attestationObject: b64.fromBytes(r.attestationObject),
      transports: r.getTransports ? r.getTransports() : [] },
    clientExtensionResults: cred.getClientExtensionResults?.() || {} };
}

export async function getPasskey(options) {
  const publicKey = { ...options, challenge: b64.toBytes(options.challenge), allowCredentials: creds(options.allowCredentials) };
  const cred = await navigator.credentials.get({ publicKey });
  const r = cred.response;
  return { id: cred.id, rawId: b64.fromBytes(cred.rawId), type: cred.type,
    response: { clientDataJSON: b64.fromBytes(r.clientDataJSON), authenticatorData: b64.fromBytes(r.authenticatorData),
      signature: b64.fromBytes(r.signature), userHandle: r.userHandle ? b64.fromBytes(r.userHandle) : null },
    clientExtensionResults: cred.getClientExtensionResults?.() || {} };
}

// The browser's own messages for a cancelled or timed-out prompt aren't helpful.
export function passkeyError(err) {
  if (err?.name === "NotAllowedError") return "The passkey prompt was cancelled or timed out.";
  if (err?.name === "InvalidStateError") return "This device already has a passkey for your account.";
  if (err?.name === "SecurityError") return "Passkeys only work at the portal's own address, not an IP address.";
  return err?.message || String(err);
}

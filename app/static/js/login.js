// Sign-in page and the second step: "Sign in with a passkey" / "Use a passkey".
import { getPasskey, passkeyError, passkeysUsable } from "./webauthn.js";

const rpId = document.getElementById("login-js").dataset.rpId;
const csrf = document.querySelector('meta[name="csrf-token"]').content;
const btn = document.getElementById("passkey-login");
const errBox = document.getElementById("pk-error");

async function post(path, body) {
  const res = await fetch(path, { method: "POST", credentials: "same-origin",
    headers: { "Content-Type": "application/json", Accept: "application/json", "X-CSRF-Token": csrf },
    body: JSON.stringify(body || {}) });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { const e = new Error(data.detail || `HTTP ${res.status}`); e.status = res.status; throw e; }
  return data;
}

if (passkeysUsable(rpId)) {
  document.querySelectorAll("[data-passkey-only]").forEach((el) => { el.hidden = false; });
  btn?.addEventListener("click", async () => {
    errBox.innerHTML = "";
    btn.disabled = true;
    try {
      const options = await post("/login/passkey/options");
      const credential = await getPasskey(options);
      const { redirect } = await post("/login/passkey/verify", { credential });
      location.href = redirect || "/";
    } catch (err) {
      if (err.status === 403) { location.reload(); return; }       // banned: show the blocked page
      errBox.innerHTML = `<div class="alert alert-error"></div>`;
      errBox.firstChild.textContent = passkeyError(err);
    } finally { btn.disabled = false; }
  });
} else {
  document.querySelectorAll("[data-passkey-off]").forEach((el) => { el.hidden = false; });
}
